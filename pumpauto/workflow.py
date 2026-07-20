from __future__ import annotations

import csv
import json
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .colorimetry import measure_phase_color_change
from .config import resolve_results_dir
from .imaging import focus_corrected
from .instruments import (
    KinesisBPC303Stage,
    PixelinkCamera,
    RigolMSO7054,
    SimAWG,
    SimCamera,
    SimLaser,
    SimScope,
    SimStage,
    Stradus639160,
    open_awg,
)
from .patterns import Point, validate
from .thermal import estimate, estimate_axisymmetric, from_config, multilayer_optics
from .waveform import assess_capture


@dataclass(frozen=True)
class Recipe:
    name: str
    points: list[Point]
    pulse_width_s: float
    repetition_hz: float
    pulse_count: int
    high_v: float
    optical_power_mw: float


@dataclass
class LabSystem:
    awg: Any
    laser: Any
    scope: Any
    stage: Any
    camera: Any
    simulated: bool

    def close(self) -> None:
        try:
            self.awg.configure_dc(0.0)
        except Exception:
            pass
        try:
            self.awg.output(False)
        except Exception:
            pass
        for device in (self.laser, self.camera, self.stage, self.scope, self.awg):
            try:
                device.close()
            except Exception:
                pass


def _burst_duration(recipe: Recipe) -> float:
    return (recipe.pulse_count - 1) / recipe.repetition_hz + recipe.pulse_width_s


def laser_peak_power(sample_power_mw: float, laser_config: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Convert requested sample power to Stradus PP without extrapolation."""
    calibration = laser_config.get("power_calibration", [])
    if not calibration:
        peak = float(laser_config["peak_power_mw"])
        return peak, {
            "mode": "manual_fallback",
            "requested_sample_mw": float(sample_power_mw),
            "stradus_pp_mw": peak,
        }
    sample_values = np.asarray([point[0] for point in calibration], dtype=float)
    if not sample_values[0] <= sample_power_mw <= sample_values[-1]:
        raise ValueError(
            f"Power {sample_power_mw:g} mW is outside the calibration range "
            f"[{sample_values[0]:g}, {sample_values[-1]:g}] mW."
        )
    peak = float(np.interp(sample_power_mw, sample_values, [point[1] for point in calibration]))
    return peak, {
        "mode": "linear_interpolation",
        "requested_sample_mw": float(sample_power_mw),
        "stradus_pp_mw": peak,
        "calibration_points": calibration,
    }


def create_system(
    config: dict[str, Any], imaging_only: bool = False, camera_only: bool = False
) -> LabSystem:
    if camera_only:
        camera = PixelinkCamera(config["camera"])
        return LabSystem(None, None, None, None, camera, False)
    if config["mode"] == "simulation":
        stage = SimStage(config["stage"]["origin_um"], config["stage"]["range_um"])
        awg = SimAWG(str(config["awg"].get("model", "T3AFG350")))
        return LabSystem(
            awg=awg,
            laser=SimLaser(),
            scope=SimScope(awg, int(config["simulation"]["seed"])),
            stage=stage,
            camera=SimCamera(
                stage,
                int(config["simulation"]["seed"]),
                float(config["simulation"]["camera_um_per_pixel"]),
                float(config["simulation"]["focus_z_um"]),
                float(config["simulation"]["spot_waist_px"]),
                float(config["simulation"]["rayleigh_range_um"]),
                float(config["simulation"]["spot_peak_counts"]),
                float(config["simulation"]["guide_angle_deg"]),
                float(config["simulation"]["guide_width_um"]),
                float(config["simulation"]["guide_contrast_counts"]),
                float(config["simulation"]["focus_slope_x"]),
                float(config["simulation"]["focus_slope_y"]),
            ),
            simulated=True,
        )
    if not config["safety"]["hardware_armed"]:
        raise RuntimeError(
            "Hardware is disarmed. Complete Diagnostics and arm it for this application session."
        )
    devices: list[Any] = []
    try:
        if imaging_only:
            stage = KinesisBPC303Stage(config["stage"])
            devices.append(stage)
            camera = PixelinkCamera(config["camera"])
            devices.append(camera)
            return LabSystem(
                awg=None,
                laser=None,
                scope=None,
                stage=stage,
                camera=camera,
                simulated=False,
            )
        awg = open_awg(config["awg"])
        devices.append(awg)
        laser = Stradus639160(
            **{
                k: config["laser"][k]
                for k in ("visa_resource", "baud_rate", "timeout_ms", "emission_settle_s")
            }
        )
        devices.append(laser)
        scope = RigolMSO7054(
            resource=config["scope"]["visa_resource"],
            **{k: config["scope"][k] for k in ("channel", "timeout_ms")},
        )
        devices.append(scope)
        stage = KinesisBPC303Stage(config["stage"])
        devices.append(stage)
        camera = PixelinkCamera(config["camera"])
        devices.append(camera)
    except Exception:
        for device in reversed(devices):
            try:
                device.close()
            except Exception:
                pass
        raise
    return LabSystem(
        awg=awg, laser=laser, scope=scope, stage=stage, camera=camera, simulated=False
    )


def validate_recipe(recipe: Recipe, config: dict[str, Any]) -> None:
    safety = config["safety"]
    corrected = [focus_corrected(point, config["stage"]["focus_plane"]) for point in recipe.points]
    validate(corrected, config["stage"]["range_um"], int(safety["max_points"]))
    if not float(safety["min_pulse_width_s"]) <= recipe.pulse_width_s <= float(
        safety["max_pulse_width_s"]
    ):
        raise ValueError("Pulse duration is outside the safety limit.")
    if not 1 <= recipe.pulse_count <= int(safety["max_pulses"]):
        raise ValueError("Pulse count is outside the safety limit.")
    if not float(safety["min_high_v"]) <= recipe.high_v <= float(safety["max_high_v"]):
        raise ValueError("TTL level is outside the safety limit.")
    if recipe.repetition_hz <= 0 or recipe.pulse_width_s * recipe.repetition_hz >= 0.9:
        raise ValueError("Invalid repetition rate or duty cycle >= 90%.")
    if _burst_duration(recipe) > float(safety["max_burst_duration_s"]):
        raise ValueError(
            f"Pulse train duration {_burst_duration(recipe):g} s exceeds the configured "
            f"{float(safety['max_burst_duration_s']):g} s safety limit."
        )
    awg_model = str(config["awg"].get("model", "T3AFG350"))
    if awg_model == "DG1062Z" and recipe.pulse_width_s < 16e-9:
        raise ValueError("The DG1062Z pulse width cannot be shorter than 16 ns.")
    max_repetition_hz = 25e6 if awg_model == "DG1062Z" else 350e6
    if recipe.repetition_hz > max_repetition_hz:
        raise ValueError(
            f"The repetition rate exceeds the {awg_model} pulse limit of "
            f"{max_repetition_hz / 1e6:g} MHz."
        )
    if not 0 < recipe.optical_power_mw <= float(safety["max_optical_power_mw"]):
        raise ValueError("Optical power is outside the configured physical limit.")
    if config["mode"] == "hardware" and not config["laser"]["power_calibration"]:
        raise ValueError("laser.power_calibration is missing; sample power cannot be converted to PP.")
    laser_peak_power(recipe.optical_power_mw, config["laser"])


def recipe_readiness(
    recipe: Recipe,
    config: dict[str, Any],
    config_path: str | Path = "config.json",
    free_bytes: int | None = None,
) -> dict[str, Any]:
    """Estimate run cost and return the non-actuating checks required before start."""
    validate_recipe(recipe, config)
    points = [focus_corrected(point, config["stage"]["focus_plane"]) for point in recipe.points]
    point_count = len(points)
    burst_s = _burst_duration(recipe)
    baseline_s = point_count * (
        float(config["stage"]["settle_s"])
        + burst_s
        + 2.0 * float(config["camera"]["exposure_ms"]) / 1000.0
        + 0.05
    )

    camera_roi = [int(value) for value in config["camera"]["roi"]]
    if camera_roi[2] and camera_roi[3]:
        frame_width, frame_height = camera_roi[2], camera_roi[3]
    elif config["mode"] == "simulation":
        frame_width = frame_height = 256
    else:
        frame_width, frame_height = 4912, 3680
    tracking_size = int(config["guide"]["tracking_roi_size_px"])
    saved_width, saved_height = min(tracking_size, frame_width), min(tracking_size, frame_height)
    spot_x, spot_y = map(float, config["guide"]["spot_pixel"])
    phase_radius = float(config["guide"]["phase_roi_radius_px"])
    roi_ready = (
        0 <= spot_x < frame_width
        and 0 <= spot_y < frame_height
        and min(saved_width, saved_height) >= 2 * phase_radius + 1
        and min(spot_x, spot_y, frame_width - 1 - spot_x, frame_height - 1 - spot_y)
        >= phase_radius
    )

    raw_npy_bytes = saved_width * saved_height * (2 * 3 + 8)
    estimated_bytes = point_count * (1_000_000 + 3 * raw_npy_bytes)
    results_dir = resolve_results_dir(config, config_path)
    if free_bytes is None:
        probe = results_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free_bytes = shutil.disk_usage(probe).free
    reserve_bytes = 512 * 1024**2
    storage_ready = estimated_bytes + reserve_bytes <= free_bytes

    def size(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024.0 or unit == "TB":
                return f"{amount:.1f} {unit}"
            amount /= 1024.0
        raise AssertionError

    checks = [
        (
            "Recipe limits",
            "READY",
            f"{point_count} points; {point_count * recipe.pulse_count} total pulses; "
            f"{burst_s:g} s burst/point",
        ),
        (
            "Corrected stage path",
            "READY",
            "; ".join(
                f"{axis.upper()} {min(getattr(point, f'{axis}_um') for point in points):.4f} to "
                f"{max(getattr(point, f'{axis}_um') for point in points):.4f} um"
                for axis in "xyz"
            ),
        ),
        (
            "Camera analysis ROI",
            "READY" if roi_ready else "BLOCKED",
            f"frame {frame_width}x{frame_height}; saved ROI {saved_width}x{saved_height}; "
            f"spot ({spot_x:g}, {spot_y:g}); phase radius {phase_radius:g} px",
        ),
        (
            "Storage",
            "READY" if storage_ready else "BLOCKED",
            f"conservative estimate {size(estimated_bytes)}; free {size(free_bytes)}; "
            f"keeps {size(reserve_bytes)} reserve; output {results_dir}",
        ),
        (
            "Baseline duration",
            "READY",
            f"at least {baseline_s:.1f} s; device communication, autofocus and alignment are not included",
        ),
        (
            "Hardware state",
            "READY"
            if config["mode"] == "simulation" or config["safety"]["hardware_armed"]
            else "BLOCKED",
            "simulation"
            if config["mode"] == "simulation"
            else "armed for this session"
            if config["safety"]["hardware_armed"]
            else "hardware is disarmed",
        ),
    ]
    return {
        "checks": checks,
        "blocked": any(status == "BLOCKED" for _, status, _ in checks),
        "estimated_duration_s": baseline_s,
        "estimated_bytes": estimated_bytes,
        "free_bytes": free_bytes,
        "results_dir": str(results_dir),
    }


def fire_single_pulse(
    sample_power_mw: float,
    pulse_width_s: float,
    high_v: float,
    config: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fire one calibrated optical pulse without stage, camera or scope access."""
    if config["mode"] != "hardware":
        raise RuntimeError("Select hardware mode before firing a real pulse.")
    if not config["safety"]["hardware_armed"]:
        raise RuntimeError("Hardware is disarmed. Complete Diagnostics before firing.")
    if pulse_width_s <= 0:
        raise ValueError("Pulse duration must be positive.")
    repetition_hz = min(1000.0, 0.1 / pulse_width_s)
    origin = config["stage"]["origin_um"]
    recipe = Recipe(
        "single_pulse",
        [Point(float(origin["x"]), float(origin["y"]), float(origin["z"]))],
        float(pulse_width_s),
        repetition_hz,
        1,
        float(high_v),
        float(sample_power_mw),
    )
    validate_recipe(recipe, config)
    peak_power_mw, conversion = laser_peak_power(sample_power_mw, config["laser"])
    say = progress or (lambda _: None)
    awg = laser = None
    try:
        awg = open_awg(config["awg"])
        awg.output(False)
        awg.configure_pulse(
            recipe.pulse_width_s,
            recipe.high_v,
            float(config["awg"]["low_v"]),
            1,
            repetition_hz,
        )
        laser = Stradus639160(
            **{
                key: config["laser"][key]
                for key in ("visa_resource", "baud_rate", "timeout_ms", "emission_settle_s")
            }
        )
        say(f"Preparing Stradus at PP={peak_power_mw:g} mW")
        laser_status = laser.prepare(peak_power_mw)
        say(f"Firing one {pulse_width_s * 1e6:g} us pulse")
        awg.output(True)
        awg.trigger()
        time.sleep(pulse_width_s + 0.05)
        return {
            "sample_power_mw": float(sample_power_mw),
            "stradus_pp_mw": peak_power_mw,
            "pulse_width_s": float(pulse_width_s),
            "awg": awg.identity,
            "laser": laser.identity,
            "laser_status": laser_status,
            "power_conversion": conversion,
        }
    finally:
        if awg is not None:
            try:
                awg.output(False)
            except Exception:
                pass
        if laser is not None:
            try:
                laser.close()
            except Exception:
                pass
        if awg is not None:
            try:
                awg.close()
            except Exception:
                pass


def image_change(before: np.ndarray, after: np.ndarray) -> tuple[float, np.ndarray]:
    if before.shape != after.shape:
        raise ValueError("Before and after images do not have the same size.")
    before_gray = before.astype(float).mean(axis=2) if before.ndim == 3 else before.astype(float)
    after_gray = after.astype(float).mean(axis=2) if after.ndim == 3 else after.astype(float)
    before_norm = (before_gray - np.median(before_gray)) / (np.std(before_gray) + 1e-9)
    after_norm = (after_gray - np.median(after_gray)) / (np.std(after_gray) + 1e-9)
    delta = np.abs(after_norm - before_norm)
    return float(np.percentile(delta, 99.9)), delta


def _analysis_roi(
    image: np.ndarray, center_px: tuple[float, float], size_px: int
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    height, width = image.shape[:2]
    x, y = map(float, center_px)
    if size_px <= 0 or not 0 <= x < width or not 0 <= y < height:
        raise ValueError("The configured spot or tracking ROI lies outside the camera image.")
    crop_width, crop_height = min(int(size_px), width), min(int(size_px), height)
    left = min(width - crop_width, max(0, int(round(x)) - crop_width // 2))
    top = min(height - crop_height, max(0, int(round(y)) - crop_height // 2))
    return image[top : top + crop_height, left : left + crop_width], (
        left,
        top,
        crop_width,
        crop_height,
    )


def _save_png(path: Path, array: np.ndarray, cmap: str | None = None) -> None:
    try:
        import matplotlib.pyplot as plt

        plt.imsave(path, array, cmap=cmap)
    except ImportError:
        pass


def run_recipe(
    recipe: Recipe,
    config: dict[str, Any],
    config_path: str | Path = "config.json",
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    point_adjuster: Callable[[LabSystem, Point, int], dict[str, Any]] | None = None,
) -> Path:
    validate_recipe(recipe, config)
    say = progress or (lambda _: None)
    is_cancelled = cancelled or (lambda: False)
    root = resolve_results_dir(config, config_path)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = root / f"{run_id}_{recipe.name.replace(' ', '_')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    system = create_system(config)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "mode": config["mode"],
        "recipe": {**asdict(recipe), "points": [asdict(p) for p in recipe.points]},
        "instruments": {
            "awg": system.awg.identity,
            "laser": system.laser.identity,
            "scope": system.scope.identity,
            "stage": system.stage.identity,
            "camera": system.camera.identity,
            "photodetector": config["photodetector"]["model"],
        },
        "points": [],
        "point_journal": "points.jsonl",
        "optical_model": multilayer_optics(config).to_dict(),
        "complete": False,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    point_journal = run_dir / manifest["point_journal"]
    point_journal.touch()
    try:
        system.awg.configure_pulse(
            recipe.pulse_width_s,
            recipe.high_v,
            float(config["awg"]["low_v"]),
            recipe.pulse_count,
            recipe.repetition_hz,
        )
        scope_settings = system.scope.configure_for_pulse(recipe.pulse_width_s, config["scope"])
        manifest["scope_settings"] = scope_settings
        manifest["scope_verification"] = "first pulse shape only" if recipe.pulse_count > 1 else "single pulse"
        peak_power_mw, conversion = laser_peak_power(recipe.optical_power_mw, config["laser"])
        manifest["power_conversion"] = conversion
        say(f"Preparing Stradus: interlock, PUL=1, PP={peak_power_mw:g} mW")
        manifest["laser_status"] = system.laser.prepare(peak_power_mw)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        thermal_input = from_config(
            config,
            recipe.optical_power_mw,
            recipe.pulse_width_s,
            pulse_count=recipe.pulse_count,
            repetition_hz=recipe.repetition_hz,
        )
        thermal = estimate(thermal_input)
        thermal_axisymmetric = estimate_axisymmetric(thermal_input, config)
        for index, nominal in enumerate(recipe.points):
            target = focus_corrected(nominal, config["stage"]["focus_plane"])
            if is_cancelled():
                raise RuntimeError("Run cancelled by the user.")
            say(f"Point {index + 1}/{len(recipe.points)}: moving to {target}")
            system.stage.move_to(target)
            if not system.simulated:
                time.sleep(float(config["stage"]["settle_s"]))
            alignment = point_adjuster(system, target, index) if point_adjuster else None
            actual = system.stage.get_position()
            before = system.camera.capture()
            before_camera_settings = (
                system.camera.settings() if hasattr(system.camera, "settings") else {}
            )
            system.scope.arm_single()
            say(f"Point {index + 1}: triggering")
            try:
                if is_cancelled():
                    raise RuntimeError("Run cancelled before triggering.")
                system.awg.output(True)
                system.awg.trigger()
                if not system.simulated:
                    deadline = time.monotonic() + _burst_duration(recipe) + 0.1
                    while time.monotonic() < deadline:
                        if is_cancelled():
                            raise RuntimeError("Run cancelled by the user.")
                        time.sleep(min(0.05, deadline - time.monotonic()))
            finally:
                system.awg.output(False)
            try:
                system.scope.wait_complete(float(config["scope"]["acquisition_timeout_s"]))
            except TimeoutError as exc:
                suggested = float(scope_settings["trigger_level_v"]) / 2.0
                raise TimeoutError(
                    f"{exc} Check PUL=1 and the DET02AFC signal; if present, try a {suggested:.4g} V trigger."
                ) from exc
            waveform_t, waveform_v = system.scope.acquire()
            pulse_metrics, capture_quality = assess_capture(
                waveform_t, waveform_v, scope_settings
            )
            pulse_analysis_error = None
            if not capture_quality["ok"]:
                pulse_analysis_error = " ".join(capture_quality["issues"])
                say(
                    f"Point {index + 1}: invalid capture: {pulse_analysis_error} "
                    f"Recommendation: {capture_quality['recommendations']}"
                )
            if system.simulated:
                system.camera.expose(actual, thermal_axisymmetric.peak_temperature_c)
            after = system.camera.capture()
            after_camera_settings = (
                system.camera.settings() if hasattr(system.camera, "settings") else {}
            )
            spot_px = tuple(map(float, config["guide"]["spot_pixel"]))
            analysis_before, analysis_roi = _analysis_roi(
                before, spot_px, int(config["guide"]["tracking_roi_size_px"])
            )
            left, top, width, height = analysis_roi
            analysis_after = after[top : top + height, left : left + width]
            score, delta = image_change(analysis_before, analysis_after)
            try:
                phase_color = measure_phase_color_change(
                    analysis_before,
                    analysis_after,
                    (spot_px[0] - left, spot_px[1] - top),
                    float(config["guide"]["phase_roi_radius_px"]),
                    float(config["guide"]["phase_change_threshold_percent"]),
                )
            except ValueError as exc:
                phase_color = {"detected": False, "error": str(exc)}
            if "error" in phase_color:
                say(f"Point {index + 1}: colorimetric verification unavailable: {phase_color['error']}")
            else:
                say(
                    f"Point {index + 1}: local RGB change "
                    f"{phase_color['intensity_change_percent']:+.1f}% "
                    f"({'detected' if phase_color['detected'] else 'below threshold'})"
                )
            stem = f"point_{index:04d}"
            np.save(run_dir / f"{stem}_before.npy", analysis_before)
            np.save(run_dir / f"{stem}_after.npy", analysis_after)
            np.save(run_dir / f"{stem}_delta.npy", delta)
            _save_png(run_dir / f"{stem}_before.png", analysis_before)
            _save_png(run_dir / f"{stem}_after.png", analysis_after)
            _save_png(run_dir / f"{stem}_delta.png", delta, "magma")
            with (run_dir / f"{stem}_waveform.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(("time_s", "voltage_v"))
                writer.writerows(zip(waveform_t, waveform_v))
            point_record = {
                "index": index,
                "nominal_um": asdict(nominal),
                "requested_um": asdict(target),
                "actual_um": asdict(actual),
                "alignment": alignment,
                "thermal": thermal.to_dict(),
                "thermal_axisymmetric_2d": thermal_axisymmetric.to_dict(),
                "pulse_metrics": pulse_metrics.to_dict() if pulse_metrics else None,
                "pulse_analysis_error": pulse_analysis_error,
                "capture_quality": capture_quality,
                "camera_settings": {
                    "before": before_camera_settings,
                    "after": after_camera_settings,
                },
                "image_change_score": score,
                "analysis_roi_px": {
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                },
                "phase_color_change": phase_color,
                "files": {
                    "before": f"{stem}_before.npy",
                    "after": f"{stem}_after.npy",
                    "delta": f"{stem}_delta.npy",
                    "waveform": f"{stem}_waveform.csv",
                },
            }
            manifest["points"].append(point_record)
            with point_journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(point_record, separators=(",", ":")) + "\n")
            if pulse_analysis_error:
                raise RuntimeError(
                    "Recipe stopped after saving the data: "
                    + pulse_analysis_error
                    + f" Recommendation: {capture_quality['recommendations']}"
                )
        manifest["complete"] = True
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        say(f"Recipe complete: {run_dir}")
        return run_dir
    except Exception as exc:
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        system.close()
