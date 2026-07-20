# Preparation before laboratory access

The goal is to arrive at the laboratory with Python, the interface, and all SDKs already installed. Perform this preparation on the computer that will connect to the instruments because USB and VISA drivers are not transferred with the project folder.

## 1. Prepare Python and the application

The kit is pinned to 64-bit Python 3.13, the version used during testing.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\prepare_offline.ps1 -OpenVendorPages
.\install_lab.ps1 -Hardware -Offline -InstallVendorDrivers
.\.venv\Scripts\python.exe -m pumpauto self-test
```

The test must finish with `SELF-TEST OK`. It does not connect to any instrument.

## 2. Install manufacturer software

Install these packages before travelling to the laboratory:

1. [Thorlabs Kinesis](https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=Motion_Control), including the 64-bit .NET APIs.
2. [Pixelink Software Suite/SDK](https://www.navitar.com/products/pixelink-cameras/pixelink-sdk), including the M18-CYL driver and `PxLAPI40.dll`.
3. Optional: [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html) if `pyvisa-py` does not recognize the required connection.

`prepare_offline.ps1` stores Python, Pixelink, Kinesis, all Python wheels, and their SHA-256 checksums. Python packages do not replace manufacturer drivers.

## 3. Check the installation without activating hardware

Open `PCMWriter.bat`, go to **Diagnostics**, and run the check. It should report:

- VISA availability and any USB/LAN resources;
- the `C:/Program Files/Thorlabs/Kinesis` directory;
- the BPC303 detected by Kinesis without motion;
- the Pixelink identified as `M18-CYL/PL-D7718` through the native API.

Enter the following values in the interface:

- AWG model and VISA resource for the T3AFG350 or DG1062Z;
- VISA resource for the Rigol MSO7054;
- BPC303 serial number;
- Pixelink serial number, exposure, gain, and ROI.

Save the configuration while keeping `mode=simulation`, `hardware_armed=false`, and `stage.calibrated=false`.

## 4. First laboratory connection

Run `python -m pumpauto diagnostics` or **Run diagnostics** in the interface first. The report separates `READY`, `MISSING`, and `BLOCKED` states, requests AWG CH1 and Stradus OFF, and does not move the stage.

Recommended sequence:

1. Connect one instrument at a time and repeat diagnostics to associate each resource with its model.
2. Open Kinesis and confirm that all three BPC303 channels appear, with no sample installed and no automatic motion.
3. Manually verify the meaning of `0..100` Kinesis units, the real travel in micrometres, and the X/Y/Z directions.
4. Set `range_um`, `origin_um`, `controller_span_units`, and `axis_inverted` in `config.json`.
5. Only then set `stage.calibrated=true`.
6. With the laser physically disabled, configure the AWG for a 50 ohm load and verify a 0-5 V TTL signal on a 50 ohm-terminated oscilloscope input. Never verify it in Hi-Z because the observed voltage would double.
7. Connect the Stradus Mini-USB port and select `USBHID::201A::1001` in **Diagnostics**. Older heads may also use RS-232 through `ASRL...::INSTR` at 19200 baud, 8-N-1, with no flow control. The program enables `PUL=1` and verifies the configured peak power before enabling AWG CH1.
   CW operation does not use the AWG: the **Laser** card in the Hardware Dashboard selects `PUL=0` and controls `LE/LP` directly. Initialize parking power with the beam blocked because the unit may briefly emit at the previously stored `LPS` value.
8. Confirm that `C1:OUTP OFF` disables the output, including after recipe cancellation.
9. Connect the DET02AFC SMA output to CH1 using a 50 ohm coaxial cable and 50 ohm input termination. Inspect and clean the optical FC/PC connector before coupling the monitor branch.
10. Start with `1 V/div` and a positive trigger at `0.1 V`. After the first low-power pulse, apply the saved recommendation so the waveform occupies approximately 2-3 divisions without clipping and the trigger is near 30% of the amplitude.
11. Verify Rigol single-shot acquisition and inspect the calculated baseline, SNR, FWHM, and integral. PCMWriter automatically sets the time scale to six pulse widths and stops the recipe if no trigger arrives.

Before arming hardware, measure several `sample power : Stradus PP` pairs and enter them under **Diagnostics > Sample:PP calibration**, for example `5:20,10:33,15:46`. Both axes must be ordered and increasing. Recipes outside the calibrated interval are blocked.

Only CH1 of the selected AWG is used. CH2 does not need to be connected or configured.

12. Below the phase-change threshold, adjust exposure or gain until the spot is visible without saturation, then run autofocus.
13. With LED illumination and the laser disabled, run **Calibrate pixel-stage** on a fixed sample texture. PCMWriter moves `+X/+Y`, returns to the origin, and stores `um_per_pixel` and both 2x2 transformation matrices. The laser spot remains fixed in the optical system and cannot provide this scale by itself.
    The preflight must identify the camera as `M18-CYL/PL-D7718`, not merely as a generic index. Initial exposure, gain, and ROI are set in **Diagnostics**; `[0,0,0,0]` keeps the full sensor area.
14. In **Align & Pulse > Hardware Dashboard**, set a Camera ROI that fully contains the spot and confirm that the histogram is not clipped. Test XYZ motion from the Stage card using the smallest safe step before autofocus; both sessions may remain active simultaneously.
15. Define the raster area and run **Map focus (5 points)**. Review the fitted-plane RMS and dry-run the trajectory to confirm that corrected Z remains within MAX travel.
16. Use a sacrificial sample for the first correlation between power, pulse, photodiode, and image response.
17. Arm hardware only after completing the preceding checks.

## 5. Values that require real measurements

- optical power at the sample versus laser setting;
- spot radius and profile;
- photodiode/oscilloscope temporal calibration;
- effective DET02AFC responsivity at 639 nm, including coupling and splitter losses;
- camera pixel-to-micrometre conversion and orientation;
- observed crystallization, amorphization, and damage thresholds;
- effective thermal parameters fitted to measurements;
- real backside thermal boundary condition and sample-holder effect;
- effective thermal boundary resistances between SiO2, Sb2Se3, and silicon;
- `n,k` values for the real layers at 639 nm, especially both Sb2Se3 phases.

Until these values are measured, the thermal map is a sensitivity-analysis tool. It does not authorize any real recipe automatically.

## 6. Control references

- [T3AFG Programming Guide](https://cdn.teledynelecroy.com/files/manuals/t3afg-programming-guide.pdf): `BSWV`, `BTWV`, `MTRIG`, and `OUTP` commands.
- [Rigol DG1000Z Programming Guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/waveform-generators/DG1000Z_ProgrammingGuide_EN.pdf): `SOURce`, `BURSt`, `TRIGger`, and `OUTPut` commands.
- [Rigol DS7000 Programming Guide](https://eu.rigol.com/eu/Images/DS7000ProgrammingGuideEN_tcm30-3985.pdf): `WAV` acquisition command family.
- [Kinesis C# Quick Start](https://media.thorlabs.com/contentassets/5f57e82e38004e2aa5dfd0071bcf0732/kinesis_with_c_quick_start_guide.pdf): device loading and the .NET API.
