# Datos espectrales

- `sb2se3_amorphous.txt`: copia de `250618_32nm-Sb2Se3_withoutann.txt`.
- `sb2se3_crystalline.txt`: copia de `250630_32nm-Sb2Se3_ann.txt`.
- `instrument_response.csv`: espectro MNWHL4, transmision no polarizada DMSP550 a 45 grados y curvas Pixelink digitalizadas. Las eficiencias y transmisiones se expresan entre 0 y 1.

El calculo usa la dispersion de Si de M. A. Green, *Solar Energy Materials and Solar Cells* 92, 1305-1310 (2008), y la ecuacion de Sellmeier de SiO2 de I. H. Malitson, *JOSA* 55, 1205-1209 (1965).

Los indices de Sb2Se3 proceden de peliculas de 32 nm. El espesor simulado sigue siendo el de la capa del dispositivo configurada en `sample.optical_layers` (40 nm por defecto).
