from __future__ import annotations

from dataclasses import asdict, dataclass
from math import exp, pi
from typing import Any

import numpy as np


@dataclass(frozen=True)
class ThermalInput:
    power_mw: float
    pulse_width_s: float
    spot_radius_um: float = 0.60
    phase: str = "amorphous"
    wavelength_nm: float = 639.0
    thickness_nm: float = 40.0
    optical_k: float = 0.642863
    pulse_count: int = 1
    repetition_hz: float = 1000.0
    ambient_c: float = 20.0
    effective_k_w_mk: float = 1.4
    areal_heat_capacity_j_m2k: float = 0.90
    crystallization_c: float = 200.0
    melting_c: float = 610.0
    absorption_override: float | None = None


@dataclass(frozen=True)
class ThermalResult:
    absorption_fraction: float
    absorbed_energy_j: float
    peak_temperature_c: float
    time_constant_s: float
    classification: str
    warning: str
    model: str = "lumped"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpticalResult:
    reflectance: float
    transmittance: float
    total_absorption: float
    sb2se3_absorption: float
    layer_absorption: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AxisymmetricSimulation:
    result: ThermalResult
    time_s: np.ndarray
    pcm_temperature_c: np.ndarray
    surface_temperature_c: np.ndarray
    radius_um: np.ndarray
    radius_edges_um: np.ndarray
    depth_um: np.ndarray
    depth_edges_um: np.ndarray
    peak_snapshot_c: np.ndarray


def absorption_fraction(optical_k: float, thickness_nm: float, wavelength_nm: float) -> float:
    """Single-pass Beer-Lambert absorption; multilayer interference is not included."""
    if min(optical_k, thickness_nm, wavelength_nm) < 0 or wavelength_nm == 0:
        raise ValueError("Invalid optical parameters.")
    return 1.0 - exp(-4.0 * pi * optical_k * thickness_nm / wavelength_nm)


def multilayer_optics(config: dict[str, Any], phase: str | None = None) -> OpticalResult:
    """Normal-incidence coherent transfer matrix for the finite optical stack.

    The 500 um silicon wafer is intentionally represented as a semi-infinite
    substrate: its back-surface phase is not stable in a real microscope setup.
    """
    sample = config["sample"]
    phase = phase or str(sample["phase"])
    wavelength_nm = float(sample["wavelength_nm"])
    if wavelength_nm <= 0:
        raise ValueError("Wavelength must be positive.")
    materials = sample["optical_materials"]

    def index(material_name: str) -> complex:
        properties = materials[material_name]
        if material_name == "Sb2Se3":
            properties = properties[phase]
        return complex(float(properties["n"]), float(properties["k"]))

    layers = sample["optical_layers"]
    layer_indices = [index(layer["material"]) for layer in layers]
    incident_index = complex(1.0, 0.0)
    substrate_index = index(sample["optical_substrate"])

    def interface(left: complex, right: complex) -> np.ndarray:
        return np.asarray(
            [[left + right, left - right], [left - right, left + right]],
            dtype=complex,
        ) / (2.0 * left)

    def propagation(refractive_index: complex, thickness_nm: float) -> np.ndarray:
        delta = 2.0 * pi * refractive_index * thickness_nm / wavelength_nm
        return np.diag((np.exp(-1j * delta), np.exp(1j * delta)))

    indices = [incident_index, *layer_indices, substrate_index]
    transfer = np.eye(2, dtype=complex)
    for layer_number, layer in enumerate(layers, start=1):
        transfer = transfer @ interface(indices[layer_number - 1], indices[layer_number])
        transfer = transfer @ propagation(indices[layer_number], float(layer["thickness_nm"]))
    transfer = transfer @ interface(indices[-2], indices[-1])
    transmission_amplitude = 1.0 / transfer[0, 0]
    reflection_amplitude = transfer[1, 0] * transmission_amplitude

    def flux(amplitudes: np.ndarray, refractive_index: complex) -> float:
        forward, backward = amplitudes
        value = (forward + backward) * np.conjugate(
            refractive_index * (forward - backward)
        )
        return float(np.real(value) / incident_index.real)

    amplitudes = np.asarray([1.0, reflection_amplitude], dtype=complex)
    layer_absorption: dict[str, float] = {}
    for layer_number, layer in enumerate(layers, start=1):
        amplitudes = np.linalg.solve(
            interface(indices[layer_number - 1], indices[layer_number]), amplitudes
        )
        left_flux = flux(amplitudes, indices[layer_number])
        amplitudes = np.linalg.solve(
            propagation(indices[layer_number], float(layer["thickness_nm"])), amplitudes
        )
        right_flux = flux(amplitudes, indices[layer_number])
        layer_absorption[str(layer["name"])] = max(0.0, left_flux - right_flux)

    reflectance = float(abs(reflection_amplitude) ** 2)
    transmittance = flux(
        np.asarray([transmission_amplitude, 0.0], dtype=complex), substrate_index
    )
    total_absorption = sum(layer_absorption.values())
    energy_error = abs(1.0 - reflectance - transmittance - total_absorption)
    if energy_error > 1e-6:
        raise RuntimeError(f"The optical matrix does not conserve energy (error={energy_error:.3g}).")
    sb2se3_absorption = next(
        value
        for layer, value in zip(layers, layer_absorption.values())
        if layer["material"] == "Sb2Se3"
    )
    return OpticalResult(
        reflectance=reflectance,
        transmittance=transmittance,
        total_absorption=total_absorption,
        sb2se3_absorption=sb2se3_absorption,
        layer_absorption=layer_absorption,
    )


def _input_absorption(inp: ThermalInput) -> float:
    value = (
        inp.absorption_override
        if inp.absorption_override is not None
        else absorption_fraction(inp.optical_k, inp.thickness_nm, inp.wavelength_nm)
    )
    if not 0.0 <= value <= 1.0:
        raise ValueError("Absorbed fraction must be between 0 and 1.")
    return value


def _classification(peak_c: float, crystallization_c: float, melting_c: float) -> str:
    if peak_c >= melting_c:
        return "melting/risk"
    if peak_c >= crystallization_c:
        return "possible crystallization"
    return "no predicted thermal change"


def estimate(inp: ThermalInput) -> ThermalResult:
    """Fast screening model for a rectangular pulse.

    The illuminated stack is represented by one thermal mass and a spreading
    conductance. It is useful for sweeps, not for selecting a safe lab recipe.
    """
    # ponytail: Keep this transparent model for fast parameter sweeps; the
    # axisymmetric solver below handles spatially resolved predictions.
    if inp.power_mw < 0 or inp.pulse_width_s <= 0 or inp.spot_radius_um <= 0:
        raise ValueError("Power, duration, and radius must be positive.")
    if inp.pulse_count < 1 or inp.repetition_hz <= 0:
        raise ValueError("Invalid pulse count or repetition rate.")
    if inp.pulse_count > 1 and inp.pulse_width_s * inp.repetition_hz >= 1.0:
        raise ValueError("Pulses overlap.")
    radius_m = inp.spot_radius_um * 1e-6
    area_m2 = pi * radius_m**2
    heat_capacity = inp.areal_heat_capacity_j_m2k * area_m2
    conductance = 4.0 * inp.effective_k_w_mk * radius_m
    tau = heat_capacity / conductance
    absorption = _input_absorption(inp)
    absorbed_power = inp.power_mw * 1e-3 * absorption
    steady_rise = absorbed_power / conductance
    on_decay = exp(-inp.pulse_width_s / tau)
    off_time = max(0.0, 1.0 / inp.repetition_hz - inp.pulse_width_s)
    off_decay = exp(-off_time / tau)
    rise = 0.0
    peak_rise = 0.0
    for pulse_index in range(inp.pulse_count):
        rise = rise * on_decay + steady_rise * (1.0 - on_decay)
        peak_rise = max(peak_rise, rise)
        if pulse_index < inp.pulse_count - 1:
            rise *= off_decay
    peak = inp.ambient_c + peak_rise
    energy = absorbed_power * inp.pulse_width_s * inp.pulse_count
    return ThermalResult(
        absorption_fraction=absorption,
        absorbed_energy_j=energy,
        peak_temperature_c=peak,
        time_constant_s=tau,
        classification=_classification(peak, inp.crystallization_c, inp.melting_c),
        warning=(
            "Lumped screening model with TMM absorption: ignores latent heat, "
            "temperature dependence, and detailed radial diffusion. Experimental calibration is required."
        ),
    )


def temperature_curve(inp: ThermalInput, samples: int = 500) -> tuple[np.ndarray, np.ndarray]:
    result = estimate(inp)
    tau = result.time_constant_s
    period = 1.0 / inp.repetition_hz
    last_pulse_end = (inp.pulse_count - 1) * period + inp.pulse_width_s
    end = last_pulse_end + max(5.0 * tau, inp.pulse_width_s)
    edges = [
        value
        for n in range(inp.pulse_count)
        for value in (n * period, n * period + inp.pulse_width_s)
    ]
    t = np.unique(np.concatenate((np.linspace(0.0, end, samples), np.asarray(edges))))
    radius_m = inp.spot_radius_um * 1e-6
    conductance = 4.0 * inp.effective_k_w_mk * radius_m
    absorbed_power = inp.power_mw * 1e-3 * result.absorption_fraction
    steady_rise = absorbed_power / conductance
    rise = 0.0
    temperatures = np.empty_like(t)
    temperatures[0] = inp.ambient_c
    for index in range(1, len(t)):
        midpoint = 0.5 * (t[index - 1] + t[index])
        pulse_number = int(midpoint // period)
        is_on = (
            pulse_number < inp.pulse_count
            and midpoint - pulse_number * period < inp.pulse_width_s
        )
        equilibrium = steady_rise if is_on else 0.0
        decay = exp(-(t[index] - t[index - 1]) / tau)
        rise = rise * decay + equilibrium * (1.0 - decay)
        temperatures[index] = inp.ambient_c + rise
    return t, temperatures


def _multilayer_mesh(
    config: dict[str, Any], phase: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    sample = config["sample"]
    materials = sample["thermal_materials"]
    dz_parts: list[np.ndarray] = []
    k_parts: list[np.ndarray] = []
    rho_parts: list[np.ndarray] = []
    cp_parts: list[np.ndarray] = []
    pcm_parts: list[np.ndarray] = []
    name_parts: list[np.ndarray] = []
    for layer in sample["thermal_layers"]:
        cells = int(layer["cells"])
        if cells < 1 or float(layer["thickness_nm"]) <= 0:
            raise ValueError(f"Invalid mesh in {layer['name']}.")
        weights = np.geomspace(1.0, float(layer.get("stretch", 1.0)), cells)
        dz = float(layer["thickness_nm"]) * 1e-9 * weights / weights.sum()
        material = materials[layer["material"]]
        if layer["material"] == "Sb2Se3":
            material = material[phase]
        for key in ("k_w_mk", "density_kg_m3", "cp_j_kgk"):
            if float(material[key]) <= 0:
                raise ValueError(f"Invalid {key} property in {layer['name']}.")
        dz_parts.append(dz)
        k_parts.append(np.full(cells, float(material["k_w_mk"])))
        rho_parts.append(np.full(cells, float(material["density_kg_m3"])))
        cp_parts.append(np.full(cells, float(material["cp_j_kgk"])))
        pcm_parts.append(np.full(cells, layer["material"] == "Sb2Se3"))
        name_parts.append(np.full(cells, layer["name"], dtype=object))
    return (
        np.concatenate(dz_parts),
        np.concatenate(k_parts),
        np.concatenate(rho_parts),
        np.concatenate(cp_parts),
        np.concatenate(pcm_parts),
        np.concatenate(name_parts),
    )


def _segment_steps(duration_s: float, first_step_s: float, count: int) -> np.ndarray:
    if duration_s <= 0:
        return np.empty(0)
    first = min(duration_s, max(1e-15, first_step_s))
    if first >= duration_s:
        return np.asarray([duration_s])
    points = np.geomspace(first, duration_s, count)
    return np.diff(np.concatenate(([0.0], points)))


def _solve_tridiagonal(
    lower: np.ndarray, diagonal: np.ndarray, upper: np.ndarray, rhs: np.ndarray
) -> np.ndarray:
    """Thomas solve along axis zero, with optional batched right-hand sides."""
    squeeze = rhs.ndim == 1
    values = np.asarray(rhs, dtype=float)
    if squeeze:
        values = values[:, None]
    batch = values.shape[1]

    def expanded(array: np.ndarray, rows: int) -> np.ndarray:
        array = np.asarray(array, dtype=float)
        if array.ndim == 1:
            array = array[:, None]
        return np.broadcast_to(array, (rows, batch))

    n = len(values)
    lo = expanded(lower, n - 1)
    diag = expanded(diagonal, n)
    up = expanded(upper, n - 1)
    cprime = np.empty((max(0, n - 1), batch))
    dprime = np.empty_like(values)
    denominator = diag[0]
    if np.any(denominator <= 0):
        raise RuntimeError("Invalid thermal matrix.")
    if n > 1:
        cprime[0] = up[0] / denominator
    dprime[0] = values[0] / denominator
    for index in range(1, n):
        denominator = diag[index] - lo[index - 1] * cprime[index - 1]
        if np.any(denominator <= 0):
            raise RuntimeError("Invalid thermal matrix.")
        if index < n - 1:
            cprime[index] = up[index] / denominator
        dprime[index] = (values[index] - lo[index - 1] * dprime[index - 1]) / denominator
    solution = np.empty_like(values)
    solution[-1] = dprime[-1]
    for index in range(n - 2, -1, -1):
        solution[index] = dprime[index] - cprime[index] * solution[index + 1]
    return solution[:, 0] if squeeze else solution


def axisymmetric_simulation(inp: ThermalInput, config: dict[str, Any]) -> AxisymmetricSimulation:
    """Transient 2D axisymmetric finite-volume model of the multilayer stack."""
    estimate(inp)  # shared input validation
    dz, conductivity, density, cp, pcm_mask, layer_names = _multilayer_mesh(config, inp.phase)
    sample = config["sample"]
    settings = sample.get("axisymmetric", {})
    radial_cells = int(settings.get("radial_cells", 48))
    extent_m = float(settings.get("radial_extent_um", 250.0)) * 1e-6
    first_fraction = float(settings.get("first_cell_fraction", 0.125))
    beam_radius_m = inp.spot_radius_um * 1e-6
    if not 8 <= radial_cells <= 256 or extent_m <= 0 or not 0 < first_fraction < 1:
        raise ValueError("Invalid axisymmetric mesh settings.")
    extent_m = max(extent_m, 8.0 * beam_radius_m)
    first_edge = min(beam_radius_m * first_fraction, extent_m / radial_cells)
    radial_edges = np.concatenate(
        ([0.0], np.geomspace(first_edge, extent_m, radial_cells))
    )
    radial_centers = 0.5 * (radial_edges[:-1] + radial_edges[1:])
    annulus_areas = pi * (radial_edges[1:] ** 2 - radial_edges[:-1] ** 2)

    areal_capacity = density * cp * dz
    vertical_interfaces = 1.0 / (
        dz[:-1] / (2.0 * conductivity[:-1])
        + dz[1:] / (2.0 * conductivity[1:])
    )
    vertical_left = np.concatenate(([0.0], vertical_interfaces))
    vertical_right = np.concatenate(
        (vertical_interfaces, [2.0 * conductivity[-1] / dz[-1]])
    )

    radial_geometry = (
        2.0
        * pi
        * radial_edges[1:-1]
        / (radial_centers[1:] - radial_centers[:-1])
    )
    radial_interfaces = radial_geometry[:, None] * (conductivity * dz)[None, :]
    radial_outer = (
        2.0
        * pi
        * radial_edges[-1]
        * conductivity
        * dz
        / (radial_edges[-1] - radial_centers[-1])
    )
    cell_capacity = annulus_areas[:, None] * areal_capacity[None, :]
    radial_left = np.vstack((np.zeros((1, len(dz))), radial_interfaces))
    radial_right = np.vstack((radial_interfaces, radial_outer[None, :]))

    optical = multilayer_optics(config, inp.phase)
    incident_power = inp.power_mw * 1e-3
    radial_fraction = np.exp(-2.0 * radial_edges[:-1] ** 2 / beam_radius_m**2) - np.exp(
        -2.0 * radial_edges[1:] ** 2 / beam_radius_m**2
    )
    radial_power_density = radial_fraction / annulus_areas
    source_on = np.zeros((len(dz), radial_cells))
    for layer_name, layer_fraction in optical.layer_absorption.items():
        layer_mask = layer_names == layer_name
        if layer_mask.any() and layer_fraction > 0:
            axial_fraction = dz[layer_mask] / dz[layer_mask].sum()
            source_on[layer_mask] = (
                incident_power
                * layer_fraction
                * axial_fraction[:, None]
                * radial_power_density[None, :]
            )

    substrate_layer = sample["thermal_layers"][-1]
    substrate_mask = layer_names == substrate_layer["name"]
    substrate_material = sample["optical_materials"][sample["optical_substrate"]]
    substrate_alpha = 4.0 * pi * float(substrate_material["k"]) / (
        float(sample["wavelength_nm"]) * 1e-9
    )
    if substrate_mask.any() and substrate_alpha > 0 and optical.transmittance > 0:
        substrate_dz = dz[substrate_mask]
        substrate_edges = np.concatenate(([0.0], np.cumsum(substrate_dz)))
        axial_fraction = np.exp(-substrate_alpha * substrate_edges[:-1]) - np.exp(
            -substrate_alpha * substrate_edges[1:]
        )
        source_on[substrate_mask] += (
            incident_power
            * optical.transmittance
            * axial_fraction[:, None]
            * radial_power_density[None, :]
        )

    rise = np.zeros_like(source_on)
    zero_source = np.zeros_like(source_on)
    times = [0.0]
    pcm_temperatures = [inp.ambient_c]
    surface_temperatures = [inp.ambient_c]
    peak_temperature = inp.ambient_c
    peak_snapshot = np.full_like(rise, inp.ambient_c)

    def advance(dt: float, source: np.ndarray) -> None:
        nonlocal rise
        vertical_diagonal = areal_capacity / dt + vertical_left + vertical_right
        rise = _solve_tridiagonal(
            -vertical_interfaces,
            vertical_diagonal,
            -vertical_interfaces,
            areal_capacity[:, None] / dt * rise + source,
        )
        radial_diagonal = cell_capacity / dt + radial_left + radial_right
        rise = _solve_tridiagonal(
            -radial_interfaces,
            radial_diagonal,
            -radial_interfaces,
            cell_capacity / dt * rise.T,
        ).T

    def run_segment(steps: np.ndarray, source: np.ndarray) -> None:
        nonlocal peak_temperature, peak_snapshot
        for dt in steps:
            advance(float(dt), source)
            times.append(times[-1] + float(dt))
            pcm_temperature = inp.ambient_c + float(rise[pcm_mask].max())
            pcm_temperatures.append(pcm_temperature)
            surface_temperatures.append(inp.ambient_c + float(rise[0, 0]))
            if pcm_temperature > peak_temperature:
                peak_temperature = pcm_temperature
                peak_snapshot = inp.ambient_c + rise.copy()

    period = 1.0 / inp.repetition_hz
    on_steps = np.full(20, inp.pulse_width_s / 20.0)
    off_time = max(0.0, period - inp.pulse_width_s)
    off_steps = _segment_steps(off_time, inp.pulse_width_s / 20.0, 18)
    for pulse_index in range(inp.pulse_count):
        cycle_start = rise.copy()
        run_segment(on_steps, source_on)
        if pulse_index < inp.pulse_count - 1:
            run_segment(off_steps, zero_source)
        if pulse_index < inp.pulse_count - 2:
            tolerance = 1e-6 * max(1.0, float(rise.max()))
            if float(np.max(np.abs(rise - cycle_start))) <= tolerance:
                skipped_cycles = inp.pulse_count - pulse_index - 2
                times.append(times[-1] + skipped_cycles * period)
                pcm_temperatures.append(inp.ambient_c + float(rise[pcm_mask].max()))
                surface_temperatures.append(inp.ambient_c + float(rise[0, 0]))
                run_segment(on_steps, source_on)
                break
    last_pulse_end = times[-1]
    cooling_time = max(5.0 * inp.pulse_width_s, min(5.0 * period, 0.01))
    run_segment(
        _segment_steps(cooling_time, inp.pulse_width_s / 20.0, 40),
        zero_source,
    )
    time_array = np.asarray(times)
    pcm_array = np.asarray(pcm_temperatures)
    peak_index = int(pcm_array.argmax())
    if peak_temperature <= inp.ambient_c + 1e-12:
        decay_time = 0.0
    else:
        target = inp.ambient_c + (peak_temperature - inp.ambient_c) / exp(1.0)
        cooling = np.flatnonzero(
            (time_array >= max(last_pulse_end, time_array[peak_index]))
            & (pcm_array <= target)
        )
        decay_time = (
            float(time_array[cooling[0]] - time_array[peak_index])
            if cooling.size
            else float(time_array[-1] - time_array[peak_index])
        )
    absorption = _input_absorption(inp)
    result = ThermalResult(
        absorption_fraction=absorption,
        absorbed_energy_j=incident_power * absorption * inp.pulse_width_s * inp.pulse_count,
        peak_temperature_c=peak_temperature,
        time_constant_s=max(0.0, decay_time),
        classification=_classification(
            peak_temperature, inp.crystallization_c, inp.melting_c
        ),
        warning=(
            "2D axisymmetric multilayer model with a Gaussian beam and TMM heat deposition. "
            "It assumes a circular spot with constant radius through the stack, uniform absorption within "
            "each finite layer, constant properties, perfect thermal interfaces, and fixed-ambient outer "
            "and back boundaries; latent heat and phase kinetics are omitted."
        ),
        model="axisymmetric_2d",
    )
    return AxisymmetricSimulation(
        result=result,
        time_s=time_array,
        pcm_temperature_c=pcm_array,
        surface_temperature_c=np.asarray(surface_temperatures),
        radius_um=radial_centers * 1e6,
        radius_edges_um=radial_edges * 1e6,
        depth_um=(np.cumsum(dz) - 0.5 * dz) * 1e6,
        depth_edges_um=np.concatenate(([0.0], np.cumsum(dz))) * 1e6,
        peak_snapshot_c=peak_snapshot,
    )


def estimate_axisymmetric(inp: ThermalInput, config: dict[str, Any]) -> ThermalResult:
    return axisymmetric_simulation(inp, config).result


def from_config(config: dict[str, Any], power_mw: float, pulse_width_s: float, **overrides: Any) -> ThermalInput:
    sample = config["sample"]
    phase = str(overrides.pop("phase", sample["phase"]))
    optical = multilayer_optics(config, phase)
    return ThermalInput(
        power_mw=power_mw,
        pulse_width_s=pulse_width_s,
        spot_radius_um=float(overrides.pop("spot_radius_um", sample["spot_radius_um"])),
        phase=phase,
        wavelength_nm=float(sample["wavelength_nm"]),
        thickness_nm=float(sample["sb2se3_thickness_nm"]),
        optical_k=float(sample["optical_k"][phase]),
        absorption_override=optical.sb2se3_absorption,
        ambient_c=float(sample["ambient_c"]),
        effective_k_w_mk=float(sample["effective_thermal_conductivity_w_mk"]),
        areal_heat_capacity_j_m2k=float(sample["effective_areal_heat_capacity_j_m2k"]),
        crystallization_c=float(sample["crystallization_c"]),
        melting_c=float(sample["melting_c"]),
        **overrides,
    )


def sensitivity_map(
    config: dict[str, Any], powers_mw: np.ndarray, widths_s: np.ndarray, **overrides: Any
) -> np.ndarray:
    peaks = np.empty((len(widths_s), len(powers_mw)))
    for row, width in enumerate(widths_s):
        for col, power in enumerate(powers_mw):
            peaks[row, col] = estimate(
                from_config(config, float(power), float(width), **overrides)
            ).peak_temperature_c
    return peaks
