from __future__ import annotations

import time
from dataclasses import dataclass
from math import ceil, degrees
from typing import Any, Callable

import numpy as np

from .patterns import Point


@dataclass(frozen=True)
class WaveguideDetection:
    center_px: tuple[float, float]
    direction_px: tuple[float, float]
    start_px: tuple[float, float]
    end_px: tuple[float, float]
    angle_deg: float
    width_px: float
    confidence: float
    snr: float
    roi: tuple[int, int, int, int]


@dataclass(frozen=True)
class StructuralFocusResult:
    best_z_um: float
    measured_best_z_um: float
    r_squared: float
    z_positions_um: tuple[float, ...]
    metrics: tuple[float, ...]


@dataclass(frozen=True)
class GuidePlan:
    points: tuple[Point, ...]
    pixels: tuple[tuple[float, float], ...]
    detection: WaveguideDetection
    length_um: float
    actual_step_um: float
    available_length_um: float


@dataclass(frozen=True)
class AlignmentResult:
    position: Point
    error_um: float
    iterations: int
    confidence: float


def _crop(image: np.ndarray, roi: tuple[int, int, int, int]) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    if image.ndim not in {2, 3}:
        raise ValueError("The waveguide image must be grayscale or RGB.")
    left, top, width, height = roi
    if width == 0 and height == 0:
        left, top, width, height = 0, 0, image.shape[1], image.shape[0]
    if min(left, top, width, height) < 0 or width < 24 or height < 24:
        raise ValueError("The waveguide ROI is invalid or too small.")
    right, bottom = left + width, top + height
    if right > image.shape[1] or bottom > image.shape[0]:
        raise ValueError("The waveguide ROI lies outside the image.")
    return image[top:bottom, left:right], (left, top, width, height)


def _gray(image: np.ndarray) -> np.ndarray:
    data = image.astype(float)
    if data.ndim == 2:
        return data
    contrasts = np.percentile(data, 99, axis=(0, 1)) - np.percentile(data, 1, axis=(0, 1))
    return data[..., int(np.argmax(contrasts))]


def _smooth(image: np.ndarray, sigma_px: float = 5.0) -> np.ndarray:
    fy = np.fft.fftfreq(image.shape[0])[:, None]
    fx = np.fft.fftfreq(image.shape[1])[None, :]
    kernel = np.exp(-2.0 * np.pi**2 * sigma_px**2 * (fx**2 + fy**2))
    return np.fft.ifft2(np.fft.fft2(image) * kernel).real


def _weighted_percentile(values: np.ndarray, weights: np.ndarray, fraction: float) -> float:
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order])
    return float(values[order[np.searchsorted(cumulative, fraction * cumulative[-1])]])


def detect_waveguide(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
    min_confidence: float = 0.0,
    expected_direction: tuple[float, float] | None = None,
    mask_center: tuple[float, float] | None = None,
    mask_radius_px: float = 0.0,
) -> WaveguideDetection:
    """Detect one straight waveguide inside a user-selected ROI."""
    cropped, roi = _crop(image, roi)
    left, top, _, _ = roi
    gray = _gray(cropped)
    response = np.abs(gray - _smooth(gray))
    threshold = float(np.percentile(response, 82.0))
    weights = np.maximum(response - threshold, 0.0)
    yy, xx = np.mgrid[top : top + gray.shape[0], left : left + gray.shape[1]]
    if mask_center is not None and mask_radius_px > 0:
        mask = (xx - mask_center[0]) ** 2 + (yy - mask_center[1]) ** 2 <= mask_radius_px**2
        weights[mask] = 0.0
    total = float(weights.sum())
    if total <= 0:
        raise ValueError("Not enough structure was detected inside the ROI.")
    center = np.asarray([(weights * xx).sum(), (weights * yy).sum()]) / total
    coordinates = np.column_stack((xx.ravel() - center[0], yy.ravel() - center[1]))
    flat_weights = weights.ravel()
    covariance = (coordinates * flat_weights[:, None]).T @ coordinates / total
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    direction = eigenvectors[:, 1]
    if direction[0] < 0 or (abs(direction[0]) < 1e-12 and direction[1] < 0):
        direction = -direction
    if expected_direction is not None:
        expected = np.asarray(expected_direction, dtype=float)
        expected /= np.linalg.norm(expected)
        if float(direction @ expected) < 0:
            direction = -direction
        if abs(float(direction @ expected)) < np.cos(np.deg2rad(30.0)):
            raise ValueError("The detected structure does not follow the expected waveguide direction.")
    projection = coordinates @ direction
    low = _weighted_percentile(projection, flat_weights, 0.02)
    high = _weighted_percentile(projection, flat_weights, 0.98)
    start, end = center + low * direction, center + high * direction
    anisotropy = float((eigenvalues[1] - eigenvalues[0]) / max(eigenvalues.sum(), 1e-12))
    noise = 1.4826 * float(np.median(np.abs(response - np.median(response)))) + 1e-9
    snr = float((np.percentile(response, 99) - np.median(response)) / noise)
    confidence = float(np.clip(anisotropy * min(1.0, snr / 8.0), 0.0, 1.0))
    if confidence < min_confidence:
        raise ValueError(
            f"Unreliable waveguide detection: confidence={confidence:.2f} < {min_confidence:.2f}."
        )
    return WaveguideDetection(
        center_px=(float(center[0]), float(center[1])),
        direction_px=(float(direction[0]), float(direction[1])),
        start_px=(float(start[0]), float(start[1])),
        end_px=(float(end[0]), float(end[1])),
        angle_deg=degrees(float(np.arctan2(direction[1], direction[0]))),
        width_px=2.0 * float(np.sqrt(max(eigenvalues[0], 0.0))),
        confidence=confidence,
        snr=snr,
        roi=roi,
    )


def structural_focus_metric(image: np.ndarray, roi: tuple[int, int, int, int]) -> float:
    cropped, _ = _crop(image, roi)
    gray = _gray(cropped)
    gy, gx = np.gradient(gray)
    energy = gx * gx + gy * gy
    threshold = np.percentile(energy, 90)
    return float(np.mean(energy[energy >= threshold]))


def structural_autofocus(
    stage: Any,
    camera: Any,
    center: Point,
    span_um: float,
    samples: int,
    roi: tuple[int, int, int, int],
    settle_s: float,
    progress: Callable[[str], None] | None = None,
) -> tuple[StructuralFocusResult, np.ndarray]:
    """Focus the LED image by maximizing structural edge energy."""
    if span_um <= 0 or samples < 5:
        raise ValueError("Structural autofocus requires a positive span and at least five samples.")
    say = progress or (lambda _: None)
    original = stage.get_position()
    positions = np.linspace(center.z_um - span_um / 2.0, center.z_um + span_um / 2.0, samples)
    metrics: list[float] = []
    images: list[np.ndarray] = []
    try:
        for index, z_um in enumerate(positions):
            stage.move_to(Point(center.x_um, center.y_um, float(z_um)))
            time.sleep(max(0.0, settle_s))
            image = camera.capture()
            metric = structural_focus_metric(image, roi)
            metrics.append(metric)
            images.append(image)
            say(f"Waveguide focus {index + 1}/{samples}: Z={z_um:.4f} µm, sharpness={metric:.3g}")
        measured = float(positions[int(np.argmax(metrics))])
        coefficients = np.polyfit(positions, metrics, 2)
        predicted = np.polyval(coefficients, positions)
        residual = float(np.sum((np.asarray(metrics) - predicted) ** 2))
        total = float(np.sum((np.asarray(metrics) - np.mean(metrics)) ** 2))
        r_squared = 1.0 - residual / total if total > 0 else 0.0
        vertex = -coefficients[1] / (2.0 * coefficients[0]) if coefficients[0] < 0 else measured
        best = float(vertex) if r_squared >= 0.6 and positions.min() <= vertex <= positions.max() else measured
        stage.move_to(Point(center.x_um, center.y_um, best))
        time.sleep(max(0.0, settle_s))
        best_image = camera.capture()
        return (
            StructuralFocusResult(
                best,
                measured,
                r_squared,
                tuple(map(float, positions)),
                tuple(metrics),
            ),
            best_image,
        )
    except Exception:
        stage.move_to(original)
        raise


def plan_waveguide_write(
    detection: WaveguideDetection,
    current: Point,
    spot_px: tuple[float, float],
    pixel_to_stage_um_per_px: np.ndarray,
    length_um: float,
    max_step_um: float,
    direction: int = 1,
    z_start_um: float | None = None,
    z_end_um: float | None = None,
) -> GuidePlan:
    if length_um <= 0 or max_step_um <= 0 or direction not in {-1, 1}:
        raise ValueError("Invalid write length, step, or direction.")
    transform = np.asarray(pixel_to_stage_um_per_px, dtype=float)
    if transform.shape != (2, 2) or abs(float(np.linalg.det(transform))) < 1e-12:
        raise ValueError("A valid 2x2 pixel-to-stage calibration is required.")
    tangent = np.asarray(detection.direction_px) * direction
    endpoint = np.asarray(detection.start_px if direction == 1 else detection.end_px)
    opposite = np.asarray(detection.end_px if direction == 1 else detection.start_px)
    stage_per_pixel = float(np.linalg.norm(-transform @ tangent))
    available = float(np.linalg.norm(-transform @ (opposite - endpoint)))
    if length_um > available + 1e-9:
        raise ValueError(
            f"Requested length ({length_um:g} µm) exceeds the visible waveguide ({available:.3g} µm)."
        )
    start_delta = transform @ (np.asarray(spot_px) - endpoint)
    stage_direction = -transform @ tangent
    stage_direction /= np.linalg.norm(stage_direction)
    intervals = max(1, ceil(length_um / max_step_um))
    actual_step = length_um / intervals
    z0 = current.z_um if z_start_um is None else z_start_um
    z1 = z0 if z_end_um is None else z_end_um
    points: list[Point] = []
    pixels: list[tuple[float, float]] = []
    for index in range(intervals + 1):
        distance = index * actual_step
        fraction = distance / length_um
        xy = np.asarray([current.x_um, current.y_um]) + start_delta + stage_direction * distance
        points.append(Point(float(xy[0]), float(xy[1]), float(z0 + fraction * (z1 - z0))))
        pixel = endpoint + tangent * (distance / stage_per_pixel)
        pixels.append((float(pixel[0]), float(pixel[1])))
    return GuidePlan(tuple(points), tuple(pixels), detection, length_um, actual_step, available)


def prepare_guide_plan(
    stage: Any,
    camera: Any,
    center: Point,
    detection_roi: tuple[int, int, int, int],
    tracking_roi: tuple[int, int, int, int],
    spot_px: tuple[float, float],
    pixel_to_stage_um_per_px: np.ndarray,
    length_um: float,
    max_step_um: float,
    direction: int,
    autofocus_span_um: float,
    autofocus_samples: int,
    min_confidence: float,
    settle_s: float,
    progress: Callable[[str], None] | None = None,
) -> tuple[GuidePlan, np.ndarray, tuple[StructuralFocusResult, StructuralFocusResult]]:
    """Focus, detect and plan a straight guide; restore the initial stage position."""
    original = stage.get_position()
    say = progress or (lambda _: None)
    try:
        stage.move_to(center)
        initial_focus, image = structural_autofocus(
            stage, camera, center, autofocus_span_um, autofocus_samples, detection_roi, settle_s, say
        )
        detection = detect_waveguide(image, detection_roi, min_confidence)
        base = Point(center.x_um, center.y_um, initial_focus.best_z_um)
        provisional = plan_waveguide_write(
            detection,
            base,
            spot_px,
            pixel_to_stage_um_per_px,
            length_um,
            max_step_um,
            direction,
        )
        say("Structural autofocus at the start of the selected length")
        start_focus, _ = structural_autofocus(
            stage,
            camera,
            provisional.points[0],
            autofocus_span_um,
            autofocus_samples,
            tracking_roi,
            settle_s,
            say,
        )
        end_center = Point(
            provisional.points[-1].x_um,
            provisional.points[-1].y_um,
            start_focus.best_z_um,
        )
        say("Structural autofocus at the end of the selected length")
        end_focus, _ = structural_autofocus(
            stage,
            camera,
            end_center,
            autofocus_span_um,
            autofocus_samples,
            tracking_roi,
            settle_s,
            say,
        )
        return (
            plan_waveguide_write(
                detection,
                base,
                spot_px,
                pixel_to_stage_um_per_px,
                length_um,
                max_step_um,
                direction,
                start_focus.best_z_um,
                end_focus.best_z_um,
            ),
            image,
            (start_focus, end_focus),
        )
    finally:
        stage.move_to(original)


def align_waveguide_at_spot(
    stage: Any,
    camera: Any,
    spot_px: tuple[float, float],
    pixel_to_stage_um_per_px: np.ndarray,
    roi: tuple[int, int, int, int],
    expected_direction: tuple[float, float],
    tolerance_um: float,
    max_correction_um: float,
    max_iterations: int,
    min_confidence: float,
    settle_s: float,
) -> AlignmentResult:
    transform = np.asarray(pixel_to_stage_um_per_px, dtype=float)
    last_error = float("inf")
    confidence = 0.0
    for iteration in range(max_iterations + 1):
        image = camera.capture()
        detection = detect_waveguide(
            image,
            roi,
            min_confidence,
            expected_direction,
            mask_center=spot_px,
            mask_radius_px=8.0,
        )
        confidence = detection.confidence
        center = np.asarray(detection.center_px)
        tangent = np.asarray(detection.direction_px)
        spot = np.asarray(spot_px)
        closest = center + tangent * float((spot - center) @ tangent)
        correction = transform @ (spot - closest)
        last_error = float(np.linalg.norm(correction))
        if last_error <= tolerance_um:
            return AlignmentResult(stage.get_position(), last_error, iteration, confidence)
        if last_error > max_correction_um:
            raise ValueError(
                f"Transverse correction {last_error:.3f} µm exceeds the {max_correction_um:.3f} µm limit."
            )
        if iteration == max_iterations:
            break
        current = stage.get_position()
        stage.move_to(
            Point(current.x_um + float(correction[0]), current.y_um + float(correction[1]), current.z_um)
        )
        time.sleep(max(0.0, settle_s))
    raise ValueError(
        f"The waveguide did not converge to the spot: error={last_error:.3f} µm after {max_iterations} corrections."
    )
