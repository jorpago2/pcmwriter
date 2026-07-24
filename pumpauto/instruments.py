from __future__ import annotations

import array
import ctypes
import json
import os
import platform
import subprocess
import sys
import time
from ctypes import create_string_buffer, util
from pathlib import Path
from typing import Any

import numpy as np

from .patterns import Point


class HardwareUnavailable(RuntimeError):
    pass


def scope_settings_for_pulse(
    pulse_width_s: float, config: dict[str, Any], channel: int
) -> dict[str, Any]:
    """Return a conservative Rigol setup with the trigger centred on screen."""
    width = float(pulse_width_s)
    if width <= 0:
        raise ValueError("Pulse duration must be positive.")
    window_factor = float(config.get("window_factor", 6.0))
    vertical_scale = float(config.get("vertical_scale_v_div", 1.0))
    if window_factor <= 1 or vertical_scale <= 0:
        raise ValueError("Oscilloscope time and vertical scales must be positive.")
    impedance = str(config.get("input_impedance", "FIFTy"))
    coupling = str(config.get("coupling", "DC")).upper()
    slope = str(config.get("trigger_slope", "POSitive"))
    bandwidth = str(config.get("bandwidth_limit", "OFF")).upper()
    trigger_source = str(config.get("trigger_source") or f"CHAN{channel}").upper()
    if impedance.upper() not in {"FIFTY", "OMEG"}:
        raise ValueError("scope.input_impedance must be FIFTy or OMEG.")
    if coupling not in {"AC", "DC", "GND"}:
        raise ValueError("scope.coupling must be AC, DC, or GND.")
    if slope.upper() not in {"POSITIVE", "NEGATIVE", "RFAL"}:
        raise ValueError("scope.trigger_slope must be POSitive, NEGative, or RFAL.")
    if bandwidth not in {"ON", "OFF"}:
        raise ValueError("scope.bandwidth_limit must be ON or OFF.")
    if trigger_source not in {"CHAN1", "CHAN2", "CHAN3", "CHAN4", "EXT", "EXT5"}:
        raise ValueError("Invalid trigger source for the MSO7054.")
    return {
        "channel": int(channel),
        "time_scale_s_div": min(1000.0, max(1e-9, width * window_factor / 10.0)),
        "time_offset_s": 0.0,
        "vertical_scale_v_div": vertical_scale,
        "vertical_offset_v": float(config.get("vertical_offset_v", 0.0)),
        "input_impedance": "FIFTy" if impedance.upper() == "FIFTY" else "OMEG",
        "coupling": coupling,
        "bandwidth_limit": bandwidth,
        "trigger_source": trigger_source,
        "trigger_level_v": float(config.get("trigger_level_v", 0.1)),
        "trigger_slope": slope,
        "window_factor": window_factor,
    }


class SimAWG:
    def __init__(self, model: str = "T3AFG350") -> None:
        self.identity = f"SIMULATED,{model},0,0"
        self.output_enabled = False
        self.triggered = False
        self.settings: dict[str, float | int] = {}

    def configure_pulse(
        self, width_s: float, high_v: float, low_v: float, count: int, repetition_hz: float
    ) -> None:
        self.settings = {
            "width_s": width_s,
            "high_v": high_v,
            "low_v": low_v,
            "count": count,
            "repetition_hz": repetition_hz,
        }
        self.triggered = False

    def output(self, enabled: bool) -> None:
        self.output_enabled = enabled

    def configure_dc(self, level_v: float) -> None:
        self.settings = {"dc_level_v": float(level_v)}

    def trigger(self) -> None:
        if not self.output_enabled:
            raise RuntimeError("The simulated AWG output is off.")
        self.triggered = True

    def close(self) -> None:
        self.output(False)


class SimLaser:
    identity = "SIMULATED,VORTRAN Stradus,639nm,160mW"

    def __init__(self) -> None:
        self.prepared = False
        self.peak_power_mw = 0.0

    def prepare(self, peak_power_mw: float) -> dict[str, Any]:
        peak = float(peak_power_mw)
        if not 0 < peak <= 160.0:
            raise ValueError("Stradus peak power must be between 0 and 160 mW.")
        self.peak_power_mw = peak
        self.prepared = True
        return {"fault_code": 0, "interlock": 1, "control_mode": 0, "pul": 1,
                "emission_enabled": 1, "peak_power_mw": peak}

    def close(self) -> None:
        self.prepared = False


class SimScope:
    identity = "SIMULATED,RIGOL MSO7054,0,0"

    def __init__(self, awg: SimAWG, seed: int = 639) -> None:
        self.awg = awg
        self.rng = np.random.default_rng(seed)
        self.settings: dict[str, Any] = {}
        self.armed = False
        self.complete = False

    def configure_for_pulse(self, pulse_width_s: float, config: dict[str, Any]) -> dict[str, Any]:
        self.settings = scope_settings_for_pulse(pulse_width_s, config, 1)
        return self.settings

    def arm_single(self) -> None:
        self.armed = True
        self.complete = False

    def wait_complete(self, timeout_s: float) -> None:
        if not self.armed or not self.awg.triggered:
            raise TimeoutError("The simulated scope did not receive an armed AWG trigger.")
        self.complete = True

    def acquire(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.complete:
            raise RuntimeError("Arm and trigger the simulated scope before acquisition.")
        width = float(self.awg.settings.get("width_s", 1e-6))
        high = 1.0  # Simulated photodiode voltage, not the AWG TTL level.
        time_scale = float(self.settings.get("time_scale_s_div", 0.3 * width))
        t = np.linspace(-5.0 * time_scale, 5.0 * time_scale, 1200)
        rise = max(width / 35.0, 1e-10)
        pulse = 0.5 * high * (
            np.tanh((t - 0.0) / rise) - np.tanh((t - width) / rise)
        )
        pulse += self.rng.normal(0.0, max(high * 0.004, 1e-4), t.size)
        self.armed = False
        self.complete = False
        self.awg.triggered = False
        return t, pulse

    def close(self) -> None:
        pass


class SimStage:
    identity = "SIMULATED,BPC303+MAX311D/M,0,0"

    def __init__(self, origin: dict[str, float], ranges: dict[str, list[float]]) -> None:
        self.ranges = ranges
        self.position = Point(float(origin["x"]), float(origin["y"]), float(origin["z"]))

    def move_to(self, target: Point) -> Point:
        for axis, value in (("x", target.x_um), ("y", target.y_um), ("z", target.z_um)):
            low, high = self.ranges[axis]
            if not low <= value <= high:
                raise ValueError(f"Motion {axis}={value} µm is outside [{low}, {high}].")
        self.position = target
        return self.position

    def get_position(self) -> Point:
        return self.position

    def close(self) -> None:
        pass


class SimCamera:
    identity = "SIMULATED,Pixelink M18-CYL,0,0"

    def __init__(
        self,
        stage: SimStage,
        seed: int = 639,
        um_per_pixel: float = 0.08,
        focus_z_um: float = 10.0,
        spot_waist_px: float = 5.0,
        rayleigh_range_um: float = 1.2,
        spot_peak_counts: float = 140.0,
        guide_angle_deg: float = 20.0,
        guide_width_um: float = 0.5,
        guide_contrast_counts: float = 45.0,
        focus_slope_x: float = 0.03,
        focus_slope_y: float = -0.015,
    ) -> None:
        self.stage = stage
        self.rng = np.random.default_rng(seed)
        self.um_per_pixel = um_per_pixel
        self.focus_z_um = focus_z_um
        self.spot_waist_px = spot_waist_px
        self.rayleigh_range_um = rayleigh_range_um
        self.spot_peak_counts = spot_peak_counts
        self.guide_angle_rad = np.deg2rad(guide_angle_deg)
        self.guide_width_um = guide_width_um
        self.guide_contrast_counts = guide_contrast_counts
        self.focus_slope_x = focus_slope_x
        self.focus_slope_y = focus_slope_y
        self.origin = stage.get_position()
        self.changed: list[tuple[Point, float]] = []
        yy, xx = np.mgrid[:256, :256]
        self.texture = (
            4.0 * np.sin(xx / 17.0)
            + 3.0 * np.cos(yy / 23.0)
            + self.rng.normal(0.0, 1.2, (256, 256))
        )

    def _local_focus_z(self) -> float:
        position = self.stage.get_position()
        return (
            self.focus_z_um
            + self.focus_slope_x * (position.x_um - self.origin.x_um)
            + self.focus_slope_y * (position.y_um - self.origin.y_um)
        )

    @staticmethod
    def _blur(image: np.ndarray, sigma_px: float) -> np.ndarray:
        if sigma_px < 0.1:
            return image
        fy = np.fft.fftfreq(image.shape[0])[:, None]
        fx = np.fft.fftfreq(image.shape[1])[None, :]
        kernel = np.exp(-2.0 * np.pi**2 * sigma_px**2 * (fx**2 + fy**2))
        return np.stack(
            [np.fft.ifft2(np.fft.fft2(image[..., channel]) * kernel).real for channel in range(3)],
            axis=-1,
        )

    def expose(self, position: Point, peak_temperature_c: float) -> None:
        strength = float(np.clip((peak_temperature_c - 120.0) / 500.0, 0.0, 1.0))
        if strength > 0:
            self.changed.append((position, strength))

    def capture(self) -> np.ndarray:
        current = self.stage.get_position()
        dx = -(current.x_um - self.origin.x_um) / self.um_per_pixel
        dy = -(current.y_um - self.origin.y_um) / self.um_per_pixel
        fy = np.fft.fftfreq(self.texture.shape[0])[:, None]
        fx = np.fft.fftfreq(self.texture.shape[1])[None, :]
        shifted_texture = np.fft.ifft2(
            np.fft.fft2(self.texture) * np.exp(-2j * np.pi * (fy * dy + fx * dx))
        ).real
        image = np.empty((256, 256, 3), dtype=np.float64)
        image[..., 0] = 58 + shifted_texture * 0.5
        image[..., 1] = 92 + shifted_texture * 0.8
        image[..., 2] = 145 + shifted_texture
        yy, xx = np.mgrid[:256, :256]
        sample_x = current.x_um + (xx - 128.0) * self.um_per_pixel
        sample_y = current.y_um + (yy - 128.0) * self.um_per_pixel
        normal_x, normal_y = -np.sin(self.guide_angle_rad), np.cos(self.guide_angle_rad)
        distance_um = (
            (sample_x - self.origin.x_um) * normal_x
            + (sample_y - self.origin.y_um) * normal_y
        )
        guide = np.exp(-2.0 * distance_um**2 / self.guide_width_um**2)
        image[..., 0] -= 0.45 * self.guide_contrast_counts * guide
        image[..., 1] -= 0.75 * self.guide_contrast_counts * guide
        image[..., 2] -= self.guide_contrast_counts * guide
        for position, strength in self.changed:
            # The spot is fixed in the optical frame; moving the sample changes
            # where a written feature appears relative to the current field.
            dx = (position.x_um - current.x_um) / self.um_per_pixel
            dy = (position.y_um - current.y_um) / self.um_per_pixel
            gaussian = np.exp(-((xx - (128 + dx)) ** 2 + (yy - (128 + dy)) ** 2) / (2 * 3.0**2))
            image[..., 0] += 30.0 * strength * gaussian
            image[..., 1] += 50.0 * strength * gaussian
            image[..., 2] += 85.0 * strength * gaussian
        defocus = abs(current.z_um - self._local_focus_z()) / max(self.rayleigh_range_um, 1e-9)
        image = self._blur(image, 1.8 * defocus)
        return np.clip(image, 0, 255).astype(np.uint8)

    def capture_spot(self) -> np.ndarray:
        image = self.capture().astype(float)
        yy, xx = np.mgrid[:256, :256]
        dz = self.stage.get_position().z_um - self._local_focus_z()
        waist = self.spot_waist_px * np.sqrt(1.0 + (dz / self.rayleigh_range_um) ** 2)
        spot = np.exp(-2.0 * ((xx - 128.0) ** 2 + (yy - 128.0) ** 2) / waist**2)
        image[..., 0] += self.spot_peak_counts * spot
        return np.clip(image, 0, 255).astype(np.uint8)

    def close(self) -> None:
        pass


class _VisaAWG:
    def __init__(
        self, resource: str, channel: int = 1, load_ohm: int = 50, timeout_ms: int = 5000
    ) -> None:
        try:
            import pyvisa
        except ImportError as exc:
            raise HardwareUnavailable("pyvisa is missing. Run install_lab.ps1.") from exc
        self.channel = int(channel)
        self.load_ohm = int(load_ohm)
        if self.load_ohm != 50:
            raise ValueError("The Stradus digital input requires a 50-ohm source.")
        self.rm = pyvisa.ResourceManager()
        self.device = None
        try:
            self.device = self.rm.open_resource(resource)
            self.device.timeout = timeout_ms
            self.identity = str(self.device.query("*IDN?")).strip()
        except Exception:
            if self.device is not None:
                self.device.close()
            self.rm.close()
            raise

    def close(self) -> None:
        try:
            self.output(False)
        finally:
            self.device.close()
            self.rm.close()


class T3AFG350(_VisaAWG):
    """Small SCPI adapter based on the official T3AFG programming guide."""

    def __init__(
        self, resource: str, channel: int = 1, load_ohm: int = 50, timeout_ms: int = 5000
    ) -> None:
        super().__init__(resource, channel, load_ohm, timeout_ms)
        if "T3AFG350" not in self.identity.upper():
            identity = self.identity
            self.device.close()
            self.rm.close()
            raise HardwareUnavailable(f"Expected Teledyne LeCroy T3AFG350, found {identity}.")
        try:
            self.output(False)
        except Exception:
            self.device.close()
            self.rm.close()
            raise

    def configure_pulse(
        self, width_s: float, high_v: float, low_v: float, count: int, repetition_hz: float
    ) -> None:
        frequency = float(repetition_hz)
        c = f"C{self.channel}"
        self.device.write(f"{c}:OUTP LOAD,{self.load_ohm}")
        self.device.write(
            f"{c}:BSWV WVTP,PULSE,FRQ,{frequency},HLEV,{high_v},LLEV,{low_v},WIDTH,{width_s}"
        )
        self.device.write(f"{c}:BTWV STATE,ON")
        self.device.write(f"{c}:BTWV GATE_NCYC,NCYC")
        self.device.write(f"{c}:BTWV TRSR,MAN")
        self.device.write(f"{c}:BTWV TIME,{int(count)}")

    def output(self, enabled: bool) -> None:
        self.device.write(f"C{self.channel}:OUTP {'ON' if enabled else 'OFF'}")

    def configure_dc(self, level_v: float) -> None:
        c = f"C{self.channel}"
        self.device.write(f"{c}:OUTP LOAD,{self.load_ohm}")
        self.device.write(f"{c}:BSWV WVTP,DC,OFST,{float(level_v)}")

    def trigger(self) -> None:
        self.device.write(f"C{self.channel}:BTWV MTRIG")

    def status(self) -> dict[str, str]:
        output_command = f"C{self.channel}:OUTP?"
        return {output_command: str(self.device.query(output_command)).strip()}


class RigolDG1062Z(_VisaAWG):
    """SCPI adapter for the Rigol DG1062Z N-cycle pulse burst."""

    def __init__(
        self, resource: str, channel: int = 1, load_ohm: int = 50, timeout_ms: int = 5000
    ) -> None:
        super().__init__(resource, channel, load_ohm, timeout_ms)
        self._pulse_configured = False
        if "DG1062Z" not in self.identity.upper():
            identity = self.identity
            self.device.close()
            self.rm.close()
            raise HardwareUnavailable(f"Expected Rigol DG1062Z, found {identity}.")
        try:
            self.output(False)
        except Exception:
            self.device.close()
            self.rm.close()
            raise

    def configure_pulse(
        self, width_s: float, high_v: float, low_v: float, count: int, repetition_hz: float
    ) -> dict[str, Any]:
        source = f":SOUR{self.channel}"
        self._pulse_configured = False
        self.output(False)
        self.device.write(f":OUTP{self.channel}:LOAD {self.load_ohm}")
        self.device.write(f"{source}:FUNC PULS")
        self.device.write(f"{source}:FREQ {float(repetition_hz)}")
        self.device.write(f"{source}:FUNC:PULS:WIDT {float(width_s)}")
        self.device.write(f"{source}:FUNC:PULS:TRAN MIN")
        self.device.write(f"{source}:VOLT:LOW {float(low_v)}")
        self.device.write(f"{source}:VOLT:HIGH {float(high_v)}")
        self.device.write(f"{source}:BURS:MODE TRIG")
        self.device.write(f"{source}:BURS:NCYC {int(count)}")
        self.device.write(f"{source}:BURS:TRIG:SOUR MAN")
        self.device.write(f"{source}:BURS:IDLE BOTTOM")
        self.device.write(f"{source}:BURS ON")
        status = self.pulse_status()
        expected = {
            "load_ohm": float(self.load_ohm),
            "frequency_hz": float(repetition_hz),
            "width_s": float(width_s),
            "low_v": float(low_v),
            "high_v": float(high_v),
            "count": float(count),
        }
        numeric_ok = all(
            np.isclose(float(status[key]), value, rtol=1e-6, atol=1e-12)
            for key, value in expected.items()
        )
        state_ok = (
            status["output"] == "OFF"
            and status["function"].startswith("PULS")
            and status["burst"] == "ON"
            and status["burst_mode"].startswith("TRIG")
            and status["trigger_source"].startswith("MAN")
            and status["idle"].startswith("BOTT")
            and status["error"].startswith("0,")
        )
        if not numeric_ok or not state_ok:
            raise RuntimeError(f"DG1062Z pulse configuration readback failed: {status}")
        self._pulse_configured = True
        return status

    def pulse_status(self) -> dict[str, Any]:
        source = f":SOUR{self.channel}"
        return {
            "output": str(self.device.query(f":OUTP{self.channel}?")).strip().upper(),
            "load_ohm": float(self.device.query(f":OUTP{self.channel}:LOAD?")),
            "function": str(self.device.query(f"{source}:FUNC?")).strip().upper(),
            "frequency_hz": float(self.device.query(f"{source}:FREQ?")),
            "width_s": float(self.device.query(f"{source}:FUNC:PULS:WIDT?")),
            "low_v": float(self.device.query(f"{source}:VOLT:LOW?")),
            "high_v": float(self.device.query(f"{source}:VOLT:HIGH?")),
            "burst": str(self.device.query(f"{source}:BURS?")).strip().upper(),
            "burst_mode": str(self.device.query(f"{source}:BURS:MODE?")).strip().upper(),
            "count": float(self.device.query(f"{source}:BURS:NCYC?")),
            "trigger_source": str(
                self.device.query(f"{source}:BURS:TRIG:SOUR?")
            ).strip().upper(),
            "idle": str(self.device.query(f"{source}:BURS:IDLE?")).strip().upper(),
            "error": str(self.device.query(":SYST:ERR?")).strip(),
        }

    def output(self, enabled: bool) -> None:
        self.device.write(f":OUTP{self.channel} {'ON' if enabled else 'OFF'}")
        if enabled and getattr(self, "_pulse_configured", False):
            self.device.write(f":SOUR{self.channel}:BURS ON")

    def configure_dc(self, level_v: float) -> None:
        self._pulse_configured = False
        self.device.write(f":OUTP{self.channel}:LOAD {self.load_ohm}")
        self.device.write(f":SOUR{self.channel}:APPL:DC DEF,DEF,{float(level_v)}")

    def trigger(self) -> None:
        status = self.pulse_status()
        if (
            status["output"] != "ON"
            or not status["function"].startswith("PULS")
            or status["burst"] != "ON"
            or not status["trigger_source"].startswith("MAN")
            or not status["idle"].startswith("BOTT")
            or not status["error"].startswith("0,")
        ):
            raise RuntimeError(f"DG1062Z is not ready for a safe manual pulse: {status}")
        self.device.write(f":SOUR{self.channel}:BURS:TRIG")

    def status(self) -> dict[str, str]:
        output_command = f":OUTP{self.channel}?"
        return {
            output_command: str(self.device.query(output_command)).strip(),
            ":SYST:ERR?": str(self.device.query(":SYST:ERR?")).strip(),
        }


AWG_MODELS = {"T3AFG350": T3AFG350, "DG1062Z": RigolDG1062Z}
STRADUS_USB_RESOURCE = "USBHID::201A::1001"


def open_awg(config: dict[str, Any]) -> _VisaAWG:
    model = str(config.get("model", "T3AFG350"))
    try:
        adapter = AWG_MODELS[model]
    except KeyError as exc:
        raise ValueError(f"Unsupported AWG model: {model}.") from exc
    return adapter(
        resource=config["visa_resource"],
        **{key: config[key] for key in ("channel", "load_ohm", "timeout_ms")},
    )


def classify_visa_device(identity: str, resource: str) -> str:
    text = f"{identity} {resource}".upper()
    if "DG1062Z" in text or "T3AFG350" in text:
        return "awg"
    if "MSO7054" in text:
        return "scope"
    if "STRADUS" in text or ("639NM" in text and "160MW" in text):
        return "laser"
    return "unknown"


def discover_visa_devices(laser_config: dict[str, Any], timeout_ms: int = 1000) -> list[dict[str, str]]:
    """Enumerate VISA resources without enabling any instrument output."""
    try:
        import pyvisa
        from pyvisa import constants
    except ImportError:
        return [{"status": "pyvisa is not installed"}]
    try:
        rm = pyvisa.ResourceManager()
    except Exception as exc:
        return [{"status": f"VISA runtime unavailable: {exc}"}]

    rows: list[dict[str, str]] = []
    try:
        resources = rm.list_resources()
        for resource in resources:
            row = {"resource": str(resource), "identity": "", "role": "unknown", "status": "ok"}
            device = None
            try:
                device = rm.open_resource(resource)
                device.timeout = int(timeout_ms)
                commands = ("*IDN?",)
                if str(resource).upper().startswith("ASRL"):
                    device.baud_rate = int(laser_config.get("baud_rate", 19200))
                    device.data_bits = 8
                    device.parity = constants.Parity.none
                    device.stop_bits = constants.StopBits.one
                    device.flow_control = constants.ControlFlow.none
                    device.write_termination = "\r"
                    device.read_termination = "\r\n"
                    commands = ("?LI", "*IDN?")
                errors = []
                for command in commands:
                    try:
                        response = str(device.query(command)).strip()
                        if command == "?LI":
                            try:
                                response += "\n" + str(device.read()).strip()
                            except Exception:
                                pass
                            if "LI=" in response:
                                response = response.split("LI=", 1)[1].splitlines()[0].strip()
                            else:
                                continue
                        row["identity"] = response
                        break
                    except Exception as exc:
                        errors.append(str(exc))
                if not row["identity"]:
                    row["status"] = "no identity response: " + "; ".join(errors)
                row["role"] = classify_visa_device(row["identity"], row["resource"])
            except Exception as exc:
                row["status"] = f"open failed: {exc}"
            finally:
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        pass
            rows.append(row)
    except Exception as exc:
        rows.append({"status": f"VISA discovery failed: {exc}"})
    finally:
        rm.close()
    return rows or [{"status": "no VISA resources found"}]


def discover_kinesis_devices(kinesis_dir: str | Path) -> list[dict[str, str]]:
    manager_dll = Path(kinesis_dir) / "Thorlabs.MotionControl.DeviceManagerCLI.dll"
    if not manager_dll.exists():
        return [{"status": f"Kinesis DeviceManagerCLI DLL not found in {manager_dll.parent}"}]
    try:
        import clr

        clr.AddReference(str(manager_dll))
        from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI

        DeviceManagerCLI.BuildDeviceList()
        serials = [str(value) for value in DeviceManagerCLI.GetDeviceList()]
    except Exception as exc:
        return [{"status": f"Kinesis discovery failed: {exc}"}]
    return [{"serial": serial, "status": "ok"} for serial in serials] or [
        {"status": "no Thorlabs Kinesis devices found"}
    ]


def discover_pixelink_devices() -> list[dict[str, str]]:
    if not util.find_library("PxLAPI40.dll"):
        return [{"status": "Pixelink PxLAPI40.dll not found"}]
    try:
        from pixelinkWrapper import PxLApi

        result = PxLApi.getNumberCameras()
        if not PxLApi.apiSuccess(result[0]):
            return [{"status": f"Pixelink enumeration failed; code {result[0]}"}]
        serials = [str(item.CameraSerialNum) for item in result[1]]
    except Exception as exc:
        return [{"status": f"Pixelink discovery failed: {exc}"}]
    return [{"serial": serial, "status": "ok"} for serial in serials] or [
        {"status": "no Pixelink cameras found"}
    ]


def _load_vortran_usb() -> Any:
    try:
        import libusb
        import vortran_lbl
    except ImportError as exc:
        raise HardwareUnavailable(
            "vortran-lbl is missing. Run install_lab.ps1 -Hardware."
        ) from exc
    if sys.platform.startswith("win") and not os.getenv("VORTRAN_LIBUSB_PATH"):
        architecture = {
            "AMD64": "x86_64",
            "X86_64": "x86_64",
            "ARM64": "arm64",
        }.get(platform.machine().upper(), "x86")
        dll = (
            Path(libusb.__file__).parent
            / "_platform"
            / "windows"
            / architecture
            / "libusb-1.0.dll"
        )
        if not dll.exists():
            raise HardwareUnavailable(f"libusb DLL not found: {dll}")
        os.environ["VORTRAN_LIBUSB_PATH"] = str(dll)
    return vortran_lbl


def discover_stradus_usb_devices() -> list[dict[str, str]]:
    """Enumerate Stradus USB HID heads without opening them or sending commands."""
    try:
        devices = _load_vortran_usb().get_usb_ports().values()
        lasers = [
            device
            for device in devices
            if not device.is_manager
            and device.vendor_id == 0x201A
            and device.product_id == 0x1001
        ]
    except Exception as exc:
        return [{"status": f"Stradus USB discovery failed: {exc}"}]
    if not lasers:
        return [{"status": "no Stradus USB HID laser found"}]
    # ponytail: one installed Stradus; add serial selection if multiple heads are used.
    return [
        {
            "resource": STRADUS_USB_RESOURCE,
            "identity": "Stradus Laser (USB HID 201A:1001)",
            "role": "laser",
            "status": "ok" if len(lasers) == 1 else f"{len(lasers)} lasers found; first will be used",
        }
    ]


def discover_windows_devices() -> list[dict[str, str]]:
    if not sys.platform.startswith("win"):
        return [{"status": "Windows PnP discovery is unavailable"}]
    command = r"""
Get-PnpDevice -PresentOnly |
  Where-Object {
    $_.Class -eq 'Ports' -or
    $_.FriendlyName -match 'Rigol|DG1000Z|MSO7054|Thorlabs|Kinesis|APT USB|Pixelink|USB3 Vision|USBTMC'
  } |
  Select-Object Status,Class,FriendlyName,InstanceId |
  ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return [{"status": f"Windows PnP discovery failed: {exc}"}]
    if result.returncode != 0:
        return [{"status": result.stderr.strip() or "Windows PnP discovery failed"}]
    if not result.stdout.strip():
        return [{"status": "no matching Windows PnP devices found"}]
    data = json.loads(result.stdout)
    return data if isinstance(data, list) else [data]


def scan_connected_hardware(config: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Read-only device enumeration used by the Diagnostics UI."""
    return {
        "visa": discover_visa_devices(config["laser"]),
        "stradus_usb": discover_stradus_usb_devices(),
        "kinesis": discover_kinesis_devices(config["stage"]["kinesis_dir"]),
        "pixelink": discover_pixelink_devices(),
        "windows_pnp": discover_windows_devices(),
    }


def _stradus_response_value(response: str, key: str) -> str | None:
    marker = f"{key}="
    for line in str(response).splitlines():
        line = line.strip().removeprefix("?")
        if line.startswith(marker):
            return line.split("=", 1)[1].strip()
    return None


class _StradusUSBDevice:
    def __init__(self, timeout_ms: int) -> None:
        lasers = _load_vortran_usb().get_lasers()
        if len(lasers) != 1:
            raise HardwareUnavailable(f"Expected one Stradus USB laser, found {len(lasers)}.")
        self.laser = lasers[0]
        self.timeout = int(timeout_ms)
        if sys.platform.startswith("win"):
            import usb.core
            import usb.util
            from vortran_lbl.usb import get_usb_backend

            self.laser.connection = usb.core.find(
                backend=get_usb_backend(),
                idVendor=self.laser.vendor_id,
                idProduct=self.laser.product_id,
                bus=self.laser.bus,
                address=self.laser.address,
            )
            if self.laser.connection is None:
                raise HardwareUnavailable("Could not open the Stradus USB HID connection.")
            self.laser.connection.set_configuration()
            usb.util.claim_interface(self.laser.connection, 0)
        elif not self.laser.open_connection():
            raise HardwareUnavailable("Could not open the Stradus USB HID connection.")

    def _response_ready(self) -> bool:
        self.laser.connection.ctrl_transfer(
            0x21, 0x09, 0x200, 0x00, self.laser.data_in_array_2
        )
        status = self.laser.read_usb(self.laser.read_timeout, include_first_byte=True)
        return bool(status and "\x01\xff" in status)

    def _receive_response(self) -> str:
        self.laser.connection.ctrl_transfer(
            0x21, 0x09, 0x200, 0x00, self.laser.data_in_array_3
        )
        response = ""
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            candidate = self.laser.read_usb(self.laser.read_timeout) or ""
            if "\n" in candidate or "Stradus:" in candidate:
                response = candidate
                break
        if not response:
            raise RuntimeError("Stradus returned no textual USB response.")
        self.laser.connection.ctrl_transfer(
            0x21, 0x09, 0x200, 0x00, self.laser.data_in_array_4
        )
        return response

    def _drain_responses(self) -> None:
        for _ in range(128):
            if not self._response_ready():
                return
            self._receive_response()
        raise RuntimeError("The Stradus USB response queue could not be cleared.")

    def query(self, command: str) -> str:
        self._drain_responses()
        encoded = (command.rstrip("\r\n") + "\r\n").encode("ascii")
        if len(encoded) > 63:
            raise ValueError("Stradus commands cannot exceed 63 bytes.")
        packet = array.array(
            "B", self.laser.prefix_1 + encoded + bytes([0xFF]) * (63 - len(encoded))
        )
        self.laser.connection.ctrl_transfer(0x21, 0x09, 0x200, 0x00, packet)

        deadline = time.monotonic() + self.timeout / 1000.0
        while time.monotonic() < deadline:
            if self._response_ready():
                response = self._receive_response()
                key = command.lstrip("?").split("=", 1)[0]
                value = _stradus_response_value(response, key)
                if value is not None:
                    return response
                raise RuntimeError(
                    f"Unexpected Stradus response to {command!r}: {response!r}"
                )
            time.sleep(0.005)
        raise RuntimeError(f"Stradus timed out waiting for {command!r}.")

    def read(self) -> str:
        return ""

    def close(self) -> None:
        connection = self.laser.connection
        if connection is None:
            return
        import usb.util

        try:
            usb.util.release_interface(connection, 0)
        finally:
            usb.util.dispose_resources(connection)
            self.laser.connection = None


class Stradus639160:
    """Minimal USB HID/RS-232 adapter for the Vortran Stradus 639-160.

    The AWG must already be OFF and at TTL low before ``prepare`` is called.
    Changing from CW to digital modulation requires an explicit beam-blocked step.
    """

    def __init__(
        self,
        visa_resource: str,
        baud_rate: int = 19200,
        timeout_ms: int = 5000,
        emission_settle_s: float = 5.5,
    ) -> None:
        self.rm = None
        if visa_resource.upper().startswith("USBHID::"):
            self.device = _StradusUSBDevice(timeout_ms)
        else:
            try:
                import pyvisa
                from pyvisa import constants
            except ImportError as exc:
                raise HardwareUnavailable("pyvisa is missing. Run install_lab.ps1.") from exc
            self.rm = pyvisa.ResourceManager()
            self.device = self.rm.open_resource(visa_resource)
            self.device.timeout = int(timeout_ms)
            self.device.baud_rate = int(baud_rate)
            self.device.data_bits = 8
            self.device.parity = constants.Parity.none
            self.device.stop_bits = constants.StopBits.one
            self.device.flow_control = constants.ControlFlow.none
            self.device.write_termination = "\r"
            self.device.read_termination = "\r\n"
        self.emission_settle_s = max(0.0, float(emission_settle_s))
        self.identity = self._value("?LI", "LI")
        identity_lower = self.identity.lower().replace(" ", "")
        if "639nm" not in identity_lower or "160mw" not in identity_lower:
            self.close()
            raise HardwareUnavailable(
                f"The resource is not identified as a Stradus 639-160: {self.identity}"
            )

    def _value(self, command: str, key: str) -> str:
        # Stradus normally returns an echoed command followed by the answer.
        # Consume both lines so the echo/answer cannot leak into the next query.
        responses = [str(self.device.query(command)).strip()]
        old_timeout = getattr(self.device, "timeout", None)
        try:
            if old_timeout is not None:
                self.device.timeout = min(int(old_timeout), 250)
            responses.append(str(self.device.read()).strip())
        except Exception:
            pass
        finally:
            if old_timeout is not None:
                self.device.timeout = old_timeout
        candidates = [_stradus_response_value(response, key) for response in responses]
        candidates = [value for value in candidates if value is not None]
        if not candidates:
            raise RuntimeError(f"Unexpected Stradus response to {command!r}: {responses!r}")
        return candidates[-1]

    def _set(self, command: str, key: str) -> str:
        return self._value(command, key)

    def prepare(
        self, peak_power_mw: float, allow_beam_blocked_mode_change: bool = False
    ) -> dict[str, Any]:
        peak = float(peak_power_mw)
        if not 0 < peak <= 160.0:
            raise ValueError("Stradus peak power must be between 0 and 160 mW.")
        try:
            fault = int(self._value("?FC", "FC"))
            if fault not in (0, 1):
                description = self._value("?FD", "FD")
                raise RuntimeError(f"Stradus fault FC={fault}: {description}")
            interlock = int(self._value("?IL", "IL"))
            if interlock != 1:
                raise RuntimeError("Stradus interlock is open; laser emission will not be enabled.")
            control_mode = int(self._value("?C", "C"))
            if control_mode != 0:
                raise RuntimeError("The Stradus must be in power-control mode (C=0).")
            self._set("EPC=0", "EPC")
            if int(self._value("?EPC", "EPC")) != 0:
                raise RuntimeError("The Stradus did not disable external power control.")

            pulse_mode = int(self._value("?PUL", "PUL"))
            if pulse_mode != 1 and not allow_beam_blocked_mode_change:
                raise RuntimeError(
                    "The Stradus is in CW mode (PUL=0). With the beam blocked, use "
                    "Prepare TTL pulse mode in the Laser card before running a pulse."
                )
            emission_was_off = int(self._value("?LE", "LE")) != 1
            if emission_was_off:
                self._set("LE=1", "LE")
                if int(self._value("?LE", "LE")) != 1:
                    raise RuntimeError("The Stradus did not confirm LE=1.")
                time.sleep(self.emission_settle_s)

            if pulse_mode != 1:
                self._set("PUL=1", "PUL")
                if int(self._value("?PUL", "PUL")) != 1:
                    raise RuntimeError("The Stradus did not confirm Digital Modulation (PUL=1).")

            self._set(f"PP={peak:.6g}", "PP")
            verified_peak = float(self._value("?PP", "PP"))
            tolerance = max(0.2, peak * 0.02)
            if abs(verified_peak - peak) > tolerance:
                raise RuntimeError(
                    f"Stradus peak power was not verified: requested {peak:g} mW, "
                    f"reported {verified_peak:g} mW."
                )
            if int(self._value("?IL", "IL")) != 1:
                raise RuntimeError("The Stradus interlock opened during preparation.")
            return {
                "fault_code": fault,
                "interlock": interlock,
                "control_mode": control_mode,
                "pul": 1,
                "emission_enabled": 1,
                "peak_power_mw": verified_peak,
            }
        except Exception as exc:
            try:
                self.safe_off()
            except Exception as off_exc:
                exc.add_note(f"Emergency laser-off verification also failed: {off_exc}")
            raise

    def enable_internal_cw(self, power_mw: float, park_power_mw: float) -> dict[str, Any]:
        power, park = float(power_mw), float(park_power_mw)
        if not 0 < power <= 160.0 or not 0 < park <= 160.0:
            raise ValueError("Stradus CW and park power must be between 0 and 160 mW.")
        fault = int(self._value("?FC", "FC"))
        if fault not in (0, 1):
            description = self._value("?FD", "FD")
            raise RuntimeError(f"Stradus fault FC={fault}: {description}")
        stored = float(self._value("?LPS", "LPS"))
        if abs(stored - park) > max(0.6, park * 0.02):
            raise RuntimeError(
                f"Stored LPS={stored:g} mW is not the {park:g} mW park power. "
                "Initialize park power with the beam blocked first."
            )
        try:
            if int(self._value("?IL", "IL")) != 1:
                raise RuntimeError("Stradus interlock is open; CW emission will not be enabled.")
            if int(self._value("?C", "C")) != 0:
                raise RuntimeError("The Stradus must be in power-control mode (C=0).")
            self.safe_off()
            self._set("EPC=0", "EPC")
            if int(self._value("?EPC", "EPC")) != 0:
                raise RuntimeError("The Stradus did not disable external power control.")
            self._set("LE=1", "LE")
            if int(self._value("?LE", "LE")) != 1:
                raise RuntimeError("The Stradus did not confirm LE=1.")
            time.sleep(self.emission_settle_s)
            self._set("PUL=0", "PUL")
            if int(self._value("?PUL", "PUL")) != 0:
                raise RuntimeError("The Stradus did not confirm internal CW mode (PUL=0).")
            self._set(f"LP={power:05.1f}", "LP")
            verified = float(self._value("?LPS", "LPS"))
            if abs(verified - power) > max(0.6, power * 0.02):
                raise RuntimeError(
                    f"Stradus CW setting was not verified: requested {power:g} mW, "
                    f"reported LPS={verified:g} mW."
                )
            return {
                "interlock": 1,
                "control_mode": 0,
                "pul": 0,
                "emission_enabled": 1,
                "laser_power_setting_mw": verified,
                "measured_power_mw": float(self._value("?LP", "LP")),
            }
        except Exception as exc:
            try:
                self.safe_off()
            except Exception as off_exc:
                exc.add_note(f"Emergency laser-off verification also failed: {off_exc}")
            raise

    def disable_internal_cw(self, park_power_mw: float) -> None:
        park = float(park_power_mw)
        if not 0 < park <= 160.0:
            raise ValueError("Stradus park power must be between 0 and 160 mW.")
        if int(self._value("?LE", "LE")) == 1:
            self._set(f"LP={park:05.1f}", "LP")
            stored = float(self._value("?LPS", "LPS"))
            if abs(stored - park) > max(0.6, park * 0.02):
                raise RuntimeError(f"Could not store the {park:g} mW park power.")
        self.safe_off()

    def initialize_cw_park(self, park_power_mw: float) -> float:
        park = float(park_power_mw)
        if not 0 < park <= 160.0:
            raise ValueError("Stradus park power must be between 0 and 160 mW.")
        try:
            fault = int(self._value("?FC", "FC"))
            if fault not in (0, 1):
                description = self._value("?FD", "FD")
                raise RuntimeError(f"Stradus fault FC={fault}: {description}")
            if int(self._value("?IL", "IL")) != 1:
                raise RuntimeError("Stradus interlock is open.")
            if int(self._value("?C", "C")) != 0:
                raise RuntimeError("The Stradus must be in power-control mode (C=0).")
            self.safe_off()
            self._set("EPC=0", "EPC")
            if int(self._value("?EPC", "EPC")) != 0:
                raise RuntimeError("The Stradus did not disable external power control.")
            self._set("LE=1", "LE")
            if int(self._value("?LE", "LE")) != 1:
                raise RuntimeError("The Stradus did not confirm LE=1.")
            time.sleep(self.emission_settle_s)
            self._set("PUL=0", "PUL")
            if int(self._value("?PUL", "PUL")) != 0:
                raise RuntimeError("The Stradus did not confirm internal CW mode (PUL=0).")
            self._set(f"LP={park:05.1f}", "LP")
            stored = float(self._value("?LPS", "LPS"))
            if abs(stored - park) > max(0.6, park * 0.02):
                raise RuntimeError(f"Could not store the {park:g} mW park power.")
            self.safe_off()
            return stored
        except Exception as exc:
            try:
                self.safe_off()
            except Exception as off_exc:
                exc.add_note(f"Emergency laser-off verification also failed: {off_exc}")
            raise

    def safe_off(self) -> None:
        self._set("LE=0", "LE")
        if int(self._value("?LE", "LE")) != 0:
            raise RuntimeError("The Stradus did not confirm LE=0; use the hardware interlock.")

    def status(self) -> dict[str, Any]:
        fault = int(self._value("?FC", "FC"))
        return {
            "fault_code": fault,
            "fault_description": self._value("?FD", "FD") if fault else "No Fault",
            "interlock": int(self._value("?IL", "IL")),
            "control_mode": int(self._value("?C", "C")),
            "external_power_control": int(self._value("?EPC", "EPC")),
            "pul": int(self._value("?PUL", "PUL")),
            "emission_enabled": int(self._value("?LE", "LE")),
            "laser_power_setting_mw": float(self._value("?LPS", "LPS")),
            "measured_power_mw": float(self._value("?LP", "LP")),
            "peak_power_mw": float(self._value("?PP", "PP")),
        }

    def close(self) -> None:
        try:
            self.safe_off()
        finally:
            self.disconnect()

    def disconnect(self) -> None:
        """Release the transport without changing laser state.

        Intended for explicitly read-only status sessions. Normal control paths
        must call close(), which verifies LE=0 first.
        """
        self.device.close()
        if self.rm is not None:
            self.rm.close()


class RigolMSO7054:
    """VISA acquisition adapter for the Rigol MSO7054."""

    def __init__(self, resource: str, channel: int = 1, timeout_ms: int = 10000) -> None:
        try:
            import pyvisa
        except ImportError as exc:
            raise HardwareUnavailable("pyvisa is missing. Run install_lab.ps1.") from exc
        self.channel = int(channel)
        self.rm = pyvisa.ResourceManager()
        self.device = None
        try:
            self.device = self.rm.open_resource(resource)
            self.device.timeout = timeout_ms
            self.identity = str(self.device.query("*IDN?")).strip()
        except Exception:
            if self.device is not None:
                self.device.close()
            self.rm.close()
            raise
        if "MSO7054" not in self.identity.upper():
            identity = self.identity
            self.device.close()
            self.rm.close()
            raise HardwareUnavailable(f"Expected Rigol MSO7054, found {identity}.")
        self.settings: dict[str, Any] = {}

    def configure_detector_input(self, config: dict[str, Any]) -> None:
        """Apply the non-emitting CH input settings required by the DET02AFC."""
        if str(config.get("input_impedance", "")).upper() != "FIFTY" or str(
            config.get("coupling", "")
        ).upper() != "DC":
            raise ValueError("The DET02AFC requires scope coupling DC and input impedance FIFTy.")
        self.device.write(f":CHAN{self.channel}:COUP DC")
        self.device.write(f":CHAN{self.channel}:IMP FIFTy")
        self.device.query("*OPC?")

    def configure_for_pulse(self, pulse_width_s: float, config: dict[str, Any]) -> dict[str, Any]:
        settings = scope_settings_for_pulse(pulse_width_s, config, self.channel)
        channel = settings["channel"]
        commands = (
            f":CHAN{channel}:DISP ON",
            f":CHAN{channel}:COUP {settings['coupling']}",
            f":CHAN{channel}:BWL {settings['bandwidth_limit']}",
            f":CHAN{channel}:IMP {settings['input_impedance']}",
            f":CHAN{channel}:SCAL {settings['vertical_scale_v_div']:.12g}",
            f":CHAN{channel}:OFFS {settings['vertical_offset_v']:.12g}",
            ":TIM:MODE MAIN",
            f":TIM:MAIN:SCAL {settings['time_scale_s_div']:.12g}",
            f":TIM:MAIN:OFFS {settings['time_offset_s']:.12g}",
            ":ACQ:TYPE NORM",
            ":TRIG:MODE EDGE",
            f":TRIG:EDGE:SOUR {settings['trigger_source']}",
            f":TRIG:EDGE:SLOP {settings['trigger_slope']}",
            f":TRIG:EDGE:LEV {settings['trigger_level_v']:.12g}",
            ":TRIG:COUP DC",
        )
        for command in commands:
            self.device.write(command)
        self.device.query("*OPC?")
        self.settings = settings
        return settings

    def arm_single(self) -> None:
        self.device.write(":SING")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if str(self.device.query(":TRIG:STAT?")).strip().upper() in {"WAIT", "RUN"}:
                return
            time.sleep(0.01)
        raise TimeoutError("The MSO7054 did not remain armed while waiting for the trigger.")

    def wait_complete(self, timeout_s: float) -> None:
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            if str(self.device.query(":TRIG:STAT?")).strip().upper() == "STOP":
                return
            time.sleep(0.02)
        raise TimeoutError("The MSO7054 did not capture the pulse within the configured timeout.")

    def acquire(self) -> tuple[np.ndarray, np.ndarray]:
        self.device.write(f":WAV:SOUR CHAN{self.channel}")
        self.device.write(":WAV:MODE NORM")
        self.device.write(":WAV:FORM ASC")
        y = np.asarray(self.device.query_ascii_values(":WAV:DATA?"), dtype=float)
        xinc = float(self.device.query(":WAV:XINC?"))
        xorigin = float(self.device.query(":WAV:XOR?"))
        x = xorigin + np.arange(y.size) * xinc
        return x, y

    def status(self) -> dict[str, str]:
        channel = self.channel
        commands = (
            f":CHAN{channel}:IMP?",
            f":CHAN{channel}:SCAL?",
            ":TRIG:EDGE:SOUR?",
            ":TRIG:EDGE:SLOP?",
            ":TRIG:EDGE:LEV?",
            ":SYST:ERR?",
        )
        return {command: str(self.device.query(command)).strip() for command in commands}

    def close(self) -> None:
        self.device.close()
        self.rm.close()


def _is_supported_pixelink(model: str, name: str) -> bool:
    return any(token in f"{model} {name}".upper() for token in ("M18", "D7718", "D3011", "D6218"))


class PixelinkCamera:
    """Native Pixelink API 4.0 adapter for the connected CYL color camera."""

    def __init__(self, config: dict[str, Any]) -> None:
        if not util.find_library("PxLAPI40.dll"):
            raise HardwareUnavailable(
                "Pixelink Software Suite/PxLAPI40.dll is missing; then run install_lab.ps1 -Hardware."
            )
        try:
            from pixelinkWrapper import PxLApi
        except (ImportError, OSError) as exc:
            raise HardwareUnavailable(
                "Pixelink Software Suite/PxLAPI40.dll is missing; then run install_lab.ps1 -Hardware."
            ) from exc
        self.api = PxLApi
        self.handle = 0
        self._streaming = False
        self.capture_retries = max(1, int(config["capture_retries"]))
        self.auto_exposure = bool(config["auto_exposure"])
        self.target_peak_fraction = float(config["target_peak_fraction"])
        self.max_auto_exposure_steps = max(1, int(config["max_auto_exposure_steps"]))

        cameras = self._result(self.api.getNumberCameras(), "enumerate cameras")
        serials = [int(item.CameraSerialNum) for item in cameras]
        requested = int(config["serial_number"])
        if requested == 0:
            if len(serials) != 1:
                raise HardwareUnavailable(
                    f"Set camera.serial_number; detected: {serials or 'none'}."
                )
            requested = serials[0]
        elif requested not in serials:
            raise HardwareUnavailable(
                f"Pixelink serial {requested} not found; detected: {serials or 'none'}."
            )

        self.handle = self._result(self.api.initialize(requested), "initialize camera")
        try:
            info = self._result(self.api.getCameraInfo(self.handle), "read identity")
            model = self._text(info.ModelName)
            name = self._text(info.CameraName)
            if not _is_supported_pixelink(model, name):
                raise HardwareUnavailable(
                    f"Unsupported Pixelink camera serial {requested}: {model} {name}"
                )
            self.identity = f"Pixelink {model or name} SN {requested}"
            self._set(self.api.FeatureId.PIXEL_FORMAT, [self.api.PixelFormat.BAYER8])
            roi = [int(value) for value in config["roi"]]
            if roi[2] > 0 and roi[3] > 0:
                self._set(self.api.FeatureId.ROI, roi)
            exposure_limits = self._limits(self.api.FeatureId.EXPOSURE)
            configured_limits = (
                float(config["min_exposure_ms"]) / 1000.0,
                float(config["max_exposure_ms"]) / 1000.0,
            )
            self.exposure_limits = (
                max(exposure_limits[0], configured_limits[0]),
                min(exposure_limits[1], configured_limits[1]),
            )
            self.exposure_s = self._set_exposure(float(config["exposure_ms"]) / 1000.0)
            self.gain_limits = self._limits(self.api.FeatureId.GAIN)
            self.gain_db = self._set_gain(float(config["gain_db"]))
            self._prepare_buffer()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _text(value: Any) -> str:
        raw = bytes(value) if not isinstance(value, bytes) else value
        return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")

    def _result(self, result: tuple[Any, ...], action: str) -> Any:
        if not self.api.apiSuccess(result[0]):
            raise RuntimeError(f"Pixelink failed to {action}; code {result[0]}.")
        return result[1] if len(result) == 2 else result[1:]

    def _set(self, feature: int, params: list[float]) -> list[float]:
        result = self.api.setFeature(
            self.handle, feature, self.api.FeatureFlags.MANUAL, params
        )
        if not self.api.apiSuccess(result[0]):
            raise RuntimeError(f"Pixelink cannot set feature {feature}; code {result[0]}.")
        actual = self.api.getFeature(self.handle, feature)
        if not self.api.apiSuccess(actual[0]):
            raise RuntimeError(f"Pixelink cannot verify feature {feature}; code {actual[0]}.")
        return [float(value) for value in actual[2]]

    def _limits(self, feature: int) -> tuple[float, float]:
        data = self._result(self.api.getCameraFeatures(self.handle, feature), "read limits")
        param = data.Features[0].Params[0]
        return float(param.fMinValue), float(param.fMaxValue)

    def _set_exposure(self, exposure_s: float) -> float:
        low, high = self.exposure_limits
        return self._set(self.api.FeatureId.EXPOSURE, [min(high, max(low, exposure_s))])[0]

    def set_exposure(self, exposure_ms: float, auto_exposure: bool) -> float:
        requested = float(exposure_ms) / 1000.0
        low, high = self.exposure_limits
        if not low <= requested <= high:
            raise ValueError(
                f"Exposure must be between {1000.0 * low:g} and {1000.0 * high:g} ms."
            )
        self.auto_exposure = bool(auto_exposure)
        self.exposure_s = self._set_exposure(requested)
        return 1000.0 * self.exposure_s

    def _set_gain(self, gain_db: float) -> float:
        low, high = self.gain_limits
        return self._set(self.api.FeatureId.GAIN, [min(high, max(low, gain_db))])[0]

    def _prepare_buffer(self) -> None:
        roi = self.api.getFeature(self.handle, self.api.FeatureId.ROI)
        if not self.api.apiSuccess(roi[0]):
            raise RuntimeError(f"Pixelink cannot read ROI; code {roi[0]}.")
        self.roi = [int(value) for value in roi[2]]
        width = self.roi[self.api.RoiParams.WIDTH]
        height = self.roi[self.api.RoiParams.HEIGHT]
        pixel_format = self.api.getFeature(self.handle, self.api.FeatureId.PIXEL_FORMAT)
        if not self.api.apiSuccess(pixel_format[0]):
            raise RuntimeError(f"Pixelink cannot read pixel format; code {pixel_format[0]}.")
        bytes_per_pixel = float(self.api.getBytesPerPixel(int(pixel_format[2][0])))
        self.raw_frame = create_string_buffer(int(width * height * bytes_per_pixel))

    def _capture_once(self) -> np.ndarray:
        managed_stream = not self._streaming
        if managed_stream:
            self.start_stream()
        try:
            result = None
            for _ in range(self.capture_retries):
                result = self.api.getNextFrame(self.handle, self.raw_frame)
                if self.api.apiSuccess(result[0]):
                    break
            if result is None or not self.api.apiSuccess(result[0]):
                raise RuntimeError(f"Pixelink capture failed; code {result[0]}.")
        finally:
            if managed_stream:
                self.stop_stream()
        descriptor = result[1]
        formatted = self.api.formatImage(
            self.raw_frame, descriptor, self.api.ImageFormat.RAW_BGR24_NON_DIB
        )
        if not self.api.apiSuccess(formatted[0]):
            raise RuntimeError(f"Pixelink RGB conversion failed; code {formatted[0]}.")
        height, width = int(descriptor.Roi.fHeight), int(descriptor.Roi.fWidth)
        bgr = np.frombuffer(formatted[1], dtype=np.uint8, count=height * width * 3)
        return bgr.reshape(height, width, 3)[..., ::-1].copy()

    def capture(self) -> np.ndarray:
        image = self._capture_once()
        if not self.auto_exposure:
            return image
        target = 255.0 * self.target_peak_fraction
        for _ in range(self.max_auto_exposure_steps - 1):
            peak = float(image.max())
            if image.max() < 250 and 0.8 * target <= peak <= 1.05 * target:
                break
            factor = float(np.clip(target / max(peak, 1.0), 0.25, 4.0))
            updated = min(self.exposure_limits[1], max(self.exposure_limits[0], self.exposure_s * factor))
            if abs(updated - self.exposure_s) > 0.01 * self.exposure_s:
                self.exposure_s = self._set_exposure(updated)
            else:
                gain = float(np.clip(self.gain_db + 20.0 * np.log10(factor), *self.gain_limits))
                if abs(gain - self.gain_db) < 0.05:
                    break
                self.gain_db = self._set_gain(gain)
            image = self._capture_once()
        return image

    def capture_spot(self) -> np.ndarray:
        return self.capture()

    def start_stream(self) -> None:
        if self._streaming:
            return
        result = self.api.setStreamState(self.handle, self.api.StreamState.START)
        if not self.api.apiSuccess(result[0]):
            raise RuntimeError(f"Pixelink cannot start streaming; code {result[0]}.")
        self._streaming = True

    def stop_stream(self) -> None:
        if not self._streaming:
            return
        try:
            self.api.setStreamState(self.handle, self.api.StreamState.STOP)
        finally:
            self._streaming = False

    def settings(self) -> dict[str, Any]:
        return {
            "exposure_ms": 1000.0 * self.exposure_s,
            "gain_db": self.gain_db,
            "roi": self.roi,
            "auto_exposure": self.auto_exposure,
            "target_peak_fraction": self.target_peak_fraction,
        }

    def close(self) -> None:
        if self.handle:
            try:
                self.stop_stream()
            except Exception:
                pass
            self.api.uninitialize(self.handle)
            self.handle = 0


class KinesisBPC303Stage:
    """Native Kinesis adapter for the three piezo channels of a BPC303."""

    def __init__(
        self,
        config: dict[str, Any],
        enable_channels: bool = True,
        set_closed_loop: bool = False,
    ) -> None:
        if not config.get("calibrated", False):
            raise HardwareUnavailable(
                "The stage is not calibrated: confirm travel, origin, and axis directions, then set stage.calibrated."
            )
        kinesis_dir = Path(config["kinesis_dir"])
        dll = kinesis_dir / "Thorlabs.MotionControl.Benchtop.Piezo.dll"
        if not dll.exists():
            raise HardwareUnavailable(f"Missing Kinesis DLL: {dll}")
        try:
            self.api = ctypes.CDLL(str(dll))
        except OSError as exc:
            raise HardwareUnavailable(f"Cannot load the BPC303 Kinesis API: {exc}") from exc
        serial = str(config["serial_number"])
        self.serial = serial.encode("ascii")
        self.channels = {axis: int(number) for axis, number in config["axis_channels"].items()}
        self.ranges = config["range_um"]
        self.controller_span = config["controller_span_units"]
        self.axis_inverted = config["axis_inverted"]
        self.position_tolerance_um = float(config["position_tolerance_um"])
        self.move_timeout_s = float(config["move_timeout_s"])
        self.channel_status: dict[str, dict[str, float | int]] = {}
        self._open = False
        self._bind_native_api()
        if self.api.TLI_BuildDeviceList() != 0 or self.api.PBC_Open(self.serial) != 0:
            raise HardwareUnavailable(f"Cannot open BPC303 {serial}.")
        self._open = True
        try:
            for channel in self.channels.values():
                if not self.api.PBC_StartPolling(self.serial, channel, 250):
                    raise HardwareUnavailable(f"Cannot start BPC303 channel {channel} polling.")
            time.sleep(0.3)
            for axis, channel in self.channels.items():
                self._prepare_channel(
                    axis,
                    channel,
                    float(config["max_voltage_v"]),
                    enable_channels,
                    set_closed_loop,
                )
        except Exception:
            self.close()
            raise
        self.identity = f"Thorlabs BPC303 {serial} (native Kinesis)"

    def _bind_native_api(self) -> None:
        serial, channel = ctypes.c_char_p, ctypes.c_short
        signatures = {
            "TLI_BuildDeviceList": ([], ctypes.c_short),
            "PBC_Open": ([serial], ctypes.c_short),
            "PBC_Close": ([serial], None),
            "PBC_StartPolling": ([serial, channel, ctypes.c_int], ctypes.c_bool),
            "PBC_StopPolling": ([serial, channel], None),
            "PBC_EnableChannel": ([serial, channel], ctypes.c_short),
            "PBC_DisableChannel": ([serial, channel], ctypes.c_short),
            "PBC_RequestPositionControlMode": ([serial, channel], ctypes.c_bool),
            "PBC_GetPositionControlMode": ([serial, channel], ctypes.c_short),
            "PBC_SetPositionControlMode": ([serial, channel, ctypes.c_short], ctypes.c_short),
            "PBC_RequestOutputVoltage": ([serial, channel], ctypes.c_bool),
            "PBC_GetOutputVoltage": ([serial, channel], ctypes.c_short),
            "PBC_RequestMaxOutputVoltage": ([serial, channel], ctypes.c_bool),
            "PBC_GetMaxOutputVoltage": ([serial, channel], ctypes.c_short),
            "PBC_RequestMaximumTravel": ([serial, channel], ctypes.c_bool),
            "PBC_GetMaximumTravel": ([serial, channel], ctypes.c_ushort),
            "PBC_RequestActualPosition": ([serial, channel], ctypes.c_short),
            "PBC_GetPosition": ([serial, channel], ctypes.c_short),
            "PBC_SetPosition": ([serial, channel, ctypes.c_short], ctypes.c_short),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = result

    def _prepare_channel(
        self,
        axis: str,
        channel: int,
        max_voltage_v: float,
        enable_channel: bool = True,
        set_closed_loop: bool = False,
    ) -> None:
        self.api.PBC_RequestMaxOutputVoltage(self.serial, channel)
        self.api.PBC_RequestMaximumTravel(self.serial, channel)
        self.api.PBC_RequestPositionControlMode(self.serial, channel)
        self.api.PBC_RequestOutputVoltage(self.serial, channel)
        time.sleep(0.25)
        voltage = self.api.PBC_GetMaxOutputVoltage(self.serial, channel) / 10.0
        travel = self.api.PBC_GetMaximumTravel(self.serial, channel) / 10.0
        expected_travel = float(self.ranges[axis][1]) - float(self.ranges[axis][0])
        if abs(voltage - max_voltage_v) > 0.1 or abs(travel - expected_travel) > 0.1:
            raise HardwareUnavailable(
                f"BPC303 {axis} reports {voltage:g} V and {travel:g} um; expected "
                f"{max_voltage_v:g} V and {expected_travel:g} um."
            )
        mode = self.api.PBC_GetPositionControlMode(self.serial, channel)
        if mode not in (2, 4) and set_closed_loop:
            result = self.api.PBC_SetPositionControlMode(self.serial, channel, 2)
            if result != 0:
                raise HardwareUnavailable(
                    f"BPC303 rejected closed-loop mode for {axis}; code {result}."
                )
            self.api.PBC_RequestPositionControlMode(self.serial, channel)
            time.sleep(0.25)
            mode = self.api.PBC_GetPositionControlMode(self.serial, channel)
        if mode not in (2, 4):
            raise HardwareUnavailable(
                f"BPC303 {axis} is not in closed-loop mode. Use Enable closed loop in the Stage card."
            )
        if enable_channel and self.api.PBC_EnableChannel(self.serial, channel) != 0:
            raise HardwareUnavailable(f"Cannot enable BPC303 channel {axis}.")

        time.sleep(0.4)
        self.api.PBC_RequestPositionControlMode(self.serial, channel)
        time.sleep(0.3)
        confirmed_mode = self.api.PBC_GetPositionControlMode(self.serial, channel)
        if confirmed_mode not in (2, 4):
            raise HardwareUnavailable(f"BPC303 {axis} did not enter closed-loop mode.")
        self.channel_status[axis] = {
            "channel": channel,
            "max_voltage_v": voltage,
            "maximum_travel_um": travel,
            "closed_loop_mode": confirmed_mode,
        }

    def status(self) -> dict[str, Any]:
        position = self.get_position()
        for axis, value in (("x", position.x_um), ("y", position.y_um), ("z", position.z_um)):
            low, high = self.ranges[axis]
            if not low - self.position_tolerance_um <= value <= high + self.position_tolerance_um:
                raise HardwareUnavailable(
                    f"BPC303 {axis} position is {value:.4f} um, outside the configured "
                    f"[{low:g}, {high:g}] um range and {self.position_tolerance_um:g} um tolerance. "
                    "With the objective clear, zero that channel "
                    "from the BPC303 front panel or Kinesis, then rerun Diagnostics."
                )
        return {
            "channels": self.channel_status,
            "position_um": {"x": position.x_um, "y": position.y_um, "z": position.z_um},
        }

    def _to_controller(self, axis: str, value_um: float) -> float:
        low, high = self.ranges[axis]
        fraction = (value_um - low) / (high - low)
        if self.axis_inverted[axis]:
            fraction = 1.0 - fraction
        unit_low, unit_high = self.controller_span
        return unit_low + fraction * (unit_high - unit_low)

    def _from_controller(self, axis: str, value: float) -> float:
        unit_low, unit_high = self.controller_span
        fraction = (value - unit_low) / (unit_high - unit_low)
        if self.axis_inverted[axis]:
            fraction = 1.0 - fraction
        low, high = self.ranges[axis]
        return low + fraction * (high - low)

    def move_to(self, target: Point) -> Point:
        bounded: dict[str, float] = {}
        for axis, value in (("x", target.x_um), ("y", target.y_um), ("z", target.z_um)):
            low, high = self.ranges[axis]
            if not low - self.position_tolerance_um <= value <= high + self.position_tolerance_um:
                raise ValueError(f"Motion {axis}={value} µm is outside [{low}, {high}].")
            bounded[axis] = min(high, max(low, float(value)))
        target = Point(bounded["x"], bounded["y"], bounded["z"])
        for axis, value in (("x", target.x_um), ("y", target.y_um), ("z", target.z_um)):
            controller = self._to_controller(axis, float(value))
            raw = int(round(controller * 32767.0 / 100.0))
            result = self.api.PBC_SetPosition(self.serial, self.channels[axis], raw)
            if result != 0:
                raise HardwareUnavailable(f"BPC303 rejected {axis}={value:g} um; code {result}.")
        deadline = time.monotonic() + self.move_timeout_s
        while True:
            actual = self.get_position()
            errors = {
                axis: abs(requested - measured)
                for axis, requested, measured in (
                    ("x", target.x_um, actual.x_um),
                    ("y", target.y_um, actual.y_um),
                    ("z", target.z_um, actual.z_um),
                )
            }
            if max(errors.values()) <= self.position_tolerance_um:
                return actual
            if time.monotonic() >= deadline:
                detail = ", ".join(f"{axis}={error:.4f} um" for axis, error in errors.items())
                raise HardwareUnavailable(
                    f"BPC303 did not reach {target} within {self.move_timeout_s:g} s; "
                    f"position errors: {detail}; tolerance={self.position_tolerance_um:g} um."
                )

    def get_position(self) -> Point:
        for channel in self.channels.values():
            self.api.PBC_RequestActualPosition(self.serial, channel)
        time.sleep(0.05)
        values = {}
        for axis, channel in self.channels.items():
            raw = self.api.PBC_GetPosition(self.serial, channel)
            values[axis] = self._from_controller(axis, raw * 100.0 / 32767.0)
        return Point(values["x"], values["y"], values["z"])

    def close(self) -> None:
        if not self._open:
            return
        for channel in self.channels.values():
            self.api.PBC_StopPolling(self.serial, channel)
        self.api.PBC_Close(self.serial)
        self._open = False


def discover_hardware(config: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Run a non-emitting, non-moving hardware preflight."""
    checks: list[tuple[str, str, str]] = []

    def output_is_off(response: str) -> bool:
        tokens = response.upper().replace(",", " ").split()
        return response.strip().upper() == "0" or "OFF" in tokens

    def error_is_clear(response: str) -> bool:
        code = response.split(",", 1)[0].split()[-1]
        return code in {"0", "+0"}

    def close(device: Any) -> None:
        if device is not None:
            try:
                device.close()
            except Exception:
                pass

    try:
        import pyvisa

        rm = pyvisa.ResourceManager()
        try:
            resources = rm.list_resources()
        finally:
            rm.close()
        checks.append(("VISA", "READY" if resources else "MISSING", ", ".join(resources) or "no resources"))
    except Exception as exc:
        checks.append(("VISA", "MISSING", str(exc)))

    awg_resource = config["awg"].get("visa_resource", "")
    awg_model = str(config["awg"].get("model", "T3AFG350"))
    if not awg_resource:
        checks.append((awg_model, "BLOCKED", "missing awg.visa_resource"))
    else:
        awg = None
        try:
            awg = open_awg(config["awg"])
            status = awg.status()
            output_response = next(value for key, value in status.items() if "OUTP" in key)
            errors = [value for key, value in status.items() if "ERR?" in key]
            ready = output_is_off(output_response) and all(map(error_is_clear, errors))
            checks.append(
                (
                    awg_model,
                    "READY" if ready else "BLOCKED",
                    f"{awg.identity}; output OFF requested; queries={status}",
                )
            )
        except Exception as exc:
            checks.append((awg_model, "MISSING", str(exc)))
        finally:
            close(awg)

    laser_resource = config["laser"].get("visa_resource", "")
    if not laser_resource:
        checks.append(("Stradus 639-160", "BLOCKED", "missing laser.visa_resource"))
    else:
        laser = None
        try:
            laser = Stradus639160(**{k: config["laser"][k] for k in ("visa_resource", "baud_rate", "timeout_ms", "emission_settle_s")})
            laser.safe_off()
            status = laser.status()
            ready = status["fault_code"] in (0, 1) and status["interlock"] == 1 and status["control_mode"] == 0 and status["emission_enabled"] == 0
            checks.append(("Stradus 639-160", "READY" if ready else "BLOCKED", f"{laser.identity}; LE=0 required, modulation mode preserved; queries ?FC/?FD/?IL/?C/?EPC/?PUL/?LE/?LPS/?LP/?PP -> {status}"))
        except Exception as exc:
            checks.append(("Stradus 639-160", "MISSING", str(exc)))
        finally:
            close(laser)

    scope_resource = config["scope"].get("visa_resource", "")
    if not scope_resource:
        checks.append(("Rigol MSO7054", "BLOCKED", "missing scope.visa_resource"))
    else:
        scope = None
        try:
            scope = RigolMSO7054(
                resource=config["scope"]["visa_resource"],
                **{k: config["scope"][k] for k in ("channel", "timeout_ms")},
            )
            scope.configure_detector_input(config["scope"])
            status = scope.status()
            actual_impedance = status[f":CHAN{scope.channel}:IMP?"].upper()
            expected_impedance = str(config["scope"]["input_impedance"]).upper()
            impedance_ready = expected_impedance == "FIFTY" and (
                actual_impedance.startswith("50") or actual_impedance.startswith("FIFT")
            )
            ready = impedance_ready and error_is_clear(status[":SYST:ERR?"])
            checks.append(("Rigol MSO7054", "READY" if ready else "BLOCKED", f"{scope.identity}; DET02AFC input configured to DC/50 ohm; queries={status}"))
        except Exception as exc:
            checks.append(("Rigol MSO7054", "BLOCKED" if scope is not None else "MISSING", str(exc)))
        finally:
            close(scope)

    kinesis = Path(config["stage"]["kinesis_dir"])
    manager_dll = kinesis / "Thorlabs.MotionControl.DeviceManagerCLI.dll"
    checks.append(("Kinesis", "READY" if manager_dll.exists() else "MISSING", str(kinesis)))
    stage_serial = str(config["stage"].get("serial_number", ""))
    if not stage_serial:
        checks.append(("BPC303", "BLOCKED", "missing stage.serial_number"))
    elif not manager_dll.exists():
        checks.append(("BPC303", "BLOCKED", "Kinesis unavailable"))
    else:
        try:
            import clr

            clr.AddReference(str(manager_dll))
            from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI

            DeviceManagerCLI.BuildDeviceList()
            serials = [str(value) for value in DeviceManagerCLI.GetDeviceList()]
            found = stage_serial in serials
            if not found:
                checks.append(("BPC303", "MISSING", f"detected: {', '.join(serials) or 'none'}; no motion"))
            elif not config["stage"]["calibrated"]:
                checks.append(("BPC303", "READY", f"detected: {stage_serial}; live status deferred until calibration; no motion"))
            else:
                stage = None
                try:
                    stage = KinesisBPC303Stage(config["stage"], enable_channels=False)
                    checks.append(("BPC303", "READY", f"{stage.identity}; channels not enabled; native status={stage.status()}"))
                except HardwareUnavailable as exc:
                    checks.append(("BPC303", "BLOCKED", str(exc)))
                finally:
                    close(stage)
        except Exception as exc:
            checks.append(("BPC303", "MISSING", str(exc)))

    camera = None
    try:
        camera = PixelinkCamera(config["camera"])
        checks.append(("Pixelink M18-CYL", "READY", f"{camera.identity}; {camera.settings()}"))
    except Exception as exc:
        checks.append(("Pixelink M18-CYL", "MISSING", str(exc)))
    finally:
        close(camera)

    checks.append(("Stage calibrated", "READY" if config["stage"]["calibrated"] else "BLOCKED", "limits and directions confirmed" if config["stage"]["calibrated"] else "pending physical validation"))
    checks.append(("Hardware armed", "READY" if config["safety"]["hardware_armed"] else "BLOCKED", "enabled" if config["safety"]["hardware_armed"] else "keep disarmed until preflight is complete"))
    return checks
