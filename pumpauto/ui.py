from __future__ import annotations

import ctypes
import json
import os
import threading
import tkinter as tk
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import messagebox, ttk
from typing import Any

import numpy as np

from .colorimetry import predict_phase_colors
from .config import load_config, resolve_results_dir, save_config, validate_config
from .imaging import (
    FocusPlane,
    FocusResult,
    PixelCalibration,
    SpotMeasurement,
    autofocus,
    calibrate_pixel_scale,
    focus_corrected,
    map_focus_plane,
    measure_spot,
)
from .instruments import (
    KinesisBPC303Stage,
    RigolMSO7054,
    Stradus639160,
    discover_hardware,
    open_awg,
    scan_connected_hardware,
)
from .patterns import Point, raster, validate
from .thermal import (
    axisymmetric_simulation,
    estimate,
    from_config,
    multilayer_optics,
    temperature_curve,
)
from .waveguides import (
    GuidePlan,
    align_waveguide_at_spot,
    prepare_guide_plan,
)
from .workflow import (
    Recipe,
    create_system,
    fire_single_pulse,
    laser_peak_power,
    recipe_readiness,
    run_recipe,
    validate_recipe,
)


FIELD_HELP = {
    "Name": "Result-folder label. Blank becomes 'run'. Use a short filesystem-safe name.",
    "Sample power (mW)": "Optical power at the sample, not Stradus PP. Range: >0 to {max_power:g} mW and inside the measured calibration interval in hardware mode.",
    "Pulse duration (µs)": "TTL high time for each optical pulse. Range: {min_width_us:g}–{max_width_us:g} µs.",
    "Repetition rate (Hz)": "Pulse frequency. Must be >0, keep duty cycle below 90%, and respect 25 MHz (DG1062Z) or 350 MHz (T3AFG350).",
    "Pulse count": "Pulses in one burst. Range: 1–{max_pulses}; total burst duration must be ≤{max_burst:g} s.",
    "Thermal pulse count": "Number of pulses included in the offline thermal estimate. Range: integer ≥1; duty cycle must remain below 100%.",
    "Thermal repetition rate (Hz)": "Pulse frequency used by the offline thermal estimate. Range: >0 Hz and pulse duration × frequency <1.",
    "TTL high (V)": "AWG logic-high voltage sent to the Stradus TTL input. Range: {min_high:g}–{max_high:g} V.",
    "Center X (µm)": "Raster centre on stage X. Allowed stage range: {x_low:g}–{x_high:g} µm; every generated point must remain inside it.",
    "Center Y (µm)": "Raster centre on stage Y. Allowed stage range: {y_low:g}–{y_high:g} µm; every generated point must remain inside it.",
    "Z (µm)": "Requested stage Z before focus-plane correction. Allowed range: {z_low:g}–{z_high:g} µm.",
    "Raster width (µm)": "Total X span centred on Center X. Range: ≥0; zero with one X point gives no X scan.",
    "Raster height (µm)": "Total Y span centred on Center Y. Range: ≥0; zero with one Y point gives no Y scan.",
    "X points": "Number of positions across the raster width. Integer ≥1; X points × Y points must be ≤{max_points}.",
    "Y points": "Number of raster rows. Integer ≥1; X points × Y points must be ≤{max_points}.",
    "Power (mW)": "Incident sample power used only by the thermal estimate. Range: ≥0 mW; it does not authorize a real exposure.",
    "Pulse (µs)": "Thermal-model pulse duration. Range: >0 µs.",
    "Spot radius (µm)": "Gaussian 1/e² beam radius on the sample. Range: >0 µm; preferably obtain it from the camera focus measurement.",
    "Phase": "Optical constants used for Sb2Se3: amorphous or crystalline. This changes absorption and predicted temperature.",
    "Radial cells": "Number of radial finite-volume cells. Range: 8–256; more cells increase runtime and radial resolution.",
    "Domain radius (µm)": "Outer radius of the axisymmetric thermal domain. Range: >0 µm and large enough that the boundary stays thermally remote.",
    "Center Z (µm)": "Middle of the autofocus sweep. The complete sweep must remain within {z_low:g}–{z_high:g} µm.",
    "Z sweep (µm)": "Total autofocus travel centred on Center Z. Range: >0 µm and contained inside the configured Z limits.",
    "Image count": "Frames sampled across the Z sweep. Integer ≥5; more frames improve sampling but take longer.",
    "Scale (µm/pixel; 0=unknown)": "Camera scale used to convert spot radii into micrometres. Range: 0 (unknown) or >0 µm/pixel.",
    "XY calibration step (µm)": "Known +X and +Y stage displacement used for pixel calibration. Range: >0 µm and small enough to stay within travel and the camera field.",
    "Spot visible without saturation\nand power below switching threshold": "Mandatory hardware acknowledgement before autofocus. Check it only with an unsaturated spot below the measured switching threshold.",
    "ROI left,top,width,height": "Pixelink ROI in pixels. Values must be non-negative integers; 0,0,0,0 selects the full sensor, otherwise width and height must both be >0.",
    "CW setpoint LP (mW)": "Feedback-regulated Stradus head power in CW mode. Range: >0 to {max_power:g} mW. It is not calibrated sample power.",
    "Park power (mW)": "Low Stradus LPS stored before shutdown and used at the next start. Range: >0 to {max_power:g} mW; establish it with the beam blocked.",
    "DC level (V)": "Constant AWG output level. Range: {low_v:g}–{high_v:g} V; changing a live output requires armed hardware.",
    "Pulse width (s)": "Manual AWG pulse high time. Range: {min_width_s:g}–{max_width_s:g} s; DG1062Z minimum is 16 ns.",
    "Repetition (Hz)": "Manual AWG burst frequency. Must be >0, below the selected generator limit, and keep duty cycle below 90%.",
    "High level (V)": "Manual AWG pulse-high voltage. Range: {min_high:g}–{max_high:g} V.",
    "Low level (V)": "Manual AWG pulse-low voltage. Range: 0–0.8 V and strictly below High level.",
    "Step (um)": "Relative manual piezo displacement per click. Range: >0 µm; the resulting coordinate must stay inside the configured axis limit.",
    "Position tolerance (µm)": "Maximum absolute readback error allowed on every axis after a move. Range: >0–1 µm; start at 0.1 µm and tune from measured stage repeatability.",
    "Move timeout (s)": "Maximum time allowed for all axes to enter the position tolerance. Range: 0.1–60 s; timeout blocks the next pulse.",
    "Expected pulse width (s)": "Expected photodiode pulse duration used to choose oscilloscope time/div. Range: >0 s.",
    "Waveguide ROI left,top,w,h": "Camera search region for one approximately straight guide. Four non-negative pixel integers; width and height must be >0.",
    "Length to switch (µm)": "Requested distance along the detected guide. Range: >0 µm and no longer than the visible detected segment.",
    "Maximum step (µm)": "Largest distance between consecutive exposure points. Range: >0 µm; PCMWriter shortens the actual final step to fit the requested length exactly.",
    "Direction": "Chooses which detected guide endpoint is used as the starting point: start-to-end or end-to-start.",
    "Autofocus span (µm)": "Z sweep performed at the guide endpoints. Range: >0 µm and fully inside the Z travel limits.",
    "Autofocus samples": "Images acquired in each endpoint Z sweep. Integer ≥5.",
    "Spot X (pixel)": "Fixed laser-spot X coordinate in the current camera ROI. Range: inside the captured image.",
    "Spot Y (pixel)": "Fixed laser-spot Y coordinate in the current camera ROI. Range: inside the captured image.",
    "XY tolerance (µm)": "Maximum residual guide-to-spot alignment error accepted before exposure. Range: >0 µm.",
    "Maximum XY correction (µm)": "Largest automatic correction allowed in one alignment iteration. Range: >0 µm and within stage travel.",
    "Mode": "Simulation uses no real outputs; Hardware enables real devices only after a successful preflight and session-only arming.",
    "AWG model": "Select the connected generator: T3AFG350 or DG1062Z. The identity is verified before model-specific commands are sent.",
    "AWG VISA": "Exact VISA resource of the selected AWG. Choose it from Scan PC or enter the vendor VISA address.",
    "Stradus USB/RS232": "Stradus transport: detected USB HID resource or legacy serial VISA address at 19200 baud.",
    "Manual Stradus PP (mW)": "Fallback laser-head PP used only when simulation has no sample-power calibration. Range: >0–160 mW.",
    "Sample power:PP calibration": "Comma-separated sample_mW:PP_mW pairs, e.g. 5:20,10:33. At least two strictly increasing pairs; PP range >0–160 mW.",
    "Oscilloscope VISA": "Exact VISA resource of the Rigol MSO7054. Its identity is checked before configuration.",
    "BPC303 serial": "Kinesis serial number of the connected BPC303. It must match a detected controller; discovery never moves it.",
    "Pixelink serial (0=single)": "Pixelink camera serial. Integer ≥0; zero is allowed only when exactly one supported camera is connected.",
    "Exposure (ms)": "Pixelink exposure time. Range: {min_exposure:g}–{max_exposure:g} ms.",
    "Gain (dB)": "Pixelink analogue/digital gain. Range: ≥0 dB and within the connected camera's supported range.",
    "Anti-saturation auto exposure": "When enabled, PCMWriter reduces exposure and then gain if the image approaches saturation.",
    "Scope V/div": "MSO7054 channel-1 vertical scale. Range: >0 V/div and supported by the instrument; choose it to avoid clipping.",
    "Trigger (V)": "Channel-1 edge-trigger level. It must lie between the photodiode baseline and pulse level.",
    "Scope input": "Channel-1 termination. Allowed values: FIFTy or OMEG; use 50 Ω only when compatible with DET02AFC and cabling.",
    "Trigger slope": "Trigger edge. Allowed values: POSitive, NEGative or RFAL; choose the measured photodiode polarity.",
    "Hardware armed for this session": "Enabled only after every preflight item is READY. It permits real motion/output for this session and is never saved as true.",
    "Stage calibrated (limits and directions confirmed)": "Confirms that origin, range, axis mapping and direction were physically measured. Do not infer this from documentation.",
}


HELP_PAGES: dict[str, tuple[str, str]] = {
    "recipe": (
        "Recipe",
        """# Recipe

## Purpose
Run a single exposure or a rectangular serpentine raster while recording the requested recipe, actual stage positions, camera images, oscilloscope waveform, thermal estimate and analysis results.

## Recommended workflow
1. Complete Diagnostics and keep Hardware armed off until every device and safety condition has been checked in the laboratory.
2. Enter the optical power at the sample, not the laser PP setting.
3. Set pulse duration, repetition rate and pulse count.
4. Enter the raster centre and size. A zero width or height with one point gives a single exposure.
5. Review the requested positions and run first in simulation mode.
6. In hardware mode, confirm the sample-power calibration, stage calibration and safe optical alignment before arming.

## Fields
- Sample power is converted to Stradus PP using the calibration stored in Diagnostics. Hardware operation never extrapolates outside that table.
- Pulse duration is the TTL high time requested from the AWG.
- Repetition rate and pulse count define the pulse train. Duty cycle and instrument limits are checked before output is enabled.
- The oscilloscope is configured around one pulse and verifies the first pulse of a multi-pulse train; total train duration is separately limited.
- Centre X/Y/Z are stage coordinates in micrometres.
- Raster width and height are centred on X/Y. X points and Y points include both ends; rows alternate direction to reduce travel.
- A saved focus plane automatically corrects the requested Z at each XY point.

## During a run
- Before reserving devices, Run readiness verifies recipe limits, corrected XYZ travel, the spot and analysis ROI, free disk space and the session arm state. It also reports conservative storage and a baseline duration estimate; communication, autofocus and alignment time are not included.
- Hardware recipes then show the readiness checklist together with power, Stradus PP, corrected XYZ extent, pulse totals, burst duration and incident energy.
- Progress reports the current point and each acquisition step.
- Cancel is cooperative: output is disabled and execution stops at the next safe checkpoint.
- The result panels show the post-exposure image, registered change map and measured optical waveform.

## Readiness safety
- Any BLOCKED item prevents the run from starting.
- Storage is estimated conservatively and PCMWriter keeps a further 512 MB disk reserve.

## Single optical pulse
- Fire one pulse uses Sample power, Pulse duration and TTL high from this tab.
- It controls only the AWG and Stradus: no stage motion, camera capture or oscilloscope acquisition.
- Hardware mode, Hardware armed and a valid sample-power-to-PP calibration are mandatory. A confirmation is required immediately before firing.

## Safety and blocking conditions
- Hardware mode requires Hardware armed, Stage calibrated and a valid sample-power-to-PP calibration.
- Stage limits, duty cycle, pulse validity and requested power are checked before firing.
- A missing or poor oscilloscope pulse, clipping, low SNR or a pulse touching the acquisition window stops the recipe after saving available evidence.
- The thermal model is advisory and never authorizes an exposure.

## Saved data
Each run creates a result folder containing manifest.json, an incremental points.jsonl journal, before/after images cropped to the configured tracking ROI, change data, waveform CSV and the relevant configuration. Requested and actual positions are stored separately for traceability.
""",
    ),
    "dashboard": (
        "Hardware dashboard",
        """# Hardware dashboard

## Purpose
Operate the equipment needed for optical alignment and pulse preparation from one screen. The dashboard keeps the live image, stage, laser, AWG and oscilloscope controls visible as cards instead of separating the instruments into tabs.

## Recommended order
1. Complete Diagnostics under Setup & Safety before opening any device session.
2. Start Camera with the LED on and choose an unsaturated ROI containing the guide and laser spot.
3. Connect Stage and use small XYZ steps while watching the live image.
4. Use Laser internal CW at a verified low park/setpoint power to locate the fixed optical spot. The AWG is not used for CW.
5. Configure the AWG pulse while its output remains off.
6. Configure and arm the oscilloscope before enabling and triggering the AWG output.
7. Continue to Autofocus or Run only after the displayed states and positions are plausible.

## Layout
- Camera spans the dashboard width because the image and histogram need the largest area.
- Stage and Laser cards contain the controls used during alignment.
- AWG and Scope cards contain electrical pulse generation and capture controls.
- Use the vertical scrollbar to reach lower cards without resizing the application.
- Each card has its own Help button with instrument-specific instructions.

## Independence
Each device has a separate connection, worker thread and lock. A long scope wait does not stop camera rendering, and a stage jog does not block laser or AWG controls. A second command sent to the same busy device is rejected rather than queued ambiguously. Automated recipes reserve all required devices and refuse to start while a dashboard session is connected.

## Safety
The dashboard does not infer a safe sequence from visible states. Laser emission, AWG output and stage movement still require their individual confirmations and limits. SAFE ALL in the header immediately disarms software, requests cancellation, stops live acquisition and closes active device sessions with laser/AWG output OFF. It requires a new preflight before rearming and never replaces the physical interlock.

## Common problems
- A card reports busy: wait for its current command to finish before repeating it.
- The dashboard scrolls slowly: stop the camera or reduce its ROI before diagnosing other devices.
- A device cannot connect: close any vendor utility or other session using the same USB/VISA resource, then run Diagnostics again.
""",
    ),
    "camera": (
        "Live camera",
        """# Live camera

## Purpose
Inspect the sample and laser spot continuously, select a region of interest, detect saturation and measure the Gaussian spot radii used by autofocus and the thermal model.

## Recommended workflow
1. Turn on the white LED whenever the sample structure must be visible.
2. Configure illumination independently on the Laser tab if the red spot must be visible.
3. Start with the full sensor, locate the guide and spot, then reduce the ROI for faster acquisition.
4. Adjust exposure and gain until the image is bright but not saturated.
5. Confirm that the full spot remains inside the ROI before trusting wx, wy or SNR.
6. Use the Stage tab independently if the sample must move while the camera remains live.

## ROI format
- Enter left, top, width, height in camera pixels.
- 0,0,0,0 requests the full sensor.
- The ROI must be supported by the Pixelink camera and must contain enough background around the measured feature.

## Display and measurements
- The main panel shows the current RGB frame; the adjacent plot shows the RGB histogram.
- The Pixelink stream remains active during live view and the plot is updated in place. Preview and histogram are sampled, while spot measurement keeps the original camera pixels. Use a smaller hardware ROI when higher frame rate is required.
- wx and wy are 1/e2 Gaussian radii estimated from intensity moments along the principal axes.
- SNR compares the detected feature with background noise. Low SNR makes the radius unreliable.
- The Pixelink image is expected to look blue because the DMSP550 dichroic suppresses the red pump light before the camera.

## Auto exposure
- Auto exposure first reduces exposure time and then gain when pixels approach the saturation threshold.
- Saturated frames are reported and should not be used for spot size, autofocus or colorimetry.
- Avoid automatic white balance: phase-change color metrics depend on preserving the same RGB response before and after exposure.

## Hardware behaviour
- Hardware mode uses the native Pixelink SDK and Bayer-to-RGB conversion.
- This tab opens only Pixelink. It never connects the BPC303, Stradus, AWG or oscilloscope.
- Camera acquisition has its own worker and lock, so laser commands, AWG programming, stage jogs and scope waits remain responsive.
- Stop affects only Pixelink acquisition; it does not move the stage or change laser/AWG output state.
- Stop acquisition before disconnecting the camera or changing its driver/configuration.

## Common problems
- No image: verify LED illumination, exposure, Pixelink serial number and SDK installation.
- Spot radius jumps: enlarge the ROI, reduce saturation, improve background contrast or prevent stage vibration.
- Guide visible but no spot: the dichroic intentionally attenuates 639 nm; use safe power and exposure settings.
""",
    ),
    "laser": (
        "Stradus laser",
        """# Stradus laser

## Purpose
Prepare, enable and disable the Stradus 639-160 independently from camera acquisition, stage motion and oscilloscope work. Commands run in a background worker so the rest of PCMWriter remains responsive.

## Operating mode
- The Laser tab uses internal feedback-regulated CW (`PUL=0`) with `LE` and `LP` commands.
- It never opens, configures or locks the AWG. Digital pulse generation remains entirely on the AWG and Recipe tabs.
- The Stradus stores the last CW setpoint as `LPS` and starts from that value the next time emission is enabled.
- PCMWriter therefore requires a validated low park power before normal CW operation.

## Controls
- CW setpoint is the requested feedback-regulated laser-head power in milliwatts. It is not automatically the power reaching the sample.
- Park power is the low `LPS` value stored immediately before shutdown and used at the next start.
- Initialize park power is a one-time attended procedure with the beam physically blocked. The laser initially emits at the previously stored LPS, writes the selected park value, verifies it and switches emission off.
- LASER ON refuses to start unless stored `LPS` already matches the park value. It starts at park power, waits for the built-in delay, then writes and verifies the requested CW power.
- LASER OFF first writes and verifies park power, then sends and verifies `LE=0`.
- Refresh status reads identity, fault, interlock, control mode, emission state, modulation mode and stored power values without enabling emission.

## Concurrency
Laser operations use only the Stradus lock. Camera, AWG, stage and scope workers continue independently and never wait for a laser command.

## Safety
Use attended operation, appropriate eyewear and a contained beam. The first park initialization can expose the previously stored power, which may be higher than intended, so the beam must be physically blocked. If the reported state is unknown, use the physical interlock.
""",
    ),
    "awg": (
        "Arbitrary waveform generator",
        """# Arbitrary waveform generator

## Purpose
Control the configured Rigol DG1062Z or Teledyne LeCroy T3AFG350 directly without starting the camera, moving the stage or opening the oscilloscope. Every command is dispatched on a background thread.

## Connection
- Connect opens the configured VISA resource, verifies the instrument identity and immediately disables channel 1 output.
- Disconnect first requests TTL low, disables the output and releases VISA.
- The selected model, VISA resource, channel and 50-ohm load come from Diagnostics.

## DC mode
- Set DC applies the requested level with the output still in its current enabled or disabled state.
- Output ON enables only the electrical output after a confirmation. PCMWriter does not infer or change the state of any externally connected equipment.
- Output OFF first requests the configured TTL-low level and then disables the channel.

## Pulse mode
- Configure pulse uses the width, repetition rate, high level, low level and pulse count shown on this tab.
- Trigger sends one manual N-cycle burst after configuration and output enable.
- Instrument-specific minimum pulse width, maximum repetition rate, duty cycle, train duration and configured safety levels are validated before programming the AWG.

## Concurrency
The AWG has its own lock, so camera frames, laser commands, stage jogs and scope acquisition continue while VISA commands execute. Laser ON/OFF never opens or locks the AWG.

## Safety
The Stradus digital input is 50 ohms. Keep the configured source load at 50 ohms and TTL levels within the validated range. Output OFF does not replace the laser interlock. PCMWriter always attempts DC low before disabling or closing the AWG.
""",
    ),
    "stage": (
        "BPC303 and NanoMax stage",
        """# BPC303 and NanoMax stage

## Purpose
Connect the Thorlabs BPC303 and move the MAX311D/M piezos manually without tying motion to the live-camera loop. Stage commands run independently from camera, laser, AWG and scope workers.

## Connection
- Connect loads the configured Kinesis installation, opens the configured BPC303 serial number and verifies that all three channels are already in closed-loop position mode. PCMWriter never changes control mode or writes a zero position merely by connecting.
- Disconnect releases the controller after any active jog completes.
- The current X, Y and Z positions are read from the controller and displayed in micrometres.

## Manual motion
- Choose a positive step size and use the minus or plus button for each axis.
- Every move waits for X, Y and Z readback to enter the configured position tolerance. A timeout blocks autofocus, alignment or exposure instead of continuing at an uncertain position.
- Each target is calculated from the latest measured position, not from an assumed previous target.
- Configured 0-20 micrometre limits are checked before motion. A target outside the range is rejected without commanding the controller.
- Refresh position performs a read only and also updates the shared Recipe and Autofocus coordinate fields.

## Concurrency
Only one stage command can run at a time, preventing overlapping Kinesis moves. Camera acquisition and other equipment tabs remain responsive because the stage uses a dedicated worker and lock. Workflows that need the same stage must wait until manual control is disconnected.

## Practical guidance
Use small steps near focus and confirm objective clearance before connecting or moving. Direction-dependent rebound can be mechanical hysteresis or closed-loop settling; approach critical coordinates consistently and allow the configured settling time. The displayed coordinate after a jog is the controller readback.

## Safety
The software limits do not detect a physical collision. Keep the objective and sample visible during initial motion, start from a known position and use the controller hardware controls if motion behaves unexpectedly.
""",
    ),
    "scope": (
        "Rigol MSO7054 oscilloscope",
        """# Rigol MSO7054 oscilloscope

## Purpose
Connect, configure, arm and acquire from the Rigol MSO7054 independently from camera display, stage motion, laser preparation and AWG programming. VISA work runs outside the Tk event loop.

## Connection
- Connect opens the configured VISA resource and verifies the oscilloscope identity.
- Disconnect closes VISA after any active acquisition finishes.
- Channel, impedance, coupling, bandwidth limit and trigger settings are taken from Diagnostics.

## Pulse setup
- Enter the expected optical pulse width and select Configure to calculate a conservative horizontal window and vertical scale using the existing scope configuration.
- Arm single places the instrument in single-sequence acquisition with the configured edge trigger.
- Acquire waits for completion, downloads channel data and reports sample count, time span and voltage range.

## Trigger guidance
The photodiode is connected to the configured channel. Trigger level must lie inside the observed pulse amplitude. If capture times out, check the detector signal, cabling, channel impedance and trigger slope before increasing the timeout. A pulse touching the acquisition-window boundary means the horizontal scale is too narrow.

## Concurrency
The scope owns only its own VISA lock. Long waits for a trigger do not block camera rendering or manual stage operations. A second scope command reports the device as busy rather than creating a competing VISA transaction.

## Safety and data quality
This tab does not enable the AWG or laser. Arm the scope before generating a pulse. Use 50-ohm termination only when compatible with the detector output and cabling. Manual acquisitions are diagnostic and are not automatically added to a recipe result folder.
""",
    ),
    "thermal": (
        "Thermal simulation",
        """# Thermal simulation

## Purpose
Estimate the transient temperature produced by a Gaussian 639 nm pulse train in the multilayer sample. The solver is a planning and sensitivity tool; it is not an experimentally validated exposure recipe.

## Modelled stack
- 200 nm SiO2 cover.
- 40 nm Sb2Se3 active layer.
- 220 nm silicon waveguide layer.
- 3 micrometre SiO2 BOX.
- 20 micrometres of silicon substrate in the thermal domain. The physical wafer is 500 micrometres thick; its full thickness is not meshed.

## Inputs
- Power is optical power incident on the sample.
- Pulse duration, pulse count and repetition rate define the temporal source.
- Spot radius is the Gaussian 1/e2 radius w in I(r)=2P/(pi w^2) exp(-2r^2/w^2).
- Phase selects the measured optical constants and thermal conductivity for amorphous or crystalline Sb2Se3.
- Radial cells and domain radius control the axisymmetric mesh. Increase resolution only after a coarser convergence check.

## Calculation
- A transfer-matrix model computes reflection, transmission and absorption in the optical stack at 639 nm.
- The absorbed power is deposited with a radial Gaussian profile in the finite layers.
- A transient 2D axisymmetric finite-volume solver resolves radius and depth through the pulse train.
- The outer radial boundary and truncated substrate back face are held at ambient temperature.

## Panels
- Simulated geometry illustrates the layer thicknesses and laser source.
- Finite-volume mesh shows the actual radial/depth discretization near the active layer.
- Sb2Se3 transient plots the active-layer temperature versus time and phase-change reference thresholds.
- T(r,z) shows the temperature field at the instant of maximum Sb2Se3 temperature.

## Interpretation
- Compare peak temperature, time above crystallization and melting thresholds, and spatial confinement.
- Repeat with plausible power, spot-size and material-property bounds; a single prediction is not a safe operating point.
- Compare mesh refinements and larger domains. A meaningful result should not change materially.

## Current limitations
- Constant material properties, ideal interfaces and a circular time-independent spot.
- No interfacial thermal resistance, latent heat, crystallization kinetics, stress or material damage model.
- Optical constants come from measured 32 nm films but are applied to the nominal 40 nm layer.
- The displayed fast estimate is only a screening comparison; the axisymmetric solution is the detailed model.

## Safety
The solver never arms hardware, moves the stage or enables the laser. Validate spot size, sample power and phase thresholds experimentally before using predictions in the laboratory.
""",
    ),
    "focus": (
        "Spot and autofocus",
        """# Spot and autofocus

## Purpose
Measure the laser spot, focus the image along Z, calibrate camera pixels against MAX stage motion and map sample tilt for automatic Z correction during a raster.

## Spot measurement
- The algorithm selects the image channel with the strongest contrast, removes the background and estimates the centroid and principal axes.
- wx and wy are Gaussian 1/e2 radii calculated from second moments.
- The measurement is rejected when SNR is too low, the image is saturated or the feature is clipped by the ROI.
- Enter a calibrated micrometre-per-pixel value before applying the measured radius to the thermal model.

## Autofocus
1. Set the current centre Z, sweep span and number of images.
2. Confirm that the visible spot is below the switching threshold and does not saturate the camera.
3. The stage scans Z in increasing order and measures wx^2+wy^2 at every position.
4. A quadratic fit estimates the minimum; the stage moves to that Z only when the fitted vertex is inside the measured interval.
5. Review the curve and fitted focus before reusing the result.

## Pixel-to-stage calibration
- Use LED illumination only and keep the pump laser off.
- The routine records a reference image, moves +X and +Y by the requested step, registers each image by phase correlation and returns to the origin.
- It stores the full 2x2 pixel/stage transform, including rotation and axis inversion, plus a scalar display scale.
- Low registration SNR, poor return accuracy or excessive anisotropy invalidates the calibration.

## Focus-plane map
- Map focus samples the four corners and centre of the Recipe raster area.
- It fits z(x,y)=a x+b y+c and reports fit quality.
- A valid plane is applied automatically to later Recipe and Waveguide writing positions so a tilted sample stays focused.
- Remap after remounting the sample, changing the objective or disturbing the optical path.

## Safety and prerequisites
- In hardware mode, stage calibration and the safe-spot confirmation are mandatory.
- Make sure the entire Z sweep is inside stage limits and cannot drive the objective into the sample.
- Autofocus uses image quality only; it cannot detect mechanical collision or prove that optical power is non-destructive.

## Practical checks
- Use an odd image count and enough span to see the focus metric rise on both sides of the minimum.
- Keep exposure fixed during one sweep when possible.
- If the curve is flat or irregular, improve illumination/ROI and do not accept the fitted Z.
""",
    ),
    "guide": (
        "Waveguide writing",
        """# Waveguide writing

## Purpose
Detect one visible silicon waveguide, create an arbitrary requested switching length and move the sample under the fixed laser spot while correcting X, Y and Z before every pulse.

## Required preparation
1. Turn on the white LED so the guide is visible; keep the pump laser off during structural detection.
2. Complete pixel-to-stage calibration and verify stage axes and limits.
3. Register the fixed laser spot at safe, non-switching power.
4. Set the pulse recipe in the Recipe tab.
5. Select an ROI containing one approximately straight guide with clear edges.

## Planning controls
- Length to switch accepts any positive length, for example 1, 3 or 7 micrometres.
- Maximum step limits spacing between exposures. The planner adds an exact final point so the requested length is not rounded down.
- Direction chooses which detected guide direction is followed from the starting point.
- Autofocus span and samples control focus measurements at the planned endpoints.
- Spot X/Y identifies the registered pump position in camera pixels.
- XY tolerance is the residual alignment error accepted before firing.
- Maximum correction bounds the closed-loop transverse stage correction at each point.

## Preview sequence
- Detect the guide centreline, angle, visible endpoints and confidence.
- Transform camera geometry into MAX coordinates.
- Generate all exposure points along the requested length.
- Autofocus at both ends and interpolate Z between them, including any saved focus-plane correction.
- Display the guide, fixed spot and planned points. Writing remains blocked until this preview is valid.

## Writing sequence
- At each point, move to the nominal XYZ position.
- Capture a fresh image, redetect the guide near the registered spot and correct transverse XY error.
- Stop before firing if confidence is low, correction exceeds the limit or alignment does not converge.
- Trigger the Recipe pulse and record the actual position and local before/after color measurement.
- Cancel stops at the next safe checkpoint and disables output.

## Invalidated plans
Changing the ROI, requested length, step, direction, focus settings, spot registration or alignment limits invalidates the preview. Generate and inspect a new plan before writing.

## Limitations and safety
- The current detector is intended for one approximately straight guide, not bends, crossings or multiple ambiguous structures.
- Hardware mode requires Hardware armed, Stage calibrated, valid sample-power calibration and an in-range plan.
- Preview geometry does not prove the pulse is safe. Establish switching and damage thresholds on sacrificial material first.
""",
    ),
    "diagnostics": (
        "Diagnostics",
        """# Diagnostics

## Purpose
Configure device connections, verify drivers and communications, define safety gates and run a non-actuating preflight before laboratory operation.

## Safe preflight behaviour
- Diagnostics forces AWG channel 1 and the Stradus output off before identification.
- It verifies the AWG output (and the Rigol AWG error queue), complete Stradus safety state, oscilloscope impedance/settings/error queue, BPC303 closed-loop state and position, and camera settings.
- It reports READY, MISSING or BLOCKED and writes a timestamped JSON report in the results folder.
- It does not move the MAX stage, fire a pulse or treat a thermal prediction as authorization.

## Connection fields
- Select simulation for offline work or hardware for real devices. Hardware armed remains the independent safety gate for motion and optical pulses.
- Scan PC performs read-only VISA, Stradus USB HID, Kinesis, Pixelink and Windows PnP enumeration. PnP exposes connected equipment even when its driver is missing. The scan never moves the stage or enables an optical/electrical output.
- Choose the detected resources in the editable lists, save the configuration, then run the safe preflight.
- AWG model selects either the Teledyne LeCroy T3AFG350 or Rigol DG1062Z driver; AWG VISA identifies its USB/LAN resource.
- Stradus USB HID identifies current lasers directly; RS232 VISA remains available for legacy heads.
- Scope VISA identifies the Rigol MSO7054.
- BPC303 serial and Pixelink serial select the stage controller and camera.
- Leave automatic discovery only where it is unambiguous; fixed laboratory configurations should use explicit identifiers.

## Power calibration
- Sample power:PP pairs use sample_mW:PP_mW syntax, for example 5:20,10:33,15:46.
- Both axes must increase strictly. Hardware recipes interpolate only between measured points and never extrapolate.
- Manual PP is a simulation fallback, not a substitute for measured sample-power calibration.

## Camera and scope settings
- Camera exposure, gain, ROI and anti-saturation auto exposure are shared with live acquisition.
- The scope uses channel 1 only, DC coupling and 50-ohm input for the DET02AFC. Preflight blocks arming if the configured or measured impedance is incompatible.
- V/div, trigger level and slope must match the DET02AFC pulse polarity and amplitude.
- Recipe acquisition derives time/div from pulse duration, keeps pre-trigger baseline and uses single acquisition.

## Safety gates
- Stage calibrated means the real origin, axis directions, travel and safe Z range have been physically verified. Do not enable it from documentation alone.
- Hardware armed is enabled only after every preflight item is READY and only for the current application session. It is never stored as true in config.json.
- Successful recipes and pulses automatically disarm the session. Equipment errors invalidate the preflight; intentional disconnects disarm but retain the valid preflight.
- Changing a connection or rerunning Diagnostics disarms the session; run preflight again before arming the exact new configuration.
- Saving configuration does not test hardware. Run diagnostics after every connection or driver change.

## Recommended first laboratory session
1. Install vendor drivers and confirm all devices in their vendor tools.
2. Run Diagnostics with Hardware armed off.
3. Verify stage motion without a valuable sample, including directions and limits.
4. Verify TTL timing with laser emission disabled.
5. Measure low-power optical pulses with the DET02AFC and tune scope scale/trigger.
6. Calibrate power at the sample and camera pixel-to-stage motion.
7. Test switching on sacrificial material before arming automated recipes.

## Troubleshooting
- MISSING usually indicates an incorrect resource/serial, disconnected device or absent vendor driver.
- BLOCKED means a required safety or calibration condition is not satisfied.
- If identification is unexpected, stop and correct the address instead of accepting the device.
- Keep Hardware armed off after any software, cabling, optical or mechanical change until preflight is repeated.
""",
    ),
}


TAB_FIELD_HELP = {
    "recipe": (
        "Name", "Sample power (mW)", "Pulse duration (µs)", "Repetition rate (Hz)",
        "Pulse count", "TTL high (V)", "Center X (µm)", "Center Y (µm)", "Z (µm)",
        "Raster width (µm)", "Raster height (µm)", "X points", "Y points",
    ),
    "thermal": (
        "Power (mW)", "Pulse (µs)", "Spot radius (µm)", "Thermal pulse count",
        "Thermal repetition rate (Hz)", "Phase", "Radial cells", "Domain radius (µm)",
    ),
    "focus": (
        "Center Z (µm)", "Z sweep (µm)", "Image count", "Scale (µm/pixel; 0=unknown)",
        "XY calibration step (µm)",
        "Spot visible without saturation\nand power below switching threshold",
    ),
    "camera": ("ROI left,top,width,height", "Scale (µm/pixel; 0=unknown)"),
    "laser": ("CW setpoint LP (mW)", "Park power (mW)"),
    "awg": (
        "DC level (V)", "Pulse width (s)", "Repetition (Hz)", "High level (V)",
        "Low level (V)", "Pulse count",
    ),
    "stage": ("Step (um)",),
    "scope": ("Expected pulse width (s)",),
    "guide": (
        "Waveguide ROI left,top,w,h", "Length to switch (µm)", "Maximum step (µm)",
        "Direction", "Autofocus span (µm)", "Autofocus samples", "Spot X (pixel)",
        "Spot Y (pixel)", "XY tolerance (µm)", "Maximum XY correction (µm)",
    ),
    "diagnostics": (
        "Mode", "AWG model", "AWG VISA", "Stradus USB/RS232", "Manual Stradus PP (mW)",
        "Sample power:PP calibration", "Oscilloscope VISA", "BPC303 serial",
        "Pixelink serial (0=single)", "Exposure (ms)", "Gain (dB)",
        "ROI left,top,width,height", "Anti-saturation auto exposure", "Scope V/div",
        "Trigger (V)", "Scope input", "Trigger slope", "Hardware armed for this session",
        "Position tolerance (µm)", "Move timeout (s)",
        "Stage calibrated (limits and directions confirmed)",
    ),
}


def _live_preview_samples(
    image: np.ndarray, max_dimension: int = 1200, max_histogram_pixels: int = 250_000
) -> tuple[np.ndarray, np.ndarray, int]:
    stride = max(1, int(np.ceil(max(image.shape[:2]) / max_dimension)))
    preview = image[::stride, ::stride]
    histogram_stride = max(
        1, int(np.ceil(np.sqrt(preview.shape[0] * preview.shape[1] / max_histogram_pixels)))
    )
    return preview, preview[::histogram_stride, ::histogram_stride], stride


class PumpAutoUI:
    def __init__(self, root: tk.Tk, config_path: str | Path = "config.json") -> None:
        self.root = root
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        self.config["safety"]["hardware_armed"] = False
        self.preflight_config: str | None = None
        self._workers: set[threading.Thread] = set()
        self._closing = False
        self.safe_all_event = threading.Event()
        self._ui_queue: SimpleQueue[Any] = SimpleQueue()
        self._tooltip_job: str | None = None
        self._tooltip_window: tk.Toplevel | None = None
        root.title("PCMWriter - Sb2Se3 optical pumping")
        icon_path = Path(__file__).resolve().parent / "assets" / "pcmwriter_icon.png"
        if icon_path.exists():
            self.app_icon = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, self.app_icon)
        width = min(1180, root.winfo_screenwidth() - 80)
        height = min(800, root.winfo_screenheight() - 100)
        root.geometry(
            f"{width}x{height}+{max(20, (root.winfo_screenwidth() - width) // 2)}"
            f"+{max(20, (root.winfo_screenheight() - height) // 2)}"
        )
        root.minsize(min(1040, width), min(700, height))
        self._configure_style()

        header = ttk.Frame(root, style="Header.TFrame", padding=(22, 10))
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="PCMWriter", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Optical pumping control and characterization for Sb2Se3",
            style="HeaderSubtitle.TLabel",
        ).grid(row=0, column=1, sticky="w", padx=(14, 0), pady=(5, 0))
        self.mode_text = tk.StringVar()
        self._update_mode_status()
        ttk.Label(header, textvariable=self.mode_text, style="Status.TLabel").grid(row=0, column=2, sticky="e")
        self.safe_all_button = ttk.Button(
            header, text="SAFE ALL", command=self._safe_all, style="Danger.TButton"
        )
        self.safe_all_button.grid(row=0, column=3, sticky="e", padx=(10, 0))
        self.root.after(20, self._drain_ui_queue)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(8, 8))
        self.setup_tab = ttk.Frame(self.notebook, padding=(8, 8))
        self.operate_tab = ttk.Frame(self.notebook, padding=(8, 8))
        self.run_tab = ttk.Frame(self.notebook, padding=(8, 8))
        self.notebook.add(self.setup_tab, text="1  Setup & Safety")
        self.notebook.add(self.operate_tab, text="2  Align & Pulse")
        self.notebook.add(self.run_tab, text="3  Run")

        self.setup_notebook = ttk.Notebook(self.setup_tab)
        self.setup_notebook.pack(fill="both", expand=True)
        self.diag_tab = ttk.Frame(self.setup_notebook, padding=(14, 10))
        self.thermal_tab = ttk.Frame(self.setup_notebook, padding=(14, 10))
        self.setup_notebook.add(self.diag_tab, text="Diagnostics")
        self.setup_notebook.add(self.thermal_tab, text="Thermal Model")

        self.operate_notebook = ttk.Notebook(self.operate_tab)
        self.operate_notebook.pack(fill="both", expand=True)
        self.dashboard_tab = ttk.Frame(self.operate_notebook, padding=(8, 8))
        self.focus_tab = ttk.Frame(self.operate_notebook, padding=(14, 10))
        self.operate_notebook.add(self.dashboard_tab, text="Hardware Dashboard")
        self.operate_notebook.add(self.focus_tab, text="Autofocus")

        self.run_notebook = ttk.Notebook(self.run_tab)
        self.run_notebook.pack(fill="both", expand=True)
        self.recipe_tab = ttk.Frame(self.run_notebook, padding=(14, 10))
        self.guide_tab = ttk.Frame(self.run_notebook, padding=(14, 10))
        self.run_notebook.add(self.recipe_tab, text="Recipe")
        self.run_notebook.add(self.guide_tab, text="Waveguide")

        self.help_groups = (
            (self.setup_notebook, ("diagnostics", "thermal")),
            (self.operate_notebook, ("dashboard", "focus")),
            (self.run_notebook, ("recipe", "guide")),
        )

        dashboard_content = self._add_tab_help(
            self.dashboard_tab,
            "dashboard",
            "Align the sample and prepare optical pulses from one hardware overview.",
        )
        dashboard_canvas = tk.Canvas(
            dashboard_content,
            highlightthickness=0,
            background=self.colors["background"],
        )
        dashboard_scroll = ttk.Scrollbar(
            dashboard_content, orient="vertical", command=dashboard_canvas.yview
        )
        dashboard_canvas.configure(yscrollcommand=dashboard_scroll.set)
        dashboard_canvas.pack(side="left", fill="both", expand=True)
        dashboard_scroll.pack(side="right", fill="y")
        dashboard = ttk.Frame(dashboard_canvas, padding=(4, 0, 10, 16))
        dashboard_window = dashboard_canvas.create_window((0, 0), window=dashboard, anchor="nw")
        dashboard.bind(
            "<Configure>",
            lambda _event: dashboard_canvas.configure(scrollregion=dashboard_canvas.bbox("all")),
        )
        dashboard_canvas.bind(
            "<Configure>",
            lambda event: dashboard_canvas.itemconfigure(dashboard_window, width=event.width),
        )

        def scroll_dashboard(event: tk.Event[Any]) -> None:
            x, y = root.winfo_pointerx(), root.winfo_pointery()
            inside = (
                dashboard_canvas.winfo_rootx() <= x < dashboard_canvas.winfo_rootx() + dashboard_canvas.winfo_width()
                and dashboard_canvas.winfo_rooty() <= y < dashboard_canvas.winfo_rooty() + dashboard_canvas.winfo_height()
            )
            if inside and self.notebook.index("current") == 1 and self.operate_notebook.index("current") == 0:
                steps = max(1, abs(int(event.delta / 120)))
                dashboard_canvas.yview_scroll(-steps if event.delta > 0 else steps, "units")

        root.bind_all("<MouseWheel>", scroll_dashboard, add="+")
        dashboard.columnconfigure(0, weight=1, uniform="device")
        dashboard.columnconfigure(1, weight=1, uniform="device")
        for row, title, description in (
            (0, "IMAGING", "Live sample view and spot measurement"),
            (2, "ALIGNMENT HARDWARE", "Position the sample and locate the fixed optical spot"),
            (4, "PULSE HARDWARE", "Generate and verify the electrical modulation pulse"),
        ):
            section = ttk.Frame(dashboard)
            section.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=(14, 6))
            ttk.Label(section, text=title, style="SectionTitle.TLabel").pack(side="left")
            ttk.Label(section, text=description, style="Muted.TLabel").pack(side="left", padx=(12, 0))
            ttk.Separator(section).pack(side="left", fill="x", expand=True, padx=(14, 0))

        self.camera_tab = ttk.LabelFrame(
            dashboard, text="CAMERA  ·  Pixelink M18-CYL", padding=(12, 10), style="DeviceCard.TLabelframe"
        )
        self.stage_tab = ttk.LabelFrame(
            dashboard, text="STAGE  ·  BPC303 / MAX311D", padding=(12, 10), style="DeviceCard.TLabelframe"
        )
        self.laser_tab = ttk.LabelFrame(
            dashboard, text="LASER  ·  Stradus 639-160", padding=(12, 10), style="DeviceCard.TLabelframe"
        )
        self.awg_tab = ttk.LabelFrame(
            dashboard, text="AWG  ·  DG1062Z / T3AFG350", padding=(12, 10), style="DeviceCard.TLabelframe"
        )
        self.scope_tab = ttk.LabelFrame(
            dashboard, text="SCOPE  ·  Rigol MSO7054", padding=(12, 10), style="DeviceCard.TLabelframe"
        )
        self.camera_tab.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=8, pady=(0, 10))
        self.stage_tab.grid(row=3, column=0, sticky="nsew", padx=(8, 6), pady=(0, 10))
        self.laser_tab.grid(row=3, column=1, sticky="nsew", padx=(6, 8), pady=(0, 10))
        self.awg_tab.grid(row=5, column=0, sticky="nsew", padx=(8, 6), pady=(0, 10))
        self.scope_tab.grid(row=5, column=1, sticky="nsew", padx=(6, 8), pady=(0, 10))

        self.run_body = self._add_tab_help(
            self.recipe_tab, "recipe", "Configure, execute and trace a point exposure or raster."
        )
        self.camera_body = self._add_tab_help(
            self.camera_tab, "camera", "Live image · ROI · spot profile · saturation"
        )
        self.laser_body = self._add_tab_help(
            self.laser_tab, "laser", "Internal CW control · independent from AWG"
        )
        self.awg_body = self._add_tab_help(
            self.awg_tab, "awg", "DC and TTL pulse generation · manual trigger"
        )
        self.stage_body = self._add_tab_help(
            self.stage_tab, "stage", "Manual XYZ motion · closed-loop position"
        )
        self.scope_body = self._add_tab_help(
            self.scope_tab, "scope", "Single-shot pulse acquisition · trigger verification"
        )
        self.thermal_body = self._add_tab_help(
            self.thermal_tab, "thermal", "Explore the axisymmetric multilayer optothermal model."
        )
        self.focus_body = self._add_tab_help(
            self.focus_tab, "focus", "Measure the spot, autofocus and map sample tilt."
        )
        self.guide_body = self._add_tab_help(
            self.guide_tab, "guide", "Detect a guide, preview the path and write with feedback."
        )
        self.diag_body = self._add_tab_help(
            self.diag_tab, "diagnostics", "Configure connections and run the safe hardware preflight."
        )
        self._build_run()
        self._build_thermal()
        self._build_focus()
        self._build_camera()
        self._build_laser()
        self._build_awg()
        self._build_stage()
        self._build_scope()
        self._build_guide()
        self._build_diagnostics()
        self._update_mode_status()
        root.bind("<F1>", self._show_current_help)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _update_mode_status(self) -> None:
        if self.config["mode"] == "simulation":
            text = "SIMULATION  |  NO HARDWARE OUTPUT"
        elif self.config["safety"]["hardware_armed"]:
            text = "HARDWARE  |  ARMED THIS SESSION"
        elif self.preflight_config is not None:
            text = "HARDWARE  |  PREFLIGHT READY"
        else:
            text = "HARDWARE  |  DISARMED"
        if hasattr(self, "mode_text"):
            self.mode_text.set(text)
        if hasattr(self, "hardware_status_text"):
            self.hardware_status_text.set(text.replace("  |  ", " | ").title())

    def _configure_style(self) -> None:
        self.colors = {
            "background": "#f3f6f8",
            "surface": "#ffffff",
            "text": "#16324f",
            "muted": "#60758a",
            "accent": "#128987",
            "accent_active": "#0d6f6e",
            "danger": "#c84b52",
        }
        self.root.configure(background=self.colors["background"])
        self.root.option_add("*Font", "{Segoe UI} 10")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.colors["background"])
        style.configure("TLabel", background=self.colors["background"], foreground=self.colors["text"])
        style.configure("Header.TFrame", background=self.colors["text"])
        style.configure("Header.TLabel", background=self.colors["text"])
        style.configure(
            "HeaderTitle.TLabel",
            background=self.colors["text"],
            foreground="white",
            font=("Segoe UI Semibold", 18),
        )
        style.configure(
            "HeaderSubtitle.TLabel",
            background=self.colors["text"],
            foreground="#c9d6e2",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Status.TLabel",
            background=self.colors["accent"],
            foreground="white",
            padding=(12, 6),
            font=("Segoe UI Semibold", 9),
        )
        style.configure("Muted.TLabel", foreground=self.colors["muted"])
        style.configure(
            "TLabelframe",
            background=self.colors["background"],
            bordercolor="#dbe3ea",
            relief="solid",
        )
        style.configure(
            "TLabelframe.Label",
            background=self.colors["background"],
            foreground=self.colors["text"],
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "DeviceCard.TLabelframe",
            background=self.colors["background"],
            bordercolor="#9fb5c5",
            borderwidth=2,
            relief="solid",
        )
        style.configure(
            "DeviceCard.TLabelframe.Label",
            background=self.colors["background"],
            foreground=self.colors["text"],
            font=("Segoe UI Semibold", 11),
            padding=(6, 3),
        )
        style.configure(
            "SectionTitle.TLabel",
            foreground=self.colors["accent_active"],
            font=("Segoe UI Semibold", 9),
        )
        style.configure("TNotebook", background=self.colors["background"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), font=("Segoe UI Semibold", 9), borderwidth=0)
        style.map(
            "TNotebook.Tab",
            background=[("selected", self.colors["surface"]), ("!selected", "#dde6ec")],
            foreground=[("selected", self.colors["accent"]), ("!selected", self.colors["muted"])],
        )
        style.configure(
            "TButton",
            padding=(12, 6),
            font=("Segoe UI Semibold", 9),
            background=self.colors["surface"],
            foreground=self.colors["text"],
            bordercolor="#c8d5df",
            lightcolor=self.colors["surface"],
            darkcolor=self.colors["surface"],
            relief="flat",
            borderwidth=1,
            focusthickness=2,
            focuscolor=self.colors["accent"],
        )
        style.map(
            "TButton",
            background=[("pressed", "#dcebea"), ("active", "#edf5f4"), ("disabled", "#eef2f4")],
            foreground=[("disabled", "#93a3b1")],
            bordercolor=[("focus", self.colors["accent"]), ("active", "#8abfbd")],
        )
        style.configure(
            "Accent.TButton",
            background=self.colors["accent"],
            foreground="white",
            bordercolor=self.colors["accent"],
            lightcolor=self.colors["accent"],
            darkcolor=self.colors["accent"],
        )
        style.map(
            "Accent.TButton",
            background=[("pressed", "#095f5e"), ("active", self.colors["accent_active"]), ("disabled", "#9fbdbc")],
            foreground=[("disabled", "#edf5f4")],
            bordercolor=[("focus", "#72c8c4"), ("disabled", "#9fbdbc")],
        )
        style.configure(
            "Danger.TButton",
            background=self.colors["danger"],
            foreground="white",
            bordercolor=self.colors["danger"],
            lightcolor=self.colors["danger"],
            darkcolor=self.colors["danger"],
        )
        style.map(
            "Danger.TButton",
            background=[("pressed", "#8e3138"), ("active", "#a83d44"), ("disabled", "#d7afb2")],
            foreground=[("disabled", "#f8eeee")],
            bordercolor=[("focus", "#ef9da2"), ("disabled", "#d7afb2")],
        )
        style.configure(
            "Help.TButton",
            padding=(10, 4),
            background="#e7f4f3",
            foreground=self.colors["accent_active"],
            bordercolor="#9bcac8",
            lightcolor="#e7f4f3",
            darkcolor="#e7f4f3",
        )
        style.map(
            "Help.TButton",
            background=[("pressed", "#cfe7e5"), ("active", "#d9ecea")],
            bordercolor=[("focus", self.colors["accent"]), ("active", self.colors["accent"])],
        )
        style.configure("TEntry", fieldbackground="white", padding=(5, 3))

    def _add_tab_help(self, tab: ttk.Frame, key: str, summary: str) -> ttk.Frame:
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text=summary, style="Muted.TLabel").pack(side="left", anchor="center")
        ttk.Button(
            bar,
            text="?  Help",
            command=lambda: self._show_help(key),
            style="Help.TButton",
        ).pack(side="right")
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)
        return body

    def _show_current_help(self, _event: tk.Event[Any] | None = None) -> str:
        child_notebook, keys = self.help_groups[self.notebook.index("current")]
        key = keys[0] if child_notebook is None else keys[child_notebook.index("current")]
        self._show_help(key)
        return "break"

    def _show_help(self, key: str) -> None:
        title, content = HELP_PAGES[key]
        fields = TAB_FIELD_HELP.get(key, ())
        if fields:
            content += "\n\n## Editable parameters\n" + "\n".join(
                f"- {field.replace(chr(10), ' ')}: {self._field_help(field)}" for field in fields
            )
        dialog = tk.Toplevel(self.root)
        dialog.title(f"PCMWriter Help - {title}")
        screen_width = dialog.winfo_screenwidth()
        screen_height = dialog.winfo_screenheight()
        width = min(780, screen_width - 60)
        height = min(720, screen_height - 80)
        dialog.geometry(
            f"{width}x{height}+{max(20, (screen_width - width) // 2)}"
            f"+{max(20, (screen_height - height) // 2)}"
        )
        dialog.minsize(min(620, width), min(500, height))
        dialog.transient(self.root)

        outer = ttk.Frame(dialog, padding=12)
        outer.pack(fill="both", expand=True)

        text_frame = ttk.Frame(outer)
        text_frame.pack(fill="both", expand=True)
        text = tk.Text(
            text_frame,
            wrap="word",
            background="#fbfcfe",
            foreground=self.colors["text"],
            relief="solid",
            borderwidth=1,
            padx=18,
            pady=14,
            cursor="arrow",
        )
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._insert_help_text(text, content)
        text.configure(state="disabled")

        ttk.Button(outer, text="Close", command=dialog.destroy).pack(anchor="e", pady=(8, 0))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.focus_set()

    def _insert_help_text(self, widget: tk.Text, content: str) -> None:
        widget.tag_configure("h1", font=("Segoe UI Semibold", 20), foreground=self.colors["text"], spacing3=12)
        widget.tag_configure(
            "h2",
            font=("Segoe UI Semibold", 13),
            foreground=self.colors["accent"],
            spacing1=12,
            spacing3=5,
        )
        widget.tag_configure("body", font=("Segoe UI", 10), lmargin2=0, spacing3=4)
        widget.tag_configure("list", font=("Segoe UI", 10), lmargin1=18, lmargin2=36, spacing3=4)
        widget.tag_configure("number", font=("Segoe UI", 10), lmargin1=18, lmargin2=36, spacing3=4)

        for raw_line in content.strip().splitlines():
            line = raw_line.rstrip()
            if line.startswith("# "):
                widget.insert("end", f"{line[2:]}\n", "h1")
            elif line.startswith("## "):
                widget.insert("end", f"{line[3:]}\n", "h2")
            elif line.startswith("- "):
                widget.insert("end", f"\u2022  {line[2:]}\n", "list")
            elif line[:1].isdigit() and ". " in line[:4]:
                widget.insert("end", f"{line}\n", "number")
            elif line:
                widget.insert("end", f"{line}\n", "body")
            else:
                widget.insert("end", "\n", "body")

    def _spawn_worker(self, target: Any) -> None:
        thread: threading.Thread

        def run() -> None:
            try:
                target()
            finally:
                self._workers.discard(thread)

        thread = threading.Thread(target=run)
        self._workers.add(thread)
        thread.start()

    def _post_ui(self, callback: Any) -> None:
        """Pass a callback to Tk without calling Tk from a worker thread."""
        if hasattr(self, "_ui_queue"):
            self._ui_queue.put(callback)
        else:
            self.root.after(0, callback)

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                self._ui_queue.get_nowait()()
        except Empty:
            pass
        if not self._closing:
            self.root.after(20, self._drain_ui_queue)

    def _reserve_resources(self, title: str, *names: str) -> list[threading.Lock] | None:
        acquired: list[threading.Lock] = []
        for name in sorted(names):
            lock = getattr(self, f"{name}_lock")
            if not lock.acquire(blocking=False):
                for held in reversed(acquired):
                    held.release()
                messagebox.showerror(title, f"{name.title()} is busy with another operation.")
                return None
            acquired.append(lock)
        connected = [name for name in names if getattr(self, f"{name}_device", None) is not None]
        if connected:
            for held in reversed(acquired):
                held.release()
            messagebox.showerror(
                title,
                "Disconnect dashboard sessions before automated operation: " + ", ".join(connected),
            )
            return None
        return acquired

    @staticmethod
    def _release_resources(locks: list[threading.Lock]) -> None:
        for lock in reversed(locks):
            lock.release()

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.safe_all_event.set()
        self._disarm_session(invalidate_preflight=True)
        self.mode_text.set("SHUTTING DOWN  |  WAITING FOR SAFE STATE")
        for name in ("cancel_event", "guide_cancel_event", "live_stop_event"):
            event = getattr(self, name, None)
            if event is not None:
                event.set()
        self.root.after(25, self._finish_close)

    def _finish_close(self) -> None:
        if any(thread.is_alive() for thread in list(self._workers)):
            self.root.after(50, self._finish_close)
            return

        self._close_dashboard_devices(park_laser=True)
        self.root.destroy()

    def _close_dashboard_devices(self, park_laser: bool) -> list[str]:
        errors: list[str] = []
        try:
            if getattr(self, "live_system", None) is not None:
                device = self.live_system
                try:
                    device.close()
                finally:
                    self.live_system = None
        except Exception as exc:
            errors.append(f"camera: {exc}")
        try:
            if getattr(self, "laser_device", None) is not None:
                device = self.laser_device
                try:
                    if park_laser:
                        device.disable_internal_cw(float(self.cw_park.get()))
                finally:
                    try:
                        device.close()
                    finally:
                        self.laser_device = None
        except Exception as exc:
            errors.append(f"laser: {exc}")
        try:
            if getattr(self, "awg_device", None) is not None:
                device = self.awg_device
                try:
                    device.configure_dc(float(self.config["awg"]["low_v"]))
                    device.output(False)
                finally:
                    try:
                        device.close()
                    finally:
                        self.awg_device = None
        except Exception as exc:
            errors.append(f"AWG: {exc}")
        for name in ("stage_device", "scope_device"):
            try:
                device = getattr(self, name, None)
                if device is not None:
                    try:
                        device.close()
                    finally:
                        setattr(self, name, None)
            except Exception as exc:
                errors.append(f"{name.removesuffix('_device')}: {exc}")
        return errors

    def _disarm_session(self, invalidate_preflight: bool = False) -> None:
        self.config["safety"]["hardware_armed"] = False
        if hasattr(self, "cfg_armed"):
            self.cfg_armed.set(False)
        if invalidate_preflight:
            self.preflight_config = None
        if hasattr(self, "armed_check"):
            self.armed_check.configure(
                state="normal" if self.preflight_config is not None else "disabled"
            )
        self._update_mode_status()

    def _safe_all(self) -> None:
        self.safe_all_button.configure(state="disabled")
        self.safe_all_event.set()
        self._disarm_session(invalidate_preflight=True)
        self.mode_text.set("SAFE ALL REQUESTED  |  STOPPING OUTPUTS")
        for name in ("cancel_event", "guide_cancel_event", "live_stop_event"):
            event = getattr(self, name, None)
            if event is not None:
                event.set()

        def work() -> None:
            locks = [
                getattr(self, f"{name}_lock")
                for name in ("awg", "camera", "laser", "scope", "stage")
            ]
            for lock in locks:
                lock.acquire()
            try:
                errors = self._close_dashboard_devices(park_laser=False)
            finally:
                self._release_resources(locks)
            self._post_ui(lambda: self._safe_all_complete(errors))

        self._spawn_worker(work)

    def _safe_all_complete(self, errors: list[str]) -> None:
        self.awg_output_enabled = False
        self.awg_pulse_configured = False
        self._set_cw_controls(True)
        self._set_live_stage_controls("disabled")
        self.awg_status.set("Disconnected | output OFF requested")
        self.cw_status.set("Internal CW: OFF requested")
        self.scope_status.set("Disconnected by SAFE ALL.")
        self.live_stage_position.set("Stage: disconnected by SAFE ALL")
        self.safe_all_button.configure(state="normal")
        self._update_mode_status()
        message = "SAFE ALL complete. Hardware remains disarmed."
        if errors:
            message += " Verify the physical interlock; shutdown errors: " + "; ".join(errors)
        self._write_log(message)

    def _field_help(self, label: str) -> str:
        safety = self.config["safety"]
        ranges = self.config["stage"]["range_um"]
        camera = self.config["camera"]
        values = {
            "max_power": min(160.0, float(safety["max_optical_power_mw"])),
            "min_width_s": float(safety["min_pulse_width_s"]),
            "max_width_s": float(safety["max_pulse_width_s"]),
            "min_width_us": float(safety["min_pulse_width_s"]) * 1e6,
            "max_width_us": float(safety["max_pulse_width_s"]) * 1e6,
            "max_pulses": int(safety["max_pulses"]),
            "max_burst": float(safety["max_burst_duration_s"]),
            "min_high": float(safety["min_high_v"]),
            "max_high": float(safety["max_high_v"]),
            "max_points": int(safety["max_points"]),
            "low_v": float(self.config["awg"]["low_v"]),
            "high_v": float(self.config["awg"]["high_v"]),
            "min_exposure": float(camera["min_exposure_ms"]),
            "max_exposure": float(camera["max_exposure_ms"]),
        }
        for axis in "xyz":
            values[f"{axis}_low"], values[f"{axis}_high"] = map(float, ranges[axis])
        return FIELD_HELP[label].format(**values)

    def _hide_tooltip(self, _event: tk.Event[Any] | None = None) -> None:
        if self._tooltip_job is not None:
            self.root.after_cancel(self._tooltip_job)
            self._tooltip_job = None
        if self._tooltip_window is not None:
            self._tooltip_window.destroy()
            self._tooltip_window = None

    def _schedule_tooltip(self, widget: tk.Widget, text: str) -> None:
        self._hide_tooltip()

        def show() -> None:
            self._tooltip_job = None
            if not widget.winfo_exists():
                return
            window = tk.Toplevel(self.root)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.geometry(f"+{widget.winfo_rootx() + 12}+{widget.winfo_rooty() + widget.winfo_height() + 5}")
            ttk.Label(
                window,
                text=text,
                wraplength=390,
                justify="left",
                padding=(9, 6),
                relief="solid",
                borderwidth=1,
            ).pack()
            self._tooltip_window = window

        self._tooltip_job = self.root.after(450, show)

    def _helped(self, widget: tk.Widget, label: str) -> tk.Widget:
        text = self._field_help(label)
        widget.configure(cursor="question_arrow")
        widget.bind("<Enter>", lambda _event: self._schedule_tooltip(widget, text), add="+")
        widget.bind("<Leave>", self._hide_tooltip, add="+")
        widget.bind("<FocusIn>", lambda _event: self._schedule_tooltip(widget, text), add="+")
        widget.bind("<FocusOut>", self._hide_tooltip, add="+")
        return widget

    def _parameter_label(self, parent: tk.Misc, label: str) -> ttk.Label:
        widget = ttk.Label(parent, text=f"{label}  ⓘ")
        self._helped(widget, label)
        return widget

    def _entry(self, parent: tk.Misc, row: int, label: str, value: str) -> tk.StringVar:
        self._parameter_label(parent, label).grid(row=row, column=0, sticky="w", padx=4, pady=2)
        variable = tk.StringVar(value=value)
        entry = ttk.Entry(parent, textvariable=variable, width=18)
        self._helped(entry, label).grid(row=row, column=1, sticky="w", padx=4, pady=2)
        return variable

    def _device_entry(
        self, parent: tk.Misc, row: int, label: str, value: str
    ) -> tuple[tk.StringVar, ttk.Combobox]:
        self._parameter_label(parent, label).grid(row=row, column=0, sticky="w", padx=4, pady=2)
        variable = tk.StringVar(value=value)
        combo = ttk.Combobox(parent, textvariable=variable, values=(), state="normal", width=18)
        self._helped(combo, label).grid(row=row, column=1, sticky="w", padx=4, pady=2)
        return variable, combo

    def _build_run(self) -> None:
        form = ttk.LabelFrame(self.run_body, text="Recipe", padding=8)
        form.pack(side="left", fill="y", padx=(0, 10))
        self.name = self._entry(form, 0, "Name", "test")
        self.power = self._entry(form, 1, "Sample power (mW)", "10")
        self.width_us = self._entry(form, 2, "Pulse duration (µs)", "1")
        self.frequency = self._entry(form, 3, "Repetition rate (Hz)", "1000")
        self.count = self._entry(form, 4, "Pulse count", "1")
        self.high_v = self._entry(form, 5, "TTL high (V)", "5")
        origin = self.config["stage"]["origin_um"]
        self.x = self._entry(form, 6, "Center X (µm)", str(origin["x"]))
        self.y = self._entry(form, 7, "Center Y (µm)", str(origin["y"]))
        self.z = self._entry(form, 8, "Z (µm)", str(origin["z"]))
        self.grid_w = self._entry(form, 9, "Raster width (µm)", "0")
        self.grid_h = self._entry(form, 10, "Raster height (µm)", "0")
        self.grid_nx = self._entry(form, 11, "X points", "1")
        self.grid_ny = self._entry(form, 12, "Y points", "1")
        actions = ttk.Frame(form)
        actions.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        self.run_button = ttk.Button(actions, text="Run recipe", command=self._start_run, style="Accent.TButton")
        self.run_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(
            actions, text="Cancel", command=self._cancel, state="disabled", style="Danger.TButton"
        )
        self.stop_button.pack(side="left", fill="x", expand=True)
        self.fire_button = ttk.Button(
            actions, text="Fire pulse", command=self._start_single_pulse, style="Danger.TButton"
        )
        self.fire_button.pack(side="left", fill="x", expand=True)
        mode = self.config["mode"]
        armed = self.config["safety"]["hardware_armed"]
        self.hardware_status_text = tk.StringVar(
            value=f"Mode: {mode} | Hardware: {'ARMED' if armed else 'DISARMED'}"
        )
        ttk.Label(form, textvariable=self.hardware_status_text).grid(
            row=14, column=0, columnspan=2, sticky="w", pady=4
        )
        right = ttk.Frame(self.run_body)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="Progress").pack(anchor="w")
        self.log = tk.Text(
            right,
            height=24,
            wrap="word",
            state="disabled",
            background=self.colors["surface"],
            foreground=self.colors["text"],
            relief="flat",
            padx=10,
            pady=10,
        )
        self.log.pack(fill="both", expand=True)
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure

            self.run_figure = Figure(figsize=(6.8, 2.8), dpi=100)
            self.run_canvas = FigureCanvasTkAgg(self.run_figure, master=right)
            self.run_canvas.get_tk_widget().pack(fill="both", expand=True, pady=(8, 0))
        except ImportError:
            self.run_figure = None

    def _recipe(self) -> Recipe:
        center = Point(float(self.x.get()), float(self.y.get()), float(self.z.get()))
        points = raster(
            center,
            float(self.grid_w.get()),
            float(self.grid_h.get()),
            int(self.grid_nx.get()),
            int(self.grid_ny.get()),
        )
        return Recipe(
            name=self.name.get().strip() or "run",
            points=points,
            pulse_width_s=float(self.width_us.get()) * 1e-6,
            repetition_hz=float(self.frequency.get()),
            pulse_count=int(self.count.get()),
            high_v=float(self.high_v.get()),
            optical_power_mw=float(self.power.get()),
        )

    def _write_log(self, message: str) -> None:
        def update() -> None:
            self.log.configure(state="normal")
            self.log.insert("end", message + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        self.root.after(0, update)

    @staticmethod
    def _readiness_report(readiness: dict[str, Any]) -> str:
        return "\n".join(
            f"[{status}] {name}: {detail}"
            for name, status, detail in readiness["checks"]
        )

    def _start_run(self) -> None:
        try:
            recipe = self._recipe()
            validate_recipe(recipe, self.config)
            readiness = recipe_readiness(recipe, self.config, self.config_path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Invalid recipe", str(exc))
            return
        readiness_report = self._readiness_report(readiness)
        if readiness["blocked"]:
            messagebox.showerror(
                "Run readiness",
                readiness_report + "\n\nResolve every BLOCKED item before starting.",
            )
            return
        if self.config["mode"] == "hardware" and not self._confirm_hardware_recipe(
            recipe, readiness
        ):
            return
        resources = self._reserve_resources("Run recipe", "awg", "laser", "scope", "stage", "camera")
        if resources is None:
            return
        self.run_button.configure(state="disabled")
        self.fire_button.configure(state="disabled")
        self.live_button.configure(state="disabled")
        self._set_focus_controls("disabled")
        self.stop_button.configure(state="normal")
        self.cancel_event = threading.Event()
        self._write_log("RUN READINESS\n" + readiness_report)
        self._write_log("Starting...")

        def work() -> None:
            failed = False
            try:
                output = run_recipe(
                    recipe,
                    self.config,
                    self.config_path,
                    self._write_log,
                    self.cancel_event.is_set,
                )
                self._write_log(f"Data saved to {output}")
                self.root.after(0, lambda path=output: self._show_last_result(path))
            except Exception as exc:
                failed = True
                self._write_log(f"ERROR: {type(exc).__name__}: {exc}")
                self.root.after(0, lambda message=str(exc): messagebox.showerror("Error", message))
            finally:
                self._release_resources(resources)
                self.root.after(0, lambda: self.run_button.configure(state="normal"))
                self.root.after(0, lambda: self.fire_button.configure(state="normal"))
                self.root.after(0, lambda: self.live_button.configure(state="normal"))
                self.root.after(0, lambda: self._set_focus_controls("normal"))
                self.root.after(0, lambda: self.stop_button.configure(state="disabled"))
                self.root.after(
                    0, lambda invalid=failed: self._disarm_session(invalidate_preflight=invalid)
                )

        self._spawn_worker(work)

    def _confirm_hardware_recipe(
        self,
        recipe: Recipe,
        readiness: dict[str, Any] | None = None,
        extra: str = "",
    ) -> bool:
        readiness = readiness or recipe_readiness(
            recipe, self.config, getattr(self, "config_path", "config.json")
        )
        points = [focus_corrected(point, self.config["stage"]["focus_plane"]) for point in recipe.points]
        pp_mw, _ = laser_peak_power(recipe.optical_power_mw, self.config["laser"])
        burst_s = (recipe.pulse_count - 1) / recipe.repetition_hz + recipe.pulse_width_s
        total_pulses = len(points) * recipe.pulse_count
        energy_mj = (
            recipe.optical_power_mw * recipe.pulse_width_s * recipe.pulse_count * len(points)
        )

        def extent(values: list[float]) -> str:
            return f"{min(values):.4f} to {max(values):.4f}"

        summary = (
            f"Recipe: {recipe.name}\n"
            f"Sample power: {recipe.optical_power_mw:g} mW | Stradus PP: {pp_mw:g} mW\n"
            f"Points: {len(points)} | Pulses/point: {recipe.pulse_count} | Total pulses: {total_pulses}\n"
            f"Pulse width: {recipe.pulse_width_s * 1e6:g} us | Rate: {recipe.repetition_hz:g} Hz\n"
            f"Burst/point: {burst_s:g} s | Total incident pulse energy: {energy_mj:g} mJ\n"
            f"Corrected X: {extent([point.x_um for point in points])} um\n"
            f"Corrected Y: {extent([point.y_um for point in points])} um\n"
            f"Corrected Z: {extent([point.z_um for point in points])} um\n\n"
            f"RUN READINESS\n{self._readiness_report(readiness)}\n\n"
            + (extra + "\n\n" if extra else "")
            + "The stage will move and the laser/AWG output will be enabled. Continue?"
        )
        return messagebox.askyesno("Confirm hardware recipe", summary)

    def _start_single_pulse(self) -> None:
        if self.config["mode"] != "hardware":
            messagebox.showerror("Single pulse", "Select hardware mode in Diagnostics first.")
            return
        if not self.config["safety"]["hardware_armed"]:
            messagebox.showerror("Single pulse", "Hardware is disarmed. Complete Diagnostics first.")
            return
        try:
            power_mw = float(self.power.get())
            width_s = float(self.width_us.get()) * 1e-6
            high_v = float(self.high_v.get())
        except ValueError:
            messagebox.showerror("Single pulse", "Power, duration and TTL high must be numeric.")
            return
        if not messagebox.askyesno(
            "Fire optical pulse",
            f"Fire one {width_s * 1e6:g} us pulse at {power_mw:g} mW on the sample?\n\n"
            "The stage will not move. Confirm beam containment and sample safety.",
        ):
            return
        resources = self._reserve_resources("Single pulse", "awg", "laser")
        if resources is None:
            return
        self.run_button.configure(state="disabled")
        self.fire_button.configure(state="disabled")
        self.live_button.configure(state="disabled")
        self._write_log("Preparing single optical pulse...")

        def work() -> None:
            failed = False
            try:
                result = fire_single_pulse(
                    power_mw, width_s, high_v, self.config, self._write_log
                )
                self._write_log(
                    f"Single pulse complete: {result['sample_power_mw']:g} mW at sample, "
                    f"PP={result['stradus_pp_mw']:g} mW, {result['pulse_width_s'] * 1e6:g} us."
                )
            except Exception as exc:
                failed = True
                self._write_log(f"ERROR: {type(exc).__name__}: {exc}")
                self.root.after(
                    0, lambda message=str(exc): messagebox.showerror("Single pulse", message)
                )
            finally:
                self._release_resources(resources)
                self.root.after(0, lambda: self.run_button.configure(state="normal"))
                self.root.after(0, lambda: self.fire_button.configure(state="normal"))
                self.root.after(0, lambda: self.live_button.configure(state="normal"))
                self.root.after(
                    0, lambda invalid=failed: self._disarm_session(invalidate_preflight=invalid)
                )

        self._spawn_worker(work)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self._write_log("Cancellation requested; output will be disabled at the next check.")

    def _show_last_result(self, output: Path) -> None:
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        last = manifest["points"][-1]
        metrics = last["pulse_metrics"]
        self._write_log(
            f"Pulse: amplitude={metrics['amplitude_v']:.4g} V, "
            f"FWHM={metrics['fwhm_s'] * 1e6:.4g} us, "
            f"SNR={metrics['snr']:.1f}, area={metrics['area_v_s']:.4g} V s"
        )
        phase = last.get("phase_color_change", {})
        if "intensity_change_percent" in phase:
            self._write_log(
                f"Local RGB change: {phase['intensity_change_percent']:+.1f}% | "
                f"B/G {phase['b_over_g_before']:.3f} -> {phase['b_over_g_after']:.3f} | "
                f"{'detected' if phase['detected'] else 'below threshold'}"
            )
        if self.run_figure is None:
            return
        after = np.load(output / last["files"]["after"])
        delta = np.load(output / last["files"]["delta"])
        waveform = np.loadtxt(output / last["files"]["waveform"], delimiter=",", skiprows=1)
        self.run_figure.clear()
        ax1 = self.run_figure.add_subplot(131)
        ax1.imshow(after)
        ax1.set_title("After image")
        ax1.axis("off")
        ax2 = self.run_figure.add_subplot(132)
        ax2.imshow(delta, cmap="magma")
        ax2.set_title(f"Change: {last['image_change_score']:.3f}")
        ax2.axis("off")
        ax3 = self.run_figure.add_subplot(133)
        width = metrics["fwhm_s"]
        scale, unit = (1e9, "ns") if width < 1e-6 else ((1e6, "us") if width < 1e-3 else (1e3, "ms"))
        ax3.plot(waveform[:, 0] * scale, waveform[:, 1])
        ax3.set(xlabel=f"Time ({unit})", ylabel="V", title=f"FWHM {width * scale:.3g} {unit}")
        self.run_figure.tight_layout()
        self.run_canvas.draw_idle()

    def _build_thermal(self) -> None:
        controls = ttk.Frame(self.thermal_body)
        controls.pack(side="left", fill="y", padx=(0, 10))
        self.th_power = self._entry(controls, 0, "Power (mW)", "10")
        self.th_width = self._entry(controls, 1, "Pulse (µs)", "1")
        self.th_radius = self._entry(
            controls, 2, "Spot radius (µm)", str(self.config["sample"]["spot_radius_um"])
        )
        self.th_count = self._entry(controls, 3, "Thermal pulse count", "1")
        self.th_frequency = self._entry(controls, 4, "Thermal repetition rate (Hz)", "1000")
        self.th_phase = tk.StringVar(value=self.config["sample"]["phase"])
        self._parameter_label(controls, "Phase").grid(row=5, column=0, sticky="w", padx=4, pady=4)
        phase_combo = ttk.Combobox(
            controls, textvariable=self.th_phase, values=("amorphous", "crystalline"), state="readonly", width=15
        )
        self._helped(phase_combo, "Phase").grid(row=5, column=1, sticky="w", padx=4, pady=4)
        mesh = self.config["sample"]["axisymmetric"]
        self.th_radial_cells = self._entry(controls, 6, "Radial cells", str(mesh["radial_cells"]))
        self.th_radial_extent = self._entry(
            controls, 7, "Domain radius (µm)", str(mesh["radial_extent_um"])
        )
        ttk.Button(controls, text="Calculate", command=self._calculate_thermal, style="Accent.TButton").grid(
            row=8, column=0, columnspan=2, sticky="ew", pady=10
        )
        self.thermal_text = tk.StringVar(value="Screening model; not a validated process recipe.")
        ttk.Label(controls, textvariable=self.thermal_text, wraplength=270).grid(
            row=9, column=0, columnspan=2, sticky="w"
        )
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure

            self.figure = Figure(figsize=(8.2, 6.2), dpi=100, constrained_layout=True)
            self.canvas = FigureCanvasTkAgg(self.figure, master=self.thermal_body)
            self.canvas.get_tk_widget().pack(side="left", fill="both", expand=True)
            self.figure.text(
                0.5,
                0.5,
                "Press Calculate to run the optothermal model",
                ha="center",
                va="center",
                color=self.colors["muted"],
            )
            self.canvas.draw_idle()
        except ImportError:
            self.figure = None
            ttk.Label(self.thermal_body, text="Install matplotlib to display plots.").pack()

    def _calculate_thermal(self) -> None:
        try:
            model_config = deepcopy(self.config)
            model_config["sample"]["axisymmetric"]["radial_cells"] = int(
                self.th_radial_cells.get()
            )
            model_config["sample"]["axisymmetric"]["radial_extent_um"] = float(
                self.th_radial_extent.get()
            )
            inp = from_config(
                model_config,
                float(self.th_power.get()),
                float(self.th_width.get()) * 1e-6,
                spot_radius_um=float(self.th_radius.get()),
                phase=self.th_phase.get(),
                pulse_count=int(self.th_count.get()),
                repetition_hz=float(self.th_frequency.get()),
            )
            result = estimate(inp)
            optical = multilayer_optics(model_config, inp.phase)
            spatial = axisymmetric_simulation(inp, model_config)
            colors = predict_phase_colors(model_config)
            vertical_cells = len(spatial.depth_um)
            radial_cells = len(spatial.radius_um)
            self.thermal_text.set(
                f"2D axisymmetric peak T: {spatial.result.peak_temperature_c:.1f} °C\n"
                f"Mesh: {radial_cells} × {vertical_cells} = {radial_cells * vertical_cells:,} cells\n"
                f"Domain: R={spatial.radius_edges_um[-1]:g} µm, Z={spatial.depth_edges_um[-1]:g} µm\n"
                f"Sb2Se3 TMM absorption: {100 * result.absorption_fraction:.1f}%\n"
                f"R={100 * optical.reflectance:.1f}%, T={100 * optical.transmittance:.1f}%\n"
                f"Pixelink crystalline/amorphous: {1 + colors['total_signal_change_percent'] / 100:.2f}x\n"
                f"B/G: {colors['amorphous']['rgb_to_green']['b']:.3f} -> "
                f"{colors['crystalline']['rgb_to_green']['b']:.3f}\n"
                f"2D result: {spatial.result.classification}\n\n"
                "Uncalibrated screening result."
            )
            if self.figure is not None:
                self.figure.clear()
                grid = self.figure.add_gridspec(2, 2, height_ratios=(0.88, 1.12))
                schematic = self.figure.add_subplot(grid[0, 0])
                mesh_view = self.figure.add_subplot(grid[0, 1])
                curve = self.figure.add_subplot(grid[1, 0])
                field = self.figure.add_subplot(grid[1, 1])
                self._draw_thermal_schematic(schematic, inp, model_config, spatial)
                display_radius = min(
                    float(spatial.radius_edges_um[-1]), max(5.0, 6 * inp.spot_radius_um)
                )
                display_depth = min(5.0, float(spatial.depth_edges_um[-1]))
                self._draw_thermal_mesh(
                    mesh_view, inp, model_config, spatial, display_radius, display_depth
                )
                t, temp = temperature_curve(inp)
                if inp.pulse_count == 1:
                    plot_end = min(
                        float(spatial.time_s[-1]),
                        max(20 * inp.pulse_width_s, 5 * spatial.result.time_constant_s),
                    )
                else:
                    last_pulse = (inp.pulse_count - 1) / inp.repetition_hz + inp.pulse_width_s
                    plot_end = min(
                        float(spatial.time_s[-1]),
                        last_pulse + max(5 * inp.pulse_width_s, 5 * spatial.result.time_constant_s),
                    )
                scale, unit = (1e6, "µs") if plot_end < 0.1 else (1e3, "ms")
                curve.plot(t * scale, temp, color="#7c8798", ls="--", lw=1.2, label="fast screening")
                curve.plot(
                    spatial.time_s * scale,
                    spatial.pcm_temperature_c,
                    color="#0e7c7b",
                    lw=1.8,
                    label="2D axisymmetric",
                )
                curve.axhline(inp.crystallization_c, color="#f79009", ls="--", lw=1, label="crystallization")
                curve.axhline(inp.melting_c, color="#d62728", ls="--", lw=1, label="melting")
                curve.set(
                    xlabel=f"Time ({unit})",
                    ylabel="Temperature (°C)",
                    title="Sb2Se3 transient",
                    xlim=(0.0, plot_end * scale),
                )
                curve.grid(alpha=0.18)
                curve.legend(fontsize=7, loc="best")
                temperature_map = field.pcolormesh(
                    spatial.radius_edges_um,
                    spatial.depth_edges_um,
                    spatial.peak_snapshot_c,
                    shading="flat",
                )
                levels = [
                    value
                    for value in (inp.crystallization_c, inp.melting_c)
                    if float(spatial.peak_snapshot_c.min()) < value < float(spatial.peak_snapshot_c.max())
                ]
                if levels:
                    contours = field.contour(
                        spatial.radius_um,
                        spatial.depth_um,
                        spatial.peak_snapshot_c,
                        levels=levels,
                        colors=[
                            "#f79009" if value == inp.crystallization_c else "#d62728"
                            for value in levels
                        ],
                        linewidths=0.9,
                    )
                    field.clabel(contours, fmt="%g °C", fontsize=6)
                field.set_xlim(0.0, display_radius)
                field.set_ylim(display_depth, 0.0)
                field.set(
                    xlabel="Radius (µm)",
                    ylabel="Depth (µm)",
                    title="T(r,z) at peak Sb2Se3 temperature",
                )
                self.figure.colorbar(
                    temperature_map, ax=field, label="Temperature (°C)", shrink=0.86
                )
                self.canvas.draw_idle()
        except Exception as exc:
            messagebox.showerror("Thermal calculation", str(exc))

    def _draw_thermal_schematic(
        self, axis: Any, inp: Any, config: dict[str, Any], spatial: Any
    ) -> None:
        from matplotlib.patches import Polygon, Rectangle

        axis.set(xlim=(0.0, 1.0), ylim=(0.0, 1.0), title="Simulated geometry")
        axis.axis("off")
        layers = config["sample"]["thermal_layers"]
        heights = (0.08, 0.055, 0.075, 0.13, 0.17)
        palette = ("#d8eef2", "#f7a23b", "#496d9d", "#d9dde3", "#8a929b")
        top = 0.54
        for layer, height, color in zip(layers, heights, palette):
            bottom = top - height
            axis.add_patch(Rectangle((0.40, bottom), 0.35, height, facecolor=color, edgecolor="#18304f", lw=0.7))
            thickness_nm = float(layer["thickness_nm"])
            thickness = f"{thickness_nm / 1000:g} µm" if thickness_nm >= 1000 else f"{thickness_nm:g} nm"
            axis.text(0.77, 0.5 * (top + bottom), f"{layer['name']}  {thickness}", va="center", fontsize=6.5)
            top = bottom
        waist = min(0.15, 0.06 + 0.035 * inp.spot_radius_um)
        axis.add_patch(
            Polygon(
                ((0.57 - 0.20, 0.94), (0.57 + 0.20, 0.94), (0.57 + waist, 0.55), (0.57 - waist, 0.55)),
                closed=True,
                facecolor="#f79009",
                edgecolor="#d95f02",
                alpha=0.24,
            )
        )
        axis.annotate("", xy=(0.57, 0.55), xytext=(0.57, 0.95), arrowprops={"arrowstyle": "->", "color": "#d62728", "lw": 1.5})
        axis.text(0.57, 0.96, f"Gaussian beam, w={inp.spot_radius_um:g} µm", ha="center", va="bottom", fontsize=7)
        axis.text(
            0.02,
            0.88,
            f"P = {inp.power_mw:g} mW\n"
            f"pulse = {inp.pulse_width_s * 1e6:g} µs\n"
            f"count = {inp.pulse_count}\n"
            f"rate = {inp.repetition_hz:g} Hz\n"
            f"phase = {inp.phase}\n"
            f"mesh = {len(spatial.radius_um)} × {len(spatial.depth_um)}",
            va="top",
            fontsize=7,
            bbox={"boxstyle": "round,pad=0.3", "facecolor": "#f5f7fb", "edgecolor": "#9aa6b2"},
        )
        axis.annotate("r", xy=(0.36, 0.05), xytext=(0.25, 0.05), arrowprops={"arrowstyle": "->", "color": "#18304f"}, fontsize=7)
        axis.annotate("z", xy=(0.25, 0.05), xytext=(0.25, 0.16), arrowprops={"arrowstyle": "->", "color": "#18304f"}, fontsize=7)

    def _draw_thermal_mesh(
        self,
        axis: Any,
        inp: Any,
        config: dict[str, Any],
        spatial: Any,
        display_radius: float,
        display_depth: float,
    ) -> None:
        palette = ("#d8eef2", "#f7a23b", "#496d9d", "#d9dde3", "#8a929b")
        depth = 0.0
        for layer, color in zip(config["sample"]["thermal_layers"], palette):
            next_depth = depth + float(layer["thickness_nm"]) / 1000.0
            axis.axhspan(depth, min(next_depth, display_depth), color=color, alpha=0.35)
            depth = next_depth
            if depth >= display_depth:
                break
        radial_lines = spatial.radius_edges_um[spatial.radius_edges_um <= display_radius]
        depth_lines = spatial.depth_edges_um[spatial.depth_edges_um <= display_depth]
        axis.vlines(radial_lines, 0.0, display_depth, color="#18304f", lw=0.35, alpha=0.50)
        axis.hlines(depth_lines, 0.0, display_radius, color="#18304f", lw=0.35, alpha=0.50)
        axis.axvline(inp.spot_radius_um, color="#d62728", ls="--", lw=1.0, label="beam radius w")
        axis.set(
            xlim=(0.0, display_radius),
            ylim=(display_depth, 0.0),
            xlabel="Radius (µm)",
            ylabel="Depth (µm)",
            title=f"Finite-volume mesh: {len(spatial.radius_um)} × {len(spatial.depth_um)} cells (zoom)",
        )
        axis.legend(fontsize=6, loc="lower right")

    def _build_focus(self) -> None:
        controls = ttk.Frame(self.focus_body)
        controls.pack(side="left", fill="y", padx=(0, 10))
        origin = self.config["stage"]["origin_um"]
        self.focus_z = self._entry(controls, 0, "Center Z (µm)", str(origin["z"]))
        self.focus_span = self._entry(controls, 1, "Z sweep (µm)", "4")
        self.focus_samples = self._entry(controls, 2, "Image count", "9")
        initial_scale = (
            self.config["simulation"]["camera_um_per_pixel"]
            if self.config["mode"] == "simulation"
            else self.config["camera"]["um_per_pixel"]
        )
        self.pixel_size = self._entry(controls, 3, "Scale (µm/pixel; 0=unknown)", str(initial_scale))
        self.calibration_step = self._entry(
            controls, 4, "XY calibration step (µm)", str(self.config["camera"]["calibration_step_um"])
        )
        self.focus_safe = tk.BooleanVar(value=self.config["mode"] == "simulation")
        focus_safe = ttk.Checkbutton(
            controls,
            text="Spot visible without saturation\nand power below switching threshold  ⓘ",
            variable=self.focus_safe,
        )
        self._helped(
            focus_safe,
            "Spot visible without saturation\nand power below switching threshold",
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=4, pady=8)
        self.focus_button = ttk.Button(
            controls, text="Run autofocus", command=self._start_focus, style="Accent.TButton"
        )
        self.focus_button.grid(row=6, column=0, columnspan=2, sticky="ew", pady=4)
        self.apply_spot_button = ttk.Button(
            controls, text="Apply radius to model", command=self._apply_spot, state="disabled"
        )
        self.apply_spot_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=4)
        self.calibrate_pixel_button = ttk.Button(
            controls, text="Calibrate pixel-to-stage", command=self._start_pixel_calibration
        )
        self.calibrate_pixel_button.grid(row=8, column=0, columnspan=2, sticky="ew", pady=4)
        self.focus_map_button = ttk.Button(
            controls, text="Map focus (5 points)", command=self._start_focus_map
        )
        self.focus_map_button.grid(row=9, column=0, columnspan=2, sticky="ew", pady=4)
        self.focus_text = tk.StringVar(
            value="Autofocus minimizes the measured Gaussian radii over a Z sweep."
        )
        ttk.Label(controls, textvariable=self.focus_text, wraplength=280).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=8
        )
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure

            self.focus_figure = Figure(figsize=(6.8, 5.8), dpi=100)
            self.focus_canvas = FigureCanvasTkAgg(self.focus_figure, master=self.focus_body)
            self.focus_canvas.get_tk_widget().pack(side="left", fill="both", expand=True)
        except ImportError:
            self.focus_figure = None
        self.last_focus: FocusResult | None = None

    def _build_camera(self) -> None:
        controls = ttk.LabelFrame(self.camera_body, text="Acquisition", padding=12)
        controls.pack(side="left", fill="y", padx=(0, 12))
        self.live_roi = self._entry(
            controls, 0, "ROI left,top,width,height", ",".join(map(str, self.config["camera"]["roi"]))
        )
        ttk.Label(
            controls,
            text="0,0,0,0 uses the full sensor.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 10))
        self.live_button = ttk.Button(
            controls, text="Start camera", command=self._start_live, style="Accent.TButton"
        )
        self.live_button.grid(row=2, column=0, sticky="ew", pady=4)
        self.live_stop_button = ttk.Button(
            controls,
            text="Stop",
            command=self._stop_live,
            state="disabled",
            style="Danger.TButton",
        )
        self.live_stop_button.grid(row=2, column=1, sticky="ew", pady=4)

        status = ttk.LabelFrame(controls, text="Live readout", padding=10)
        status.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.camera_status = tk.StringVar(value="Camera stopped")
        self.camera_spot = tk.StringVar(value="Spot: --")
        self.camera_snr = tk.StringVar(value="SNR: --")
        self.camera_settings = tk.StringVar(value="Exposure: --  |  Gain: --")
        ttk.Label(status, textvariable=self.camera_status, font=("Segoe UI Semibold", 11)).pack(anchor="w")
        ttk.Label(status, textvariable=self.camera_spot).pack(anchor="w", pady=(8, 0))
        ttk.Label(status, textvariable=self.camera_snr).pack(anchor="w", pady=(3, 0))
        ttk.Label(status, textvariable=self.camera_settings, style="Muted.TLabel", wraplength=280).pack(
            anchor="w", pady=(8, 0)
        )

        viewer = ttk.LabelFrame(self.camera_body, text="Image and histogram", padding=8)
        viewer.pack(side="left", fill="both", expand=True)
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure

            self.camera_figure = Figure(figsize=(8.2, 4.3), dpi=100, facecolor=self.colors["surface"])
            self.camera_canvas = FigureCanvasTkAgg(self.camera_figure, master=viewer)
            self.camera_canvas.get_tk_widget().pack(fill="both", expand=True)
            axis = self.camera_figure.add_subplot(111)
            axis.text(
                0.5,
                0.5,
                "Select 'Start camera' to begin",
                ha="center",
                va="center",
                color=self.colors["muted"],
                transform=axis.transAxes,
            )
            axis.axis("off")
            self.camera_canvas.draw_idle()
            self.camera_image_artist = None
        except ImportError:
            self.camera_figure = None
            self.camera_image_artist = None
        self.live_stop_event = threading.Event()
        self.live_render_pending = False
        self.live_system: Any | None = None
        self.camera_lock = threading.Lock()

    def _build_laser(self) -> None:
        controls = ttk.LabelFrame(self.laser_body, text="CW control", padding=14)
        controls.pack(fill="x", pady=(0, 8))
        self.cw_power = self._entry(
            controls, 0, "CW setpoint LP (mW)", str(self.config["laser"]["peak_power_mw"])
        )
        self.cw_park = self._entry(controls, 1, "Park power (mW)", "1.0")
        self.cw_on_button = ttk.Button(
            controls, text="LASER ON", command=self._cw_on, style="Danger.TButton"
        )
        self.cw_on_button.grid(row=2, column=0, sticky="ew", padx=3, pady=6)
        self.cw_off_button = ttk.Button(
            controls, text="LASER OFF", command=self._cw_off, state="disabled"
        )
        self.cw_off_button.grid(row=2, column=1, sticky="ew", padx=3, pady=6)
        ttk.Button(
            controls,
            text="Initialize park (beam blocked)",
            command=self._laser_initialize_park,
            style="Danger.TButton",
        ).grid(row=3, column=0, columnspan=2, sticky="ew", padx=3, pady=6)
        ttk.Button(controls, text="Refresh status", command=self._laser_refresh).grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=3, pady=6
        )
        self.cw_status = tk.StringVar(value="Live laser: OFF")
        ttk.Label(controls, textvariable=self.cw_status, wraplength=310).grid(
            row=5, column=0, columnspan=2, sticky="w", padx=4, pady=(10, 0)
        )
        details = ttk.LabelFrame(self.laser_body, text="State", padding=14)
        details.pack(fill="x")
        self.laser_details = tk.StringVar(
            value="Disconnected. Refresh status is read-only with respect to emission enable."
        )
        ttk.Label(details, textvariable=self.laser_details, justify="left", wraplength=390).pack(
            anchor="nw"
        )
        self.laser_lock = threading.Lock()
        self.laser_device: Any | None = None

    def _build_awg(self) -> None:
        controls = ttk.LabelFrame(self.awg_body, text="Manual control", padding=14)
        controls.pack(fill="x", pady=(0, 8))
        self.awg_dc_level = self._entry(controls, 0, "DC level (V)", str(self.config["awg"]["low_v"]))
        self.awg_width = self._entry(controls, 1, "Pulse width (s)", "1e-6")
        self.awg_frequency = self._entry(controls, 2, "Repetition (Hz)", "1000")
        self.awg_high = self._entry(controls, 3, "High level (V)", str(self.config["awg"]["high_v"]))
        self.awg_low = self._entry(controls, 4, "Low level (V)", str(self.config["awg"]["low_v"]))
        self.awg_count = self._entry(controls, 5, "Pulse count", "1")
        buttons = ttk.Frame(controls)
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        for column in range(2):
            buttons.columnconfigure(column, weight=1)
        ttk.Button(buttons, text="Connect", command=self._awg_connect).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(buttons, text="Disconnect", command=self._awg_disconnect).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(buttons, text="Set DC", command=self._awg_set_dc).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(buttons, text="Output OFF", command=lambda: self._awg_output(False)).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(buttons, text="Output ON", command=lambda: self._awg_output(True), style="Danger.TButton").grid(row=2, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(buttons, text="Configure pulse", command=self._awg_configure_pulse).grid(row=2, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(buttons, text="Trigger", command=self._awg_trigger).grid(row=3, column=0, columnspan=2, sticky="ew", padx=2, pady=2)
        status = ttk.LabelFrame(self.awg_body, text="State", padding=14)
        status.pack(fill="x")
        self.awg_status = tk.StringVar(value="Disconnected; output state unknown until connected.")
        ttk.Label(status, textvariable=self.awg_status, justify="left", wraplength=390).pack(anchor="nw")
        self.awg_lock = threading.Lock()
        self.awg_device: Any | None = None
        self.awg_output_enabled = False
        self.awg_pulse_configured = False
        self.awg_burst_duration_s = 0.0

    def _build_stage(self) -> None:
        controls = ttk.LabelFrame(self.stage_body, text="Manual piezo motion", padding=14)
        controls.pack(fill="x", pady=(0, 8))
        connection = ttk.Frame(controls)
        connection.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Button(connection, text="Connect", command=self._stage_connect).pack(side="left", padx=2)
        ttk.Button(connection, text="Disconnect", command=self._stage_disconnect).pack(side="left", padx=2)
        ttk.Button(connection, text="Refresh", command=self._stage_refresh).pack(side="left", padx=2)
        self.live_stage_step = self._entry(controls, 1, "Step (um)", "0.1")
        self.live_stage_buttons: list[ttk.Button] = []
        for row, axis in enumerate("XYZ", start=2):
            ttk.Label(controls, text=axis).grid(row=row, column=0, sticky="w", padx=4, pady=4)
            for column, sign, label in ((1, -1, "-"), (2, 1, "+")):
                button = ttk.Button(
                    controls,
                    text=label,
                    width=6,
                    state="disabled",
                    command=lambda name=axis.lower(), direction=sign: self._jog_live(name, direction),
                )
                button.grid(row=row, column=column, sticky="ew", padx=3, pady=3)
                self.live_stage_buttons.append(button)
        status = ttk.LabelFrame(self.stage_body, text="Position", padding=14)
        status.pack(fill="x")
        self.live_stage_position = tk.StringVar(value="Stage: disconnected")
        ttk.Label(status, textvariable=self.live_stage_position, justify="left", wraplength=390).pack(anchor="nw")
        self.stage_lock = threading.Lock()
        self.stage_device: Any | None = None

    def _build_scope(self) -> None:
        controls = ttk.LabelFrame(self.scope_body, text="Manual control", padding=14)
        controls.pack(fill="x", pady=(0, 8))
        self.scope_pulse_width = self._entry(controls, 0, "Expected pulse width (s)", "1e-6")
        ttk.Button(controls, text="Connect", command=self._scope_connect).grid(row=1, column=0, sticky="ew", padx=3, pady=4)
        ttk.Button(controls, text="Disconnect", command=self._scope_disconnect).grid(row=1, column=1, sticky="ew", padx=3, pady=4)
        ttk.Button(controls, text="Configure", command=self._scope_configure).grid(row=2, column=0, sticky="ew", padx=3, pady=4)
        ttk.Button(controls, text="Arm single", command=self._scope_arm).grid(row=2, column=1, sticky="ew", padx=3, pady=4)
        ttk.Button(controls, text="Acquire", command=self._scope_acquire).grid(row=3, column=0, columnspan=2, sticky="ew", padx=3, pady=4)
        status = ttk.LabelFrame(self.scope_body, text="Acquisition state", padding=14)
        status.pack(fill="x")
        self.scope_status = tk.StringVar(value="Disconnected.")
        ttk.Label(status, textvariable=self.scope_status, justify="left", wraplength=390).pack(anchor="nw")
        self.scope_lock = threading.Lock()
        self.scope_device: Any | None = None

    def _device_worker(self, lock: threading.Lock, title: str, work: Any) -> bool:
        if not lock.acquire(blocking=False):
            messagebox.showerror(title, f"{title} is busy with another command.")
            return False

        def run() -> None:
            try:
                work()
            except Exception as exc:
                self.root.after(0, lambda message=str(exc): self._device_error(title, message))
            finally:
                lock.release()

        self._spawn_worker(run)
        return True

    def _device_error(self, title: str, message: str) -> None:
        if self.config["mode"] == "hardware":
            self._disarm_session(invalidate_preflight=True)
        messagebox.showerror(title, message)

    def _set_cw_controls(self, available: bool, laser_on: bool = False) -> None:
        self.cw_on_button.configure(state="normal" if available and not laser_on else "disabled")
        self.cw_off_button.configure(state="normal" if laser_on else "disabled")

    def _cw_on(self) -> None:
        if self.config["mode"] != "hardware" or not self.config["safety"]["hardware_armed"]:
            messagebox.showerror("Live laser", "Select armed hardware mode in Diagnostics first.")
            return
        try:
            power_mw, park_mw = float(self.cw_power.get()), float(self.cw_park.get())
            limit = min(160.0, float(self.config["safety"]["max_optical_power_mw"]))
            if not 0 < power_mw <= limit or not 0 < park_mw <= limit:
                raise ValueError(f"CW and park power must be between 0 and {limit:g} mW.")
        except ValueError as exc:
            messagebox.showerror("Live laser", str(exc) or "Power must be a positive number.")
            return
        if not messagebox.askyesno(
            "Enable internal CW",
            f"Enable feedback-regulated CW at LP={power_mw:g} mW?\n\n"
            f"Stored LPS must already equal the {park_mw:g} mW park power. "
            "The AWG is not used. Confirm beam containment and eye protection.",
        ):
            return
        self._set_cw_controls(False)
        self.cw_status.set("CW laser: starting from park power...")

        def work() -> None:
            try:
                if self.laser_device is None:
                    self.laser_device = Stradus639160(
                        **{
                            key: self.config["laser"][key]
                            for key in ("visa_resource", "baud_rate", "timeout_ms", "emission_settle_s")
                        }
                    )
                status = self.laser_device.enable_internal_cw(power_mw, park_mw)
                if self.safe_all_event.is_set():
                    self.laser_device.close()
                    self.laser_device = None
                    return
                self._write_log(f"INTERNAL CW ON: LP={power_mw:g} mW.")
                details = (
                    f"{self.laser_device.identity}\nLE={status['emission_enabled']} | "
                    f"PUL={status['pul']} | LPS={status['laser_power_setting_mw']:g} mW | "
                    f"measured LP={status['measured_power_mw']:g} mW"
                )
                self.root.after(0, lambda: self.cw_status.set(f"Internal CW: ON | LP {power_mw:g} mW"))
                self.root.after(0, lambda value=details: self.laser_details.set(value))
                self.root.after(0, lambda: self._set_cw_controls(True, True))
            except Exception as exc:
                if self.laser_device is not None:
                    try:
                        self.laser_device.close()
                    except Exception:
                        pass
                    self.laser_device = None
                self.root.after(
                    0, lambda message=str(exc): self._device_error("Live laser", message)
                )
                self.root.after(0, lambda: self.cw_status.set("Live laser: OFF"))
                self.root.after(0, lambda: self._set_cw_controls(True))

        if not self._device_worker(self.laser_lock, "Stradus", work):
            self._set_cw_controls(True)
            self.cw_status.set("CW laser: busy")

    def _awg_connect(self) -> None:
        def work() -> None:
            if self.awg_device is None:
                self.awg_device = open_awg(self.config["awg"])
            self.awg_device.configure_dc(float(self.config["awg"]["low_v"]))
            self.awg_device.output(False)
            self.awg_output_enabled = False
            self.awg_pulse_configured = False
            self.root.after(0, lambda: self.awg_status.set(f"Connected: {self.awg_device.identity}\nOutput OFF"))

        self._device_worker(self.awg_lock, "AWG", work)

    def _awg_disconnect(self) -> None:
        def work() -> None:
            if self.awg_device is not None:
                self.awg_device.configure_dc(float(self.config["awg"]["low_v"]))
                self.awg_device.output(False)
                self.awg_device.close()
                self.awg_device = None
            self.awg_output_enabled = False
            self.awg_pulse_configured = False
            self.root.after(0, lambda: self.awg_status.set("Disconnected | output OFF"))
            self.root.after(0, self._disarm_session)

        self._device_worker(self.awg_lock, "AWG", work)

    def _awg_set_dc(self) -> None:
        try:
            level = float(self.awg_dc_level.get())
            low, high = float(self.config["awg"]["low_v"]), float(self.config["awg"]["high_v"])
            if not min(low, high) <= level <= max(low, high):
                raise ValueError(f"DC level must be inside [{min(low, high):g}, {max(low, high):g}] V.")
        except ValueError as exc:
            messagebox.showerror("AWG", str(exc) or "DC level must be numeric.")
            return
        if self.awg_output_enabled and (
            self.config["mode"] != "hardware" or not self.config["safety"]["hardware_armed"]
        ):
            messagebox.showerror("AWG", "Hardware must be armed before changing a live output.")
            return

        def work() -> None:
            if self.awg_device is None:
                self.awg_device = open_awg(self.config["awg"])
            self.awg_device.configure_dc(level)
            self.awg_pulse_configured = False
            state = "ON" if self.awg_output_enabled else "OFF"
            self.root.after(0, lambda: self.awg_status.set(f"Connected: {self.awg_device.identity}\nDC={level:g} V | output {state}"))

        self._device_worker(self.awg_lock, "AWG", work)

    def _awg_output(self, enabled: bool) -> None:
        if enabled and (
            self.config["mode"] != "hardware" or not self.config["safety"]["hardware_armed"]
        ):
            messagebox.showerror("AWG", "Select armed hardware mode in Diagnostics first.")
            return
        if enabled and not messagebox.askyesno(
            "AWG output",
            "Enable the configured electrical output? Confirm cabling, 50-ohm load and safe levels.",
        ):
            return

        def work() -> None:
            if self.awg_device is None:
                self.awg_device = open_awg(self.config["awg"])
            if not enabled:
                self.awg_device.configure_dc(float(self.config["awg"]["low_v"]))
            self.awg_device.output(enabled)
            self.awg_output_enabled = enabled
            self.root.after(0, lambda: self.awg_status.set(f"Connected: {self.awg_device.identity}\nOutput {'ON' if enabled else 'OFF'}"))

        self._device_worker(self.awg_lock, "AWG", work)

    def _awg_configure_pulse(self) -> None:
        try:
            width = float(self.awg_width.get())
            frequency = float(self.awg_frequency.get())
            high = float(self.awg_high.get())
            low = float(self.awg_low.get())
            count = int(self.awg_count.get())
            safety = self.config["safety"]
            if not float(safety["min_pulse_width_s"]) <= width <= float(safety["max_pulse_width_s"]):
                raise ValueError("Pulse width is outside the configured safety range.")
            if frequency <= 0 or not 1 <= count <= int(safety["max_pulses"]):
                raise ValueError("Repetition rate and pulse count must be positive and within limits.")
            duration = (count - 1) / frequency + width
            if duration > float(safety["max_burst_duration_s"]):
                raise ValueError(
                    f"Burst duration {duration:g} s exceeds the configured safety limit."
                )
            if width * frequency >= 0.9:
                raise ValueError("Pulse duty cycle must be below 90%.")
            model = str(self.config["awg"].get("model", "T3AFG350"))
            if model == "DG1062Z" and width < 16e-9:
                raise ValueError("The DG1062Z pulse width cannot be shorter than 16 ns.")
            maximum_frequency = 25e6 if model == "DG1062Z" else 350e6
            if frequency > maximum_frequency:
                raise ValueError(
                    f"Repetition rate exceeds the {model} pulse limit of "
                    f"{maximum_frequency / 1e6:g} MHz."
                )
            if not float(safety["min_high_v"]) <= high <= float(safety["max_high_v"]):
                raise ValueError("High level is outside the configured TTL range.")
            if not 0 <= low <= 0.8 or low >= high:
                raise ValueError("Low level must be between 0 and 0.8 V and below the high level.")
        except ValueError as exc:
            messagebox.showerror("AWG", str(exc) or "Invalid pulse settings.")
            return

        def work() -> None:
            if self.awg_device is None:
                self.awg_device = open_awg(self.config["awg"])
            self.awg_device.output(False)
            self.awg_output_enabled = False
            self.awg_device.configure_pulse(width, high, low, count, frequency)
            self.awg_pulse_configured = True
            self.awg_burst_duration_s = duration
            self.root.after(0, lambda: self.awg_status.set(
                f"Pulse configured | width={width:g} s | {frequency:g} Hz | {count} pulse(s)\nOutput OFF"
            ))

        self._device_worker(self.awg_lock, "AWG", work)

    def _awg_trigger(self) -> None:
        if self.config["mode"] != "hardware" or not self.config["safety"]["hardware_armed"]:
            messagebox.showerror("AWG", "Select armed hardware mode in Diagnostics first.")
            return
        if not self.awg_pulse_configured:
            messagebox.showerror("AWG", "Configure the pulse before triggering it.")
            return
        if not self.awg_output_enabled:
            messagebox.showerror("AWG", "Enable the AWG output explicitly before triggering.")
            return

        def work() -> None:
            if self.awg_device is None:
                raise RuntimeError("Connect and configure the AWG first.")
            self.awg_device.trigger()
            self.safe_all_event.wait(self.awg_burst_duration_s + 0.05)
            self.awg_device.output(False)
            self.awg_output_enabled = False
            self.root.after(0, lambda: self.awg_status.set("Manual burst complete | output OFF"))
            self.root.after(0, self._disarm_session)

        self._device_worker(self.awg_lock, "AWG", work)

    def _stage_connect(self) -> None:
        if self.config["mode"] != "hardware" or not self.config["safety"]["hardware_armed"]:
            messagebox.showerror("Stage", "Select armed hardware mode in Diagnostics first.")
            return

        def work() -> None:
            if self.stage_device is None:
                self.stage_device = KinesisBPC303Stage(self.config["stage"])
            position = self.stage_device.get_position()
            self.root.after(0, lambda value=position: self._show_live_stage_position(value))
            self.root.after(0, lambda: self._set_live_stage_controls("normal"))

        self._device_worker(self.stage_lock, "Stage", work)

    def _stage_disconnect(self) -> None:
        def work() -> None:
            if self.stage_device is not None:
                self.stage_device.close()
                self.stage_device = None
            self.root.after(0, lambda: self._set_live_stage_controls("disabled"))
            self.root.after(0, lambda: self.live_stage_position.set("Stage: disconnected"))
            self.root.after(0, self._disarm_session)

        self._device_worker(self.stage_lock, "Stage", work)

    def _stage_refresh(self) -> None:
        def work() -> None:
            if self.stage_device is None:
                raise RuntimeError("Connect the BPC303 first.")
            position = self.stage_device.get_position()
            self.root.after(0, lambda value=position: self._show_live_stage_position(value))

        self._device_worker(self.stage_lock, "Stage", work)

    def _cw_off(self) -> None:
        self._set_cw_controls(False)
        self.cw_status.set("CW laser: storing park power and turning off...")
        try:
            park_mw = float(self.cw_park.get())
        except ValueError:
            messagebox.showerror("Live laser", "Park power must be a number.")
            self._set_cw_controls(False, True)
            return

        def work() -> None:
            try:
                if self.laser_device is not None:
                    self.laser_device.disable_internal_cw(park_mw)
                    self.laser_device.close()
                    self.laser_device = None
                self._write_log(f"INTERNAL CW OFF: parked at {park_mw:g} mW.")
                self.root.after(0, lambda: self.cw_status.set("Internal CW: OFF"))
                self.root.after(0, lambda: self.laser_details.set(f"LE=0 verified | park LPS={park_mw:g} mW"))
                self.root.after(0, lambda: self._set_cw_controls(True))
            except Exception as exc:
                self.root.after(
                    0, lambda message=str(exc): self._device_error("Live laser", message)
                )
                self.root.after(0, lambda: self.cw_status.set("Live laser: state unknown; use hardware interlock"))
                self.root.after(0, lambda: self._set_cw_controls(False, True))
            finally:
                self.root.after(0, self._disarm_session)

        if not self._device_worker(self.laser_lock, "Stradus", work):
            self._set_cw_controls(False, True)
            self.cw_status.set("CW laser: busy")

    def _laser_initialize_park(self) -> None:
        if self.config["mode"] != "hardware" or not self.config["safety"]["hardware_armed"]:
            messagebox.showerror(
                "Initialize park power", "Select armed hardware mode in Diagnostics first."
            )
            return
        if self.laser_device is not None:
            messagebox.showerror(
                "Initialize park power",
                "Turn the laser OFF before changing its stored park power.",
            )
            return
        limit = min(160.0, float(self.config["safety"]["max_optical_power_mw"]))
        try:
            park_mw = float(self.cw_park.get())
            if not 0 < park_mw <= limit:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Initialize park power", f"Park power must be between 0 and {limit:g} mW."
            )
            return
        if not messagebox.askyesno(
            "Initialize park power",
            f"PHYSICALLY BLOCK THE BEAM.\n\nThe Stradus currently stores its previous LPS and may emit "
            f"at that power for several seconds before PCMWriter stores {park_mw:g} mW.\n\n"
            "Confirm that the beam is blocked and laser safety controls are in place.",
        ):
            return

        def work() -> None:
            laser = Stradus639160(
                **{
                    key: self.config["laser"][key]
                    for key in ("visa_resource", "baud_rate", "timeout_ms", "emission_settle_s")
                }
            )
            try:
                stored = laser.initialize_cw_park(park_mw)
                self.root.after(0, lambda: self.cw_status.set(f"Internal CW: OFF | park {stored:g} mW"))
                self.root.after(0, lambda: self.laser_details.set(f"Park initialization complete. LE=0 | LPS={stored:g} mW"))
            finally:
                laser.close()
                self.root.after(0, self._disarm_session)

        self._device_worker(self.laser_lock, "Initialize park power", work)

    def _laser_refresh(self) -> None:
        def work() -> None:
            temporary = self.laser_device is None
            laser = self.laser_device or Stradus639160(
                **{
                    key: self.config["laser"][key]
                    for key in ("visa_resource", "baud_rate", "timeout_ms", "emission_settle_s")
                }
            )
            try:
                status = laser.status()
                text = (
                    f"{laser.identity}\nFC={status['fault_code']} ({status['fault_description']}) | "
                    f"IL={status['interlock']} | C={status['control_mode']} | EPC={status['external_power_control']}\n"
                    f"LE={status['emission_enabled']} | PUL={status['pul']} | "
                    f"PP={status['peak_power_mw']:g} mW | LPS={status['laser_power_setting_mw']:g} mW"
                )
                self.root.after(0, lambda value=text: self.laser_details.set(value))
            finally:
                if temporary:
                    laser.disconnect()

        self._device_worker(self.laser_lock, "Stradus", work)

    def _set_live_stage_controls(self, state: str) -> None:
        for button in self.live_stage_buttons:
            button.configure(state=state)

    def _show_live_stage_position(self, position: Point) -> None:
        self.live_stage_position.set(
            f"Stage: X={position.x_um:.4f}, Y={position.y_um:.4f}, Z={position.z_um:.4f} um"
        )
        self.x.set(f"{position.x_um:.4f}")
        self.y.set(f"{position.y_um:.4f}")
        self.z.set(f"{position.z_um:.4f}")
        self.focus_z.set(f"{position.z_um:.4f}")

    def _jog_live(self, axis: str, direction: int) -> None:
        try:
            step = float(self.live_stage_step.get())
            if step <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Manual piezo motion", "Step must be a positive number.")
            return
        self._set_live_stage_controls("disabled")

        def work() -> None:
            try:
                if self.stage_device is None:
                    raise RuntimeError("Connect the BPC303 first.")
                current = self.stage_device.get_position()
                coordinates = {"x": current.x_um, "y": current.y_um, "z": current.z_um}
                coordinates[axis] += direction * step
                self.stage_device.move_to(Point(coordinates["x"], coordinates["y"], coordinates["z"]))
                threading.Event().wait(float(self.config["stage"]["settle_s"]))
                actual = self.stage_device.get_position()
                self.root.after(0, lambda value=actual: self._show_live_stage_position(value))
            finally:
                self.root.after(
                    0,
                    lambda: self._set_live_stage_controls("normal" if self.stage_device is not None else "disabled"),
                )

        if not self._device_worker(self.stage_lock, "Manual piezo motion", work):
            self._set_live_stage_controls("normal" if self.stage_device is not None else "disabled")

    def _scope_connect(self) -> None:
        def work() -> None:
            if self.scope_device is None:
                self.scope_device = RigolMSO7054(
                    resource=self.config["scope"]["visa_resource"],
                    channel=int(self.config["scope"]["channel"]),
                    timeout_ms=int(self.config["scope"]["timeout_ms"]),
                )
            self.root.after(0, lambda: self.scope_status.set(f"Connected: {self.scope_device.identity}"))

        self._device_worker(self.scope_lock, "Scope", work)

    def _scope_disconnect(self) -> None:
        def work() -> None:
            if self.scope_device is not None:
                self.scope_device.close()
                self.scope_device = None
            self.root.after(0, lambda: self.scope_status.set("Disconnected."))
            self.root.after(0, self._disarm_session)

        self._device_worker(self.scope_lock, "Scope", work)

    def _scope_configure(self) -> None:
        try:
            width = float(self.scope_pulse_width.get())
            if width <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Scope", "Expected pulse width must be positive.")
            return

        def work() -> None:
            if self.scope_device is None:
                self.scope_device = RigolMSO7054(
                    resource=self.config["scope"]["visa_resource"],
                    channel=int(self.config["scope"]["channel"]),
                    timeout_ms=int(self.config["scope"]["timeout_ms"]),
                )
            settings = self.scope_device.configure_for_pulse(width, self.config["scope"])
            self.root.after(0, lambda: self.scope_status.set(
                f"Configured: {self.scope_device.identity}\n"
                f"time/div={settings['time_scale_s_div']:.6g} s | trigger={settings['trigger_level_v']:.4g} V"
            ))

        self._device_worker(self.scope_lock, "Scope", work)

    def _scope_arm(self) -> None:
        def work() -> None:
            if self.scope_device is None:
                raise RuntimeError("Connect and configure the scope first.")
            self.scope_device.arm_single()
            self.root.after(0, lambda: self.scope_status.set("Armed: waiting for one trigger."))

        self._device_worker(self.scope_lock, "Scope", work)

    def _scope_acquire(self) -> None:
        def work() -> None:
            if self.scope_device is None:
                raise RuntimeError("Connect and arm the scope first.")
            self.scope_device.wait_complete(float(self.config["scope"]["acquisition_timeout_s"]))
            times, volts = self.scope_device.acquire()
            text = (
                f"Acquired {len(volts)} samples\n"
                f"time {times[0]:.6g} to {times[-1]:.6g} s | voltage {volts.min():.6g} to {volts.max():.6g} V"
            )
            self.root.after(0, lambda value=text: self.scope_status.set(value))

        self._device_worker(self.scope_lock, "Scope", work)

    def _set_focus_controls(self, state: str) -> None:
        for button in (
            self.focus_button,
            self.calibrate_pixel_button,
            self.focus_map_button,
        ):
            button.configure(state=state)

    def _start_focus(self) -> None:
        if self.config["mode"] == "hardware" and not self.focus_safe.get():
            messagebox.showerror(
                "Autofocus blocked",
                "Confirm that the spot is visible, unsaturated, and below the switching threshold.",
            )
            return
        try:
            center = Point(float(self.x.get()), float(self.y.get()), float(self.focus_z.get()))
            span = float(self.focus_span.get())
            samples = int(self.focus_samples.get())
            pixel_size = float(self.pixel_size.get())
            if pixel_size < 0:
                raise ValueError("Scale cannot be negative.")
        except ValueError as exc:
            messagebox.showerror("Autofocus", str(exc))
            return
        resources = self._reserve_resources("Autofocus", "stage", "camera")
        if resources is None:
            return
        self._set_focus_controls("disabled")
        self.run_button.configure(state="disabled")

        def work() -> None:
            system = None
            try:
                system = create_system(self.config, imaging_only=True)
                result, image = autofocus(
                    system.stage,
                    system.camera,
                    center,
                    span,
                    samples,
                    pixel_size or 1.0,
                    0.0 if system.simulated else float(self.config["stage"]["settle_s"]),
                    lambda text: self.root.after(0, lambda value=text: self.focus_text.set(value)),
                )
                self.root.after(0, lambda: self.z.set(f"{result.best_z_um:.4f}"))
                self.root.after(
                    0,
                    lambda calibrated=pixel_size > 0: self._show_focus(result, image, calibrated),
                )
            except Exception as exc:
                self.root.after(
                    0, lambda message=str(exc): messagebox.showerror("Autofocus", message)
                )
            finally:
                if system is not None:
                    system.close()
                self._release_resources(resources)
                self.root.after(0, lambda: self._set_focus_controls("normal"))
                self.root.after(0, lambda: self.run_button.configure(state="normal"))

        self._spawn_worker(work)

    def _start_pixel_calibration(self) -> None:
        try:
            step_um = float(self.calibration_step.get())
            if step_um <= 0:
                raise ValueError("The step must be positive.")
        except ValueError as exc:
            messagebox.showerror("Pixel-to-stage calibration", str(exc))
            return
        if self.config["mode"] == "hardware" and not messagebox.askyesno(
            "Pixel-to-stage calibration",
            "Confirm LED illumination, laser disabled, and visible texture. The MAX will move +X and +Y, then return to the origin.",
        ):
            return
        resources = self._reserve_resources("Pixel-to-stage calibration", "stage", "camera")
        if resources is None:
            return
        self._set_focus_controls("disabled")
        self.run_button.configure(state="disabled")

        def work() -> None:
            system = None
            try:
                system = create_system(self.config, imaging_only=True)
                camera = self.config["camera"]
                result = calibrate_pixel_scale(
                    system.stage,
                    system.camera,
                    step_um,
                    0.0 if system.simulated else float(self.config["stage"]["settle_s"]),
                    float(camera["calibration_min_snr"]),
                    float(camera["calibration_max_return_error_px"]),
                    float(camera["calibration_max_anisotropy"]),
                    lambda text: self.root.after(0, lambda value=text: self.focus_text.set(value)),
                )
                camera["calibration_step_um"] = step_um
                camera["um_per_pixel"] = result.um_per_pixel
                camera["stage_to_pixel_px_per_um"] = [list(row) for row in result.stage_to_pixel_px_per_um]
                camera["pixel_to_stage_um_per_px"] = [list(row) for row in result.pixel_to_stage_um_per_px]
                save_config(self.config, self.config_path)
                self.root.after(0, lambda value=result: self._show_pixel_calibration(value))
            except Exception as exc:
                self.root.after(
                    0,
                    lambda message=str(exc): messagebox.showerror("Pixel-to-stage calibration", message),
                )
            finally:
                if system is not None:
                    system.close()
                self._release_resources(resources)
                self.root.after(0, lambda: self._set_focus_controls("normal"))
                self.root.after(0, lambda: self.run_button.configure(state="normal"))

        self._spawn_worker(work)

    def _start_focus_map(self) -> None:
        if self.config["mode"] == "hardware" and not self.focus_safe.get():
            messagebox.showerror(
                "Focus map",
                "Confirm that the spot is unsaturated and below the switching threshold.",
            )
            return
        try:
            center = Point(float(self.x.get()), float(self.y.get()), float(self.focus_z.get()))
            width, height = float(self.grid_w.get()), float(self.grid_h.get())
            span, samples = float(self.focus_span.get()), int(self.focus_samples.get())
            pixel_size = float(self.pixel_size.get())
            if width <= 0 or height <= 0:
                raise ValueError("Set the raster width and height before mapping focus.")
            if pixel_size < 0:
                raise ValueError("Scale cannot be negative.")
        except ValueError as exc:
            messagebox.showerror("Focus map", str(exc))
            return
        resources = self._reserve_resources("Focus map", "stage", "camera")
        if resources is None:
            return
        centers = [
            Point(center.x_um + dx, center.y_um + dy, center.z_um)
            for dx, dy in (
                (-width / 2, -height / 2),
                (width / 2, -height / 2),
                (width / 2, height / 2),
                (-width / 2, height / 2),
                (0.0, 0.0),
            )
        ]
        self._set_focus_controls("disabled")
        self.run_button.configure(state="disabled")

        def work() -> None:
            system = None
            try:
                system = create_system(self.config, imaging_only=True)
                plane = map_focus_plane(
                    system.stage,
                    system.camera,
                    centers,
                    span,
                    samples,
                    pixel_size or 1.0,
                    0.0 if system.simulated else float(self.config["stage"]["settle_s"]),
                    lambda text: self.root.after(0, lambda value=text: self.focus_text.set(value)),
                )
                self.config["stage"]["focus_plane"] = {
                    "enabled": True,
                    "a": plane.a,
                    "b": plane.b,
                    "c": plane.c,
                    "rms_um": plane.rms_um,
                    "r_squared": plane.r_squared,
                }
                save_config(self.config, self.config_path)
                corrected_z = plane.z(center.x_um, center.y_um)
                self.root.after(0, lambda: self.z.set(f"{corrected_z:.4f}"))
                self.root.after(0, lambda: self.focus_z.set(f"{corrected_z:.4f}"))
                self.root.after(0, lambda value=plane: self._show_focus_plane(value))
            except Exception as exc:
                self.root.after(0, lambda message=str(exc): messagebox.showerror("Focus map", message))
            finally:
                if system is not None:
                    system.close()
                self._release_resources(resources)
                self.root.after(0, lambda: self._set_focus_controls("normal"))
                self.root.after(0, lambda: self.run_button.configure(state="normal"))

        self._spawn_worker(work)

    def _show_focus_plane(self, plane: FocusPlane) -> None:
        self.focus_text.set(
            f"Active plane: z={plane.a:.6g}x {plane.b:+.6g}y {plane.c:+.6g}\n"
            f"RMS={plane.rms_um:.4f} µm; R²={plane.r_squared:.4f}\n"
            "Raster recipes will correct Z automatically."
        )
        if self.focus_figure is None:
            return
        self.focus_figure.clear()
        axis = self.focus_figure.add_subplot(111, projection="3d")
        xs = np.asarray([point.x_um for point in plane.points])
        ys = np.asarray([point.y_um for point in plane.points])
        zs = np.asarray([point.z_um for point in plane.points])
        grid_x, grid_y = np.meshgrid(np.linspace(xs.min(), xs.max(), 12), np.linspace(ys.min(), ys.max(), 12))
        axis.plot_surface(grid_x, grid_y, plane.a * grid_x + plane.b * grid_y + plane.c, alpha=0.35)
        axis.scatter(xs, ys, zs, color="red")
        axis.set(xlabel="X (µm)", ylabel="Y (µm)", zlabel="Focus Z (µm)")
        self.focus_figure.tight_layout()
        self.focus_canvas.draw_idle()

    def _start_live(self) -> None:
        try:
            roi = [int(value.strip()) for value in self.live_roi.get().split(",")]
            if len(roi) != 4 or any(value < 0 for value in roi):
                raise ValueError
            if (roi[2] == 0) != (roi[3] == 0):
                raise ValueError
            pixel_size = float(self.pixel_size.get()) or 1.0
            if pixel_size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Live camera", "ROI must be left,top,width,height; use 0,0,0,0 for the full image."
            )
            return
        if not self.camera_lock.acquire(blocking=False):
            messagebox.showerror("Live camera", "Camera is busy with another acquisition.")
            return
        self.config["camera"]["roi"] = roi
        self.live_stop_event.clear()
        self.live_render_pending = False
        self.live_button.configure(state="disabled")
        self.live_stop_button.configure(state="normal")
        self.camera_status.set("Connecting to camera...")
        self.camera_spot.set("Spot: --")
        self.camera_snr.set("SNR: --")

        def work() -> None:
            system = None
            try:
                system = create_system(self.config, camera_only=True)
                self.live_system = system
                if hasattr(system.camera, "start_stream"):
                    system.camera.start_stream()
                while not self.live_stop_event.is_set():
                    if self.live_render_pending:
                        self.live_stop_event.wait(0.02)
                        continue
                    image = system.camera.capture_spot()
                    if system.simulated and roi[2] and roi[3]:
                        left, top, width, height = roi
                        image = image[top : top + height, left : left + width]
                        if image.size == 0:
                            raise ValueError("ROI lies outside the image.")
                    settings = system.camera.settings() if hasattr(system.camera, "settings") else {}
                    try:
                        measurement = measure_spot(image, pixel_size)
                        measurement_error = ""
                    except ValueError as exc:
                        measurement = None
                        measurement_error = str(exc)
                    self.live_render_pending = True
                    self._post_ui(
                        lambda frame=image, spot=measurement, values=settings, error=measurement_error: self._show_live(
                            frame, spot, values, error
                        )
                    )
                    self.live_stop_event.wait(0.15)
            except Exception as exc:
                self._post_ui(lambda message=str(exc): messagebox.showerror("Live camera", message))
            finally:
                if self.live_system is system:
                    self.live_system = None
                if system is not None:
                    system.close()
                self.camera_lock.release()
                self._post_ui(lambda: self.live_button.configure(state="normal"))
                self._post_ui(lambda: self.live_stop_button.configure(state="disabled"))
                self._post_ui(lambda: self.camera_status.set("Camera stopped"))

        self._spawn_worker(work)

    def _stop_live(self) -> None:
        self.live_stop_event.set()
        self.camera_status.set("Stopping camera...")

    def _show_live(
        self,
        image: np.ndarray,
        measurement: SpotMeasurement | None,
        settings: dict[str, object],
        measurement_error: str = "",
    ) -> None:
        self.live_render_pending = False
        scale_known = float(self.pixel_size.get()) > 0
        exposure = settings.get("exposure_ms", "simulated")
        gain = settings.get("gain_db", "simulated")
        if measurement is None:
            warning = measurement_error
            self.camera_status.set("Spot unavailable")
            self.camera_spot.set(f"Spot: {measurement_error}")
            self.camera_snr.set("SNR: --")
        else:
            unit = "µm" if scale_known else "pixel"
            wx = measurement.w_major_um if scale_known else measurement.w_major_px
            wy = measurement.w_minor_um if scale_known else measurement.w_minor_px
            warning = "SATURATED" if measurement.saturated else "level OK"
            self.camera_status.set("Saturation detected" if measurement.saturated else "Live")
            self.camera_spot.set(f"Spot: wx={wx:.3f}, wy={wy:.3f} {unit}")
            self.camera_snr.set(f"SNR: {measurement.snr:.1f}")
        self.camera_settings.set(f"Exposure: {exposure} ms  |  Gain: {gain} dB")
        if self.camera_figure is None:
            return
        preview, histogram_image, preview_stride = _live_preview_samples(image)
        if self.camera_image_artist is None:
            self.camera_figure.clear()
            self.camera_image_axis = self.camera_figure.add_subplot(121)
            self.camera_histogram_axis = self.camera_figure.add_subplot(122)
            self.camera_image_artist = self.camera_image_axis.imshow(preview)
            (self.camera_spot_artist,) = self.camera_image_axis.plot([], [], "+", color="red")
            self.camera_image_axis.axis("off")
            self.camera_histogram_lines = [
                self.camera_histogram_axis.plot([], [], color=color)[0]
                for color in ("b", "g", "r")
            ]
            self.camera_histogram_axis.axvline(250, color="red", ls="--")
            self.camera_histogram_axis.set(
                xlim=(0, 255), xlabel="Digital level", ylabel="Pixels", title="Histogram"
            )
            self.camera_figure.tight_layout()
        else:
            self.camera_image_artist.set_data(preview)
        preview_height, preview_width = preview.shape[:2]
        self.camera_image_artist.set_extent((-0.5, preview_width - 0.5, preview_height - 0.5, -0.5))
        self.camera_image_axis.set_xlim(-0.5, preview_width - 0.5)
        self.camera_image_axis.set_ylim(preview_height - 0.5, -0.5)
        if measurement is not None:
            self.camera_spot_artist.set_data(
                [measurement.x_px / preview_stride], [measurement.y_px / preview_stride]
            )
        else:
            self.camera_spot_artist.set_data([], [])
        self.camera_image_axis.set_title(
            warning,
            color="red" if measurement is None or measurement.saturated else "green",
        )
        channels = histogram_image[..., None] if histogram_image.ndim == 2 else histogram_image
        maximum = 1
        for channel, line in zip(range(channels.shape[-1]), self.camera_histogram_lines):
            counts, edges = np.histogram(channels[..., channel], bins=64, range=(0, 256))
            line.set_data(0.5 * (edges[:-1] + edges[1:]), counts)
            maximum = max(maximum, int(counts.max()))
        for line in self.camera_histogram_lines[channels.shape[-1] :]:
            line.set_data([], [])
        self.camera_histogram_axis.set_ylim(0, 1.05 * maximum)
        self.camera_canvas.draw_idle()

    def _show_pixel_calibration(self, result: PixelCalibration) -> None:
        self.pixel_size.set(f"{result.um_per_pixel:.8g}")
        self.focus_text.set(
            f"Scale: {result.um_per_pixel:.6f} µm/pixel\n"
            f"X-stage rotation: {result.rotation_deg:.2f}°\n"
            f"Anisotropy: {result.anisotropy:.4f}; SNR={result.registration_snr:.1f}\n"
            f"Return error: {result.return_error_px:.3f} pixel"
        )

    def _show_focus(self, result: FocusResult, image: np.ndarray, scale_calibrated: bool) -> None:
        self.last_focus = result
        best_measurement = min(result.measurements, key=lambda item: item.focus_metric)
        width_text = (
            f"{best_measurement.w_major_um:.3f} × {best_measurement.w_minor_um:.3f} µm"
            if scale_calibrated
            else f"{best_measurement.w_major_px:.2f} x {best_measurement.w_minor_px:.2f} pixel"
        )
        self.focus_text.set(
            f"Best focus: Z={result.best_z_um:.4f} µm\n"
            f"Observed radius: {width_text}\n"
            f"Quadratic fit R²={result.r_squared:.4f}"
        )
        self.apply_spot_button.configure(state="normal" if scale_calibrated else "disabled")
        if self.focus_figure is None:
            return
        self.focus_figure.clear()
        ax1 = self.focus_figure.add_subplot(121)
        ax1.plot(
            result.z_positions_um,
            [item.focus_metric for item in result.measurements],
            "o-",
        )
        ax1.axvline(result.best_z_um, color="green", ls="--")
        ax1.set(xlabel="Z (µm)", ylabel="wx² + wy² (pixel²)")
        ax2 = self.focus_figure.add_subplot(122)
        ax2.imshow(image)
        ax2.set_title("Spot near focus")
        ax2.axis("off")
        self.focus_figure.tight_layout()
        self.focus_canvas.draw_idle()

    def _apply_spot(self) -> None:
        if self.last_focus is None:
            return
        measurement = min(self.last_focus.measurements, key=lambda item: item.focus_metric)
        radius = float(np.sqrt(measurement.w_major_um * measurement.w_minor_um))
        if not messagebox.askyesno(
            "Update thermal model",
            f"Use the observed equivalent radius of {radius:.3f} µm? This includes camera PSF broadening.",
        ):
            return
        self.config["sample"]["spot_radius_um"] = radius
        self.config["camera"]["um_per_pixel"] = float(self.pixel_size.get())
        save_config(self.config, self.config_path)
        self.th_radius.set(f"{radius:.4f}")
        messagebox.showinfo("Spot", "Radius updated in the configuration and thermal model.")

    def _build_guide(self) -> None:
        guide = self.config["guide"]
        controls = ttk.Frame(self.guide_body)
        controls.pack(side="left", fill="y", padx=(0, 10))
        self.guide_roi = self._entry(
            controls, 0, "Waveguide ROI left,top,w,h", ",".join(map(str, guide["detection_roi"]))
        )
        self.guide_length = self._entry(controls, 1, "Length to switch (µm)", str(guide["length_um"]))
        self.guide_step = self._entry(controls, 2, "Maximum step (µm)", str(guide["max_step_um"]))
        self.guide_direction = tk.StringVar(value="start to end")
        self._parameter_label(controls, "Direction").grid(row=3, column=0, sticky="w", padx=4, pady=4)
        direction_combo = ttk.Combobox(
            controls,
            textvariable=self.guide_direction,
            values=("start to end", "end to start"),
            state="readonly",
            width=15,
        )
        self._helped(direction_combo, "Direction").grid(row=3, column=1, sticky="w", padx=4, pady=4)
        self.guide_focus_span = self._entry(
            controls, 4, "Autofocus span (µm)", str(guide["autofocus_span_um"])
        )
        self.guide_focus_samples = self._entry(
            controls, 5, "Autofocus samples", str(guide["autofocus_samples"])
        )
        self.guide_spot_x = self._entry(controls, 6, "Spot X (pixel)", str(guide["spot_pixel"][0]))
        self.guide_spot_y = self._entry(controls, 7, "Spot Y (pixel)", str(guide["spot_pixel"][1]))
        self.guide_tolerance = self._entry(
            controls, 8, "XY tolerance (µm)", str(guide["alignment_tolerance_um"])
        )
        self.guide_max_correction = self._entry(
            controls, 9, "Maximum XY correction (µm)", str(guide["max_correction_um"])
        )
        self.guide_spot_button = ttk.Button(
            controls, text="Register visible spot", command=self._register_guide_spot
        )
        self.guide_spot_button.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(10, 3))
        self.guide_preview_button = ttk.Button(
            controls, text="Autofocus and preview", command=self._preview_guide
        )
        self.guide_preview_button.grid(row=11, column=0, columnspan=2, sticky="ew", pady=3)
        self.guide_execute_button = ttk.Button(
            controls, text="Write waveguide", command=self._start_guide_run, state="disabled", style="Accent.TButton"
        )
        self.guide_execute_button.grid(row=12, column=0, sticky="ew", pady=3)
        self.guide_cancel_button = ttk.Button(
            controls, text="Cancel", command=self._cancel_guide, state="disabled", style="Danger.TButton"
        )
        self.guide_cancel_button.grid(row=12, column=1, sticky="ew", pady=3)
        self.guide_text = tk.StringVar(
            value="Use LED illumination. Pulse settings come from the Recipe tab."
        )
        ttk.Label(controls, textvariable=self.guide_text, wraplength=285).grid(
            row=13, column=0, columnspan=2, sticky="w", pady=8
        )
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure

            self.guide_figure = Figure(figsize=(6.8, 5.8), dpi=100)
            self.guide_canvas = FigureCanvasTkAgg(self.guide_figure, master=self.guide_body)
            self.guide_canvas.get_tk_widget().pack(side="left", fill="both", expand=True)
        except ImportError:
            self.guide_figure = None
        self.guide_plan: GuidePlan | None = None
        self.guide_plan_signature: tuple[Any, ...] | None = None
        self.guide_cancel_event = threading.Event()

    def _guide_values(self) -> dict[str, Any]:
        try:
            roi_values = tuple(int(value.strip()) for value in self.guide_roi.get().split(","))
            if len(roi_values) != 4:
                raise ValueError("ROI must contain four integers.")
            length = float(self.guide_length.get())
            step = float(self.guide_step.get())
            span = float(self.guide_focus_span.get())
            samples = int(self.guide_focus_samples.get())
            spot = (float(self.guide_spot_x.get()), float(self.guide_spot_y.get()))
            tolerance = float(self.guide_tolerance.get())
            max_correction = float(self.guide_max_correction.get())
            if min(length, step, span, tolerance, max_correction) <= 0 or samples < 5:
                raise ValueError("Length, step, focus span, and tolerances must be positive.")
        except ValueError as exc:
            raise ValueError(f"Invalid waveguide parameters: {exc}") from exc
        matrix = self.config["camera"]["pixel_to_stage_um_per_px"]
        if matrix:
            pixel_to_stage = np.asarray(matrix, dtype=float)
        elif self.config["mode"] == "simulation":
            scale = float(self.config["simulation"]["camera_um_per_pixel"])
            pixel_to_stage = -scale * np.eye(2)
        else:
            raise ValueError("Calibrate the pixel-to-stage transform first.")
        size = int(self.config["guide"]["tracking_roi_size_px"])
        tracking_roi = (
            int(round(spot[0] - size / 2)),
            int(round(spot[1] - size / 2)),
            size,
            size,
        )
        direction = 1 if self.guide_direction.get() == "start to end" else -1
        signature = (
            roi_values,
            length,
            step,
            span,
            samples,
            spot,
            direction,
            float(self.x.get()),
            float(self.y.get()),
            float(self.focus_z.get()),
        )
        return {
            "roi": roi_values,
            "tracking_roi": tracking_roi,
            "length": length,
            "step": step,
            "span": span,
            "samples": samples,
            "spot": spot,
            "tolerance": tolerance,
            "max_correction": max_correction,
            "direction": direction,
            "pixel_to_stage": pixel_to_stage,
            "signature": signature,
        }

    def _set_guide_controls(self, state: str) -> None:
        self.guide_spot_button.configure(state=state)
        self.guide_preview_button.configure(state=state)
        self.guide_execute_button.configure(
            state="normal" if state == "normal" and self.guide_plan is not None else "disabled"
        )

    def _guide_progress(self, message: str) -> None:
        self.root.after(0, lambda value=message: self.guide_text.set(value))
        self._write_log("WAVEGUIDE: " + message)

    def _register_guide_spot(self) -> None:
        if self.config["mode"] == "hardware" and not messagebox.askyesno(
            "Register spot",
            "Confirm LED illumination and a visible laser spot at a safe power below the switching threshold. "
            "This button does not enable the laser.",
        ):
            return
        resources = self._reserve_resources("Register spot", "stage", "camera")
        if resources is None:
            return
        self._set_guide_controls("disabled")

        def work() -> None:
            system = None
            try:
                system = create_system(self.config, imaging_only=True)
                image = system.camera.capture_spot()
                measurement = measure_spot(image, 1.0)
                self.config["guide"]["spot_pixel"] = [measurement.x_px, measurement.y_px]
                save_config(self.config, self.config_path)
                self.root.after(0, lambda: self.guide_spot_x.set(f"{measurement.x_px:.3f}"))
                self.root.after(0, lambda: self.guide_spot_y.set(f"{measurement.y_px:.3f}"))
                self.root.after(
                    0,
                    lambda: self.guide_text.set(
                        f"Spot registered: ({measurement.x_px:.2f}, {measurement.y_px:.2f}) pixel; "
                        f"SNR={measurement.snr:.1f}"
                    ),
                )
                self.root.after(0, lambda: self._show_guide_spot(image, measurement.x_px, measurement.y_px))
            except Exception as exc:
                self.root.after(0, lambda message=str(exc): messagebox.showerror("Register spot", message))
            finally:
                if system is not None:
                    system.close()
                self._release_resources(resources)
                self.root.after(0, lambda: self._set_guide_controls("normal"))

        self._spawn_worker(work)

    def _show_guide_spot(self, image: np.ndarray, x_px: float, y_px: float) -> None:
        if self.guide_figure is None:
            return
        self.guide_figure.clear()
        axis = self.guide_figure.add_subplot(111)
        axis.imshow(image)
        axis.plot(x_px, y_px, "r+", ms=14, mew=2)
        axis.set_title("Registered fixed spot")
        axis.axis("off")
        self.guide_figure.tight_layout()
        self.guide_canvas.draw_idle()

    def _preview_guide(self) -> None:
        try:
            values = self._guide_values()
            center = Point(float(self.x.get()), float(self.y.get()), float(self.focus_z.get()))
        except ValueError as exc:
            messagebox.showerror("Waveguide preview", str(exc))
            return
        if self.config["mode"] == "hardware" and not messagebox.askyesno(
            "Structural autofocus",
            "Confirm LED illumination and pump laser off. The stage will scan Z and both ends of the requested length.",
        ):
            return
        resources = self._reserve_resources("Waveguide preview", "stage", "camera")
        if resources is None:
            return
        self.guide_plan = None
        self.guide_plan_signature = None
        self._set_guide_controls("disabled")
        self.run_button.configure(state="disabled")
        self._set_focus_controls("disabled")

        def work() -> None:
            system = None
            try:
                system = create_system(self.config, imaging_only=True)
                settle = 0.0 if system.simulated else float(self.config["stage"]["settle_s"])
                plan, image, focuses = prepare_guide_plan(
                    system.stage,
                    system.camera,
                    center,
                    values["roi"],
                    values["tracking_roi"],
                    values["spot"],
                    values["pixel_to_stage"],
                    values["length"],
                    values["step"],
                    values["direction"],
                    values["span"],
                    values["samples"],
                    float(self.config["guide"]["min_confidence"]),
                    settle,
                    self._guide_progress,
                )
                validate(list(plan.points), self.config["stage"]["range_um"], int(self.config["safety"]["max_points"]))
                self.guide_plan = plan
                self.guide_plan_signature = values["signature"]
                guide = self.config["guide"]
                guide["detection_roi"] = list(values["roi"])
                guide["length_um"] = values["length"]
                guide["max_step_um"] = values["step"]
                guide["autofocus_span_um"] = values["span"]
                guide["autofocus_samples"] = values["samples"]
                guide["spot_pixel"] = list(values["spot"])
                guide["alignment_tolerance_um"] = values["tolerance"]
                guide["max_correction_um"] = values["max_correction"]
                save_config(self.config, self.config_path)
                self.root.after(0, lambda: self._show_guide_plan(plan, image, focuses))
            except Exception as exc:
                self.root.after(0, lambda message=str(exc): messagebox.showerror("Waveguide preview", message))
            finally:
                if system is not None:
                    system.close()
                self._release_resources(resources)
                self.root.after(0, lambda: self._set_guide_controls("normal"))
                self.root.after(0, lambda: self.run_button.configure(state="normal"))
                self.root.after(0, lambda: self._set_focus_controls("normal"))

        self._spawn_worker(work)

    def _show_guide_plan(self, plan: GuidePlan, image: np.ndarray, focuses: tuple[Any, Any]) -> None:
        self.guide_text.set(
            f"Waveguide detected: angle={plan.detection.angle_deg:.2f}°, "
            f"confidence={plan.detection.confidence:.2f}\n"
            f"{plan.length_um:g} µm over {len(plan.points)} points; step={plan.actual_step_um:.4f} µm\n"
            f"Z: {focuses[0].best_z_um:.4f} → {focuses[1].best_z_um:.4f} µm. Review the overlay before writing."
        )
        if self.guide_figure is None:
            return
        self.guide_figure.clear()
        axis = self.guide_figure.add_subplot(111)
        axis.imshow(image)
        start, end = plan.detection.start_px, plan.detection.end_px
        axis.plot([start[0], end[0]], [start[1], end[1]], "y-", lw=1.5, label="waveguide")
        pixels = np.asarray(plan.pixels)
        axis.plot(pixels[:, 0], pixels[:, 1], "c.-", ms=5, lw=1, label="write path")
        spot = tuple(map(float, self.config["guide"]["spot_pixel"]))
        axis.plot(spot[0], spot[1], "r+", ms=14, mew=2, label="fixed spot")
        left, top, width, height = plan.detection.roi
        axis.plot(
            [left, left + width, left + width, left, left],
            [top, top, top + height, top + height, top],
            "w--",
            lw=0.8,
        )
        axis.legend(loc="upper right", fontsize=8)
        axis.set_title("Required preview")
        axis.axis("off")
        self.guide_figure.tight_layout()
        self.guide_canvas.draw_idle()

    def _start_guide_run(self) -> None:
        if self.guide_plan is None:
            return
        try:
            values = self._guide_values()
            if values["signature"] != self.guide_plan_signature:
                raise ValueError("Parameters changed; preview the waveguide again.")
            recipe = Recipe(
                name=(self.name.get().strip() or "run") + "_guide",
                points=list(self.guide_plan.points),
                pulse_width_s=float(self.width_us.get()) * 1e-6,
                repetition_hz=float(self.frequency.get()),
                pulse_count=int(self.count.get()),
                high_v=float(self.high_v.get()),
                optical_power_mw=float(self.power.get()),
            )
            run_config = deepcopy(self.config)
            run_config["stage"]["focus_plane"]["enabled"] = False
            validate_recipe(recipe, run_config)
            readiness = recipe_readiness(recipe, run_config, self.config_path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Write waveguide", str(exc))
            return
        readiness_report = self._readiness_report(readiness)
        if readiness["blocked"]:
            messagebox.showerror(
                "Waveguide readiness",
                readiness_report + "\n\nResolve every BLOCKED item before starting.",
            )
            return
        if self.config["mode"] == "hardware" and not self._confirm_hardware_recipe(
            recipe,
            readiness,
            f"Waveguide length: {self.guide_plan.length_um:g} um. "
            "Confirm LED illumination, the reviewed preview and a safe area.",
        ):
            return
        resources = self._reserve_resources(
            "Write waveguide", "awg", "laser", "scope", "stage", "camera"
        )
        if resources is None:
            return
        self.guide_cancel_event.clear()
        self._set_guide_controls("disabled")
        self.guide_cancel_button.configure(state="normal")
        self.run_button.configure(state="disabled")
        self._set_focus_controls("disabled")
        self._guide_progress("RUN READINESS\n" + readiness_report)
        plan = self.guide_plan

        def adjust(system: Any, target: Point, index: int) -> dict[str, Any]:
            result = align_waveguide_at_spot(
                system.stage,
                system.camera,
                values["spot"],
                values["pixel_to_stage"],
                values["tracking_roi"],
                plan.detection.direction_px,
                values["tolerance"],
                values["max_correction"],
                int(self.config["guide"]["max_alignment_iterations"]),
                float(self.config["guide"]["min_confidence"]),
                0.0 if system.simulated else float(self.config["stage"]["settle_s"]),
            )
            self._guide_progress(
                f"Point {index + 1}/{len(plan.points)} aligned: error={result.error_um:.3f} µm"
            )
            return {
                "error_um": result.error_um,
                "iterations": result.iterations,
                "confidence": result.confidence,
            }

        def work() -> None:
            failed = False
            try:
                output = run_recipe(
                    recipe,
                    run_config,
                    self.config_path,
                    self._guide_progress,
                    self.guide_cancel_event.is_set,
                    adjust,
                )
                self._guide_progress(f"Writing complete: {output}")
                self.root.after(0, lambda path=output: self._show_last_result(path))
            except Exception as exc:
                failed = True
                self._guide_progress(f"ERROR: {type(exc).__name__}: {exc}")
                self.root.after(0, lambda message=str(exc): messagebox.showerror("Waveguide writing", message))
            finally:
                self._release_resources(resources)
                self.root.after(0, lambda: self._set_guide_controls("normal"))
                self.root.after(0, lambda: self.guide_cancel_button.configure(state="disabled"))
                self.root.after(0, lambda: self.run_button.configure(state="normal"))
                self.root.after(0, lambda: self._set_focus_controls("normal"))
                self.root.after(
                    0, lambda invalid=failed: self._disarm_session(invalidate_preflight=invalid)
                )

        self._spawn_worker(work)

    def _cancel_guide(self) -> None:
        self.guide_cancel_event.set()
        self._guide_progress("Cancellation requested; stopping before the next pulse.")

    def _build_diagnostics(self) -> None:
        ttk.Label(
            self.diag_body,
            text=(
                "Preflight forces C1 and the Stradus output OFF, identifies the equipment, and checks "
                "drivers and interlocks. It never moves the stage."
            ),
            wraplength=850,
        ).pack(anchor="w", pady=(0, 10))
        config_frame = ttk.LabelFrame(self.diag_body, text="Connection", padding=8)
        config_frame.pack(side="left", fill="y", padx=(0, 10))
        self._parameter_label(config_frame, "Mode").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.cfg_mode = tk.StringVar(value=self.config["mode"])
        mode_combo = ttk.Combobox(
            config_frame,
            textvariable=self.cfg_mode,
            values=("simulation", "hardware"),
            state="readonly",
            width=18,
        )
        self._helped(mode_combo, "Mode").grid(row=0, column=1, sticky="w", padx=4, pady=2)
        self.cfg_awg_model = tk.StringVar(value=self.config["awg"].get("model", "T3AFG350"))
        self._parameter_label(config_frame, "AWG model").grid(
            row=0, column=2, sticky="w", padx=(14, 4), pady=2
        )
        awg_model_combo = ttk.Combobox(
            config_frame,
            textvariable=self.cfg_awg_model,
            values=("T3AFG350", "DG1062Z"),
            state="readonly",
            width=13,
        )
        self._helped(awg_model_combo, "AWG model").grid(
            row=0, column=3, sticky="w", padx=4, pady=2
        )
        self.cfg_awg, self.cfg_awg_combo = self._device_entry(
            config_frame, 1, "AWG VISA", self.config["awg"]["visa_resource"]
        )
        self.cfg_laser, self.cfg_laser_combo = self._device_entry(
            config_frame, 2, "Stradus USB/RS232", self.config["laser"]["visa_resource"]
        )
        self.scan_button = ttk.Button(
            config_frame, text="Scan PC", command=self._scan_devices, style="Accent.TButton"
        )
        self.scan_button.grid(row=1, column=2, columnspan=2, sticky="ew", padx=(14, 4), pady=2)
        self.scan_status = tk.StringVar(value="No device scan run")
        ttk.Label(config_frame, textvariable=self.scan_status, wraplength=220).grid(
            row=2, column=2, columnspan=2, sticky="nw", padx=(14, 4), pady=4
        )
        self.cfg_laser_power = self._entry(
            config_frame, 3, "Manual Stradus PP (mW)", str(self.config["laser"]["peak_power_mw"])
        )
        self.cfg_power_calibration = self._entry(
            config_frame,
            4,
            "Sample power:PP calibration",
            ",".join(f"{sample:g}:{pp:g}" for sample, pp in self.config["laser"]["power_calibration"]),
        )
        self.cfg_scope, self.cfg_scope_combo = self._device_entry(
            config_frame, 5, "Oscilloscope VISA", self.config["scope"]["visa_resource"]
        )
        self.cfg_stage, self.cfg_stage_combo = self._device_entry(
            config_frame, 6, "BPC303 serial", self.config["stage"]["serial_number"]
        )
        self.cfg_camera_serial, self.cfg_camera_combo = self._device_entry(
            config_frame, 7, "Pixelink serial (0=single)", str(self.config["camera"]["serial_number"])
        )
        self.cfg_camera_exposure = self._entry(
            config_frame, 8, "Exposure (ms)", str(self.config["camera"]["exposure_ms"])
        )
        self.cfg_camera_gain = self._entry(
            config_frame, 9, "Gain (dB)", str(self.config["camera"]["gain_db"])
        )
        self.cfg_camera_roi = self._entry(
            config_frame, 10, "ROI left,top,width,height", ",".join(map(str, self.config["camera"]["roi"]))
        )
        self.cfg_camera_auto = tk.BooleanVar(value=self.config["camera"]["auto_exposure"])
        camera_auto = ttk.Checkbutton(
            config_frame, text="Anti-saturation auto exposure  ⓘ", variable=self.cfg_camera_auto
        )
        self._helped(camera_auto, "Anti-saturation auto exposure").grid(
            row=11, column=0, columnspan=2, sticky="w", padx=4, pady=4
        )
        self.cfg_scope_scale = self._entry(
            config_frame, 12, "Scope V/div", str(self.config["scope"]["vertical_scale_v_div"])
        )
        self.cfg_scope_trigger = self._entry(
            config_frame, 13, "Trigger (V)", str(self.config["scope"]["trigger_level_v"])
        )
        self.cfg_scope_impedance = self._entry(
            config_frame, 14, "Scope input", self.config["scope"]["input_impedance"]
        )
        self.cfg_scope_slope = self._entry(
            config_frame, 15, "Trigger slope", self.config["scope"]["trigger_slope"]
        )
        self.cfg_position_tolerance = self._entry(
            config_frame,
            16,
            "Position tolerance (µm)",
            str(self.config["stage"]["position_tolerance_um"]),
        )
        self.cfg_move_timeout = self._entry(
            config_frame, 17, "Move timeout (s)", str(self.config["stage"]["move_timeout_s"])
        )
        self.cfg_armed = tk.BooleanVar(value=self.config["safety"]["hardware_armed"])
        self.armed_check = ttk.Checkbutton(
            config_frame,
            text="Hardware armed for this session  ⓘ",
            variable=self.cfg_armed,
            state="disabled",
        )
        self._helped(self.armed_check, "Hardware armed for this session")
        self.armed_check.grid(
            row=18, column=0, columnspan=2, sticky="w", padx=4, pady=4
        )
        self.cfg_calibrated = tk.BooleanVar(value=self.config["stage"]["calibrated"])
        calibrated_check = ttk.Checkbutton(
            config_frame,
            text="Stage calibrated (limits and directions confirmed)  ⓘ",
            variable=self.cfg_calibrated,
        )
        self._helped(
            calibrated_check, "Stage calibrated (limits and directions confirmed)"
        ).grid(row=19, column=0, columnspan=2, sticky="w", padx=4, pady=4)
        ttk.Button(config_frame, text="Save configuration", command=self._save_connection).grid(
            row=20, column=0, sticky="ew", padx=4, pady=6
        )
        self.diagnose_button = ttk.Button(
            config_frame, text="Run diagnostics", command=self._diagnose, style="Accent.TButton"
        )
        self.diagnose_button.grid(
            row=20, column=1, sticky="ew", padx=4, pady=6
        )
        self.diag_text = tk.Text(
            self.diag_body,
            height=28,
            wrap="word",
            state="disabled",
            background=self.colors["surface"],
            foreground=self.colors["text"],
            relief="flat",
            padx=10,
            pady=10,
        )
        self.diag_text.pack(side="left", fill="both", expand=True)

    def _scan_devices(self) -> None:
        resources = self._reserve_resources(
            "Device scan", "awg", "laser", "scope", "stage", "camera"
        )
        if resources is None:
            return
        self.scan_button.configure(state="disabled")
        self.scan_status.set("Scanning PC...")

        def work() -> None:
            try:
                report = scan_connected_hardware(self.config)
                self.root.after(0, lambda: self._apply_device_scan(report))
            except Exception as exc:
                self.root.after(0, lambda message=str(exc): self._device_scan_failed(message))
            finally:
                self._release_resources(resources)

        self._spawn_worker(work)

    def _device_scan_failed(self, message: str) -> None:
        self.scan_button.configure(state="normal")
        self.scan_status.set("Scan failed")
        messagebox.showerror("Device scan", message)

    def _apply_device_scan(self, report: dict[str, list[dict[str, str]]]) -> None:
        visa = [row for row in report["visa"] if row.get("resource")]
        all_visa = [row["resource"] for row in visa]

        def choices(role: str) -> list[str]:
            matched = [row["resource"] for row in visa if row.get("role") == role]
            return matched or all_visa

        def update(combo: ttk.Combobox, variable: tk.StringVar, values: list[str]) -> None:
            combo.configure(values=values)
            if len(values) == 1 and variable.get() not in values:
                variable.set(values[0])

        awgs, scopes = choices("awg"), choices("scope")
        lasers = [
            row["resource"]
            for row in report.get("stradus_usb", []) + visa
            if row.get("resource") and row.get("role") == "laser"
        ]
        stages = [row["serial"] for row in report["kinesis"] if row.get("serial")]
        cameras = [row["serial"] for row in report["pixelink"] if row.get("serial")]
        update(self.cfg_awg_combo, self.cfg_awg, awgs)
        update(self.cfg_laser_combo, self.cfg_laser, lasers)
        update(self.cfg_scope_combo, self.cfg_scope, scopes)
        update(self.cfg_stage_combo, self.cfg_stage, stages)
        update(self.cfg_camera_combo, self.cfg_camera_serial, cameras)

        selected_awg = next((row for row in visa if row["resource"] == self.cfg_awg.get()), None)
        if selected_awg:
            identity = selected_awg.get("identity", "").upper()
            if "DG1062Z" in identity:
                self.cfg_awg_model.set("DG1062Z")
            elif "T3AFG350" in identity:
                self.cfg_awg_model.set("T3AFG350")

        lines = ["Connected-device scan (read-only):"]
        for section, rows in report.items():
            lines.append(f"\n{section.upper()}")
            for row in rows:
                lines.append("  " + " | ".join(f"{key}={value}" for key, value in row.items()))
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("end", "\n".join(lines))
        self.diag_text.configure(state="disabled")
        self.scan_status.set(
            f"Found {len(visa)} VISA, {len(lasers)} Stradus, {len(stages)} Kinesis, {len(cameras)} Pixelink, "
            f"{sum('FriendlyName' in row for row in report['windows_pnp'])} PnP"
        )
        self.scan_button.configure(state="normal")

    def _connection_candidate(self) -> dict[str, Any]:
        candidate = deepcopy(self.config)
        candidate["mode"] = self.cfg_mode.get().strip()
        candidate["awg"]["model"] = self.cfg_awg_model.get().strip()
        candidate["awg"]["visa_resource"] = self.cfg_awg.get().strip()
        candidate["laser"]["visa_resource"] = self.cfg_laser.get().strip()
        candidate["laser"]["peak_power_mw"] = float(self.cfg_laser_power.get())
        calibration_text = self.cfg_power_calibration.get().strip()
        candidate["laser"]["power_calibration"] = (
            [[float(value) for value in pair.split(":")] for pair in calibration_text.split(",")]
            if calibration_text
            else []
        )
        candidate["scope"]["visa_resource"] = self.cfg_scope.get().strip()
        candidate["stage"]["serial_number"] = self.cfg_stage.get().strip()
        candidate["camera"]["serial_number"] = int(self.cfg_camera_serial.get())
        candidate["camera"]["exposure_ms"] = float(self.cfg_camera_exposure.get())
        candidate["camera"]["gain_db"] = float(self.cfg_camera_gain.get())
        candidate["camera"]["roi"] = [
            int(value.strip()) for value in self.cfg_camera_roi.get().split(",")
        ]
        candidate["camera"]["auto_exposure"] = bool(self.cfg_camera_auto.get())
        candidate["scope"]["vertical_scale_v_div"] = float(self.cfg_scope_scale.get())
        candidate["scope"]["trigger_level_v"] = float(self.cfg_scope_trigger.get())
        candidate["scope"]["input_impedance"] = self.cfg_scope_impedance.get().strip()
        candidate["scope"]["trigger_slope"] = self.cfg_scope_slope.get().strip()
        candidate["stage"]["position_tolerance_um"] = float(self.cfg_position_tolerance.get())
        candidate["stage"]["move_timeout_s"] = float(self.cfg_move_timeout.get())
        candidate["safety"]["hardware_armed"] = bool(self.cfg_armed.get())
        candidate["stage"]["calibrated"] = bool(self.cfg_calibrated.get())
        validate_config(candidate)
        return candidate

    @staticmethod
    def _preflight_key(config: dict[str, Any]) -> str:
        comparable = deepcopy(config)
        comparable["safety"]["hardware_armed"] = False
        return json.dumps(comparable, sort_keys=True)

    def _persist_preflight_report(
        self, candidate: dict[str, Any], checks: list[tuple[str, str, str]]
    ) -> Path:
        now = datetime.now(timezone.utc)
        root = resolve_results_dir(candidate, self.config_path)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"preflight_{now.strftime('%Y%m%dT%H%M%S.%fZ')}.json"
        payload = {
            "created_utc": now.isoformat(),
            "passed": all(status == "READY" for _, status, _ in checks),
            "checks": [
                {"name": name, "status": status, "detail": detail}
                for name, status, detail in checks
            ],
            "configuration": candidate,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _diagnose(self) -> None:
        self.config["safety"]["hardware_armed"] = False
        self.cfg_armed.set(False)
        self.armed_check.configure(state="disabled")
        self.preflight_config = None
        self._update_mode_status()
        try:
            candidate = self._connection_candidate()
        except Exception as exc:
            messagebox.showerror("Invalid configuration", str(exc))
            return
        candidate["safety"]["hardware_armed"] = False
        if candidate["mode"] == "simulation":
            report = (
                "[READY] Simulation mode: no hardware output is possible.\n\n"
                "Switch to hardware mode and run diagnostics before hardware can be armed.\n\n"
                "Active form configuration:\n" + json.dumps(candidate, indent=2)
            )
            self.diag_text.configure(state="normal")
            self.diag_text.delete("1.0", "end")
            self.diag_text.insert("end", report)
            self.diag_text.configure(state="disabled")
            self._update_mode_status()
            return
        resources = self._reserve_resources(
            "Diagnostics", "awg", "laser", "scope", "stage", "camera"
        )
        if resources is None:
            return
        self.diagnose_button.configure(state="disabled")
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("end", "Running safe hardware preflight...\n")
        self.diag_text.configure(state="disabled")

        def work() -> None:
            try:
                checks = [
                    item for item in discover_hardware(candidate) if item[0] != "Hardware armed"
                ]
                self._post_ui(lambda: self._apply_diagnostics(candidate, checks))
            except Exception as exc:
                self._post_ui(lambda message=str(exc): self._diagnostics_failed(message))
            finally:
                self._release_resources(resources)

        self._spawn_worker(work)

    def _diagnostics_failed(self, message: str) -> None:
        self.diagnose_button.configure(state="normal")
        self.preflight_config = None
        self.armed_check.configure(state="disabled")
        self.cfg_armed.set(False)
        self._update_mode_status()
        messagebox.showerror("Diagnostics", message)

    def _apply_diagnostics(
        self, candidate: dict[str, Any], checks: list[tuple[str, str, str]]
    ) -> None:
        self.diagnose_button.configure(state="normal")
        try:
            report_path = self._persist_preflight_report(candidate, checks)
            checks.append(("Preflight report", "READY", str(report_path)))
        except Exception as exc:
            report_path = None
            checks.append(("Preflight report", "BLOCKED", str(exc)))
        counts = {
            status: sum(item[1] == status for item in checks)
            for status in ("READY", "MISSING", "BLOCKED")
        }
        ready = counts["MISSING"] == 0 and counts["BLOCKED"] == 0
        if ready:
            self.preflight_config = self._preflight_key(candidate)
            self.armed_check.configure(state="normal")
        report = "\n".join(f"[{status}] {name}: {detail}" for name, status, detail in checks)
        report += f"\n\nSummary: READY {counts['READY']} | MISSING {counts['MISSING']} | BLOCKED {counts['BLOCKED']}"
        report += (
            "\n\nPREFLIGHT PASSED: hardware may be armed for this session."
            if ready
            else "\n\nPREFLIGHT FAILED: hardware remains disarmed."
        )
        report += "\n\nActive form configuration:\n" + json.dumps(candidate, indent=2)
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("end", report)
        self.diag_text.configure(state="disabled")
        self._update_mode_status()
        self._write_log(
            f"PREFLIGHT {'PASSED' if ready else 'FAILED'}"
            + (f" | report={report_path}" if report_path else " | report write failed")
        )

    def _save_connection(self) -> None:
        resources = self._reserve_resources(
            "Save configuration", "awg", "laser", "scope", "stage", "camera"
        )
        if resources is None:
            return
        self._release_resources(resources)
        try:
            candidate = self._connection_candidate()
            wants_armed = candidate["mode"] == "hardware" and candidate["safety"]["hardware_armed"]
            if wants_armed and self._preflight_key(candidate) != self.preflight_config:
                self.cfg_armed.set(False)
                raise ValueError(
                    "Run diagnostics again with this exact configuration before arming hardware."
                )
            if wants_armed and not messagebox.askyesno(
                "Arm hardware",
                "Arming enables real motion and laser output for this session. Confirm that the setup is safe.",
            ):
                self.cfg_armed.set(False)
                return
            if candidate["stage"]["calibrated"] and not self.config["stage"]["calibrated"]:
                if not messagebox.askyesno(
                    "Confirm calibration",
                    "Confirm that travel, origin, direction, and units have been physically verified on all three axes.",
                ):
                    self.cfg_calibrated.set(False)
                    return
            if candidate["mode"] != "hardware":
                candidate["safety"]["hardware_armed"] = False
                self.cfg_armed.set(False)
            save_config(candidate, self.config_path)
            self.config = candidate
            if candidate["safety"]["hardware_armed"]:
                self.safe_all_event.clear()
            if (
                candidate["mode"] != "hardware"
                or self._preflight_key(candidate) != self.preflight_config
            ):
                self.preflight_config = None
                self.armed_check.configure(state="disabled")
            self._update_mode_status()
            messagebox.showinfo(
                "Configuration",
                f"Saved to {self.config_path.resolve()}\n\n"
                + (
                    "Hardware is armed for this session only."
                    if candidate["safety"]["hardware_armed"]
                    else "Hardware remains disarmed."
                ),
            )
        except Exception as exc:
            messagebox.showerror("Invalid configuration", str(exc))


def launch(config_path: str | Path = "config.json") -> None:
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    PumpAutoUI(root, config_path)
    root.mainloop()
