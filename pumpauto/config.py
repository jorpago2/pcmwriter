from __future__ import annotations

import json
from copy import deepcopy
from math import isfinite
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "simulation",
    "results_dir": "results",
    "safety": {
        "hardware_armed": False,
        "min_high_v": 3.5,
        "max_high_v": 5.0,
        "min_pulse_width_s": 1e-8,
        "max_pulse_width_s": 0.01,
        "max_optical_power_mw": 160.0,
        "max_pulses": 1000,
        "max_burst_duration_s": 10.0,
        "max_points": 10000,
    },
    "awg": {
        "model": "T3AFG350",
        "visa_resource": "",
        "channel": 1,
        "load_ohm": 50,
        "high_v": 5.0,
        "low_v": 0.0,
        "timeout_ms": 5000,
    },
    "laser": {
        "visa_resource": "",
        "baud_rate": 19200,
        "timeout_ms": 5000,
        "emission_settle_s": 5.5,
        "peak_power_mw": 10.0,
        "power_calibration": [],
    },
    "scope": {
        "visa_resource": "",
        "channel": 1,
        "timeout_ms": 10000,
        "input_impedance": "FIFTy",
        "coupling": "DC",
        "bandwidth_limit": "OFF",
        "vertical_scale_v_div": 1.0,
        "vertical_offset_v": 0.0,
        "trigger_source": "CHAN1",
        "trigger_level_v": 0.1,
        "trigger_slope": "POSitive",
        "window_factor": 6.0,
        "acquisition_timeout_s": 2.0,
    },
    "photodetector": {
        "model": "Thorlabs DET02AFC",
        "wavelength_range_nm": [400.0, 1100.0],
        "bandwidth_hz": 1e9,
        "rise_time_s": 1e-9,
        "minimum_load_ohm": 50.0,
        "maximum_peak_power_mw": 18.0,
        "maximum_output_v_50ohm": 3.3,
    },
    "stage": {
        "serial_number": "",
        "kinesis_dir": "C:/Program Files/Thorlabs/Kinesis",
        "axis_channels": {"x": 1, "y": 2, "z": 3},
        "range_um": {"x": [0.0, 20.0], "y": [0.0, 20.0], "z": [0.0, 20.0]},
        "origin_um": {"x": 10.0, "y": 10.0, "z": 10.0},
        "max_voltage_v": 75.0,
        "settle_s": 0.30,
        "position_tolerance_um": 0.10,
        "move_timeout_s": 5.0,
        "calibrated": False,
        "controller_span_units": [0.0, 100.0],
        "axis_inverted": {"x": False, "y": False, "z": False},
        "focus_plane": {"enabled": False, "a": 0.0, "b": 0.0, "c": 10.0, "rms_um": 0.0, "r_squared": 0.0},
    },
    "camera": {
        "serial_number": 0,
        "exposure_ms": 10.0,
        "gain_db": 0.0,
        "roi": [0, 0, 0, 0],
        "auto_exposure": True,
        "target_peak_fraction": 0.85,
        "min_exposure_ms": 0.01,
        "max_exposure_ms": 100.0,
        "max_auto_exposure_steps": 4,
        "capture_retries": 4,
        "calibration_step_um": 1.0,
        "calibration_min_snr": 8.0,
        "calibration_max_return_error_px": 1.0,
        "calibration_max_anisotropy": 1.2,
        "stage_to_pixel_px_per_um": [],
        "pixel_to_stage_um_per_px": [],
        "um_per_pixel": 0.0,
    },
    "guide": {
        "detection_roi": [32, 32, 192, 192],
        "tracking_roi_size_px": 96,
        "length_um": 3.0,
        "max_step_um": 0.25,
        "autofocus_span_um": 2.0,
        "autofocus_samples": 7,
        "spot_pixel": [128.0, 128.0],
        "alignment_tolerance_um": 0.08,
        "max_correction_um": 0.5,
        "max_alignment_iterations": 3,
        "min_confidence": 0.25,
        "phase_roi_radius_px": 8.0,
        "phase_change_threshold_percent": 5.0,
    },
    "sample": {
        "wavelength_nm": 639.0,
        "spot_radius_um": 0.60,
        "ambient_c": 20.0,
        "phase": "amorphous",
        "optical_k": {"amorphous": 0.642863, "crystalline": 1.898485},
        "optical_materials": {
            "SiO2": {"n": 1.457, "k": 0.0},
            "Si": {"n": 3.87, "k": 0.016},
            "Sb2Se3": {
                "amorphous": {"n": 4.112396, "k": 0.642863},
                "crystalline": {"n": 5.284340, "k": 1.898485}
            }
        },
        "optical_layers": [
            {"name": "SiO2 cap", "material": "SiO2", "thickness_nm": 200.0},
            {"name": "Sb2Se3", "material": "Sb2Se3", "thickness_nm": 40.0},
            {"name": "Si waveguide", "material": "Si", "thickness_nm": 220.0},
            {"name": "SiO2 BOX", "material": "SiO2", "thickness_nm": 3000.0}
        ],
        "optical_substrate": "Si",
        "sb2se3_thickness_nm": 40.0,
        "effective_thermal_conductivity_w_mk": 1.4,
        "effective_areal_heat_capacity_j_m2k": 0.90,
        "crystallization_c": 200.0,
        "melting_c": 610.0,
        "axisymmetric": {
            "radial_cells": 48,
            "radial_extent_um": 250.0,
            "first_cell_fraction": 0.125,
        },
        "thermal_materials": {
            "SiO2": {"k_w_mk": 1.38, "density_kg_m3": 2200.0, "cp_j_kgk": 703.0},
            "Si": {"k_w_mk": 130.0, "density_kg_m3": 2329.0, "cp_j_kgk": 700.0},
            "Sb2Se3": {
                "amorphous": {"k_w_mk": 0.22, "density_kg_m3": 5840.0, "cp_j_kgk": 263.0},
                "crystalline": {"k_w_mk": 0.72, "density_kg_m3": 5840.0, "cp_j_kgk": 263.0},
            },
        },
        "thermal_layers": [
            {"name": "SiO2 cap", "material": "SiO2", "thickness_nm": 200.0, "cells": 4},
            {"name": "Sb2Se3", "material": "Sb2Se3", "thickness_nm": 40.0, "cells": 2},
            {"name": "Si waveguide", "material": "Si", "thickness_nm": 220.0, "cells": 4},
            {"name": "SiO2 BOX", "material": "SiO2", "thickness_nm": 3000.0, "cells": 12},
            {
                "name": "Si substrate",
                "material": "Si",
                "thickness_nm": 20000.0,
                "cells": 60,
                "stretch": 100.0
            },
        ],
    },
    "simulation": {
        "seed": 639,
        "camera_um_per_pixel": 0.08,
        "focus_z_um": 10.0,
        "spot_waist_px": 5.0,
        "rayleigh_range_um": 1.2,
        "spot_peak_counts": 140.0,
        "guide_angle_deg": 20.0,
        "guide_width_um": 0.5,
        "guide_contrast_counts": 45.0,
        "focus_slope_x": 0.03,
        "focus_slope_y": -0.015,
    },
}


class ConfigError(ValueError):
    pass


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path = "config.json") -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return deepcopy(DEFAULT_CONFIG)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(user, dict):
        raise ConfigError("The configuration must be a JSON object.")
    config = _merge(DEFAULT_CONFIG, user)
    validate_config(config)
    return config


def save_config(config: dict[str, Any], path: str | Path = "config.json") -> None:
    validate_config(config)
    persisted = deepcopy(config)
    persisted["safety"]["hardware_armed"] = False
    Path(path).write_text(json.dumps(persisted, indent=2), encoding="utf-8")


def validate_config(config: dict[str, Any]) -> None:
    if config.get("mode") not in {"simulation", "hardware"}:
        raise ConfigError("mode must be 'simulation' or 'hardware'.")
    stage = config["stage"]
    if set(stage["range_um"]) != {"x", "y", "z"}:
        raise ConfigError("stage.range_um must define x, y and z.")
    if set(stage["axis_channels"]) != {"x", "y", "z"}:
        raise ConfigError("stage.axis_channels must define x, y and z.")
    if set(stage["origin_um"]) != {"x", "y", "z"}:
        raise ConfigError("stage.origin_um must define x, y and z.")
    if set(stage["axis_inverted"]) != {"x", "y", "z"}:
        raise ConfigError("stage.axis_inverted must define x, y and z.")
    for axis, bounds in config["stage"]["range_um"].items():
        if len(bounds) != 2 or float(bounds[0]) >= float(bounds[1]):
            raise ConfigError(f"Invalid limits for axis {axis}: {bounds}")
    channels = [int(stage["axis_channels"][axis]) for axis in "xyz"]
    if len(set(channels)) != 3 or any(channel not in (1, 2, 3) for channel in channels):
        raise ConfigError("stage.axis_channels must map x, y and z to distinct BPC303 channels 1, 2 and 3.")
    span = [float(value) for value in stage["controller_span_units"]]
    if len(span) != 2 or not 0 <= span[0] < span[1] <= 100:
        raise ConfigError("stage.controller_span_units must be an increasing interval within [0, 100].")
    for axis in "xyz":
        low, high = map(float, stage["range_um"][axis])
        if not low <= float(stage["origin_um"][axis]) <= high:
            raise ConfigError(f"stage.origin_um.{axis} is outside its configured range.")
    if not 0 < float(stage["position_tolerance_um"]) <= 1.0:
        raise ConfigError("stage.position_tolerance_um must be >0 and <=1 um.")
    if not 0.1 <= float(stage["move_timeout_s"]) <= 60.0:
        raise ConfigError("stage.move_timeout_s must be between 0.1 and 60 s.")
    plane = config["stage"]["focus_plane"]
    if not all(isfinite(float(plane[key])) for key in ("a", "b", "c", "rms_um", "r_squared")):
        raise ConfigError("Invalid focus plane.")
    sample = config["sample"]
    axisymmetric = sample["axisymmetric"]
    if not 8 <= int(axisymmetric["radial_cells"]) <= 256:
        raise ConfigError("The axisymmetric model requires 8 to 256 radial cells.")
    if float(axisymmetric["radial_extent_um"]) <= 0 or not 0 < float(
        axisymmetric["first_cell_fraction"]
    ) < 1:
        raise ConfigError("Invalid axisymmetric radial mesh.")
    optical_materials = sample["optical_materials"]
    if sample["optical_substrate"] not in optical_materials:
        raise ConfigError("The optical substrate is missing from optical_materials.")
    sb_layers = 0
    for layer in sample["optical_layers"]:
        material_name = layer["material"]
        if material_name not in optical_materials or float(layer["thickness_nm"]) <= 0:
            raise ConfigError(f"Invalid optical layer: {layer}.")
        sb_layers += material_name == "Sb2Se3"
    if sb_layers != 1:
        raise ConfigError("Exactly one Sb2Se3 optical layer is required.")
    for name, material in optical_materials.items():
        phases = material.values() if name == "Sb2Se3" else (material,)
        for properties in phases:
            if float(properties["n"]) <= 0 or float(properties["k"]) < 0:
                raise ConfigError(f"Invalid optical constants for {name}.")
    scope = config["scope"]
    safety = config["safety"]
    if not 0 < float(safety["min_pulse_width_s"]) <= float(safety["max_pulse_width_s"]):
        raise ConfigError("Invalid pulse-duration limits.")
    if float(safety["max_optical_power_mw"]) <= 0:
        raise ConfigError("safety.max_optical_power_mw must be positive.")
    if float(safety["max_burst_duration_s"]) <= 0:
        raise ConfigError("safety.max_burst_duration_s must be positive.")
    if config["awg"].get("model") not in {"T3AFG350", "DG1062Z"}:
        raise ConfigError("awg.model must be T3AFG350 or DG1062Z.")
    if int(config["awg"]["channel"]) != 1:
        raise ConfigError("PCMWriter uses AWG channel 1 only.")
    if int(config["awg"]["load_ohm"]) != 50:
        raise ConfigError("The AWG output must match the Stradus 50-ohm input.")
    if not 0 <= float(config["awg"]["low_v"]) <= 0.8:
        raise ConfigError("The Stradus low level must be between 0 and 0.8 V.")
    if not float(config["awg"]["low_v"]) < float(config["awg"]["high_v"]):
        raise ConfigError("awg.high_v must be greater than awg.low_v.")
    if not float(safety["min_high_v"]) <= float(config["awg"]["high_v"]) <= float(
        safety["max_high_v"]
    ):
        raise ConfigError("awg.high_v is outside the configured TTL safety range.")
    if int(config["laser"]["baud_rate"]) != 19200:
        raise ConfigError("The Stradus must use 19200 baud.")
    if not 0 < float(config["laser"]["peak_power_mw"]) <= 160.0:
        raise ConfigError("laser.peak_power_mw must be between 0 and 160 mW.")
    if float(config["laser"]["emission_settle_s"]) < 0:
        raise ConfigError("laser.emission_settle_s cannot be negative.")
    calibration = config["laser"]["power_calibration"]
    if calibration:
        if len(calibration) < 2 or any(len(point) != 2 for point in calibration):
            raise ConfigError("laser.power_calibration requires [sample_power, PP] pairs.")
        sample_values = [float(point[0]) for point in calibration]
        pp_values = [float(point[1]) for point in calibration]
        if not all(isfinite(value) for value in sample_values + pp_values):
            raise ConfigError("Power calibration contains non-finite values.")
        if any(value < 0 for value in sample_values) or any(not 0 < value <= 160 for value in pp_values):
            raise ConfigError("Calibration requires sample power >= 0 and PP between 0 and 160 mW.")
        if any(b <= a for a, b in zip(sample_values, sample_values[1:])) or any(
            b <= a for a, b in zip(pp_values, pp_values[1:])
        ):
            raise ConfigError("Power calibration must increase strictly on both axes.")
    camera = config["camera"]
    if int(camera["serial_number"]) < 0:
        raise ConfigError("camera.serial_number cannot be negative.")
    if float(camera["exposure_ms"]) <= 0 or float(camera["gain_db"]) < 0:
        raise ConfigError("Invalid camera exposure or gain.")
    if not 0 < float(camera["min_exposure_ms"]) <= float(camera["max_exposure_ms"]):
        raise ConfigError("Invalid camera exposure limits.")
    if not float(camera["min_exposure_ms"]) <= float(camera["exposure_ms"]) <= float(
        camera["max_exposure_ms"]
    ):
        raise ConfigError("camera.exposure_ms is outside its limits.")
    if len(camera["roi"]) != 4 or any(int(value) < 0 for value in camera["roi"]):
        raise ConfigError("camera.roi must be [left, top, width, height] with non-negative values.")
    if (int(camera["roi"][2]) == 0) != (int(camera["roi"][3]) == 0):
        raise ConfigError("camera.roi width and height must both be zero or both be positive.")
    if not 0.1 <= float(camera["target_peak_fraction"]) <= 0.95:
        raise ConfigError("camera.target_peak_fraction must be between 0.1 and 0.95.")
    if int(camera["max_auto_exposure_steps"]) < 1 or int(camera["capture_retries"]) < 1:
        raise ConfigError("Capture retries and auto-exposure steps must be positive.")
    if float(camera["calibration_step_um"]) <= 0 or float(camera["calibration_min_snr"]) <= 0:
        raise ConfigError("Calibration step and SNR must be positive.")
    if float(camera["calibration_max_return_error_px"]) <= 0 or float(
        camera["calibration_max_anisotropy"]
    ) < 1:
        raise ConfigError("Invalid pixel-to-stage calibration limits.")
    for key in ("stage_to_pixel_px_per_um", "pixel_to_stage_um_per_px"):
        matrix = camera[key]
        if matrix and (len(matrix) != 2 or any(len(row) != 2 for row in matrix)):
            raise ConfigError(f"camera.{key} must be a 2x2 matrix or empty.")
    guide = config["guide"]
    if float(guide["length_um"]) <= 0 or float(guide["max_step_um"]) <= 0:
        raise ConfigError("Waveguide write length and step must be positive.")
    if int(guide["autofocus_samples"]) < 5 or float(guide["autofocus_span_um"]) <= 0:
        raise ConfigError("Waveguide autofocus requires a positive span and at least five samples.")
    if len(guide["spot_pixel"]) != 2 or len(guide["detection_roi"]) != 4:
        raise ConfigError("Invalid spot coordinates or waveguide ROI.")
    if int(guide["tracking_roi_size_px"]) < 32 or int(guide["max_alignment_iterations"]) < 1:
        raise ConfigError("Invalid waveguide tracking settings.")
    if not 0 < float(guide["min_confidence"]) <= 1:
        raise ConfigError("guide.min_confidence must be between 0 and 1.")
    if min(float(guide["alignment_tolerance_um"]), float(guide["max_correction_um"])) <= 0:
        raise ConfigError("Invalid waveguide alignment tolerances.")
    if min(float(guide["phase_roi_radius_px"]), float(guide["phase_change_threshold_percent"])) <= 0:
        raise ConfigError("Invalid phase-verification ROI or threshold.")
    if float(scope["vertical_scale_v_div"]) <= 0:
        raise ConfigError("scope.vertical_scale_v_div must be positive.")
    if float(scope["window_factor"]) <= 1:
        raise ConfigError("scope.window_factor must be greater than 1.")
    if float(scope["acquisition_timeout_s"]) <= 0:
        raise ConfigError("scope.acquisition_timeout_s must be positive.")
    if int(scope["channel"]) != 1:
        raise ConfigError("PCMWriter uses oscilloscope channel 1 only.")
    if str(scope["input_impedance"]).upper() not in {"FIFTY", "OMEG"}:
        raise ConfigError("scope.input_impedance must be FIFTy or OMEG.")
    if str(scope["coupling"]).upper() not in {"AC", "DC", "GND"}:
        raise ConfigError("scope.coupling must be AC, DC or GND.")
    if str(scope["bandwidth_limit"]).upper() not in {"ON", "OFF"}:
        raise ConfigError("scope.bandwidth_limit must be ON or OFF.")
    if str(scope["trigger_source"]).upper() != "CHAN1":
        raise ConfigError("PCMWriter uses oscilloscope channel 1 as the trigger source.")
    if str(scope["trigger_slope"]).upper() not in {"POSITIVE", "NEGATIVE", "RFAL"}:
        raise ConfigError("scope.trigger_slope must be POSitive, NEGative or RFAL.")
    if config["mode"] == "hardware" and config["safety"]["hardware_armed"]:
        missing = [
            name
            for name, value in {
                "awg.visa_resource": config["awg"].get("visa_resource"),
                "laser.visa_resource": config["laser"].get("visa_resource"),
                "scope.visa_resource": config["scope"].get("visa_resource"),
                "stage.serial_number": config["stage"].get("serial_number"),
            }.items()
            if not value
        ]
        if missing:
            raise ConfigError("Missing hardware resources: " + ", ".join(missing))


def resolve_results_dir(config: dict[str, Any], config_path: str | Path = "config.json") -> Path:
    result = Path(config["results_dir"])
    if not result.is_absolute():
        result = Path(config_path).resolve().parent / result
    return result
