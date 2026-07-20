from __future__ import annotations

import argparse
import json
import tempfile
from copy import deepcopy
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config, save_config
from .instruments import discover_hardware
from .patterns import Point
from .workflow import Recipe, run_recipe


def example_recipe(config: dict) -> Recipe:
    origin = config["stage"]["origin_um"]
    return Recipe(
        name="smoke_test",
        points=[Point(origin["x"], origin["y"], origin["z"])],
        pulse_width_s=1e-6,
        repetition_hz=1000.0,
        pulse_count=1,
        high_v=5.0,
        optical_power_mw=10.0,
    )


def self_test() -> None:
    config = deepcopy(DEFAULT_CONFIG)
    with tempfile.TemporaryDirectory(prefix="pumpauto-") as temp:
        config["results_dir"] = temp
        output = run_recipe(example_recipe(config), config)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        required = ["point_0000_before.npy", "point_0000_after.npy", "point_0000_waveform.csv"]
        if not manifest["complete"] or not all((output / name).exists() for name in required):
            raise RuntimeError("The simulated workflow did not generate all expected artifacts.")
        peak = manifest["points"][0]["thermal_axisymmetric_2d"]["peak_temperature_c"]
        print(f"SELF-TEST OK | T pico 2D={peak:.2f} C")


def main() -> None:
    parser = argparse.ArgumentParser(description="PCMWriter optical-pumping automation")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("ui")
    sub.add_parser("self-test")
    sub.add_parser("simulate")
    sub.add_parser("diagnostics")
    sub.add_parser("colorimetry")
    sub.add_parser("init-config")
    args = parser.parse_args()
    if args.command in {None, "ui"}:
        from .ui import launch

        launch(args.config)
    elif args.command == "self-test":
        self_test()
    elif args.command == "simulate":
        config = load_config(args.config)
        if config["mode"] != "simulation":
            raise SystemExit("simulate requiere mode=simulation.")
        print(run_recipe(example_recipe(config), config, args.config, print))
    elif args.command == "diagnostics":
        config = load_config(args.config)
        for name, status, detail in discover_hardware(config):
            print(f"[{status}] {name}: {detail}")
    elif args.command == "colorimetry":
        from .colorimetry import predict_phase_colors

        print(json.dumps(predict_phase_colors(load_config(args.config)), indent=2))
    elif args.command == "init-config":
        path = Path(args.config)
        if path.exists():
            raise SystemExit(f"No se sobrescribe {path}.")
        save_config(deepcopy(DEFAULT_CONFIG), path)
        print(f"Created {path}")


if __name__ == "__main__":
    main()
