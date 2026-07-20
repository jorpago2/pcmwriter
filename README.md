<p align="center">
  <img src="pumpauto/assets/pcmwriter_icon.png" width="128" alt="PCMWriter icon">
</p>

<h1 align="center">PCMWriter</h1>

<p align="center">
  Automated optical programming and characterization of phase-change materials on silicon photonic devices.
</p>

> [!WARNING]
> PCMWriter is experimental research software. Thermal predictions are starting estimates, not exposure limits. Real laser emission and stage motion require hardware validation, calibrated sample power, a reviewed trajectory, and the laboratory's physical safety controls.

## Overview

PCMWriter coordinates a fixed 639 nm laser spot and an XYZ piezo stage to modify selected regions of an `Sb2Se3` film on silicon waveguides. It combines:

- pulsed and CW laser control;
- camera-based spot measurement, autofocus and waveguide tracking;
- calibrated XYZ motion and focus-plane correction;
- oscilloscope verification of the optical pulse;
- before/after image and colorimetric analysis;
- reproducible recipes with per-point traceability;
- multilayer optical absorption and a transient 2D axisymmetric thermal model;
- simulation mode for development without laboratory hardware.

The software never treats a simulated temperature or an image prediction as permission to expose a sample.

## Supported setup

| Function | Equipment |
|---|---|
| Laser | Vortran Stradus 639-160, TTL pulse modulation and independent CW control |
| AWG | Teledyne LeCroy T3AFG350 or Rigol DG1062Z |
| Oscilloscope | Rigol MSO7054 |
| Photodetector | Thorlabs DET02AFC |
| XYZ positioning | Thorlabs BPC303 with MAX311D/M stage |
| Camera | Pixelink M18-CYL through Pixelink API 4.0 |
| Objective | Thorlabs MY50X-805, 50x, NA 0.55 |

Hardware support depends on the vendor drivers and the exact firmware/API versions installed on the laboratory computer. See [EQUIPMENT.md](EQUIPMENT.md) for the referenced manuals and interfaces.

## Quick start: simulation

PCMWriter currently targets Windows x64. From PowerShell in the repository root:

```powershell
.\install_lab.ps1
.\PCMWriter.bat
```

The setup script creates `.venv`, installs the simulation dependencies, copies `config.example.json` to the ignored local file `config.json`, and runs a self-test. The interface starts in simulation mode with hardware disarmed.

Equivalent commands:

```powershell
.\.venv\Scripts\python.exe -m pumpauto self-test
.\.venv\Scripts\python.exe -m pumpauto simulate
.\.venv\Scripts\python.exe -m pumpauto ui
```

## Hardware setup

Vendor drivers are intentionally not stored in this repository. Install the required Kinesis and Pixelink software, then run:

```powershell
.\install_lab.ps1 -Hardware
.\.venv\Scripts\python.exe -m pumpauto diagnostics
```

Diagnostics identifies configured devices without moving the stage. It requests safe AWG and laser states before hardware can be armed. Complete the staged checks in [LAB_SETUP.md](LAB_SETUP.md) before enabling any optical output.

For a laboratory computer without Internet access, an offline kit can be prepared on another Windows computer:

```powershell
.\prepare_offline.ps1 -OpenVendorPages
.\install_lab.ps1 -Hardware -Offline -InstallVendorDrivers
```

The generated `offline/` and `vendor_installers/` directories remain local and are excluded from Git.

## Safety model

PCMWriter applies software safeguards in addition to â€” never instead of â€” physical laser safety controls:

- hardware starts disarmed on every application launch;
- preflight must succeed before a session can be armed;
- sample power must be converted through a measured Stradus `PP` calibration without extrapolation;
- recipes are rejected outside configured pulse, power, point and XYZ limits;
- **Run readiness** checks the corrected trajectory, analysis ROI, disk space and armed state before devices are reserved;
- each stage move must reach its requested position within tolerance before exposure;
- an invalid oscilloscope capture stops the recipe without automatic re-exposure;
- **SAFE ALL** requests cancellation, AWG output OFF and Stradus emission OFF;
- shutdown attempts every device-safe action independently even if another device fails to close.

The first validation should always use low power, an empty stage or sacrificial sample, and direct observation of every commanded state.

## Configuration

Public defaults are stored in [config.example.json](config.example.json). The active `config.json` is local and ignored because it contains machine-specific resources, serial numbers and calibrations.

Important calibrations include:

- sample power to Stradus `PP`;
- stage origin, axis direction and usable XYZ range;
- camera pixel-to-stage transformation;
- camera scale in micrometres per pixel;
- spot radius at focus;
- focus plane over the sample.

Do not copy a calibrated `config.json` between setups without repeating the physical checks.

## Reproducible output

Each run is written under the configured results directory and includes:

- requested recipe and configuration snapshot;
- nominal, corrected and measured stage positions;
- calibrated optical power and pulse parameters;
- oscilloscope waveform and extracted pulse metrics;
- cropped before, after and change images;
- camera settings and colorimetric response;
- thermal-model inputs and predictions;
- completion or failure state in `manifest.json`;
- incremental point records in `points.jsonl`.

This preserves partial evidence if a run stops between points.

## Optical and thermal model

The default sample stack is:

```text
SiO2 cap (200 nm) / Sb2Se3 (40 nm) / Si (220 nm) / SiO2 BOX (3 um) / Si substrate
```

A transfer-matrix calculation estimates layer-resolved absorption. The transient solver uses an axisymmetric Gaussian heat source and models the first 20 um of the silicon substrate. Interface thermal resistance, latent heat and phase-transition kinetics are not yet included, so the model is intended for comparison and experiment planning rather than safety certification.

## Tests

Run the standard-library test suite and end-to-end simulated self-test:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pumpauto self-test
```

No test that merely imports a driver proves that a real instrument is compatible. Hardware acceptance must verify identification, safe state, commands and measured response on the target computer.

## Repository layout

```text
pumpauto/                  Application source, models and instrument adapters
pumpauto/assets/           PCMWriter application icon
pumpauto/spectra/          Optical constants and instrument response data
tests/                     Automated tests and simulated workflows
EQUIPMENT.md               Referenced hardware and programming documentation
LAB_SETUP.md               Laboratory setup and staged hardware validation
config.example.json        Safe public configuration template
install_lab.ps1            Environment and dependency setup
prepare_offline.ps1        Optional offline-kit preparation
PCMWriter.bat              Console-free Windows launcher
```

Local configurations, results, virtual environments, vendor installers, offline wheels, build products and the private presentation are excluded through `.gitignore`.

## Project status

Simulation, analysis, safety checks and device adapters are implemented and covered by automated tests. Final acceptance with the complete physical setup is still in progress. In particular, the exact Kinesis API behaviour, Pixelink acquisition, Stradus state transitions and Rigol SCPI sequence must be confirmed on the intended laboratory computer before routine sample processing.

Windows x64 binary releases are planned after this hardware validation. The source code remains the reference implementation for review and reproducibility.

### Building a Windows release

From a hardware-capable development environment:

```powershell
.\build_release.ps1
```

The script runs the tests and self-test before creating an `onedir` bundle, `release/PCMWriter-Windows-x64-vX.Y.Z.zip`, and its SHA-256 checksum. The archive includes a safe simulation `config.json`; vendor drivers remain external prerequisites.

## Contributing

Bug reports and pull requests should include:

- simulation or hardware mode;
- relevant equipment model and connection type;
- sanitized configuration fields;
- exact error message and steps to reproduce;
- confirmation that no unsafe motion or optical output was attempted.

Never publish serial numbers, local paths, calibration files containing sensitive data, or experimental results without permission.

## License

PCMWriter is released under the [MIT License](LICENSE).

Developed at the Nanophotonics Technology Center (NTC), Universitat PolitÃ¨cnica de ValÃ¨ncia.

