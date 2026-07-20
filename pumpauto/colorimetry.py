from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .thermal import multilayer_optics


_DATA = Path(__file__).with_name("spectra")
_SI_WAVELENGTHS = np.arange(380.0, 701.0, 10.0)
# Green, Sol. Energy Mater. Sol. Cells 92, 1305-1310 (2008).
_SI_N = np.asarray([
    6.616, 6.039, 5.613, 5.330, 5.119, 4.949, 4.812, 4.691, 4.587, 4.497,
    4.419, 4.350, 4.294, 4.241, 4.193, 4.151, 4.112, 4.077, 4.045, 4.015,
    3.988, 3.963, 3.940, 3.918, 3.898, 3.879, 3.861, 3.844, 3.828, 3.813,
    3.798, 3.784, 3.772,
])
_SI_K = np.asarray([
    .946, .445, .296, .227, .176, .138, .107, .086, .071, .062, .055,
    .049, .044, .039, .036, .033, .030, .028, .026, .024, .023, .021,
    .020, .018, .017, .016, .015, .014, .013, .013, .012, .011, .011,
])


@lru_cache(maxsize=1)
def _spectra() -> tuple[np.ndarray, dict[str, np.ndarray]]:
    instrument = np.loadtxt(_DATA / "instrument_response.csv", delimiter=",", skiprows=1)
    sb2se3 = {}
    for phase in ("amorphous", "crystalline"):
        text = (_DATA / f"sb2se3_{phase}.txt").read_text(encoding="utf-8").replace(",", ".")
        sb2se3[phase] = np.loadtxt(text.splitlines(), comments=";")
    return instrument, sb2se3


def _sio2_n(wavelength_nm: float) -> float:
    """Malitson fused-silica Sellmeier equation at 20 C."""
    wavelength_um2 = (wavelength_nm / 1000.0) ** 2
    return float(np.sqrt(
        1.0
        + 0.6961663 * wavelength_um2 / (wavelength_um2 - 0.0684043**2)
        + 0.4079426 * wavelength_um2 / (wavelength_um2 - 0.1162414**2)
        + 0.8974794 * wavelength_um2 / (wavelength_um2 - 9.896161**2)
    ))


def _set_indices(config: dict[str, Any], phase: str, wavelength_nm: float, sb: np.ndarray) -> None:
    materials = config["sample"]["optical_materials"]
    materials["SiO2"] = {"n": _sio2_n(wavelength_nm), "k": 0.0}
    materials["Si"] = {
        "n": float(np.interp(wavelength_nm, _SI_WAVELENGTHS, _SI_N)),
        "k": float(np.interp(wavelength_nm, _SI_WAVELENGTHS, _SI_K)),
    }
    materials["Sb2Se3"][phase] = {
        "n": float(np.interp(wavelength_nm, sb[:, 0], sb[:, 1])),
        "k": float(np.interp(wavelength_nm, sb[:, 0], sb[:, 2])),
    }
    config["sample"]["wavelength_nm"] = wavelength_nm


def _phase_prediction(config: dict[str, Any], phase: str) -> dict[str, Any]:
    instrument, sb2se3 = _spectra()
    working = deepcopy(config)
    wavelengths = instrument[:, 0]
    # Power spectrum -> photon flux -> electrons. Common hc factor cancels in ratios.
    weights = (
        instrument[:, 1, None]
        * instrument[:, 2, None]
        * instrument[:, 3, None]
        * instrument[:, 4:7]
        * wavelengths[:, None]
    )
    reflectance = np.empty(wavelengths.size)
    for index, wavelength_nm in enumerate(wavelengths):
        _set_indices(working, phase, float(wavelength_nm), sb2se3[phase])
        reflectance[index] = multilayer_optics(working, phase).reflectance

    counts = np.trapezoid(weights * reflectance[:, None], wavelengths, axis=0)
    fractions = counts / counts.sum()
    _set_indices(working, phase, 639.0, sb2se3[phase])
    at_639 = multilayer_optics(working, phase)
    channels = ("r", "g", "b")
    return {
        "relative_electrons": dict(zip(channels, map(float, counts))),
        "rgb_fraction": dict(zip(channels, map(float, fractions))),
        "rgb_to_green": dict(zip(channels, map(float, counts / counts[1]))),
        "camera_weighted_reflectance": float(
            counts.sum() / np.trapezoid(weights.sum(axis=1), wavelengths)
        ),
        "reflectance_639": at_639.reflectance,
        "sb2se3_absorption_639": at_639.sb2se3_absorption,
    }


@lru_cache(maxsize=8)
def _predict_phase_colors_cached(sample_json: str) -> dict[str, Any]:
    config = {"sample": json.loads(sample_json)}
    amorphous = _phase_prediction(config, "amorphous")
    crystalline = _phase_prediction(config, "crystalline")
    a = np.asarray(list(amorphous["relative_electrons"].values()))
    c = np.asarray(list(crystalline["relative_electrons"].values()))
    return {
        "amorphous": amorphous,
        "crystalline": crystalline,
        "crystalline_change_percent_rgb": dict(zip(("r", "g", "b"), map(float, 100.0 * (c / a - 1.0)))),
        "total_signal_change_percent": float(100.0 * (c.sum() / a.sum() - 1.0)),
    }


def predict_phase_colors(config: dict[str, Any]) -> dict[str, Any]:
    """Predict raw Pixelink RGB for the amorphous and crystalline optical stack."""
    sample_json = json.dumps(config["sample"], sort_keys=True, separators=(",", ":"))
    return deepcopy(_predict_phase_colors_cached(sample_json))


def measure_phase_color_change(
    before: np.ndarray,
    after: np.ndarray,
    spot_px: tuple[float, float],
    radius_px: float,
    threshold_percent: float,
) -> dict[str, Any]:
    """Measure local raw RGB change around the fixed spot without auto white balance."""
    if before.shape != after.shape or before.ndim != 3 or before.shape[2] != 3:
        raise ValueError("Colorimetric verification requires two same-size RGB images.")
    if radius_px <= 0 or threshold_percent <= 0:
        raise ValueError("Invalid colorimetric ROI or threshold.")
    yy, xx = np.mgrid[: before.shape[0], : before.shape[1]]
    mask = (xx - spot_px[0]) ** 2 + (yy - spot_px[1]) ** 2 <= radius_px**2
    if not np.any(mask):
        raise ValueError("The spot lies outside the camera image.")
    before_rgb = before[mask].astype(float).mean(axis=0)
    after_rgb = after[mask].astype(float).mean(axis=0)
    total_change = float(100.0 * (after_rgb.sum() / max(before_rgb.sum(), 1e-12) - 1.0))
    channels = ("r", "g", "b")
    return {
        "before_rgb": dict(zip(channels, map(float, before_rgb))),
        "after_rgb": dict(zip(channels, map(float, after_rgb))),
        "intensity_change_percent": total_change,
        "b_over_g_before": float(before_rgb[2] / max(before_rgb[1], 1e-12)),
        "b_over_g_after": float(after_rgb[2] / max(after_rgb[1], 1e-12)),
        "detected": abs(total_change) >= threshold_percent,
        "direction": "brighter" if total_change >= 0 else "darker",
        "threshold_percent": threshold_percent,
    }
