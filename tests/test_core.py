from __future__ import annotations

import tempfile
import unittest
import json
import inspect
import ast
import sys
import types
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from pumpauto.config import DEFAULT_CONFIG, ConfigError, load_config, save_config, validate_config
from pumpauto.colorimetry import _predict_phase_colors_cached, predict_phase_colors
from pumpauto.imaging import autofocus, calibrate_pixel_scale, fit_focus, fit_focus_plane, focus_corrected, measure_spot
from pumpauto.instruments import (
    HardwareUnavailable,
    RigolDG1062Z,
    RigolMSO7054,
    KinesisBPC303Stage,
    PixelinkCamera,
    SimAWG,
    SimScope,
    Stradus639160,
    T3AFG350,
    _StradusUSBDevice,
    _is_supported_pixelink,
    classify_visa_device,
    discover_stradus_usb_devices,
    discover_windows_devices,
    discover_visa_devices,
    discover_hardware,
    scope_settings_for_pulse,
)
from pumpauto.patterns import Point, raster, validate
from pumpauto.thermal import absorption_fraction, axisymmetric_simulation, estimate, from_config, multilayer_optics
from pumpauto.ui import FIELD_HELP, HELP_PAGES, TAB_FIELD_HELP, PumpAutoUI, _live_preview_samples
from pumpauto.waveguides import align_waveguide_at_spot, prepare_guide_plan
from pumpauto.workflow import Recipe, _analysis_roi, create_system, fire_single_pulse, laser_peak_power, recipe_readiness, run_recipe, validate_recipe
from pumpauto.waveform import analyze_pulse, assess_capture


class CoreTests(unittest.TestCase):
    def test_analysis_roi_is_bounded_and_small(self) -> None:
        image = np.zeros((100, 120, 3), dtype=np.uint8)
        cropped, bounds = _analysis_roi(image, (118.0, 2.0), 32)
        self.assertEqual(cropped.shape, (32, 32, 3))
        self.assertEqual(bounds, (88, 0, 32, 32))

    def test_run_readiness_blocks_low_disk_and_invalid_spot(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        recipe = Recipe("ready", [Point(10, 10, 10)], 1e-6, 1000, 1, 5, 10)
        ready = recipe_readiness(recipe, config, free_bytes=2 * 1024**3)
        self.assertFalse(ready["blocked"])
        self.assertGreater(ready["estimated_duration_s"], 0)
        low_disk = recipe_readiness(recipe, config, free_bytes=1)
        self.assertTrue(low_disk["blocked"])
        config["guide"]["spot_pixel"] = [9999.0, 9999.0]
        invalid_spot = recipe_readiness(recipe, config, free_bytes=2 * 1024**3)
        self.assertEqual(
            next(status for name, status, _ in invalid_spot["checks"] if name == "Camera analysis ROI"),
            "BLOCKED",
        )

    def test_live_preview_limits_render_and_histogram_work(self) -> None:
        image = np.zeros((368, 491, 3), dtype=np.uint8)
        preview, histogram, stride = _live_preview_samples(
            image, max_dimension=120, max_histogram_pixels=2_000
        )
        self.assertEqual(stride, 5)
        self.assertLessEqual(max(preview.shape[:2]), 120)
        self.assertLessEqual(histogram.shape[0] * histogram.shape[1], 2_000)

    def test_live_camera_always_uses_real_pixelink(self) -> None:
        camera = Mock()
        with patch("pumpauto.workflow.PixelinkCamera", return_value=camera):
            system = create_system(deepcopy(DEFAULT_CONFIG), camera_only=True)
        self.assertIs(system.camera, camera)
        self.assertFalse(system.simulated)

    def test_single_pulse_uses_calibrated_power_and_always_disables_output(self) -> None:
        class FakeAWG:
            identity = "fake awg"

            def __init__(self) -> None:
                self.outputs: list[bool] = []
                self.settings = None
                self.triggered = False

            def output(self, enabled: bool) -> None:
                self.outputs.append(enabled)

            def configure_pulse(self, *settings) -> None:
                self.settings = settings

            def trigger(self) -> None:
                self.triggered = True

            def close(self) -> None:
                self.output(False)

        class FakeLaser:
            identity = "fake laser"

            def __init__(self, **_kwargs) -> None:
                self.closed = False

            def prepare(self, power: float) -> dict:
                return {"peak_power_mw": power}

            def close(self) -> None:
                self.closed = True

        config = deepcopy(DEFAULT_CONFIG)
        config["mode"] = "hardware"
        config["safety"]["hardware_armed"] = True
        config["awg"]["visa_resource"] = "USB::AWG"
        config["laser"]["visa_resource"] = "ASRL1::INSTR"
        config["laser"]["power_calibration"] = [[5.0, 20.0], [10.0, 40.0]]
        awg = FakeAWG()
        with (
            patch("pumpauto.workflow.open_awg", return_value=awg),
            patch("pumpauto.workflow.Stradus639160", FakeLaser),
            patch("pumpauto.workflow.time.sleep"),
        ):
            result = fire_single_pulse(7.5, 1e-6, 5.0, config)
        self.assertTrue(awg.triggered)
        self.assertEqual(awg.settings[3], 1)
        self.assertEqual(awg.outputs[:2], [False, True])
        self.assertFalse(awg.outputs[-1])
        self.assertAlmostEqual(result["stradus_pp_mw"], 30.0)

    def test_disarmed_hardware_allows_incremental_device_setup(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        config["mode"] = "hardware"
        validate_config(config)
        self.assertEqual(config["camera"]["min_exposure_ms"], 4.183)
        config["safety"]["hardware_armed"] = True
        with self.assertRaisesRegex(ConfigError, "Missing hardware resources"):
            validate_config(config)

    def test_hardware_armed_is_never_persisted(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        config["safety"]["hardware_armed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = f"{temp}/config.json"
            save_config(config, path)
            loaded = load_config(path)
        self.assertFalse(loaded["safety"]["hardware_armed"])

    def test_stage_mapping_and_origin_are_validated(self) -> None:
        duplicate = deepcopy(DEFAULT_CONFIG)
        duplicate["stage"]["axis_channels"]["z"] = duplicate["stage"]["axis_channels"]["x"]
        with self.assertRaisesRegex(ConfigError, "distinct BPC303 channels"):
            validate_config(duplicate)
        outside = deepcopy(DEFAULT_CONFIG)
        outside["stage"]["origin_um"]["x"] = 21.0
        with self.assertRaisesRegex(ConfigError, "outside its configured range"):
            validate_config(outside)

        unsafe_low = deepcopy(DEFAULT_CONFIG)
        unsafe_low["awg"]["low_v"] = -1.0
        with self.assertRaisesRegex(ConfigError, "between 0 and 0.8"):
            validate_config(unsafe_low)

        unsafe_tolerance = deepcopy(DEFAULT_CONFIG)
        unsafe_tolerance["stage"]["position_tolerance_um"] = 2.0
        with self.assertRaisesRegex(ConfigError, "position_tolerance_um"):
            validate_config(unsafe_tolerance)

    def test_preflight_fingerprint_ignores_only_session_arm_state(self) -> None:
        base = deepcopy(DEFAULT_CONFIG)
        armed = deepcopy(base)
        armed["safety"]["hardware_armed"] = True
        self.assertEqual(PumpAutoUI._preflight_key(base), PumpAutoUI._preflight_key(armed))
        changed = deepcopy(base)
        changed["awg"]["visa_resource"] = "USB::CHANGED"
        self.assertNotEqual(PumpAutoUI._preflight_key(base), PumpAutoUI._preflight_key(changed))
        operational = deepcopy(base)
        operational["camera"]["exposure_ms"] = 50.0
        operational["scope"]["trigger_level_v"] = 0.5
        operational["laser"]["peak_power_mw"] = 20.0
        operational["stage"]["position_tolerance_um"] = 0.2
        self.assertEqual(PumpAutoUI._preflight_key(base), PumpAutoUI._preflight_key(operational))

    def test_ui_workers_are_joined_and_automation_reserves_devices(self) -> None:
        worker_source = inspect.getsource(PumpAutoUI._spawn_worker)
        close_source = inspect.getsource(PumpAutoUI._finish_close)
        run_source = inspect.getsource(PumpAutoUI._start_run)
        self.assertNotIn("daemon=True", worker_source)
        self.assertIn("thread.is_alive()", close_source)
        self.assertIn("_reserve_resources", run_source)
        self.assertIn("_disarm_session", run_source)

    def test_safe_all_disarms_invalidates_preflight_and_closes_active_outputs(self) -> None:
        ui = PumpAutoUI.__new__(PumpAutoUI)
        ui.root = Mock()
        ui.root.after.side_effect = lambda _delay, callback: callback()
        ui._spawn_worker = lambda work: work()
        ui.config = deepcopy(DEFAULT_CONFIG)
        ui.config["mode"] = "hardware"
        ui.config["safety"]["hardware_armed"] = True
        ui.preflight_config = "passed"
        ui.cfg_armed = Mock()
        ui.armed_check = Mock()
        ui.mode_text = Mock()
        ui.safe_all_button = Mock()
        ui.safe_all_event = threading.Event()
        ui.cancel_event = threading.Event()
        ui.guide_cancel_event = threading.Event()
        ui.live_stop_event = threading.Event()
        for name in ("awg", "camera", "laser", "scope", "stage"):
            setattr(ui, f"{name}_lock", threading.Lock())
        ui.live_system = None
        ui.laser_device = Mock()
        ui.awg_device = Mock()
        ui.stage_device = Mock()
        ui.scope_device = Mock()
        ui.cw_park = Mock()
        ui.cw_park.get.return_value = "1"
        ui.awg_output_enabled = True
        ui.awg_pulse_configured = True
        ui._set_cw_controls = Mock()
        ui._set_ttl_controls = Mock()
        ui._set_live_stage_controls = Mock()
        ui.awg_status = Mock()
        ui.cw_status = Mock()
        ui.ttl_status = Mock()
        ui.scope_status = Mock()
        ui.live_stage_position = Mock()
        ui._write_log = Mock()
        ui._update_mode_status = Mock()
        laser, awg, stage, scope = (
            ui.laser_device,
            ui.awg_device,
            ui.stage_device,
            ui.scope_device,
        )

        ui._safe_all()

        self.assertFalse(ui.config["safety"]["hardware_armed"])
        self.assertIsNone(ui.preflight_config)
        self.assertTrue(ui.safe_all_event.is_set())
        self.assertTrue(ui.cancel_event.is_set())
        laser.close.assert_called_once()
        awg.output.assert_called_with(False)
        awg.close.assert_called_once()
        stage.close.assert_called_once()
        scope.close.assert_called_once()
        self.assertIsNone(ui.stage_device)
        self.assertIsNone(ui.scope_device)

    def test_shutdown_does_not_apply_cw_park_commands_to_ttl_mode(self) -> None:
        ui = PumpAutoUI.__new__(PumpAutoUI)
        ui.config = deepcopy(DEFAULT_CONFIG)
        ui.live_system = None
        ui.laser_device = Mock()
        ui.laser_mode = "ttl"
        ui.awg_device = None
        ui.stage_device = None
        ui.scope_device = None
        ui.cw_park = Mock()
        laser = ui.laser_device

        self.assertEqual(ui._close_dashboard_devices(park_laser=True), [])

        ui.cw_park.get.assert_not_called()
        laser.disable_internal_cw.assert_not_called()
        laser.close.assert_called_once()
        self.assertIsNone(ui.laser_device)

    def test_manual_awg_burst_turns_output_off_and_disarms(self) -> None:
        ui = PumpAutoUI.__new__(PumpAutoUI)
        ui.config = deepcopy(DEFAULT_CONFIG)
        ui.config["mode"] = "hardware"
        ui.config["safety"]["hardware_armed"] = True
        ui.awg_pulse_configured = True
        ui.awg_output_enabled = True
        ui.awg_burst_duration_s = 0.01
        ui.awg_device = Mock()
        ui.safe_all_event = Mock()
        ui.awg_status = Mock()
        ui.root = Mock()
        ui.root.after.side_effect = lambda _delay, callback: callback()
        ui._disarm_session = Mock()
        ui._device_worker = lambda _lock, _title, work: (work(), True)[1]
        ui.awg_lock = threading.Lock()

        ui._awg_trigger()

        ui.awg_device.trigger.assert_called_once()
        ui.awg_device.output.assert_called_once_with(False)
        ui._disarm_session.assert_called_once()

    def test_manual_awg_output_off_invalidates_the_pulse_configuration(self) -> None:
        ui = PumpAutoUI.__new__(PumpAutoUI)
        ui.config = deepcopy(DEFAULT_CONFIG)
        ui.awg_device = Mock(identity="DG1062Z")
        ui.awg_output_enabled = True
        ui.awg_pulse_configured = True
        ui.awg_status = Mock()
        ui.root = Mock()
        ui.root.after.side_effect = lambda _delay, callback: callback()
        ui.awg_lock = threading.Lock()
        ui._device_worker = lambda _lock, _title, work: (work(), True)[1]

        ui._awg_output(False)

        ui.awg_device.configure_dc.assert_called_once_with(0.0)
        ui.awg_device.output.assert_called_once_with(False)
        self.assertFalse(ui.awg_pulse_configured)

    def test_connected_pixelink_model_is_supported(self) -> None:
        self.assertTrue(_is_supported_pixelink("D3011", "PL-D6218CU-CYL"))
        self.assertTrue(_is_supported_pixelink("M18-CYL", "PL-D7718"))
        self.assertFalse(_is_supported_pixelink("D1234", "MONO"))

    def test_scan_pc_classifies_supported_visa_devices(self) -> None:
        class FakeDevice:
            def __init__(self, identity: str) -> None:
                self.identity = identity

            def query(self, _command: str) -> str:
                return self.identity

            def close(self) -> None:
                pass

        identities = {
            "USB::AWG": "RIGOL TECHNOLOGIES,DG1062Z,1,1",
            "USB::SCOPE": "RIGOL TECHNOLOGIES,MSO7054,1,1",
        }

        class FakeResourceManager:
            def list_resources(self) -> tuple[str, ...]:
                return tuple(identities)

            def open_resource(self, resource: str) -> FakeDevice:
                return FakeDevice(identities[resource])

            def close(self) -> None:
                pass

        with patch("pyvisa.ResourceManager", return_value=FakeResourceManager()):
            rows = discover_visa_devices(DEFAULT_CONFIG["laser"])
        self.assertEqual({row["role"] for row in rows}, {"awg", "scope"})
        self.assertEqual(classify_visa_device("Stradus 639nm 160mW", "ASRL1::INSTR"), "laser")

    def test_scan_pc_detects_stradus_usb_hid(self) -> None:
        device = Mock(is_manager=False, vendor_id=0x201A, product_id=0x1001)
        module = Mock()
        module.get_usb_ports.return_value = {"laser": device}
        with patch("pumpauto.instruments._load_vortran_usb", return_value=module):
            rows = discover_stradus_usb_devices()
        self.assertEqual(rows[0]["resource"], "USBHID::201A::1001")
        self.assertEqual(rows[0]["role"], "laser")

    @patch("pumpauto.instruments.sys.platform", "win32")
    def test_scan_pc_reports_connected_devices_with_driver_errors(self) -> None:
        result = Mock(
            returncode=0,
            stdout='{"Status":"Error","Class":null,"FriendlyName":"MSO7054","InstanceId":"USB\\\\VID"}',
            stderr="",
        )
        with patch("pumpauto.instruments.subprocess.run", return_value=result):
            rows = discover_windows_devices()
        self.assertEqual(rows[0]["FriendlyName"], "MSO7054")
        self.assertEqual(rows[0]["Status"], "Error")

    def test_every_tab_has_detailed_contextual_help(self) -> None:
        self.assertEqual(
            set(HELP_PAGES),
            {
                "recipe", "dashboard", "camera", "laser", "awg", "stage", "scope",
                "thermal", "focus", "guide", "diagnostics",
            },
        )
        for title, content in HELP_PAGES.values():
            self.assertTrue(title)
            self.assertIn("## Purpose", content)
            self.assertGreater(len(content), 1_000)

    def test_every_editable_field_has_formatted_context_help(self) -> None:
        source = inspect.getsource(sys.modules[PumpAutoUI.__module__])
        tree = ast.parse(source)
        labels = {
            node.args[2].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"_entry", "_device_entry"}
            and len(node.args) > 2
            and isinstance(node.args[2], ast.Constant)
        }
        self.assertEqual(labels - set(FIELD_HELP), set())
        self.assertEqual(set(TAB_FIELD_HELP), set(HELP_PAGES) - {"dashboard"})
        ui = PumpAutoUI.__new__(PumpAutoUI)
        ui.config = deepcopy(DEFAULT_CONFIG)
        for label in set().union(*TAB_FIELD_HELP.values()):
            text = ui._field_help(label)
            self.assertGreater(len(text), 20)
            self.assertNotIn("{", text)
        self.assertIn("_helped", inspect.getsource(PumpAutoUI._entry))

    def test_f1_uses_the_selected_workflow_subtab_help(self) -> None:
        ui = PumpAutoUI.__new__(PumpAutoUI)
        ui.notebook = Mock()
        ui.notebook.index.return_value = 1
        setup = Mock()
        operate = Mock()
        operate.index.return_value = 1
        run = Mock()
        ui.help_groups = (
            (setup, ("diagnostics", "thermal")),
            (operate, ("dashboard", "focus")),
            (run, ("recipe", "guide")),
        )
        ui._show_help = Mock()
        self.assertEqual(ui._show_current_help(), "break")
        ui._show_help.assert_called_once_with("focus")

    def test_preflight_blocks_unconfigured_hardware_without_creating_stage(self) -> None:
        class FakeResourceManager:
            def list_resources(self) -> tuple[()]:
                return ()

            def close(self) -> None:
                pass

        class FakeCamera:
            identity = "fake camera"

            def settings(self) -> dict:
                return {}

            def close(self) -> None:
                pass

        with (
            patch("pyvisa.ResourceManager", return_value=FakeResourceManager()),
            patch("pumpauto.instruments.PixelinkCamera", return_value=FakeCamera()),
        ):
            checks = discover_hardware(DEFAULT_CONFIG)
        status = {name: value for name, value, _ in checks}
        self.assertEqual(status["T3AFG350"], "BLOCKED")
        self.assertEqual(status["Stradus 639-160"], "BLOCKED")
        self.assertEqual(status["Rigol MSO7054"], "BLOCKED")
        self.assertEqual(status["BPC303"], "BLOCKED")
        self.assertEqual(status["Stage calibrated"], "BLOCKED")

    def test_pixelink_auto_exposure_reduces_saturation(self) -> None:
        camera = PixelinkCamera.__new__(PixelinkCamera)
        camera.auto_exposure = True
        camera.target_peak_fraction = 0.8
        camera.max_auto_exposure_steps = 3
        camera.exposure_s = 0.01
        camera.exposure_limits = (1e-5, 0.1)
        camera._capture_once = Mock(
            side_effect=[np.full((8, 8, 3), 255, dtype=np.uint8), np.full((8, 8, 3), 204, dtype=np.uint8)]
        )

        def set_exposure(value: float) -> float:
            camera.exposure_s = value
            return value

        camera._set_exposure = Mock(side_effect=set_exposure)
        image = camera.capture()
        self.assertEqual(int(image.max()), 204)
        self.assertLess(camera.exposure_s, 0.01)

    def test_pixelink_live_exposure_is_applied_and_bounded(self) -> None:
        camera = PixelinkCamera.__new__(PixelinkCamera)
        camera.exposure_limits = (1e-5, 0.1)
        camera._set_exposure = Mock(return_value=0.005)

        self.assertEqual(camera.set_exposure(5.0, False), 5.0)
        self.assertFalse(camera.auto_exposure)
        camera._set_exposure.assert_called_once_with(0.005)
        with self.assertRaisesRegex(ValueError, "between"):
            camera.set_exposure(101.0, False)

    def test_pixelink_stream_start_and_stop_are_idempotent(self) -> None:
        camera = PixelinkCamera.__new__(PixelinkCamera)
        camera.api = Mock()
        camera.api.apiSuccess.return_value = True
        camera.api.setStreamState.return_value = (0,)
        camera.handle = 1
        camera._streaming = False
        camera.start_stream()
        camera.start_stream()
        camera.stop_stream()
        camera.stop_stream()
        self.assertEqual(camera.api.setStreamState.call_count, 2)
        self.assertFalse(camera._streaming)

    def test_pixelink_auto_exposure_uses_gain_at_exposure_limit(self) -> None:
        camera = PixelinkCamera.__new__(PixelinkCamera)
        camera.auto_exposure = True
        camera.target_peak_fraction = 0.8
        camera.max_auto_exposure_steps = 2
        camera.exposure_s = 0.1
        camera.exposure_limits = (1e-5, 0.1)
        camera.gain_db = 0.0
        camera.gain_limits = (0.0, 24.0)
        camera._capture_once = Mock(
            side_effect=[np.full((8, 8, 3), 10, dtype=np.uint8), np.full((8, 8, 3), 160, dtype=np.uint8)]
        )

        def set_gain(value: float) -> float:
            camera.gain_db = value
            return value

        camera._set_gain = Mock(side_effect=set_gain)
        camera.capture()
        self.assertGreater(camera.gain_db, 0.0)

    def test_stradus_pulsed_command_order(self) -> None:
        class FakeStradus:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.values = {
                    "FC": "1", "IL": "1", "C": "0", "EPC": "0", "PUL": "0",
                    "LE": "0", "PP": "0", "LP": "0", "LPS": "0",
                }
                self.pending = ""
                self.timeout = 5000

            def query(self, command: str) -> str:
                self.commands.append(command)
                if command.startswith("?"):
                    key = command[1:]
                else:
                    key, value = command.split("=", 1)
                    blocked_in_standby = key in ("PP", "PUL") and self.values["LE"] == "0"
                    if not blocked_in_standby:
                        self.values[key] = value
                        if key == "LP":
                            self.values["LPS"] = value
                self.pending = f"{key}={self.values[key]}\r\n"
                return command + "\r\n"

            def read(self) -> str:
                return self.pending

        laser = Stradus639160.__new__(Stradus639160)
        laser.device = FakeStradus()
        laser.emission_settle_s = 0.0
        with self.assertRaisesRegex(RuntimeError, "Prepare TTL pulse mode"):
            laser.prepare(12.5)
        status = laser.prepare(12.5, allow_beam_blocked_mode_change=True)
        self.assertEqual(status["fault_code"], 1)
        self.assertEqual(status["pul"], 1)
        self.assertAlmostEqual(status["peak_power_mw"], 12.5)
        self.assertLess(laser.device.commands.index("LE=1"), laser.device.commands.index("PUL=1"))
        self.assertFalse(any("CH2" in command.upper() for command in laser.device.commands))

    def test_internal_cw_uses_stradus_only_and_returns_to_park(self) -> None:
        class FakeStradus:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.values = {
                    "FC": "1", "IL": "1", "C": "0", "EPC": "0", "PUL": "1",
                    "LE": "0", "PP": "10", "LP": "0.8", "LPS": "1.0",
                }
                self.pending = ""
                self.timeout = 5000

            def query(self, command: str) -> str:
                self.commands.append(command)
                if command.startswith("?"):
                    key = command[1:]
                else:
                    key, value = command.split("=", 1)
                    if key != "PUL" or self.values["LE"] == "1":
                        self.values[key] = value
                        if key == "LP":
                            self.values["LPS"] = value
                self.pending = f"{key}={self.values[key]}\r\n"
                return command + "\r\n"

            def read(self) -> str:
                return self.pending

        laser = Stradus639160.__new__(Stradus639160)
        laser.device = FakeStradus()
        laser.emission_settle_s = 0.0
        status = laser.enable_internal_cw(8.0, 1.0)
        self.assertEqual(status["pul"], 0)
        self.assertEqual(status["emission_enabled"], 1)
        self.assertAlmostEqual(status["laser_power_setting_mw"], 8.0)
        le_on = laser.device.commands.index("LE=1")
        self.assertIn("PUL=0", laser.device.commands[le_on + 1:])
        laser.disable_internal_cw(1.0)
        self.assertEqual(laser.device.values["LE"], "0")
        self.assertAlmostEqual(float(laser.device.values["LPS"]), 1.0)
        self.assertNotIn("PP=8", laser.device.commands)

    def test_internal_cw_rejects_an_unverified_park_power_before_emission(self) -> None:
        laser = Stradus639160.__new__(Stradus639160)
        laser.emission_settle_s = 0.0
        laser._value = Mock(side_effect=["1", "30"])
        laser.safe_off = Mock()
        with self.assertRaisesRegex(RuntimeError, "park power"):
            laser.enable_internal_cw(8.0, 1.0)
        laser.safe_off.assert_not_called()

    def test_laser_tab_does_not_open_or_control_the_awg(self) -> None:
        source = "".join(
            inspect.getsource(method)
            for method in (PumpAutoUI._cw_on, PumpAutoUI._cw_off, PumpAutoUI._ttl_on, PumpAutoUI._ttl_off)
        )
        self.assertNotIn("open_awg", source)

    def test_read_only_stradus_disconnect_sends_no_laser_command(self) -> None:
        laser = Stradus639160.__new__(Stradus639160)
        laser.device = Mock()
        laser.rm = Mock()
        laser.safe_off = Mock()
        laser.disconnect()
        laser.safe_off.assert_not_called()
        laser.device.close.assert_called_once_with()
        laser.rm.close.assert_called_once_with()

    def test_stradus_safe_off_preserves_modulation_mode(self) -> None:
        class FakeStradus:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.pending = ""
                self.timeout = 5000
                self.le = "1"

            def query(self, command: str) -> str:
                self.commands.append(command)
                if command.startswith("?"):
                    key, value = command[1:], self.le
                else:
                    key, value = command.split("=", 1)
                    if key == "LE":
                        self.le = value
                self.pending = f"{key}={value}\r\n"
                return command + "\r\n"

            def read(self) -> str:
                return self.pending

        laser = Stradus639160.__new__(Stradus639160)
        laser.device = FakeStradus()
        laser.safe_off()
        self.assertEqual(laser.device.commands, ["LE=0", "?LE"])

    def test_stradus_usb_drains_stale_responses_without_sending_more_commands(self) -> None:
        device = _StradusUSBDevice.__new__(_StradusUSBDevice)
        device.timeout = 100
        device.laser = Mock()
        device.laser.read_timeout = 40
        device.laser.prefix_1 = bytearray([0xA0])
        device.laser.data_in_array_2 = bytearray([0xA1])
        device.laser.data_in_array_3 = bytearray([0xA2])
        device.laser.data_in_array_4 = bytearray([0xA3])
        device.laser.read_usb.side_effect = [
            "\x01\xff", "\x01\xff", "LE=1\nStradus:0>", None,
            "\x01\xff", "\x01\xff", "PP=9.8\nStradus:0>",
        ]
        self.assertIn("PP=9.8", device.query("PP=10"))
        packets = [call.args[4] for call in device.laser.connection.ctrl_transfer.call_args_list]
        sent = [bytes(packet) for packet in packets if packet[0] == 0xA0]
        self.assertEqual(len(sent), 1)
        self.assertIn(b"PP=10\r\n", sent[0])

    def test_stradus_usb_open_does_not_reset_device_on_windows(self) -> None:
        laser = Mock(vendor_id=0x201A, product_id=0x1001, bus=1, address=2)
        connection = Mock()
        module = Mock()
        module.get_lasers.return_value = [laser]
        vortran = types.ModuleType("vortran_lbl")
        vortran_usb = types.ModuleType("vortran_lbl.usb")
        vortran_usb.get_usb_backend = Mock(return_value=Mock())
        vortran.usb = vortran_usb
        usb = types.ModuleType("usb")
        usb_core = types.ModuleType("usb.core")
        usb_util = types.ModuleType("usb.util")
        usb_core.find = Mock(return_value=connection)
        usb_util.claim_interface = Mock()
        usb.core, usb.util = usb_core, usb_util
        with (
            patch.dict(
                sys.modules,
                {
                    "vortran_lbl": vortran,
                    "vortran_lbl.usb": vortran_usb,
                    "usb": usb,
                    "usb.core": usb_core,
                    "usb.util": usb_util,
                },
            ),
            patch("pumpauto.instruments._load_vortran_usb", return_value=module),
        ):
            device = _StradusUSBDevice(5000)
        self.assertIs(device.laser.connection, connection)
        connection.reset.assert_not_called()
        usb_util.claim_interface.assert_called_once_with(connection, 0)

        laser = Stradus639160.__new__(Stradus639160)
        laser.device = Mock()
        laser.device.query.return_value = "?FC=1\n?C=0\nStradus:9>"
        laser.device.read.return_value = ""
        self.assertEqual(laser._value("?C", "C"), "0")

    def test_sample_power_is_interpolated_to_stradus_pp_without_extrapolation(self) -> None:
        laser = {"peak_power_mw": 10.0, "power_calibration": [[2.0, 8.0], [10.0, 40.0]]}
        peak, record = laser_peak_power(5.0, laser)
        self.assertAlmostEqual(peak, 20.0)
        self.assertEqual(record["mode"], "linear_interpolation")
        with self.assertRaisesRegex(ValueError, "outside the calibration range"):
            laser_peak_power(12.0, laser)

    def test_awg_sets_50_ohm_load_before_ttl_levels(self) -> None:
        class FakeAWG:
            def __init__(self) -> None:
                self.commands: list[str] = []

            def write(self, command: str) -> None:
                self.commands.append(command)

        awg = T3AFG350.__new__(T3AFG350)
        awg.channel = 1
        awg.load_ohm = 50
        awg.device = FakeAWG()
        awg.configure_pulse(10e-9, 5.0, 0.0, 1, 1e6)
        self.assertEqual(awg.device.commands[0], "C1:OUTP LOAD,50")
        self.assertIn("HLEV,5.0,LLEV,0.0", awg.device.commands[1])
        awg.device.commands.clear()
        awg.configure_dc(0.0)
        self.assertEqual(awg.device.commands, ["C1:OUTP LOAD,50", "C1:BSWV WVTP,DC,OFST,0.0"])

    def test_preflight_status_uses_read_only_equipment_queries(self) -> None:
        device = Mock()
        device.query.side_effect = lambda command: {
            "C1:OUTP?": "C1:OUTP OFF,LOAD,50",
            "SYST:ERR?": '0,"No error"',
            ":CHAN1:IMP?": "50",
            ":CHAN1:SCAL?": "1.0",
            ":TRIG:EDGE:SOUR?": "CHAN1",
            ":TRIG:EDGE:SLOP?": "POS",
            ":TRIG:EDGE:LEV?": "0.1",
            ":SYST:ERR?": '0,"No error"',
        }[command]
        awg = T3AFG350.__new__(T3AFG350)
        awg.channel = 1
        awg.device = device
        awg_status = awg.status()
        self.assertIn("OFF", awg_status["C1:OUTP?"])
        self.assertEqual(set(awg_status), {"C1:OUTP?"})
        scope = RigolMSO7054.__new__(RigolMSO7054)
        scope.channel = 1
        scope.device = device
        self.assertEqual(scope.status()[":CHAN1:IMP?"], "50")

    def test_scope_preflight_configures_detector_input(self) -> None:
        scope = RigolMSO7054.__new__(RigolMSO7054)
        scope.channel = 1
        scope.device = Mock()
        scope.configure_detector_input({"coupling": "DC", "input_impedance": "FIFTy"})
        self.assertEqual(
            [call.args[0] for call in scope.device.write.call_args_list],
            [":CHAN1:COUP DC", ":CHAN1:IMP FIFTy"],
        )
        scope.device.query.assert_called_once_with("*OPC?")

    def test_preflight_report_is_persisted_with_checks_and_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ui = PumpAutoUI.__new__(PumpAutoUI)
            ui.config_path = Path(temp) / "config.json"
            candidate = deepcopy(DEFAULT_CONFIG)
            candidate["results_dir"] = "results"
            path = ui._persist_preflight_report(
                candidate, [("AWG", "READY", "C1:OUTP? -> OFF")]
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["checks"][0]["detail"], "C1:OUTP? -> OFF")

    def test_visa_adapters_reject_the_wrong_instrument_identity(self) -> None:
        class FakeDevice:
            timeout = 0

            def __init__(self) -> None:
                self.closed = False
                self.commands: list[str] = []

            def query(self, _command: str) -> str:
                return "ACME,NOT-THE-REQUESTED-INSTRUMENT,1,1"

            def write(self, command: str) -> None:
                self.commands.append(command)

            def close(self) -> None:
                self.closed = True

        class FakeResourceManager:
            def __init__(self) -> None:
                self.device = FakeDevice()
                self.closed = False

            def open_resource(self, _resource: str) -> FakeDevice:
                return self.device

            def close(self) -> None:
                self.closed = True

        awg_rm = FakeResourceManager()
        with patch("pyvisa.ResourceManager", return_value=awg_rm):
            with self.assertRaisesRegex(HardwareUnavailable, "Expected Teledyne"):
                T3AFG350("USB::WRONG")
        self.assertTrue(awg_rm.device.closed)
        self.assertTrue(awg_rm.closed)
        self.assertEqual(awg_rm.device.commands, [])

        scope_rm = FakeResourceManager()
        with patch("pyvisa.ResourceManager", return_value=scope_rm):
            with self.assertRaisesRegex(HardwareUnavailable, "Expected Rigol"):
                RigolMSO7054("USB::WRONG")
        self.assertTrue(scope_rm.device.closed)
        self.assertTrue(scope_rm.closed)

    def test_dg1062z_configures_manual_n_cycle_burst_and_enforces_limits(self) -> None:
        class FakeAWG:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.output = "OFF"
                self.burst = "OFF"

            def write(self, command: str) -> None:
                self.commands.append(command)
                if command.startswith(":OUTP1 "):
                    self.output = command.rsplit(" ", 1)[1]
                    if self.output == "ON":
                        self.burst = "OFF"
                elif command == ":SOUR1:BURS ON":
                    self.burst = "ON"

            def query(self, command: str) -> str:
                return {
                    ":OUTP1?": self.output,
                    ":OUTP1:LOAD?": "50",
                    ":SOUR1:FUNC?": "PULS",
                    ":SOUR1:FREQ?": "1e6",
                    ":SOUR1:FUNC:PULS:WIDT?": "20e-9",
                    ":SOUR1:VOLT:LOW?": "0",
                    ":SOUR1:VOLT:HIGH?": "5",
                    ":SOUR1:BURS?": self.burst,
                    ":SOUR1:BURS:MODE?": "TRIG",
                    ":SOUR1:BURS:NCYC?": "3",
                    ":SOUR1:BURS:TRIG:SOUR?": "MAN",
                    ":SOUR1:BURS:IDLE?": "BOTTOM",
                    ":SYST:ERR?": '0,"No error"',
                }[command]

        awg = RigolDG1062Z.__new__(RigolDG1062Z)
        awg.channel = 1
        awg.load_ohm = 50
        awg.device = FakeAWG()
        status = awg.configure_pulse(20e-9, 5.0, 0.0, 3, 1e6)
        self.assertEqual(awg.device.commands[0], ":OUTP1 OFF")
        self.assertIn(":SOUR1:BURS:NCYC 3", awg.device.commands)
        self.assertIn(":SOUR1:BURS:IDLE BOTTOM", awg.device.commands)
        self.assertEqual(status["idle"], "BOTTOM")
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            awg.trigger()
        awg.output(True)
        awg.trigger()
        self.assertEqual(awg.device.commands[-1], ":SOUR1:BURS:TRIG")
        awg.device.commands.clear()
        awg.configure_dc(0.0)
        self.assertEqual(
            awg.device.commands,
            [":OUTP1:LOAD 50", ":SOUR1:APPL:DC DEF,DEF,0.0"],
        )

        config = deepcopy(DEFAULT_CONFIG)
        config["awg"]["model"] = "DG1062Z"
        recipe = Recipe("rigol", [Point(10, 10, 10)], 10e-9, 1e6, 1, 5.0, 10.0)
        with self.assertRaisesRegex(ValueError, "shorter than 16 ns"):
            validate_recipe(recipe, config)
        validate_recipe(Recipe("rigol", recipe.points, 16e-9, 1e6, 1, 5.0, 10.0), config)

    def test_recipe_rejects_bursts_longer_than_safety_limit(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        config["safety"]["max_burst_duration_s"] = 2.5
        point = [Point(10, 10, 10)]
        validate_recipe(Recipe("ok", point, 1e-6, 1.0, 3, 5.0, 10.0), config)
        with self.assertRaisesRegex(ValueError, "exceeds the configured"):
            validate_recipe(Recipe("long", point, 1e-6, 1.0, 4, 5.0, 10.0), config)

    def test_default_awg_limits_allow_long_pulses_and_bursts(self) -> None:
        safety = DEFAULT_CONFIG["safety"]
        self.assertEqual(safety["max_pulse_width_s"], 1.0)
        self.assertEqual(safety["max_burst_duration_s"], 1000.0)
        point = [Point(10, 10, 10)]
        validate_recipe(Recipe("one-second", point, 1.0, 0.5, 1, 5.0, 10.0), DEFAULT_CONFIG)
        exact_limit_hz = 999.0 / 999.5
        validate_recipe(
            Recipe("long-burst", point, 0.5, exact_limit_hz, 1000, 5.0, 10.0),
            DEFAULT_CONFIG,
        )
        with self.assertRaisesRegex(ValueError, "exceeds the configured"):
            validate_recipe(Recipe("too-long", point, 0.5, 0.999, 1000, 5.0, 10.0), DEFAULT_CONFIG)

    def test_simulated_scope_requires_arm_and_awg_trigger(self) -> None:
        awg = SimAWG()
        scope = SimScope(awg)
        awg.configure_pulse(1e-6, 5.0, 0.0, 1, 1e3)
        scope.configure_for_pulse(1e-6, DEFAULT_CONFIG["scope"])
        with self.assertRaisesRegex(RuntimeError, "Arm and trigger"):
            scope.acquire()
        scope.arm_single()
        with self.assertRaises(TimeoutError):
            scope.wait_complete(0.1)
        awg.output(True)
        awg.trigger()
        scope.wait_complete(0.1)
        time_s, voltage_v = scope.acquire()
        self.assertEqual(time_s.shape, voltage_v.shape)

    def test_scope_settings_and_rigol_scpi(self) -> None:
        settings = scope_settings_for_pulse(1e-6, DEFAULT_CONFIG["scope"], 1)
        self.assertAlmostEqual(settings["time_scale_s_div"], 0.6e-6)
        self.assertEqual(settings["input_impedance"], "FIFTy")
        long_settings = scope_settings_for_pulse(1.0, DEFAULT_CONFIG["scope"], 1)
        self.assertAlmostEqual(long_settings["time_scale_s_div"], 0.6)
        self.assertEqual(DEFAULT_CONFIG["scope"]["acquisition_timeout_s"], 10.0)

        class FakeRigol:
            def __init__(self) -> None:
                self.commands: list[str] = []

            def write(self, command: str) -> None:
                self.commands.append(command)

            def query(self, command: str) -> str:
                return "1" if command == "*OPC?" else "WAIT"

        scope = RigolMSO7054.__new__(RigolMSO7054)
        scope.channel = 1
        scope.device = FakeRigol()
        actual = scope.configure_for_pulse(1e-6, DEFAULT_CONFIG["scope"])
        scope.arm_single()
        self.assertEqual(actual["trigger_source"], "CHAN1")
        self.assertIn(":CHAN1:IMP FIFTy", scope.device.commands)
        self.assertIn(":TIM:MAIN:SCAL 6e-07", scope.device.commands)
        self.assertIn(":TRIG:EDGE:LEV 0.1", scope.device.commands)
        self.assertEqual(scope.device.commands[-1], ":SING")

    def test_absorption_is_physical(self) -> None:
        value = absorption_fraction(0.532, 40.0, 639.0)
        self.assertGreater(value, 0.0)
        self.assertLess(value, 1.0)

    def test_transfer_matrix_conserves_energy_and_reports_pcm_absorption(self) -> None:
        optical = multilayer_optics(DEFAULT_CONFIG, "amorphous")
        self.assertAlmostEqual(
            optical.reflectance + optical.transmittance + optical.total_absorption, 1.0, places=9
        )
        self.assertGreater(optical.sb2se3_absorption, 0.0)
        self.assertLess(optical.sb2se3_absorption, 1.0)
        self.assertNotAlmostEqual(
            optical.sb2se3_absorption,
            absorption_fraction(0.532, 40.0, 639.0),
            places=3,
        )

    def test_measured_spectra_predict_a_brighter_crystalline_phase(self) -> None:
        hits = _predict_phase_colors_cached.cache_info().hits
        prediction = predict_phase_colors(DEFAULT_CONFIG)
        predict_phase_colors(DEFAULT_CONFIG)
        self.assertGreater(_predict_phase_colors_cached.cache_info().hits, hits)
        self.assertAlmostEqual(prediction["total_signal_change_percent"], 56.25, delta=0.5)
        self.assertGreater(
            prediction["crystalline"]["rgb_to_green"]["b"],
            prediction["amorphous"]["rgb_to_green"]["b"],
        )

    def test_bpc303_native_position_conversion(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.api = Mock()
        stage.api.PBC_SetPosition.return_value = 0
        stage.api.PBC_GetPosition.side_effect = [0, 16384, 32767, 0, 16384, 32767]
        stage.serial = b"71524504"
        stage.channels = {"x": 1, "y": 2, "z": 3}
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}
        stage.controller_span = [0.0, 100.0]
        stage.axis_inverted = {axis: False for axis in "xyz"}
        stage.position_tolerance_um = 0.1
        stage.move_timeout_s = 5.0

        stage.move_to(Point(0.0, 10.0, 20.0))
        with patch("pumpauto.instruments.time.sleep"):
            actual = stage.get_position()

        self.assertEqual(
            [call.args[2] for call in stage.api.PBC_SetPosition.call_args_list],
            [0, 16384, 32767],
        )
        self.assertAlmostEqual(actual.x_um, 0.0)
        self.assertAlmostEqual(actual.y_um, 10.0, places=3)
        self.assertAlmostEqual(actual.z_um, 20.0)

    def test_bpc303_clamps_zero_readback_noise_before_motion(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.api = Mock()
        stage.api.PBC_SetPosition.return_value = 0
        stage.serial = b"71524504"
        stage.channels = {"x": 1, "y": 2, "z": 3}
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}
        stage.controller_span = [0.0, 100.0]
        stage.axis_inverted = {axis: False for axis in "xyz"}
        stage.position_tolerance_um = 0.1
        stage.move_timeout_s = 1.0
        stage.get_position = Mock(return_value=Point(-0.0061, 10.0, 10.0))

        stage.move_to(Point(-0.0061, 10.0, 10.0))

        self.assertEqual(stage.api.PBC_SetPosition.call_args_list[0].args[2], 0)

    def test_bpc303_move_times_out_before_continuing_at_wrong_position(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.api = Mock()
        stage.api.PBC_SetPosition.return_value = 0
        stage.serial = b"71524504"
        stage.channels = {"x": 1, "y": 2, "z": 3}
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}
        stage.controller_span = [0.0, 100.0]
        stage.axis_inverted = {axis: False for axis in "xyz"}
        stage.position_tolerance_um = 0.05
        stage.move_timeout_s = 1.0
        stage.get_position = Mock(return_value=Point(9.0, 10.0, 10.0))

        with patch("pumpauto.instruments.time.monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaisesRegex(HardwareUnavailable, "position errors"):
                stage.move_to(Point(10.0, 10.0, 10.0))

    def test_bpc303_rejects_open_loop_without_changing_mode_or_position(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.api = Mock()
        stage.api.PBC_GetMaxOutputVoltage.return_value = 750
        stage.api.PBC_GetMaximumTravel.return_value = 200
        stage.api.PBC_GetPositionControlMode.return_value = 1
        stage.serial = b"71524504"
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}

        with patch("pumpauto.instruments.time.sleep"):
            with self.assertRaisesRegex(HardwareUnavailable, "closed-loop mode"):
                stage._prepare_channel("x", 1, 75.0)

        stage.api.PBC_SetPositionControlMode.assert_not_called()
        stage.api.PBC_SetPosition.assert_not_called()
        stage.api.PBC_EnableChannel.assert_not_called()

    def test_bpc303_explicitly_enables_closed_loop_without_writing_position(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.api = Mock()
        stage.api.PBC_GetMaxOutputVoltage.return_value = 750
        stage.api.PBC_GetMaximumTravel.return_value = 200
        stage.api.PBC_GetPositionControlMode.side_effect = [1, 2, 2]
        stage.api.PBC_SetPositionControlMode.return_value = 0
        stage.api.PBC_GetPosition.return_value = 16384
        stage.serial = b"71524504"
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}
        stage.channel_status = {}

        with patch("pumpauto.instruments.time.sleep"):
            stage._prepare_channel("x", 1, 75.0, enable_channel=False, set_closed_loop=True)

        stage.api.PBC_SetPositionControlMode.assert_called_once_with(stage.serial, 1, 2)
        stage.api.PBC_SetPosition.assert_not_called()
        stage.api.PBC_EnableChannel.assert_not_called()

    def test_bpc303_preflight_reads_closed_loop_state_without_enabling_channel(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.api = Mock()
        stage.api.PBC_GetMaxOutputVoltage.return_value = 750
        stage.api.PBC_GetMaximumTravel.return_value = 200
        stage.api.PBC_GetPositionControlMode.return_value = 2
        stage.api.PBC_GetPosition.return_value = 16384
        stage.serial = b"71524504"
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}
        stage.channel_status = {}

        with patch("pumpauto.instruments.time.sleep"):
            stage._prepare_channel("x", 1, 75.0, enable_channel=False)

        stage.api.PBC_EnableChannel.assert_not_called()
        stage.api.PBC_SetPosition.assert_not_called()
        self.assertEqual(stage.channel_status["x"]["closed_loop_mode"], 2)

    def test_bpc303_reports_signed_negative_position(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.api = Mock()
        stage.api.PBC_GetPosition.side_effect = [-6633, 0, 0]
        stage.serial = b"71524504"
        stage.channels = {"x": 1, "y": 2, "z": 3}
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}
        stage.controller_span = [0.0, 100.0]
        stage.axis_inverted = {axis: False for axis in "xyz"}

        with patch("pumpauto.instruments.time.sleep"):
            position = stage.get_position()

        self.assertAlmostEqual(position.x_um, -4.048585467085787)

    def test_bpc303_status_blocks_position_outside_configured_range(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}
        stage.position_tolerance_um = 0.1
        stage.channel_status = {}
        stage.get_position = Mock(return_value=Point(-3.846, 10.0, 10.0))

        with self.assertRaisesRegex(HardwareUnavailable, "zero that channel"):
            stage.status()

    def test_bpc303_status_accepts_zero_noise_within_tolerance(self) -> None:
        stage = KinesisBPC303Stage.__new__(KinesisBPC303Stage)
        stage.ranges = {axis: [0.0, 20.0] for axis in "xyz"}
        stage.position_tolerance_um = 0.1
        stage.channel_status = {}
        stage.get_position = Mock(return_value=Point(-0.0061, 0.0, 0.0))

        self.assertAlmostEqual(stage.status()["position_um"]["x"], -0.0061)

    def test_focus_plane_fit_and_raster_correction(self) -> None:
        points = [
            Point(0, 0, 9),
            Point(10, 0, 10),
            Point(0, 10, 8.5),
            Point(10, 10, 9.5),
        ]
        plane = fit_focus_plane(points)
        self.assertAlmostEqual(plane.a, 0.1)
        self.assertAlmostEqual(plane.b, -0.05)
        corrected = focus_corrected(
            Point(4, 6, 20),
            {"enabled": True, "a": plane.a, "b": plane.b, "c": plane.c},
        )
        self.assertAlmostEqual(corrected.z_um, 9.1)

    def test_raster_is_serpentine_and_bounded(self) -> None:
        points = raster(Point(10, 10, 10), 2, 2, 3, 2)
        self.assertEqual(len(points), 6)
        self.assertEqual([p.x_um for p in points], [9.0, 10.0, 11.0, 11.0, 10.0, 9.0])
        validate(points, DEFAULT_CONFIG["stage"]["range_um"], 10)

    def test_temperature_grows_with_power(self) -> None:
        low = estimate(from_config(DEFAULT_CONFIG, 1.0, 1e-6)).peak_temperature_c
        high = estimate(from_config(DEFAULT_CONFIG, 2.0, 1e-6)).peak_temperature_c
        self.assertGreater(high, low)

    def test_pulse_train_energy_and_accumulation(self) -> None:
        single_input = from_config(DEFAULT_CONFIG, 2.0, 1e-6, pulse_count=1, repetition_hz=5e5)
        train_input = from_config(DEFAULT_CONFIG, 2.0, 1e-6, pulse_count=5, repetition_hz=5e5)
        single = estimate(single_input)
        train = estimate(train_input)
        self.assertAlmostEqual(train.absorbed_energy_j, 5 * single.absorbed_energy_j)
        self.assertGreater(train.peak_temperature_c, single.peak_temperature_c)

    def test_axisymmetric_solver_uses_stack_and_resolves_radial_diffusion(self) -> None:
        cold = axisymmetric_simulation(from_config(DEFAULT_CONFIG, 0.0, 1e-6), DEFAULT_CONFIG)
        hot = axisymmetric_simulation(from_config(DEFAULT_CONFIG, 1.0, 1e-6), DEFAULT_CONFIG)
        self.assertEqual(hot.result.model, "axisymmetric_2d")
        self.assertTrue(np.all(cold.pcm_temperature_c == 20.0))
        self.assertGreater(hot.result.peak_temperature_c, cold.result.peak_temperature_c)
        self.assertEqual(hot.peak_snapshot_c.shape, (len(hot.depth_um), len(hot.radius_um)))
        self.assertEqual(len(hot.depth_edges_um), len(hot.depth_um) + 1)
        self.assertEqual(len(hot.radius_edges_um), len(hot.radius_um) + 1)
        self.assertAlmostEqual(hot.depth_edges_um[-1], 23.46, places=6)
        pcm = (hot.depth_um >= 0.2) & (hot.depth_um <= 0.24)
        self.assertGreater(hot.peak_snapshot_c[pcm, 0].max(), hot.peak_snapshot_c[pcm, -1].max())

    def test_camera_spot_measurement_and_autofocus(self) -> None:
        yy, xx = np.mgrid[:101, :101]
        image = (
            10 + 180 * np.exp(-2 * ((xx - 52.3) ** 2 + (yy - 48.7) ** 2) / 8.0**2)
        ).astype(np.uint8)
        measured = measure_spot(image, 0.1)
        self.assertAlmostEqual(measured.w_major_px, 8.0, delta=0.4)
        rgb = np.zeros((101, 101, 3), dtype=float)
        rgb[..., 0], rgb[..., 1], rgb[..., 2] = 20, 35, 150
        rgb[..., 2] += 90 * np.exp(-2 * ((xx - 25) ** 2 + (yy - 30) ** 2) / 14.0**2)
        rgb[..., 0] += 190 * np.exp(-2 * ((xx - 70.2) ** 2 + (yy - 61.4) ** 2) / 5.0**2)
        red_spot = measure_spot(np.clip(rgb, 0, 255).astype(np.uint8), 0.1)
        self.assertAlmostEqual(red_spot.x_px, 70.2, delta=0.3)
        self.assertAlmostEqual(red_spot.y_px, 61.4, delta=0.3)
        self.assertAlmostEqual(red_spot.w_major_px, 5.0, delta=0.5)
        edge_measurements = []
        for width in (8.0, 7.0, 6.0):
            frame = (10 + 180 * np.exp(-2 * ((xx - 50) ** 2 + (yy - 50) ** 2) / width**2)).astype(np.uint8)
            edge_measurements.append(measure_spot(frame, 0.1))
        with self.assertRaisesRegex(ValueError, "Focus not bracketed"):
            fit_focus([8.0, 9.0, 10.0], edge_measurements)
        system = create_system(DEFAULT_CONFIG)
        try:
            result, _ = autofocus(
                system.stage, system.camera, Point(10, 10, 10), 4.0, 9, 0.08, 0.0
            )
        finally:
            system.close()
        self.assertAlmostEqual(result.best_z_um, 10.0, delta=0.05)
        self.assertGreater(result.r_squared, 0.99)

    def test_guide_plan_has_exact_length_and_closed_loop_alignment(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        guide = config["guide"]
        transform = -float(config["simulation"]["camera_um_per_pixel"]) * np.eye(2)
        spot = tuple(guide["spot_pixel"])
        size = int(guide["tracking_roi_size_px"])
        tracking_roi = (
            int(spot[0] - size / 2),
            int(spot[1] - size / 2),
            size,
            size,
        )
        system = create_system(config, imaging_only=True)
        try:
            origin = system.stage.get_position()
            plan, _, _ = prepare_guide_plan(
                system.stage,
                system.camera,
                origin,
                tuple(guide["detection_roi"]),
                tracking_roi,
                spot,
                transform,
                1.0,
                0.25,
                1,
                2.0,
                7,
                0.25,
                0.0,
            )
            self.assertEqual(system.stage.get_position(), origin)
        finally:
            system.close()
        self.assertEqual(len(plan.points), 5)
        self.assertAlmostEqual(plan.actual_step_um, 0.25)
        distance = np.hypot(
            plan.points[-1].x_um - plan.points[0].x_um,
            plan.points[-1].y_um - plan.points[0].y_um,
        )
        self.assertAlmostEqual(distance, 1.0)

        with tempfile.TemporaryDirectory() as temp:
            config["results_dir"] = temp

            def adjust(system, target, index):
                result = align_waveguide_at_spot(
                    system.stage,
                    system.camera,
                    spot,
                    transform,
                    tracking_roi,
                    plan.detection.direction_px,
                    0.08,
                    0.5,
                    3,
                    0.25,
                    0.0,
                )
                return {"error_um": result.error_um, "iterations": result.iterations}

            recipe = Recipe("guide", list(plan.points), 1e-6, 1000, 1, 5, 10)
            output = run_recipe(recipe, config, point_adjuster=adjust)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertTrue(manifest["complete"])
            self.assertTrue(all(point["alignment"]["error_um"] <= 0.08 for point in manifest["points"]))
            self.assertTrue(all(point["phase_color_change"]["detected"] for point in manifest["points"]))

    def test_pixel_stage_calibration_recovers_scale_and_returns_origin(self) -> None:
        system = create_system(DEFAULT_CONFIG)
        origin = system.stage.get_position()
        try:
            result = calibrate_pixel_scale(system.stage, system.camera, 1.0, 0.0)
            self.assertAlmostEqual(result.um_per_pixel, 0.08, delta=0.001)
            self.assertGreater(result.registration_snr, 8.0)
            self.assertEqual(system.stage.get_position(), origin)
        finally:
            system.close()

    def test_waveform_metrics(self) -> None:
        time_s = np.linspace(-2e-6, 5e-6, 7001)
        voltage_v = 0.02 + 2.0 * ((time_s >= 1e-6) & (time_s <= 3e-6))
        metrics = analyze_pulse(time_s, voltage_v)
        self.assertAlmostEqual(metrics.baseline_v, 0.02)
        self.assertAlmostEqual(metrics.delay_50_s, 1e-6, delta=2e-9)
        self.assertAlmostEqual(metrics.fwhm_s, 2e-6, delta=2e-9)
        self.assertAlmostEqual(metrics.area_v_s, 4e-6, delta=5e-9)
        self.assertEqual(analyze_pulse(time_s, 0.02 - (voltage_v - 0.02)).polarity, "negative")

    def test_capture_quality_detects_clipping_and_recommends_attenuation(self) -> None:
        time_s = np.linspace(-3e-6, 3e-6, 1200)
        voltage_v = 4.0 * ((time_s >= 0) & (time_s <= 1e-6))
        scope = {**DEFAULT_CONFIG["scope"], "vertical_scale_v_div": 1.0}
        _, quality = assess_capture(time_s, voltage_v, scope)
        self.assertFalse(quality["ok"])
        self.assertIn("detector_action", quality["recommendations"])

    def test_complete_simulated_run(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        config["laser"]["power_calibration"] = [[5.0, 20.0], [15.0, 40.0]]
        config["stage"]["focus_plane"] = {
            "enabled": True, "a": 0.0, "b": 0.0, "c": 9.0, "rms_um": 0.0, "r_squared": 1.0
        }
        with tempfile.TemporaryDirectory() as temp:
            config["results_dir"] = temp
            recipe = Recipe("test", [Point(10, 10, 10)], 1e-6, 1000, 1, 5, 10)
            output = run_recipe(recipe, config)
            self.assertTrue((output / "manifest.json").exists())
            self.assertEqual(len((output / "points.jsonl").read_text().splitlines()), 1)
            self.assertTrue((output / "point_0000_waveform.csv").exists())
            manifest_text = (output / "manifest.json").read_text()
            manifest = json.loads(manifest_text)
            self.assertIn("thermal_axisymmetric_2d", manifest_text)
            self.assertNotIn("thermal_multilayer_1d", manifest_text)
            self.assertIn('"capture_quality"', manifest_text)
            self.assertIn('"camera_settings"', manifest_text)
            self.assertIn("optical_model", manifest)
            self.assertEqual(manifest["power_conversion"]["stradus_pp_mw"], 30.0)
            self.assertEqual(manifest["laser_status"]["peak_power_mw"], 30.0)
            self.assertEqual(manifest["points"][0]["nominal_um"]["z_um"], 10)
            self.assertEqual(manifest["points"][0]["requested_um"]["z_um"], 9.0)
            self.assertEqual(np.load(output / "point_0000_before.npy").shape, (96, 96, 3))

    def test_hardware_recipe_confirmation_summarizes_the_real_exposure(self) -> None:
        ui = PumpAutoUI.__new__(PumpAutoUI)
        ui.config = deepcopy(DEFAULT_CONFIG)
        ui.config["mode"] = "hardware"
        ui.config["safety"]["hardware_armed"] = True
        ui.config["laser"]["power_calibration"] = [[5.0, 20.0], [15.0, 40.0]]
        recipe = Recipe(
            "confirm", [Point(9, 10, 11), Point(12, 10, 11)], 2e-6, 1000, 3, 5, 10
        )
        with patch("pumpauto.ui.messagebox.askyesno", return_value=True) as confirm:
            self.assertTrue(ui._confirm_hardware_recipe(recipe))
        message = confirm.call_args.args[1]
        self.assertIn("Stradus PP: 30", message)
        self.assertIn("Total pulses: 6", message)
        self.assertIn("Corrected X: 9.0000 to 12.0000", message)
        self.assertIn("Total incident pulse energy: 0.00012 mJ", message)


if __name__ == "__main__":
    unittest.main()
