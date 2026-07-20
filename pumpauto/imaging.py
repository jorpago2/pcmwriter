from __future__ import annotations

import time
from dataclasses import dataclass
from math import atan2, degrees
from typing import Any, Callable

import numpy as np

from .patterns import Point


@dataclass(frozen=True)
class SpotMeasurement:
    x_px: float
    y_px: float
    w_major_px: float
    w_minor_px: float
    angle_deg: float
    snr: float
    saturated: bool
    pixel_size_um: float

    @property
    def w_major_um(self) -> float:
        return self.w_major_px * self.pixel_size_um

    @property
    def w_minor_um(self) -> float:
        return self.w_minor_px * self.pixel_size_um

    @property
    def focus_metric(self) -> float:
        return self.w_major_px**2 + self.w_minor_px**2


@dataclass(frozen=True)
class FocusResult:
    best_z_um: float
    measured_best_z_um: float
    r_squared: float
    measurements: tuple[SpotMeasurement, ...]
    z_positions_um: tuple[float, ...]


@dataclass(frozen=True)
class PixelCalibration:
    um_per_pixel: float
    stage_to_pixel_px_per_um: tuple[tuple[float, float], tuple[float, float]]
    pixel_to_stage_um_per_px: tuple[tuple[float, float], tuple[float, float]]
    rotation_deg: float
    anisotropy: float
    registration_snr: float
    return_error_px: float


@dataclass(frozen=True)
class FocusPlane:
    a: float
    b: float
    c: float
    rms_um: float
    r_squared: float
    points: tuple[Point, ...]

    def z(self, x_um: float, y_um: float) -> float:
        return self.a * x_um + self.b * y_um + self.c


def fit_focus_plane(points: list[Point]) -> FocusPlane:
    if len(points) < 3:
        raise ValueError("The focus plane requires at least three points.")
    design = np.asarray([[point.x_um, point.y_um, 1.0] for point in points])
    if np.linalg.matrix_rank(design) < 3:
        raise ValueError("The focus-map XY points are collinear.")
    z = np.asarray([point.z_um for point in points])
    coefficients, _, _, _ = np.linalg.lstsq(design, z, rcond=None)
    predicted = design @ coefficients
    residual = float(np.sum((z - predicted) ** 2))
    total = float(np.sum((z - z.mean()) ** 2))
    r_squared = 1.0 - residual / total if total > 1e-15 else float(residual < 1e-15)
    return FocusPlane(
        *(float(value) for value in coefficients),
        rms_um=float(np.sqrt(residual / len(points))),
        r_squared=r_squared,
        points=tuple(points),
    )


def map_focus_plane(
    stage: Any,
    camera: Any,
    centers: list[Point],
    span_um: float,
    samples: int,
    pixel_size_um: float,
    settle_s: float,
    progress: Callable[[str], None] | None = None,
) -> FocusPlane:
    """Autofocus each XY point and restore the starting stage position."""
    original = stage.get_position()
    focused: list[Point] = []
    say = progress or (lambda _: None)
    try:
        for index, center in enumerate(centers):
            say(f"Focus map {index + 1}/{len(centers)}: X={center.x_um:g}, Y={center.y_um:g}")
            result, _ = autofocus(
                stage, camera, center, span_um, samples, pixel_size_um, settle_s, say
            )
            focused.append(Point(center.x_um, center.y_um, result.best_z_um))
        return fit_focus_plane(focused)
    finally:
        stage.move_to(original)


def focus_corrected(point: Point, plane: dict[str, Any]) -> Point:
    if not plane.get("enabled", False):
        return point
    return Point(
        point.x_um,
        point.y_um,
        float(plane["a"]) * point.x_um + float(plane["b"]) * point.y_um + float(plane["c"]),
    )


def phase_shift(reference: np.ndarray, moved: np.ndarray) -> tuple[float, float, float]:
    """Return feature displacement (dx, dy) using phase correlation."""
    if reference.shape != moved.shape:
        raise ValueError("Calibration images must have the same size.")
    ref = reference.astype(float).mean(axis=2) if reference.ndim == 3 else reference.astype(float)
    img = moved.astype(float).mean(axis=2) if moved.ndim == 3 else moved.astype(float)
    ref -= ref.mean()
    img -= img.mean()
    if ref.std() < 1e-6 or img.std() < 1e-6:
        raise ValueError("The image does not have enough texture to register motion.")
    window = np.outer(np.hanning(ref.shape[0]), np.hanning(ref.shape[1]))
    cross = np.fft.fft2(img * window) * np.conj(np.fft.fft2(ref * window))
    cross /= np.maximum(np.abs(cross), 1e-12)
    correlation = np.fft.ifft2(cross).real
    py, px = np.unravel_index(int(np.argmax(correlation)), correlation.shape)

    def subpixel(values: np.ndarray, index: int) -> float:
        left, center, right = values[(index - 1) % values.size], values[index], values[(index + 1) % values.size]
        denominator = left - 2.0 * center + right
        return 0.0 if abs(denominator) < 1e-12 else 0.5 * (left - right) / denominator

    dx = (px if px <= correlation.shape[1] // 2 else px - correlation.shape[1]) + subpixel(
        correlation[py, :], px
    )
    dy = (py if py <= correlation.shape[0] // 2 else py - correlation.shape[0]) + subpixel(
        correlation[:, px], py
    )
    snr = (float(correlation[py, px]) - float(np.median(correlation))) / (
        float(np.std(correlation)) + 1e-12
    )
    return float(dx), float(dy), snr


def calibrate_pixel_scale(
    stage: Any,
    camera: Any,
    step_um: float,
    settle_s: float,
    min_snr: float = 8.0,
    max_return_error_px: float = 1.0,
    max_anisotropy: float = 1.2,
    progress: Callable[[str], None] | None = None,
) -> PixelCalibration:
    """Move +X/+Y, register the LED images and always restore the initial position."""
    if step_um <= 0:
        raise ValueError("The calibration step must be positive.")
    say = progress or (lambda _: None)
    origin = stage.get_position()
    columns: list[tuple[float, float]] = []
    qualities: list[float] = []
    return_errors: list[float] = []
    try:
        reference = camera.capture()
        for axis in ("X", "Y"):
            target = Point(
                origin.x_um + (step_um if axis == "X" else 0.0),
                origin.y_um + (step_um if axis == "Y" else 0.0),
                origin.z_um,
            )
            say(f"Moving +{axis} by {step_um:g} µm")
            stage.move_to(target)
            time.sleep(max(0.0, settle_s))
            dx, dy, quality = phase_shift(reference, camera.capture())
            columns.append((dx / step_um, dy / step_um))
            qualities.append(quality)
            stage.move_to(origin)
            time.sleep(max(0.0, settle_s))
            rdx, rdy, return_quality = phase_shift(reference, camera.capture())
            qualities.append(return_quality)
            return_errors.append(float(np.hypot(rdx, rdy)))
    finally:
        stage.move_to(origin)

    matrix = np.column_stack(columns)
    if abs(float(np.linalg.det(matrix))) < 1e-9:
        raise ValueError("The pixel-to-stage transform is singular.")
    pixel_to_stage = np.linalg.inv(matrix)
    singular_values = np.linalg.svd(pixel_to_stage, compute_uv=False)
    result = PixelCalibration(
        um_per_pixel=float(np.sqrt(abs(np.linalg.det(pixel_to_stage)))),
        stage_to_pixel_px_per_um=tuple(tuple(float(v) for v in row) for row in matrix),
        pixel_to_stage_um_per_px=tuple(tuple(float(v) for v in row) for row in pixel_to_stage),
        rotation_deg=degrees(atan2(float(matrix[1, 0]), float(matrix[0, 0]))),
        anisotropy=float(singular_values.max() / singular_values.min()),
        registration_snr=min(qualities),
        return_error_px=max(return_errors),
    )
    if result.registration_snr < min_snr:
        raise ValueError(f"Unreliable registration: SNR={result.registration_snr:.1f}.")
    if result.return_error_px > max_return_error_px:
        raise ValueError(f"Excessive return error/hysteresis: {result.return_error_px:.2f} pixel.")
    if result.anisotropy > max_anisotropy:
        raise ValueError(f"Incompatible X/Y scales: anisotropy={result.anisotropy:.3f}.")
    return result


def measure_spot(
    image: np.ndarray,
    pixel_size_um: float,
    background: np.ndarray | None = None,
    roi_radius_px: int = 24,
) -> SpotMeasurement:
    """Measure Gaussian 1/e2 radii from the brightest camera channel."""
    if pixel_size_um <= 0 or image.ndim not in {2, 3}:
        raise ValueError("Invalid image or pixel calibration.")
    if background is not None and background.shape != image.shape:
        raise ValueError("Background and image must have the same size.")
    channels = image[..., None] if image.ndim == 2 else image
    background_channels = None if background is None else (background[..., None] if background.ndim == 2 else background)
    stride = max(1, int(np.ceil(max(image.shape[:2]) / 1024)))
    sampled = channels[::stride, ::stride].astype(float)
    if background_channels is not None:
        sampled -= background_channels[::stride, ::stride].astype(float)
    flat = sampled.reshape(-1, sampled.shape[-1])
    medians = np.median(flat, axis=0)
    contrasts = np.max(flat, axis=0) - medians
    channel_index = int(np.argmax(contrasts))
    signal = channels[..., channel_index].astype(float)
    if background_channels is not None:
        signal -= background_channels[..., channel_index].astype(float)
    signal -= medians[channel_index]
    peak_y, peak_x = np.unravel_index(int(np.argmax(signal)), signal.shape)
    y0, y1 = max(0, peak_y - roi_radius_px), min(signal.shape[0], peak_y + roi_radius_px + 1)
    x0, x1 = max(0, peak_x - roi_radius_px), min(signal.shape[1], peak_x + roi_radius_px + 1)
    roi = signal[y0:y1, x0:x1]
    noise_sample = signal[::stride, ::stride]
    absolute_deviation = np.abs(noise_sample - np.median(noise_sample))
    noise = max(1e-9, 1.4826 * float(np.median(absolute_deviation)))
    peak = float(roi.max())
    threshold = max(3.0 * noise, 0.01 * peak)
    weights = np.where(roi >= threshold, roi, 0.0)
    total = float(weights.sum())
    if peak / noise < 5.0 or total <= 0:
        raise ValueError("Spot not detected: SNR below 5.")
    yy, xx = np.mgrid[y0:y1, x0:x1]
    cx = float((weights * xx).sum() / total)
    cy = float((weights * yy).sum() / total)
    dx, dy = xx - cx, yy - cy
    covariance = np.array(
        [
            [(weights * dx * dx).sum(), (weights * dx * dy).sum()],
            [(weights * dx * dy).sum(), (weights * dy * dy).sum()],
        ]
    ) / total
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    if eigenvalues[0] <= 0:
        raise ValueError("The measured spot has no valid width.")
    major_vector = eigenvectors[:, 1]
    dtype_peak = 1.0 if np.issubdtype(image.dtype, np.floating) and image.max() <= 1.5 else float(
        np.iinfo(image.dtype).max if np.issubdtype(image.dtype, np.integer) else 255.0
    )
    raw_channel = image if image.ndim == 2 else image[..., channel_index]
    saturated = bool(np.max(raw_channel[y0:y1, x0:x1]) >= 0.98 * dtype_peak)
    return SpotMeasurement(
        x_px=cx,
        y_px=cy,
        w_major_px=2.0 * float(np.sqrt(eigenvalues[1])),
        w_minor_px=2.0 * float(np.sqrt(eigenvalues[0])),
        angle_deg=degrees(atan2(float(major_vector[1]), float(major_vector[0]))),
        snr=peak / noise,
        saturated=saturated,
        pixel_size_um=pixel_size_um,
    )


def fit_focus(z_positions_um: list[float], measurements: list[SpotMeasurement]) -> FocusResult:
    if len(z_positions_um) != len(measurements) or len(measurements) < 3:
        raise ValueError("Autofocus requires at least three Z measurements.")
    if any(item.saturated for item in measurements):
        raise ValueError("Some images are saturated; reduce power, gain, or exposure.")
    z = np.asarray(z_positions_um, dtype=float)
    metric = np.asarray([item.focus_metric for item in measurements])
    coefficients = np.polyfit(z, metric, 2)
    predicted = np.polyval(coefficients, z)
    residual = float(np.sum((metric - predicted) ** 2))
    total = float(np.sum((metric - metric.mean()) ** 2))
    r_squared = 1.0 - residual / total if total > 0 else 0.0
    measured_best = float(z[int(np.argmin(metric))])
    vertex = -coefficients[1] / (2.0 * coefficients[0]) if coefficients[0] > 0 else measured_best
    best = float(vertex) if r_squared >= 0.8 and z.min() <= vertex <= z.max() else measured_best
    return FocusResult(best, measured_best, r_squared, tuple(measurements), tuple(z_positions_um))


def autofocus(
    stage: Any,
    camera: Any,
    center: Point,
    span_um: float,
    samples: int,
    pixel_size_um: float,
    settle_s: float,
    progress: Callable[[str], None] | None = None,
) -> tuple[FocusResult, np.ndarray]:
    """Scan Z and minimize Gaussian spot width. Laser power must already be camera-safe."""
    if span_um <= 0 or samples < 5:
        raise ValueError("Use a positive span and at least five Z positions.")
    say = progress or (lambda _: None)
    original = stage.get_position()
    positions = np.linspace(center.z_um - span_um / 2.0, center.z_um + span_um / 2.0, samples)
    measurements: list[SpotMeasurement] = []
    images: list[np.ndarray] = []
    try:
        for index, z in enumerate(positions):
            stage.move_to(Point(center.x_um, center.y_um, float(z)))
            time.sleep(max(0.0, settle_s))
            image = camera.capture_spot() if hasattr(camera, "capture_spot") else camera.capture()
            measurement = measure_spot(image, pixel_size_um)
            if measurement.saturated:
                raise ValueError("Camera saturated during autofocus; reduce power, gain, or exposure.")
            measurements.append(measurement)
            images.append(image)
            say(f"Z {index + 1}/{samples}: {z:.3f} µm, w={measurement.w_major_um:.3f} µm")
        result = fit_focus(list(positions), measurements)
        stage.move_to(Point(center.x_um, center.y_um, result.best_z_um))
        best_image = images[int(np.argmin([item.focus_metric for item in measurements]))]
        return result, best_image
    except Exception:
        stage.move_to(original)
        raise
