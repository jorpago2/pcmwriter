# Preparacion antes de ir al laboratorio

El objetivo es llegar al laboratorio con Python, la interfaz y los SDK ya instalados. Haz esta preparacion en el mismo ordenador que se conectara a los equipos, porque los controladores USB y VISA no se transfieren con la carpeta del proyecto.

## 1. Preparar Python y la aplicacion

El kit esta fijado a Python 3.13 de 64 bits, la version usada en las pruebas.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\prepare_offline.ps1 -OpenVendorPages
.\install_lab.ps1 -Hardware -Offline -InstallVendorDrivers
.\.venv\Scripts\python.exe -m pumpauto self-test
```

La prueba debe terminar con `SELF-TEST OK`. Todavia no conecta ningun equipo.

## 2. Instalar software de fabricante

Instalar antes de desplazarse al laboratorio:

1. [Thorlabs Kinesis](https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=Motion_Control), incluyendo las API .NET de 64 bits.
2. [Pixelink Software Suite/SDK](https://www.navitar.com/products/pixelink-cameras/pixelink-sdk), incluyendo el controlador y `PxLAPI40.dll` para la M18-CYL.
3. Opcional: [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html) si `pyvisa-py` no reconoce la conexion concreta.

`prepare_offline.ps1` guarda Python, Pixelink, Kinesis, todas las ruedas y sus SHA-256; los paquetes Python no sustituyen a los controladores de fabricante.

## 3. Comprobar sin activar nada

Abrir `PCMWriter.bat`, ir a `Diagnostico` y ejecutar el chequeo. Deben aparecer:

- VISA disponible y los recursos USB/LAN;
- carpeta `C:/Program Files/Thorlabs/Kinesis`;
- BPC303 detectado por Kinesis, sin movimiento;
- Pixelink identificada como `M18-CYL/PL-D7718` mediante la API nativa.

Copiar en la propia interfaz:

- modelo de AWG y recurso VISA del T3AFG350 o DG1062Z;
- recurso VISA del Rigol MSO7054;
- numero de serie del BPC303;
- numero de serie de la Pixelink, exposicion, ganancia y ROI.

Guardar la configuracion manteniendo `mode=simulation`, `hardware armado=false` y `stage calibrado=false`.

## 4. Primera conexion en el laboratorio

Ejecutar primero `python -m pumpauto diagnostics` o **Ejecutar diagnostico** en la interfaz. El informe separa `LISTO`, `FALTA` y `BLOQUEADO`, fuerza C1 y el Stradus a OFF y no mueve el stage.

Orden recomendado:

1. Conectar un equipo cada vez y repetir el diagnostico para asociar recurso y modelo.
2. Abrir Kinesis y comprobar que los tres canales del BPC303 aparecen, sin muestra y sin ejecutar movimientos automaticos.
3. Verificar manualmente el significado de `0..100` unidades Kinesis, el recorrido real en micras y el sentido de X/Y/Z.
4. Ajustar en `config.json` `range_um`, `origin_um`, `controller_span_units` y `axis_inverted`.
5. Solo entonces marcar `stage calibrado`.
6. Con el laser deshabilitado fisicamente, cargar el T3AFG a 50 ohm y comprobar en una entrada de osciloscopio terminada a 50 ohm que el TTL es 0-5 V. Nunca verificarlo en Hi-Z, porque el nivel observado se duplicaria.
7. Conectar el Mini-USB del Stradus y seleccionar `USBHID::201A::1001` en **Diagnostico**. Para cabezales antiguos también se admite RS-232 mediante `ASRL...::INSTR` a 19200 baud, 8-N-1 y sin control de flujo. El programa activa `PUL=1` y verifica la potencia pico configurada antes de habilitar C1 del AWG.
   Para CW no se usa el AWG: la tarjeta **Laser** del Hardware Dashboard selecciona `PUL=0` y controla `LE/LP` directamente. La primera inicialización de la potencia de aparcamiento debe hacerse con el haz bloqueado, porque el equipo puede emitir brevemente al valor `LPS` que ya tuviera almacenado.
8. Validar que `C1:OUTP OFF` deja la salida inactiva, tambien tras cancelar una receta.
9. Conectar la salida SMA del DET02AFC a CH1 con cable coaxial de 50 ohm y terminacion de entrada de 50 ohm. Revisar y limpiar el conector optico FC/PC antes de acoplar la rama de monitorizacion.
10. Empezar con `1 V/div` y trigger positivo a `0.1 V`. Tras el primer pulso de baja potencia, usar la recomendacion guardada para ocupar aproximadamente 2-3 divisiones sin recorte y situar el trigger alrededor del 30% de la amplitud.
11. Comprobar la adquisicion unica del Rigol y revisar baseline, SNR, FWHM e integral calculados. El programa ajusta automaticamente la escala temporal a seis anchos de pulso y detiene la receta si no llega el trigger.

Antes de armar hardware, medir varios pares `potencia en muestra : PP Stradus` y escribirlos en **Diagnostico > Calibracion muestra:PP**, por ejemplo `5:20,10:33,15:46`. Deben estar ordenados y crecer en ambos ejes. Las recetas fuera de ese intervalo quedan bloqueadas.

Solo se utiliza CH1 del AWG seleccionado. No hace falta conectar ni configurar CH2.
12. Con potencia por debajo del umbral de cambio, ajustar exposicion o ganancia hasta ver el spot sin saturacion y ejecutar el autofocus.
13. Con iluminacion LED y el laser deshabilitado, ejecutar **Calibrar pixel-stage** sobre una textura fija de la muestra. El programa mueve `+X/+Y`, vuelve al origen y guarda `um_per_pixel` y ambas matrices 2x2. El spot laser permanece fijo en el sistema optico y no sirve por si solo para esta escala.
    La camara debe aparecer en el preflight como `M18-CYL/PL-D7718`, no solo como un indice generico. La exposicion inicial, ganancia y ROI se ajustan en **Diagnostico**; un ROI `[0,0,0,0]` conserva el sensor completo.
14. En **Align & Pulse > Hardware Dashboard**, definir en la tarjeta **Camera** un ROI que contenga por completo el spot y confirmar el histograma sin recorte. Probar los movimientos XYZ desde la tarjeta **Stage** con el paso mínimo seguro antes de usar autofocus; ambas sesiones pueden permanecer activas a la vez.
15. Definir el area del raster y ejecutar **Mapear foco (5 puntos)**. Revisar el RMS del plano y comprobar en seco que la Z corregida permanece dentro del recorrido del MAX.
16. Usar una muestra sacrificable para la primera correlacion entre potencia, pulso, fotodiodo e imagen.
17. Armar hardware solo tras completar lo anterior.

## 5. Datos que deben rellenarse con medidas reales

- potencia optica en la muestra frente al ajuste del laser;
- radio y perfil del spot;
- calibracion temporal del fotodiodo/osciloscopio;
- responsividad efectiva del DET02AFC a 639 nm, incluyendo acoplo y splitter;
- conversion pixel-micra y orientacion de la camara;
- umbrales observados de cristalizacion, amorfizacion y dano;
- parametros termicos efectivos que ajusten el modelo a las medidas.
- condicion termica real de la cara posterior y efecto del soporte de muestra;
- resistencias termicas efectivas entre SiO2, Sb2Se3 y silicio.
- indices `n,k` de las capas reales a 639 nm, especialmente ambas fases de Sb2Se3.

Hasta disponer de estos datos, el mapa termico es una herramienta de sensibilidad. No habilita automaticamente ninguna receta real.

## 6. Referencias de control usadas

- [T3AFG Programming Guide](https://cdn.teledynelecroy.com/files/manuals/t3afg-programming-guide.pdf): comandos `BSWV`, `BTWV`, `MTRIG` y `OUTP`.
- [Rigol DG1000Z Programming Guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/waveform-generators/DG1000Z_ProgrammingGuide_EN.pdf): comandos `SOURce`, `BURSt`, `TRIGger` y `OUTPut`.
- [Rigol DS7000 Programming Guide](https://eu.rigol.com/eu/Images/DS7000ProgrammingGuideEN_tcm30-3985.pdf): familia de comandos de adquisicion `WAV`.
- [Kinesis C# Quick Start](https://media.thorlabs.com/contentassets/5f57e82e38004e2aa5dfd0071bcf0732/kinesis_with_c_quick_start_guide.pdf): carga de dispositivos y API .NET.
