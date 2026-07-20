from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class PulseMetrics:
    polarity: str
    baseline_v: float
    noise_rms_v: float
    amplitude_v: float
    peak_v: float
    peak_time_s: float
    delay_50_s: float
    fwhm_s: float
    rise_10_90_s: float
    fall_90_10_s: float
    area_v_s: float
    snr: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _interpolate_crossing(
    time_s: np.ndarray, signal: np.ndarray, first: int, second: int, level: float
) -> float:
    y0, y1 = signal[first], signal[second]
    if y1 == y0:
        return float((time_s[first] + time_s[second]) / 2.0)
    fraction = (level - y0) / (y1 - y0)
    return float(time_s[first] + fraction * (time_s[second] - time_s[first]))


def _left_crossing(time_s: np.ndarray, signal: np.ndarray, peak: int, level: float) -> float:
    candidates = np.flatnonzero(signal[:peak] <= level)
    if not candidates.size:
        raise ValueError("The pulse starts before the acquisition window.")
    first = int(candidates[-1])
    return _interpolate_crossing(time_s, signal, first, first + 1, level)


def _right_crossing(time_s: np.ndarray, signal: np.ndarray, peak: int, level: float) -> float:
    candidates = np.flatnonzero(signal[peak + 1 :] <= level)
    if not candidates.size:
        raise ValueError("The pulse ends after the acquisition window.")
    second = peak + 1 + int(candidates[0])
    return _interpolate_crossing(time_s, signal, second - 1, second, level)


def analyze_pulse(
    time_s: np.ndarray,
    voltage_v: np.ndarray,
    trigger_time_s: float = 0.0,
    baseline_fraction: float = 0.15,
) -> PulseMetrics:
    """Analyze the dominant positive or negative pulse in one oscilloscope trace."""
    time_s = np.asarray(time_s, dtype=float)
    voltage_v = np.asarray(voltage_v, dtype=float)
    if (
        time_s.ndim != 1
        or voltage_v.ndim != 1
        or len(time_s) != len(voltage_v)
        or len(time_s) < 20
        or not np.all(np.isfinite(time_s))
        or not np.all(np.isfinite(voltage_v))
        or not np.all(np.diff(time_s) > 0)
        or not 0.05 <= baseline_fraction <= 0.4
    ):
        raise ValueError("Invalid waveform or baseline window.")
    baseline_samples = max(10, int(len(voltage_v) * baseline_fraction))
    baseline_data = voltage_v[:baseline_samples]
    baseline = float(np.median(baseline_data))
    noise = float(np.sqrt(np.mean((baseline_data - baseline) ** 2)))
    centered = voltage_v - baseline
    polarity = 1.0 if float(centered.max()) >= float(-centered.min()) else -1.0
    signal = polarity * centered
    peak_index = int(np.argmax(signal))
    amplitude = float(signal[peak_index])
    if peak_index < 2 or peak_index > len(signal) - 3:
        raise ValueError("The peak lies outside the usable window.")
    snr = float(amplitude / max(noise, np.finfo(float).eps))
    if amplitude <= 0 or snr < 5.0:
        raise ValueError(f"Pulse not detected: SNR={snr:.2f}.")
    # ponytail: one dominant pulse is enough for commissioning; segment trains
    # only when real Rigol traces show that the burst must be analyzed pulse by pulse.
    left_10 = _left_crossing(time_s, signal, peak_index, 0.10 * amplitude)
    left_50 = _left_crossing(time_s, signal, peak_index, 0.50 * amplitude)
    left_90 = _left_crossing(time_s, signal, peak_index, 0.90 * amplitude)
    right_90 = _right_crossing(time_s, signal, peak_index, 0.90 * amplitude)
    right_50 = _right_crossing(time_s, signal, peak_index, 0.50 * amplitude)
    right_10 = _right_crossing(time_s, signal, peak_index, 0.10 * amplitude)
    area_mask = (time_s >= left_10) & (time_s <= right_10)
    area = float(np.trapezoid(np.clip(signal[area_mask], 0.0, None), time_s[area_mask]))
    return PulseMetrics(
        polarity="positive" if polarity > 0 else "negative",
        baseline_v=baseline,
        noise_rms_v=noise,
        amplitude_v=amplitude,
        peak_v=baseline + polarity * amplitude,
        peak_time_s=float(time_s[peak_index]),
        delay_50_s=left_50 - trigger_time_s,
        fwhm_s=right_50 - left_50,
        rise_10_90_s=left_90 - left_10,
        fall_90_10_s=right_10 - right_90,
        area_v_s=area,
        snr=snr,
    )


def assess_capture(
    time_s: np.ndarray, voltage_v: np.ndarray, scope: dict[str, Any]
) -> tuple[PulseMetrics | None, dict[str, Any]]:
    """Validate one trace and suggest safer settings without firing again."""
    time_s = np.asarray(time_s, dtype=float)
    voltage_v = np.asarray(voltage_v, dtype=float)
    issues: list[str] = []
    try:
        metrics = analyze_pulse(time_s, voltage_v)
    except ValueError as exc:
        metrics = None
        issues.append(str(exc))

    scale = float(scope["vertical_scale_v_div"])
    offset = float(scope.get("vertical_offset_v", 0.0))
    baseline_count = max(10, int(len(voltage_v) * 0.15))
    baseline = float(np.median(voltage_v[:baseline_count]))
    centered = voltage_v - baseline
    polarity = 1.0 if centered.max() >= -centered.min() else -1.0
    amplitude = float(np.max(polarity * centered))
    recommendations: dict[str, Any] = {
        "trigger_level_v": baseline + polarity * 0.30 * amplitude,
    }

    if float(np.mean(time_s < 0.0)) < 0.20:
        issues.append("Insufficient baseline before the trigger.")
        recommendations["window_factor"] = max(10.0, float(scope["window_factor"]) * 2.0)

    clipped = float(np.max(np.abs(voltage_v + offset))) >= 3.6 * scale
    if clipped:
        issues.append("The waveform reaches the vertical boundary and may be clipped.")
        recommendations["vertical_scale_v_div"] = min(1.0, scale * 2.0)
        if scale >= 1.0:
            recommendations["detector_action"] = "Reduce power in the DET02AFC branch or add attenuation."
    elif amplitude < 0.5 * scale:
        issues.append("The signal occupies less than half a vertical division.")
        recommendations["vertical_scale_v_div"] = max(0.001, scale / 2.0)

    if metrics is not None:
        active = np.flatnonzero(polarity * centered >= 0.10 * metrics.amplitude_v)
        if active.size and min(int(active[0]), len(voltage_v) - 1 - int(active[-1])) < 0.05 * len(voltage_v):
            issues.append("The pulse lies too close to the time-window boundary.")
            recommendations["window_factor"] = max(10.0, float(scope["window_factor"]) * 2.0)

    return metrics, {
        "ok": not issues,
        "issues": list(dict.fromkeys(issues)),
        "recommendations": recommendations if issues else {},
    }
