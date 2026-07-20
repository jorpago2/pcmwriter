# Spectral data

- `sb2se3_amorphous.txt`: copy of `250618_32nm-Sb2Se3_withoutann.txt`.
- `sb2se3_crystalline.txt`: copy of `250630_32nm-Sb2Se3_ann.txt`.
- `instrument_response.csv`: MNWHL4 spectrum, unpolarized DMSP550 transmission at 45 degrees, and digitized Pixelink curves. Efficiencies and transmission values are expressed between 0 and 1.

The calculation uses the silicon dispersion from M. A. Green, *Solar Energy Materials and Solar Cells* 92, 1305-1310 (2008), and the SiO2 Sellmeier equation from I. H. Malitson, *JOSA* 55, 1205-1209 (1965).

The Sb2Se3 refractive indices were measured on 32 nm films. The simulated thickness remains the device-layer thickness configured in `sample.optical_layers` (40 nm by default).
