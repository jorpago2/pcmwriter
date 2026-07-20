# Referencia de equipos

Datos contrastados con la documentacion de fabricante para preparar el software antes de acceder al laboratorio.

| Equipo | Datos relevantes | Consecuencia para la automatizacion |
|---|---|---|
| Vortran Stradus 639-160 | 639 +/- 4 nm, 160 mW nominales (+10% posible), modulacion digital hasta 200 MHz y subida <2 ns. Entrada SMB de 50 ohm: OFF <=0.8 V, ON 3.5-5 V | El limite inicial de receta se fija en 10 ns. El T3AFG se fuerza a carga de 50 ohm y niveles 0-5 V. Hay que activar `PUL=1` despues de cada encendido del laser. |
| Teledyne LeCroy T3AFG350 | Pulso minimo 3.3 ns, ajuste de ancho con 100 ps de resolucion y flancos desde 1 ns | El rango 10 ns-10 ms esta dentro de especificaciones. Los comandos `OUTP LOAD`, `BSWV`, `BTWV` y `MTRIG` usados por el adaptador coinciden con la guia SCPI. |
| Rigol DG1062Z | Pulso minimo 16 ns, periodo minimo 40 ns y burst N-cycle con disparo manual | El adaptador fuerza carga de 50 ohm, niveles 0-5 V, burst manual y salida inicialmente OFF. La receta bloquea anchos menores de 16 ns y frecuencias de pulso superiores a 25 MHz. |
| Thorlabs DET02AFC | Si, FC/PC, 400-1100 nm, 1 GHz, subida/bajada maxima 1 ns, carga minima 50 ohm, salida positiva hasta 3.3 V sobre 50 ohm y potencia pico maxima 18 mW | Rigol en DC, 50 ohm, flanco positivo. Primer disparo a 1 V/div y trigger 0.1 V. La rama del 10% puede acercarse peligrosamente a 18 mW si el laser trabaja a plena potencia. |
| Rigol MSO7054 | 500 MHz, 10 GSa/s con un canal y 100 Mpts de memoria estandar | Ancho de banda completo, single-shot y escala temporal automatica. Con un solo canal la tasa maxima es suficiente para pulsos de 10 ns. |
| Thorlabs BPC303 | Tres canales, salida configurable hasta 150 V, USB y control por Kinesis | El controlador puede superar el limite del stage; el software mantiene 75 V como maximo para este montaje. |
| Thorlabs MAX311D/M | XYZ, piezos en lazo cerrado, 20 um por eje, 0-75 V, resolucion teorica 5 nm y repetibilidad bidireccional 50 nm | El raster automatico solo usa los 20 um piezoelectricos; los mandos diferenciales de 4 mm quedan fuera de la automatizacion. |
| Thorlabs MY50X-805 | 50X con lente de tubo de 200 mm, NA 0.55, WD 13 mm, rango 436-656 nm y resolucion Rayleigh indicada 0.6 um a 550 nm | A 639 nm, la resolucion de Rayleigh teorica es aproximadamente 0.71 um. Hace falta conocer la lente de tubo para convertir correctamente pixeles a micras. |
| Pixelink M18-CYL | Color, 4912 x 3680, pixel 1.25 um, 14 fps, sensor AR1820 de 1/2.3 pulgadas y rolling shutter | Adecuada para imagen estatica, medida de spot y autofocus; no se usa para medir temporalmente el pulso. Se controla como M18-CYL/PL-D7718 mediante Pixelink API 4.0. |

## Presupuesto temporal

Tomando como aproximacion respuestas gaussianas independientes, la subida instrumental conjunta es:

`sqrt(2 ns^2 + 1 ns^2 + 0.7 ns^2) = 2.34 ns`

Los terminos son el laser (<2 ns), el DET02AFC (1 ns) y el MSO7054 (`0.35 / 500 MHz = 0.7 ns`). Por ello 10 ns es un minimo inicial razonable para caracterizar el pulso; por debajo de ese valor la deconvolucion del instrumento empezaria a ser importante.

## Configuracion inicial del Rigol

- CH1, DC y 50 ohm.
- Ancho de banda sin limitar.
- Single-shot, trigger EDGE positivo en CH1 a 0.1 V.
- 1 V/div para que una salida posible de 3.3 V no quede recortada.
- Ventana total igual a seis anchos de pulso, con el trigger centrado.
- Tras observar el primer pulso de baja potencia, ajustar V/div para ocupar 4-6 divisiones y el trigger a aproximadamente 30% de la amplitud medida.

## Documentacion de fabricante

- [Stradus 639-160 datasheet](https://vortranlaser.com/wp-content/uploads/2024/02/Stradus_639-160_Datasheet_12052.pdf)
- [Stradus user manual](https://preview-assets-us-01.kc-usercontent.com/078db625-9c43-005d-663d-c89ba2b8888d/eaba4f91-42f3-405f-abb3-8b61f863e873/Stradus%20Manual.pdf)
- [T3AFG programming guide](https://www.teledynelecroy.com/files/manuals/t3afg-programming-guide.pdf) y [datasheet T3AFG200/350/500](https://cdn.teledynelecroy.com/files/pdf/t3afg200-350-500-datasheet.pdf)
- [Rigol DG1000Z programming guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/waveform-generators/DG1000Z_ProgrammingGuide_EN.pdf) y [DG1062Z product page](https://www.rigolna.com/products/waveform-generators/dg1000z/)
- [Rigol MSO7000 programming guide](https://eu.rigol.com/eu/Images/DS7000ProgrammingGuideEN_tcm30-3985.pdf) y [datasheet](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MSO7000-DS7000_DataSheet-EN.pdf)
- [Thorlabs DET02AFC](https://www.thorlabs.com/thorproduct.cfm?partnumber=DET02AFC)
- [BPC303 manual](https://www.thorlabs.com/images/TabImages/22883-D02.pdf), [Kinesis](https://www.thorlabs.com/newgrouppage9.cfm?objectgroup_id=10285) y [Kinesis C# quick start](https://www.thorlabs.com/images/tabimages/Kinesis_with_C_Quick_Start_Guide.pdf)
- [MAX311D/M specifications](https://www.thorlabs.com/newgrouppage9.cfm?objectgroup_id=2386)
- [MY50X-805 specifications](https://www.thorlabs.com/NewGroupPage9.cfm?ObjectGroup_ID=1044&Visual_ID=1725)
- [Pixelink SDK](https://support.pixelink.com/support/solutions/articles/3000034901-latest-windows-sdk) y [M18-CYL datasheet](https://www.navitar.com/-/media/project/oneweb/oneweb/navitar/pixelink-m-datasheets/m18-cyl-datasheet.pdf)
