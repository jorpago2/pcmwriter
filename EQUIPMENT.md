# Equipment reference

These specifications were checked against manufacturer documentation so the software can be prepared before laboratory access.

| Equipment | Relevant specifications | Automation implications |
|---|---|---|
| Vortran Stradus 639-160 | 639 +/- 4 nm, 160 mW nominal output (up to +10%), digital modulation up to 200 MHz, and <2 ns rise time. 50 ohm SMB input: OFF <=0.8 V, ON 3.5-5 V | The initial recipe limit is 10 ns. The T3AFG output is configured for a 50 ohm load and 0-5 V levels. `PUL=1` must be enabled after each laser power cycle. |
| Teledyne LeCroy T3AFG350 | 3.3 ns minimum pulse width, 100 ps pulse-width resolution, and edge times from 1 ns | The 10 ns-10 ms range is within specification. The adapter uses the documented `OUTP LOAD`, `BSWV`, `BTWV`, and `MTRIG` SCPI commands. |
| Rigol DG1062Z | 16 ns minimum pulse width, 40 ns minimum period, and manually triggered N-cycle burst | The adapter enforces a 50 ohm load, 0-5 V levels, manual burst, and an initially disabled output. Recipes reject widths below 16 ns and pulse frequencies above 25 MHz. |
| Thorlabs DET02AFC | Silicon, FC/PC, 400-1100 nm, 1 GHz, <=1 ns rise/fall time, 50 ohm minimum load, positive output up to 3.3 V into 50 ohm, and 18 mW maximum peak power | Configure the Rigol for DC coupling, 50 ohm input, and a positive edge. Start at 1 V/div with a 0.1 V trigger. The 10% monitor branch may approach the 18 mW limit when the laser operates at full power. |
| Rigol MSO7054 | 500 MHz, 10 GSa/s with one channel, and 100 Mpts standard memory | Use full bandwidth, single-shot acquisition, and automatic time scaling. The single-channel sample rate is sufficient for 10 ns pulses. |
| Thorlabs BPC303 | Three channels, configurable output up to 150 V, USB, and Kinesis control | The controller can exceed the stage limit, so PCMWriter caps this setup at 75 V. |
| Thorlabs MAX311D/M | Closed-loop XYZ piezos, 20 um per axis, 0-75 V, 5 nm theoretical resolution, and 50 nm bidirectional repeatability | Automated rasters use only the 20 um piezo travel. The 4 mm differential drives remain outside automation. |
| Thorlabs MY50X-805 | 50X with a 200 mm tube lens, NA 0.55, 13 mm WD, 436-656 nm range, and 0.6 um stated Rayleigh resolution at 550 nm | At 639 nm, the theoretical Rayleigh resolution is approximately 0.71 um. The actual tube lens must be known to convert camera pixels to micrometres correctly. |
| Pixelink M18-CYL | Colour, 4912 x 3680, 1.25 um pixels, 14 fps, 1/2.3-inch AR1820 rolling-shutter sensor | Suitable for static imaging, spot measurement, and autofocus; it is not used for temporal pulse measurement. PCMWriter controls it as M18-CYL/PL-D7718 through Pixelink API 4.0. |

## Temporal response budget

Approximating the independent responses as Gaussian, the combined instrumental rise time is:

`sqrt(2 ns^2 + 1 ns^2 + 0.7 ns^2) = 2.34 ns`

The terms correspond to the laser (<2 ns), DET02AFC (1 ns), and MSO7054 (`0.35 / 500 MHz = 0.7 ns`). Therefore, 10 ns is a reasonable initial minimum for pulse characterization. Below this value, instrument-response deconvolution becomes important.

## Initial Rigol configuration

- CH1, DC coupling, and 50 ohm input.
- Full bandwidth.
- Single-shot acquisition with a positive EDGE trigger on CH1 at 0.1 V.
- 1 V/div so a possible 3.3 V output is not clipped.
- Total acquisition window equal to six pulse widths, with the trigger centred.
- After observing the first low-power pulse, adjust V/div to occupy 4-6 divisions and set the trigger to approximately 30% of the measured amplitude.

## Manufacturer documentation

- [Stradus 639-160 datasheet](https://vortranlaser.com/wp-content/uploads/2024/02/Stradus_639-160_Datasheet_12052.pdf)
- [Stradus user manual](https://preview-assets-us-01.kc-usercontent.com/078db625-9c43-005d-663d-c89ba2b8888d/eaba4f91-42f3-405f-abb3-8b61f863e873/Stradus%20Manual.pdf)
- [T3AFG programming guide](https://www.teledynelecroy.com/files/manuals/t3afg-programming-guide.pdf) and [T3AFG200/350/500 datasheet](https://cdn.teledynelecroy.com/files/pdf/t3afg200-350-500-datasheet.pdf)
- [Rigol DG1000Z programming guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/waveform-generators/DG1000Z_ProgrammingGuide_EN.pdf) and [DG1062Z product page](https://www.rigolna.com/products/waveform-generators/dg1000z/)
- [Rigol MSO7000 programming guide](https://eu.rigol.com/eu/Images/DS7000ProgrammingGuideEN_tcm30-3985.pdf) and [datasheet](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MSO7000-DS7000_DataSheet-EN.pdf)
- [Thorlabs DET02AFC](https://www.thorlabs.com/thorproduct.cfm?partnumber=DET02AFC)
- [BPC303 manual](https://www.thorlabs.com/images/TabImages/22883-D02.pdf), [Kinesis](https://www.thorlabs.com/newgrouppage9.cfm?objectgroup_id=10285), and [Kinesis C# quick start](https://www.thorlabs.com/images/tabimages/Kinesis_with_C_Quick_Start_Guide.pdf)
- [MAX311D/M specifications](https://www.thorlabs.com/newgrouppage9.cfm?objectgroup_id=2386)
- [MY50X-805 specifications](https://www.thorlabs.com/NewGroupPage9.cfm?ObjectGroup_ID=1044&Visual_ID=1725)
- [Pixelink SDK](https://support.pixelink.com/support/solutions/articles/3000034901-latest-windows-sdk) and [M18-CYL datasheet](https://www.navitar.com/-/media/project/oneweb/oneweb/navitar/pixelink-m-datasheets/m18-cyl-datasheet.pdf)
