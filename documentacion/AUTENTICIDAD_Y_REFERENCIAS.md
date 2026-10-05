# Autenticidad e integridad: la etapa `authenticity` y las referencias por proveedor

Guía completa de la **etapa de autenticidad e integridad** (`authenticity`):
qué revisa, cómo decide, qué cuesta cada capa, cómo **dar de alta los sellos,
firmas y logotipos válidos de cada proveedor**, cómo se opera y cuáles son sus
límites.

> **¿Solo quieres probarla o usarla con la carta NOM-035?** Empieza por la guía
> práctica: [`CARTA_NOM35.md`](CARTA_NOM35.md) — tres pasos, de gratis a caro, con
> lo que debe salir en cada uno.
>
> La etapa responde dos preguntas distintas y es **genérica** para las dos:
>
> - **¿El archivo fue tocado después de generarse?** (capa 0, `integrity`) —
>   para cualquier tipo, digital de origen o escaneado: un tipo lo pide con
>   `"$presets": ["INTEGRIDAD_PDF"]` (o `INTEGRIDAD_IMAGEN` para JPG/PNG) en su
>   catálogo, sin bloque `authenticity` ni código, y paga solo CPU.
> - **¿Es la captura de un papel impreso, firmado con tinta y sellado?** (capas
>   1-4) — el caso de `CARTA_NOM35`, primer tipo que la usa.
>
> La especificación del bloque, los presets y la tabla de señales están en
> [`catalogs/README.md`](../catalogs/README.md) → «Autenticidad e integridad» y
> «Presets de campos».

---

## 1. El problema

La carta se revisaba a mano. Automatizarla con un modelo de texto que responda
«¿hay sello? SÍ/NO» es justo lo que se puede engañar: basta un documento de Word
con la firma y el sello **pegados como imagen**.

Medido sobre 46 cartas reales (`test-docs/nom35`, carpeta = número de proveedor):

| Lo que llegó | Cuántas | Cómo se ve por dentro |
|---|---|---|
| Captura genuina (escáner o app de celular) | 23 | Cero texto, **una** imagen a página completa, ruido de papel, sello y firma que cambian de una carta a otra |
| Documento de Word con imagen pegada | 22 | 800+ caracteres de texto vectorial y una o más imágenes que cubren 1–9 % de la página |
| No concluyente por estructura | 1 | Un escaneo pegado *dentro* de una hoja de Word, sin texto |

El caso que lo resume: tres cartas de **tres empleados distintos** con el **mismo
archivo de imagen** (mismo md5) como bloque firma+sello, colocado a tres alturas
distintas según cómo acomodó Word el texto. Un sello de goma no se reacomoda.

Dos cosas que **no** sirven, también medidas:

- **La transparencia (PNG con alfa) no es la señal.** El caso de arriba pegó un
  JPEG opaco; buscar «imagen con transparencia» no lo habría detectado.
- **El programa que generó el PDF no es una compuerta.** Es trivial de editar,
  hay capturas genuinas con productor vacío o desconocido, y hay escaneos
  genuinos exportados desde Word. Se reporta como evidencia y nunca decide.

---

## 2. Las cinco capas

```
                      ┌──────────────────────────────────────────────────────┐
  bytes originales ─► │ 0 INTEGRIDAD   pdf_integrity.py    gratis, exacta    │──► ARCHIVO_SIN_REGUARDADO, SIN_ANOTACIONES,
  (antes de truncar)  │                                                      │    SIN_TEXTO_SOBRE_ESCANEO, FIRMA_DIGITAL_INTACTA…
  PDF / JPG / PNG ──► │ 1 ESTRUCTURA   pdf_forensics.py    gratis, exacta    │──► CAPTURA_FISICA
                      │ 2 PIXELES      pixel_forensics.py  gratis, suave     │──► FONDO_NATURAL, NIVEL_DE_ERROR_UNIFORME
                      │ 3 VISIÓN       vision_inspector.py ~$0.005-0.01      │──► *_PRESENTE, *_SIN_INSERCION,
                      │                                                      │    SELLO_CORRESPONDE_EMISOR,
                      │                                                      │    *_COINCIDE_REFERENCIA
                      │ 4 MEMORIA      fingerprint_store.py DynamoDB         │──► FIRMA_SELLO_NO_REUTILIZADOS,
                      └──────────────────────────────────────────────────────┘    CARTA_NO_DUPLICADA
                                 │
                                 ▼
             campos `computed` del catálogo  ──►  motor de reglas (`boolean_true` + `on_fail`)
                                                   reject → RECHAZADO · review → REQUIERE_REVISION · info → aviso
```

| Capa | Qué responde | Cómo | ¿Puede rechazar? |
|---|---|---|---|
| **0 Integridad** | ¿El *archivo* fue tocado después de generarse? | Lee los bytes **originales** de la carga (antes del recorte de páginas, que reescribe el PDF y borraría justo esta evidencia): cuántas veces se escribió el archivo (la cadena `startxref`/`/Prev` que un editor deja al «guardar»; descuenta la sección extra de un linearizado y una generación por firma digital), fechas Info vs XMP, qué software lo produjo y lo tocó (Producer, Creator, historial XMP), anotaciones por página, campos de formulario, JavaScript/adjuntos, si la firma digital más reciente cubre el archivo completo (**solo la geometría del `/ByteRange`: no verifica criptografía**), y el orden de pintado de la página: texto visible pintado *después* de la imagen del escaneo, una imagen pintada *encima* del texto. En JPG/PNG: EXIF `Software`, el segmento APP13 de Photoshop, PNG `Software`, historial XMP, fechas EXIF, datos de cámara. Sin raster, sin OCR, sin modelo. | **Sí** — es determinista. Solo «modificado después de firmar» es duro por defecto; el resto arranca suave (§6, medición) |
| **1 Estructura** | ¿El archivo es la *captura* de un papel o una *composición* digital? | Lee el PDF por dentro: caracteres de texto **visibles** vs invisibles (la capa OCR de un escaneo «buscable» es invisible), y dónde cae y cuánto cubre cada imagen. Sin modelo: no hay IA a la que engañar. Con `expected_capture: "DIGITAL"` (una factura) la pregunta se invierte: lo raro es un escaneo. | **Sí** — es determinista |
| **2 Pixeles** | ¿La hoja pasó por papel? ¿Un JPEG tiene una zona comprimida distinta al resto? | Ruido en las zonas en blanco. Una exportación digital tiene blanco 255 perfecto, desviación 0.000. Además, análisis de nivel de error (ELA) sobre los bytes JPEG que trae el archivo — nunca sobre nuestro raster. | No (solo revisión). Un escáner con «limpieza de fondo» también deja blancos perfectos; el ELA no está calibrado con imágenes reales y es solo evidencia (`info`) |
| **3 Visión** | ¿Están firma, sello y logo? ¿Se ven insertados? ¿El sello nombra al emisor? ¿Corresponden a lo registrado? | **El código calcula DÓNDE y el modelo solo dice QUÉ** (ver abajo). Primera llamada: la página + recortes de las regiones candidatas → qué es cada una. Segunda: los recortes elegidos a resolución nativa + las referencias del proveedor → examen. Todo lo que hay en la imagen es *dato*, nunca instrucción; la salida es una herramienta forzada con booleanos y enums. | No (solo revisión). Es un juicio, y un juicio puede equivocarse |
| **4 Memoria** | ¿Esta firma/sello ya apareció en la carta de **otro** empleado? ¿Esta carta ya se recibió? | Huella de tinta de cada elemento + hash de cualquier imagen incrustada bajo la firma/sello, guardados por proveedor. | **Sí** — una firma autógrafa nunca se repite exacta |

#### Por qué el modelo no ubica, solo clasifica

Medido con cartas reales: al pedirle al modelo la caja de la firma, la puso **sobre
el párrafo, ~20 % de la página arriba** de donde está (y pedirla en pixeles en vez
de normalizada salió peor). Con esa caja, el recorte de «la firma» es un pedazo de
texto, y la comparación contra referencias y la huella no valen nada. En cambio,
el mismo modelo **clasifica un recorte sin fallar**. Así que la geometría nunca
sale del modelo:

| Página | De dónde salen las regiones | `bboxSource` |
|---|---|---|
| **Digital** (PDF de Word) | Los objetos de imagen que el PDF coloca en la página: posición **exacta**, leída del archivo. Si el modelo dice «este es la firma», queda **probado** que la firma es una imagen insertada (`FIRMA_ES_IMAGEN_INSERTADA`) | `PDF` |
| **Escaneo / foto** | `pixel_forensics.propose_regions`: tinta que no es texto corrido. Tres pasadas (tinta oscura sin unir renglones; tinta oscura unida en bloque, para sellos negros de recuadro roto; tinta de color, para sellos azules sin recuadro), borrando antes las rayas largas (la raya de firma casi toca el sello y los fundía en un bloque) y descartando renglones, párrafos, motas, bordes de escáner y fragmentos | `TINTA` |
| El modelo ve el elemento pero no lo reconoce en ninguna región | Ninguna: se anota que está presente y **no** se recorta, examina ni compara | `MODELO` |

Resultado medido: 6 de 6 escaneos genuinos con firma, sello y logo bien ubicados
y veredicto `AUTENTICO`; las 4 imágenes insertadas de una carta de Word bien
clasificadas.

Dos indicios de inserción se **quitaron** tras medir: «trama de impresión» y
«nitidez distinta» se reportaban en 2 de 3 sellos de goma genuinos — a 150 dpi en
JPEG un modelo no puede ver trama, la adivina, y una adivinanza que manda a
revisión la mayoría de las cartas genuinas anula el propósito de automatizar.
Quedan los tres que un escaneo sí puede mostrar: halo rectangular, borde
recortado y fondo uniforme artificial; y el prompt pide marcarlos solo si son
inequívocos.

La leyenda del sello contra el emisor se compara **en Python**, entre dos cadenas
(ignorando «S.A. de C.V.», «Grupo»…): el modelo solo transcribe, no opina.

### Capas bajo demanda: un tipo paga solo lo que lee

Ninguna capa corre por estar configurada: corre porque **algún campo del tipo va
a leer una señal suya** (`_layers_needed` en `authenticity.py`; tabla exacta en
`catalogs/README.md` → «Capas bajo demanda»). Una capa que nadie lee queda
`NO_SOLICITADA` en el resultado y no cuenta en `AUTENTICIDAD_EVALUADA`.

Lo que eso significa en costo:

| El tipo declara | Capas que corren | Lo que paga |
|---|---|---|
| `"$presets": ["INTEGRIDAD_PDF"]` | 0 | milisegundos de CPU en la Lambda. **Ni un raster ni una llamada a Bedrock** |
| `"$presets": ["INTEGRIDAD_IMAGEN"]` | 0 + el ELA de la capa 2 sobre los bytes de la imagen | ídem |
| `"$presets": ["CAPTURA_FISICA"]` | 1 + 2 | rasterizar `pages.max_pages` páginas (CPU) |
| `elements` en el bloque (como `CARTA_NOM35`) | 1 + 2 + 3 + 4 | las llamadas de visión (§6) |

Dos palancas más para la capa de visión, en el bloque: `vision.trigger:
"on_suspicion"` (la llamada de ubicación solo se paga si una capa gratis levantó
un motivo no informativo; un archivo limpio sale con `VISION_OMITIDA_SIN_SOSPECHA`)
y `vision.page_review: true` (una llamada más que busca indicios de edición en
la página rendereada → `vision.no_edit_signs`; juicio, revisión como máximo).

### Lo que NO detecta (límite honesto)

Quien **imprime** el Word con la firma pegada y **lo vuelve a escanear** pasa las
capas 0, 1 y 2: el archivo *es* entonces una captura genuina. Para ese caso solo
quedan la capa 3 (halo rectangular, nitidez distinta, trama de puntos de un
«sello» que salió de una impresora → revisión humana) y la capa 4 (la misma tinta
bajo la carta de otro empleado → rechazo). Por eso la memoria entre cartas no es
un adorno: es la única defensa contra ese hueco.

La capa 0 tiene su propio hueco, igual de honesto: «Guardar como…» en Acrobat,
exportar de nuevo desde Word o pasar el archivo por Vista Previa de macOS
**reescriben el archivo completo** — una sola generación, fechas frescas — y
esconden la historia; quedan solo el productor/historial XMP, las anotaciones
que sobrevivan y el orden de pintado. Y de una firma digital solo se revisa que
su `/ByteRange` llegue al final del archivo: **no se verifica el certificado ni
el hash** (no hay `cryptography` en el bundle).

---

## 3. Qué significa «válido»: las referencias por proveedor

Un sello, firma o logo es **válido** cuando corresponde a uno de los que ese
proveedor tiene **registrados con anticipación**. Reglas del juego:

- Un proveedor puede tener **N firmas, N sellos y N logos**: dos sucursales = dos
  sellos; tres personas autorizadas = tres firmas; membrete viejo y nuevo = dos
  logos. Todas las activas valen: a la carta le basta corresponder a **una**.
- **Proveedor sin referencias todavía → no se rechaza por eso.** Se exige lo que
  sí se puede saber: que firma, sello y logo **estén presentes** y que **no sean
  imágenes insertadas**. Los campos `*_COINCIDE_REFERENCIA` quedan *vacíos*
  (= «no se pudo determinar»), no en falso, y el resultado lo dice con el motivo
  `SIN_REFERENCIAS_REGISTRADAS`.
- Si hay sellos registrados pero no firmas, se compara solo el sello. Cada
  elemento es independiente.
- «Corresponder» **no** es «ser idéntico». Al revés: una firma *idéntica* a otra
  es la señal de fraude. Corresponder es *mismo firmante / mismo sello / mismo
  logotipo*.

### 3.1 De dónde sacar una referencia (lo más importante)

> **Una referencia debe salir de un documento en papel cuya autenticidad te
> conste** — una carta que recibiste físicamente, o que confirmaste con el
> proveedor.
>
> **Nunca registres como referencia la firma o el sello recortados de un PDF
> hecho en Word.** Estarías dando de alta, como «válida», exactamente la imagen
> pegada que la etapa existe para detectar. Si `pnpm autenticidad` dice que las
> cartas de un proveedor son `DIGITAL`, **ese proveedor todavía no tiene de
> dónde sacar referencias**: pídele primero una carta impresa, firmada, sellada
> y escaneada.

En `test-docs/nom35` los `firma*.png`/`sello*.png`/`logo*.png` de cada carpeta
son recortes derivados de las cartas de ejemplo. Sirven tal cual **solo** en las
carpetas cuyas cartas son capturas genuinas (p. ej. `10021`, `10040`, `10110`,
`10356`); los de carpetas `DIGITAL` (p. ej. `10037`, `10047`, `10284`) **no deben
registrarse**.

### 3.2 Cómo recortar bien

| Hazlo así | Evita |
|---|---|
| Escanea la hoja a **200–300 dpi**, a color | Fotos oscuras, movidas o con sombra encima del elemento |
| Recorta **solo el elemento**, con un margen chico de papel alrededor | Subir la hoja completa (se rechaza: «parece la hoja completa») |
| **Un elemento por archivo**: la firma sola, el sello solo | El bloque firma + nombre impreso + sello en una sola imagen |
| Si el sello cae **encima** de la firma, busca otra carta donde estén separados | Registrar el traslape como si fuera «la firma» |
| Firma: incluye el trazo completo, sin el nombre impreso debajo | Cortar colas o rúbricas del trazo |
| Sello: que la leyenda (razón social, sucursal) se lea | Sellos a medio entintar donde no se lee nada |
| Logo: el logotipo o la franja del membrete | Todo el encabezado con dirección y teléfonos |
| PNG o JPG, lado menor ≥ 120 px (logo ≥ 80 px), ≤ 3.5 MB | Capturas de pantalla chiquitas |
| **2 o 3 muestras por firmante** si las tienes (de cartas distintas) | Subir tres veces el mismo archivo (se detecta y se omite) |

El alta revisa cada recorte con **el mismo código de la Lambda** y te dice, antes
de escribir nada, cuál rechaza y por qué: muy chico, muy grande, sin tinta
distinguible, archivo repetido, o *misma tinta* que una ya registrada.

### 3.3 Cómo clasificar: la carpeta del proveedor

Una carpeta por proveedor. Dentro, cada archivo se clasifica **por subcarpeta**
(recomendado) o **por prefijo del nombre**. El nombre del archivo es la
**etiqueta** que verá quien revise: que diga *quién firma* o *qué sucursal*.

```
referencias/
└── 10040/                                   ← número de proveedor
    ├── firma/
    │   ├── julio-cesar-lemus.png            ← quién firma
    │   ├── julio-cesar-lemus-2.png          ← otra muestra del mismo firmante
    │   └── omar-hernandez-reynosa.png
    ├── sello/
    │   ├── planta-envasadora.png            ← qué sucursal
    │   └── ppl-sur.png
    └── logo/
        ├── membrete-superior.png
        └── roseta-inferior.png
```

Equivalente plano, por prefijo: `firma-julio-cesar-lemus.png`,
`sello_planta-envasadora.png`, `logo-membrete-superior.png`. También se aceptan
`firma1.png`, `sello2.png` (la convención de `test-docs/nom35`), pero el alta te
avisa que la etiqueta «1» no le dice nada a nadie.

Los nombres de elemento (`firma`, `sello`, `logo`) **salen del catálogo del tipo**
(`authenticity.elements`), no del script: un tipo futuro que declare `RUBRICA` o
`HOLOGRAMA` se clasifica con esas mismas palabras. Un archivo que no se puede
clasificar se reporta y **no se sube**.

> No commitees la carpeta `referencias/`: son firmas de personas reales. Déjala
> fuera del repo (o bajo `test-docs/`, que ya está en `.gitignore`).

### 3.4 Alta, consulta y baja

```bash
# 1. Ensayo: revisa y clasifica sin escribir nada (funciona hasta sin AWS)
pnpm refs:register -- --key 10040 --dir referencias/10040 --dry-run

# 2. Alta en dev (pide confirmación S/n)
pnpm refs:register -- --key 10040 --dir referencias/10040

# 3. Alta en prod — explícita, nunca es el default
pnpm refs:register -- --key 10040 --dir referencias/10040 --env prod

# Un solo recorte
pnpm refs:register -- --key 10021 --file firma.png --kind FIRMA --label "Pablo Ontiveros"

# Consultar
pnpm refs:list -- --key 10040        # un proveedor
pnpm refs:list                       # todos los que tienen referencias

# Baja: desactiva (reversible) o borra (fila + imagen)
pnpm refs:remove -- --key 10040 --kind SELLO --ref ppl-sur
pnpm refs:remove -- --key 10040 --kind SELLO --ref ppl-sur --delete
```

Salida típica del alta:

```
Proveedor PROVEEDOR 10040 — tipo CARTA_NOM35 — ambiente dev (document-validation-fingerprints-dev)

  FIRMA  julio-cesar-lemus            julio-cesar-lemus.png          670x300    ALTA
  FIRMA  julio-cesar-lemus-2          julio-cesar-lemus-2.png        560x280    ALTA
  SELLO  planta-envasadora            planta-envasadora.png          480x330    ALTA
  SELLO  ppl-sur                      ppl-sur.png                    390x340    YA REGISTRADA como SELLO#ppl-sur
  LOGO   membrete-superior            membrete-superior.png          1620x220   ALTA

Se registrarán: 2 FIRMA, 1 SELLO, 1 LOGO
¿Registrar 4 referencia(s) en dev? (S/n)
```

| Estado | Significado |
|---|---|
| `ALTA` | Se va a registrar |
| `YA REGISTRADA como …` | Ese mismo archivo ya está dado de alta; se omite |
| `REPETIDA (mismo archivo que …)` | Dos archivos de la carpeta son idénticos; entra uno |
| `MISMA TINTA que …` | Es otra imagen del *mismo trazo exacto* (≥ 0.97). Una basta; `--force` para subirla igual |
| `RECHAZADA: …` | No pasa la revisión de calidad; corrige el recorte |

Dónde queda cada cosa: la imagen en
`s3://<bucket>/references/PROVEEDOR/<proveedor>/<FIRMA|SELLO|LOGO>/<refId>.png`
(mismo bucket de documentos; solo `raw/` dispara el flujo, así que ahí es inerte)
y el índice en la tabla `document-validation-fingerprints[-dev]`, bajo
`Pk = R#PROVEEDOR#<proveedor>`. **dev y prod son tablas y buckets distintos**: lo
que registras en dev no existe en prod.

Una baja o un alta aplican a los documentos que se procesen **después**; los ya
procesados no cambian.

### 3.5 Cómo sabe el sistema de qué proveedor es la carta

Quien sube el documento manda el número de proveedor como **metadata de carga**:

```bash
curl -X POST "$API/documents" -H "x-api-key: $KEY" -H "Content-Type: application/json" -d '{
  "fileName": "carta-nom35.pdf",
  "documentType": "CARTA_NOM35",
  "metadata": { "proveedor": "10040", "promotor": "88213" }
}'
```

El catálogo declara de dónde leerlo (`references.key_from: "metadata.proveedor"`).
Sin esa llave la carta **no se rechaza**: corre en modo «sin referencias» y el
resultado lleva el motivo `SIN_CLAVE_DE_REFERENCIAS`. La clave la *afirma* quien
sube; el sistema no puede verificarla — por eso la comparación contra referencias
manda a revisión y no rechaza.

Localmente: `pnpm procesar carta.pdf CARTA_NOM35 --meta proveedor=10040`.

---

## 4. Qué devuelve

Campos `computed` de `CARTA_NOM35` (en `documents[i].fields`, como cualquier otro):

| Campo | Falso significa | Si falla |
|---|---|---|
| `AUTENTICIDAD_EVALUADA` | Alguna capa habilitada no pudo correr | revisión |
| `CAPTURA_FISICA` | El PDF es digital con imágenes insertadas | **revisión hoy → rechazo** (§5) |
| `FONDO_NATURAL` | Fondo perfectamente uniforme | revisión |
| `FIRMA_PRESENTE` · `SELLO_PRESENTE` · `LOGO_PRESENTE` | No se ve el elemento | revisión |
| `FIRMA_SIN_INSERCION` · `SELLO_SIN_INSERCION` | Hay una imagen incrustada justo ahí, o el modelo ve indicios de inserción/impresión | revisión |
| `SELLO_CORRESPONDE_EMISOR` | La leyenda del sello nombra a otra empresa | revisión |
| `FIRMA_` · `SELLO_` · `LOGO_COINCIDE_REFERENCIA` | No corresponde a ninguna registrada. **Vacío** si el proveedor no tiene | revisión |
| `FIRMA_SELLO_NO_REUTILIZADOS` | La misma firma/sello está en la carta de **otro** empleado | **rechazo** |
| `FIRMA_SELLO_NO_PARECIDOS` | Se parecen demasiado a los de otra carta | revisión |
| `CARTA_NO_DUPLICADA` | Esta misma carta (empleado + fecha) ya se recibió | aviso |
| `ORIGEN_DOCUMENTO`, `TEXTO_SELLO`, `VEREDICTO_AUTENTICIDAD`, `MOTIVOS_AUTENTICIDAD` | informativos | — |

Campos de los **presets** (`catalogs/_shared.json` → `_field_presets`; un tipo
los pide con `"$presets": [...]`, todos arrancan en `review` salvo donde se dice):

| Preset | Campo | Falso significa |
|---|---|---|
| `INTEGRIDAD_PDF` | `ARCHIVO_SIN_REGUARDADO` | un editor guardó el archivo sobre sí mismo, o después de firmarlo |
| | `FIRMA_DIGITAL_INTACTA` | la firma digital más reciente ya no cubre el archivo. **Vacío** sin firma |
| | `FECHAS_METADATOS_CONSISTENTES` | modificación anterior a la creación, o Info y XMP en desacuerdo. **Vacío** sin fechas |
| | `SIN_EDITOR_EN_METADATOS` | Producer/Creator/historial XMP nombran un editor de PDF |
| | `SIN_ANOTACIONES` | hay anotaciones (texto libre, sello, tachadura…) encima del contenido; los vínculos no cuentan |
| | `SIN_CAMPOS_FORMULARIO` | hay campos AcroForm editables |
| | `SIN_TEXTO_SOBRE_ESCANEO` | texto visible escrito encima de la imagen del escaneo. **Vacío** si no es un escaneo |
| | `SIN_IMAGEN_SOBRE_TEXTO` | una imagen colocada encima del texto original (parche). **Vacío** sin texto nativo |
| | `SIN_CONTENIDO_ACTIVO` | JavaScript, acciones automáticas o adjuntos no permitidos |
| | `EDITOR_DETECTADO`, `AUTENTICIDAD_EVALUADA`, `VEREDICTO_AUTENTICIDAD`, `MOTIVOS_AUTENTICIDAD` | como arriba |
| `INTEGRIDAD_IMAGEN` | `SIN_EDITOR_EN_METADATOS` | EXIF `Software`, APP13 de Photoshop, PNG `Software` o historial XMP nombran un editor |
| | `FECHAS_METADATOS_CONSISTENTES` | fecha EXIF de modificación posterior a la de captura |
| | `METADATOS_DE_CAMARA` (`info`) | sin marca/modelo/fecha de captura — la mensajería los quita, no acusa |
| | `NIVEL_DE_ERROR_UNIFORME` (`info`) | ELA: un bloque comprimido distinto al resto — sin calibrar, evidencia |
| `CAPTURA_FISICA` | `CAPTURA_FISICA`, `ORIGEN_DOCUMENTO`, `FONDO_NATURAL` | las capas 1-2 de la tabla de arriba, para cualquier tipo que exija papel |

**Vacío ≠ falso.** Un campo vacío es «no se pudo determinar» (no aplica a ese
archivo, no hay referencias, no hay tabla) y **no** cuenta como «no». La única
excepción es `AUTENTICIDAD_EVALUADA`: si la etapa no pudo correr, el documento va
a una persona; nunca se aprueba callado. Eso incluye el interruptor general
`AUTHENTICITY_ENABLED=false`: apagar la revisión de fraude no convierte cartas
sin revisar en cartas aprobadas.

Además, cada documento lleva un bloque compacto `authenticity` en el envelope:

```json
"authenticity": {
  "verdict": "NO_AUTENTICO",
  "layers": { "structure": "FALLA", "pixels": "OK", "vision": "OK", "memory": "FALLA" },
  "reasons": [
    { "code": "TEXTO_NATIVO_CON_IMAGEN_PEGADA", "hard": true,
      "message": "El PDF tiene texto nativo y una imagen insertada: no es la captura de un papel" },
    { "code": "FIRMA_ES_IMAGEN_INSERTADA", "hard": true,
      "message": "FIRMA: hay una imagen incrustada en el PDF justo donde está el elemento" },
    { "code": "FIRMA_REUTILIZADO_EN_OTRA_CARTA", "hard": true,
      "message": "FIRMA: la misma imagen que en otra carta (AURORA CHAVEZ RAMIREZ|2026-09-17)" }
  ],
  "referencesFound": false
}
```

`verdict` describe el **hallazgo** (`AUTENTICO` · `SOSPECHOSO` · `NO_AUTENTICO` ·
`NO_EVALUADO`), no el estatus del documento: en modo observación un documento
`NO_AUTENTICO` sale `REQUIERE_REVISION`. El detalle completo (cajas, estadísticas
por página, hashes de imagen, huellas) queda en `results/<documentId>.json`.

<details>
<summary>Catálogo de motivos (<code>reasons[].code</code>)</summary>

| Código | Capa | Duro |
|---|---|---|
| `TEXTO_NATIVO_CON_IMAGEN_PEGADA` | estructura | sí |
| `DOCUMENTO_DIGITAL_SIN_CAPTURA` | estructura | sí |
| `IMAGEN_CON_TRANSPARENCIA` | estructura | sí |
| `<ELEMENTO>_ES_IMAGEN_INSERTADA` | estructura + visión | sí |
| `ORIGEN_OFIMATICA` | estructura | no (evidencia) |
| `FONDO_SIN_RUIDO_DE_CAPTURA` | pixeles | no |
| `CAPTURA_NO_ES_PAPEL` | visión | no |
| `<ELEMENTO>_AUSENTE` | visión | no |
| `<ELEMENTO>_CON_INDICIOS_DE_INSERCION` | visión | no |
| `<ELEMENTO>_NO_CORRESPONDE_AL_EMISOR` | visión | no |
| `<ELEMENTO>_NO_COINCIDE_CON_REFERENCIAS` | referencias | no |
| `SIN_REFERENCIAS_REGISTRADAS` · `SIN_CLAVE_DE_REFERENCIAS` | referencias | no (informativo) |
| `<ELEMENTO>_REUTILIZADO_EN_OTRA_CARTA` | memoria | sí |
| `<ELEMENTO>_MUY_SIMILAR_EN_OTRA_CARTA` | memoria | no |
| `CARTA_YA_RECIBIDA` | memoria | no (informativo) |
| `PAGINAS_SIN_ANALIZAR` · `ETAPA_DESACTIVADA` | — | no |
| `MODIFICADO_DESPUES_DE_FIRMAR` | integridad | **sí** |
| `ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES` | integridad | no (suave; `integrity.severity` lo sube) |
| `FECHAS_DE_METADATOS_INCONSISTENTES` | integridad | no (suave) |
| `EDITOR_DE_PDF_EN_METADATOS` | integridad | no (suave) |
| `ANOTACIONES_EN_PAGINA` · `CAMPOS_DE_FORMULARIO` | integridad | no (suave) |
| `TEXTO_ESCRITO_SOBRE_ESCANEO` · `IMAGEN_SOBRE_TEXTO_NATIVO` | integridad | no (suave) |
| `CONTENIDO_ACTIVO_EN_PDF` | integridad | no (suave) |
| `IMAGEN_CON_SOFTWARE_DE_EDICION` · `FECHAS_EXIF_INCONSISTENTES` | integridad (imágenes) | no (suave) |
| `FIRMA_DIGITAL_PRESENTE` · `MODIFICADO_DESPUES_DE_CREADO` · `SIN_METADATOS_DE_CAMARA` · `INTEGRIDAD_NO_DETERMINABLE` | integridad | no (informativo) |
| `NIVEL_DE_ERROR_NO_UNIFORME` | pixeles (ELA) | no (suave) |
| `CAPTURA_DISTINTA_DE_LA_ESPERADA` | estructura (`expected_capture`) | no (suave) |
| `INDICIOS_DE_EDICION_EN_PAGINA` | visión (`page_review`) | no |
| `VISION_OMITIDA_SIN_SOSPECHA` | visión (`trigger: on_suspicion`) | no (informativo) |

«Duro» hace el veredicto `NO_AUTENTICO`; «suave», `SOSPECHOSO`; «informativo» no
lo mueve. Lo que le pasa al DOCUMENTO sigue siendo el `on_fail` del campo. La
severidad de cada código de integridad vive en `pdf_integrity.SEVERITY` y un tipo
la sobreescribe con `authenticity.integrity.severity`.

</details>

---

## 5. Arranque en observación → rechazo

Hoy ~45 % de las cartas reales son `DIGITAL`. Rechazarlas todas el primer día
sería un corte operativo, así que `CAPTURA_FISICA` arranca con
`"on_fail": "review"`: esas cartas van a `REQUIERE_REVISION` con el motivo exacto.
Lo que **sí rechaza desde el día uno** es `FIRMA_SELLO_NO_REUTILIZADOS`.

Para endurecer, cuando ya se midió y se avisó a los proveedores:

1. `pnpm autenticidad -- --dir <cartas recientes>` — cuántas caerían hoy (gratis).
2. En `catalogs/base/recursos_humanos/CARTA_NOM35.json`, campo `CAPTURA_FISICA`:
   `"on_fail": "review"` → `"on_fail": "reject"`.
3. `pnpm sync:one -- --type CARTA_NOM35` (dev) y, tras validar,
   `pnpm sync:one:prod -- --type CARTA_NOM35`. **Sin deploy.**

El mensaje de la regla es lo que recibe el proveedor; ya es accionable: *«la carta
debe imprimirse, firmarse con tinta, sellarse y escanearse»*.

---

## 6. Operación

| Quiero… | Comando | Costo |
|---|---|---|
| Ver qué diría la etapa de una carpeta de documentos — integridad incluida, aunque el tipo no la declare (`--sin-integridad` para quitarla) | `pnpm autenticidad -- --dir <carpeta> [--type TIPO]` | $0, sin AWS |
| Entender por qué un archivo salió `DIGITAL` o con un motivo de integridad (cadena de guardados, fechas, software, firmas, anotaciones, texto sobre imagen) | `pnpm autenticidad -- --file x.pdf --detalle` | $0 |
| Regresión tras tocar umbrales o código | `pnpm test:autenticidad` | $0 |
| Probar también la capa de visión (carpetas ancla) | `pnpm test:autenticidad -- --vision` | ~$0.08 |
| Procesar una carta completa en local | `pnpm procesar carta.pdf CARTA_NOM35 --meta proveedor=10040` | ~$0.015 |
| Alta / consulta / baja de referencias | `pnpm refs:register` · `refs:list` · `refs:remove` | centavos de S3/DynamoDB |
| Apagar la etapa de emergencia | `AUTHENTICITY_ENABLED=false` + deploy | — (todo va a revisión) |

**Costo por carta.** Medido (2026-09-21, Haiku 4.5): la visión cuesta ~$0.009
en un escaneo genuino (~6,600 tokens de entrada en 2 llamadas) y $0.0115 en una
carta digital con 6 referencias enviadas; una carta digital completa salió en
**$0.0161**. Detalle en `COSTOS.md` §10. Estimación previa, para referencia: ~$0.014 una captura genuina
(OCR + extracción + 2 llamadas de visión), ~$0.008 una digital (el examen de
recortes se omite: ya está probado que es digital). Las llamadas de visión salen
en su propia línea, `BedrockAuthenticity`, en `pnpm costos`.

**Modelo de visión.** La etapa `vision` resuelve su modelo por su propia cadena —
`model_ids.vision` del catálogo → `BEDROCK_VISION_MODEL_ID` → Haiku 4.5 — y
**nunca** hereda `BEDROCK_MODEL_ID` ni la lista de fallback: en este despliegue
son modelos de solo texto.

**Lo que no cuesta, y las dos palancas.** La capa 0 (integridad) y el ELA de la
capa 2 no facturan nada en Bedrock ni Textract: corren en la Lambda, en
milisegundos, sobre bytes que ya están en memoria. En la capa de visión,
`vision.trigger: "on_suspicion"` omite la llamada de ubicación (`locate`) cuando
ninguna capa gratis levantó un motivo no informativo — el archivo limpio no paga
modelo —, y `vision.page_review: true` agrega una llamada más, del tamaño de
`locate` (la página a 1568 px; ESTIMADA en ~3,300 tokens de entrada, no medida
aún), en la misma línea `BedrockAuthenticity`.

### Medido sobre las 49 cartas reales (2026-09-24)

`pnpm autenticidad -- --dir test-docs/nom35/samples` con la capa de integridad
forzada sobre las 46 cartas (23 capturas genuinas, 22 de Word, 1 indeterminada) y
las 3 capturas de pantalla. Dos lecciones salieron de la PRIMERA corrida y ya
están en el código; la tabla es la corrida final:

- **Word escribe todo PDF en dos pasadas** (el documento y luego una sección
  incremental con su XMP, con creación == modificación al segundo): 17 de 49
  archivos daban `ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES`, casi todos
  cartas de Word y también 3 escaneos exportados a través de Word
  (`10223/doc3`, `10275/doc2`, `10275/doc3`). Esa segunda sección se descuenta
  cuando cae dentro de `date_tolerance_s` de la creación (`chain.generatorTwoPass`);
  un editor que vuelve días después mueve `/ModDate` y sí cuenta.
- **Word sobre un membrete-imagen** a página completa daba
  `TEXTO_ESCRITO_SOBRE_ESCANEO` en 3 cartas (800+ caracteres sobre la imagen):
  de ahí `max_overlay_chars: 200` — a partir de ese volumen es una
  composición digital y decide la estructura, no un escaneo con texto encima.
- **El ELA acusaba 3 escaneos genuinos** (`10040/doc3` ratio 10.9,
  `10110/doc1`, `10329/doc1` ratio 8.1): `ela_max_ratio` subió a 12 y el
  preset lo deja en `info`. No está calibrado con parches reales: es evidencia
  en `--detalle`, no una regla.

| Hallazgo (corrida final) | Archivos | Lectura |
|---|---|---|
| `ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES` | 0 de 49 | tras descontar las dos pasadas de Word |
| `IMAGEN_SOBRE_TEXTO_NATIVO` | 7 de 49 | todas cartas de Word con la firma/sello pegados sobre la línea de firma — el mismo fraude que ya atrapa `CAPTURA_FISICA` |
| `MODIFICADO_DESPUES_DE_CREADO` | 4 de 49 | informativo, no mueve nada |
| `EDITOR_DE_PDF_EN_METADATOS` | 2 de 49 | las dos cartas de `10177`: `Producer = iLovePDF` |
| `SIN_METADATOS_DE_CAMARA` | 3 de 49 | las tres capturas de pantalla; informativo |
| `TEXTO_ESCRITO_SOBRE_ESCANEO` · `NIVEL_DE_ERROR_NO_UNIFORME` | 0 de 49 | ver arriba |
| `10329/doc1` (RICOH) | linearizado real: 2 `startxref`, **1 generación** | la excepción de linearización funciona con un archivo de verdad |

Y el invariante que importa: **cada escaneo genuino verificado a ojo tiene
exactamente 1 generación y ningún motivo duro de integridad** (`pnpm
test:autenticidad` lo vigila desde ahora). La conclusión operativa: `CARTA_NOM35`
**no** lleva el preset `INTEGRIDAD_PDF` todavía — lo que encontraría en estas
cartas ya lo atrapa `CAPTURA_FISICA`, y ningún archivo real editado a mano ha
pasado por la capa. El preset está pensado para los tipos digitales de origen,
donde un guardado posterior, una anotación o un parche sí son la anomalía. Antes
de ponérselo a
cualquier tipo: mide con su carpeta real.

### Calibración (de dónde salen los umbrales)

| Medición sobre cartas reales | Valor | Umbral |
|---|---|---|
| Texto visible en un escaneo / en una carta de Word | 0–4 / 555–1180 caracteres | digital ≥ 200, escaneo ≤ 40 |
| Cobertura de la marca de agua de CamScanner / de una firma pegada | < 1 % / 1–9 % | decoración < 1 % |
| Cobertura de un escaneo pegado dentro de Word | 77 % | «imagen insertada» solo si ≤ 35 % |
| Ruido de fondo (p90) en captura genuina / exportación digital | 0.30–22 / 0.000 | artificial ≤ 0.05 **y** blanco ≥ 99.9 % |
| Parecido entre dos **firmas** genuinas del mismo firmante | 0.10–0.53 | revisión ≥ 0.85, rechazo ≥ 0.97 |
| Parecido entre firmas **pegadas** reutilizadas | 0.89–1.00 | ↑ |
| Parecido entre dos **sellos** genuinos (la goma es rígida) | hasta 0.86 | revisión ≥ 0.95, **nunca rechaza por parecido** |

Un sello de goma está *hecho* para dejar la misma marca; por eso en un sello el
parecido solo no rechaza — únicamente la prueba exacta (la misma imagen
incrustada) lo hace. Todos estos umbrales viven en el catálogo del tipo.

---

## 7. Problemas frecuentes

| Síntoma | Causa y qué hacer |
|---|---|
| Todas las cartas salen `REQUIERE_REVISION` con `AUTENTICIDAD_EVALUADA` en falso | La capa de visión está fallando. Revisa `authenticity.visionError` en `results/<id>.json`: casi siempre es «Model access» sin habilitar para el modelo de visión en esa cuenta/región, o `AUTHENTICITY_ENABLED=false` |
| Una captura genuina sale `INDETERMINADO` | Es un escaneo pegado dentro de Word u otra suite. No se acusa; deciden visión y memoria. Pide al proveedor el PDF directo del escáner |
| `FONDO_NATURAL` falso en un escaneo real | Escáner con limpieza de fondo agresiva en **toda** la hoja. Es señal suave (revisión). Si es sistemático en un proveedor, sube `pixels.noise_std_min` |
| El proveedor tiene referencias pero `referencesFound` sale falso | La carga no mandó `metadata.proveedor` (motivo `SIN_CLAVE_DE_REFERENCIAS`), o las referencias se registraron en el otro ambiente |
| `FIRMA_COINCIDE_REFERENCIA` falso con la firma correcta | Falta ese firmante en las referencias, o la muestra registrada es mala. Registra 2–3 muestras por firmante |
| `refs:register` rechaza todo por «muy chica» | Recortes de captura de pantalla. Escanea a 200–300 dpi |
| Un reenvío legítimo sale como duplicado | Es un aviso (`info`), no cambia el estatus. La misma carta = mismo empleado + misma fecha |
| Se reprocesó una carta y ahora «choca consigo misma» | No debería: la fila se identifica por empleado+fecha y por el hash del archivo. Si pasa, es un bug — reportarlo con el `documentId` |
| Un escaneo genuino sale con `ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES` | Alguien lo abrió y lo guardó encima: Acrobat con «Reconocer texto» (OCR) + Guardar, un escaneo pegado en Word y exportado, una app que «optimiza» al enviar. Es exactamente por lo que la señal arranca suave: mide la tasa en tu carpeta (`pnpm autenticidad --detalle` imprime la cadena de guardados) antes de subirla con `integrity.severity` o `on_fail: reject` |
| Un PDF grande o «web-optimized» sale con 2 secciones de referencias cruzadas | Es un archivo **linearizado**: dos secciones por diseño. La capa lo detecta (`/Linearized` + `/L` igual al largo del archivo) y lo cuenta como UNA generación; si aun así acusa, el archivo se modificó después de linearizarlo (el `/L` ya no cuadra) |
| Un PDF firmado digitalmente siempre trae 2 generaciones | Firmar ES un guardado incremental; la capa descuenta una generación por firma. Solo acusa (`MODIFICADO_DESPUES_DE_FIRMAR`, duro) cuando el `/ByteRange` de la firma más reciente no llega al final del archivo |
| Una foto de identificación sale con `SIN_METADATOS_DE_CAMARA` | WhatsApp, Telegram y la mayoría de los correos quitan el EXIF; una captura de pantalla nunca lo tuvo. Es informativo y no mueve el estatus. Solo `IMAGEN_CON_SOFTWARE_DE_EDICION` (Photoshop, GIMP… en EXIF/APP13/XMP) es una señal |
| Un CFDI en PDF sale con `CONTENIDO_ACTIVO_EN_PDF` por su XML adjunto | PDF/A-3 lleva el XML como archivo incrustado, y es legítimo: declara `"integrity": {"allow_embedded_files": true}` en el bloque del tipo |

---

## 8. Mapa de código

| Archivo | Rol |
|---|---|
| `enrichment-lambda/pipeline/authenticity.py` | Orquestador: lee el bloque del catálogo (opcional), decide qué capas piden los campos (`_layers_needed`), las corre, escribe los campos `computed` y el bloque de auditoría |
| `…/pipeline/pdf_integrity.py` | Capa 0 (pypdf + regex + Pillow sobre los bytes originales): cadena de guardados, fechas, software, firmas, formularios, contenido activo, EXIF/APP13/XMP; `SEVERITY`, `MESSAGES` y `SIGNALS` de sus motivos |
| `…/pipeline/pdf_forensics.py` | Capa 1 (solo pypdf); el walker registra además el orden de pintado y dónde cae cada string (`overlay_facts`) y pinta las apariencias de las anotaciones |
| `…/pipeline/pixel_forensics.py` | Capa 2 (ruido de fondo y ELA, `error_level_stats`) y huellas de tinta (solo numpy) |
| `enrichment-lambda/document_catalog.py` · `scripts/catalog-files.mjs` (`expandPresets`, usado por `sync-catalogs.mjs`) | `$presets`: inyección de los campos de `_shared.json` → `_field_presets` en el loader y en el índice de tipos del query Lambda |
| `…/pipeline/vision_inspector.py` | Capa 3: prompts, esquemas y llamadas de visión |
| `…/pipeline/fingerprint_store.py` | Capa 4: tabla de huellas y lectura de referencias |
| `…/pipeline/page_raster.py` | Página/imagen → arreglo → JPEG (pypdfium2 + Pillow) |
| `…/pipeline/bedrock.py` | Bloques de imagen en Converse y la etapa `vision` |
| `…/validators/engine.py` | `on_fail` (reject/review/info) y `allow_empty` en `boolean_true` |
| `catalogs/base/recursos_humanos/CARTA_NOM35.json` | El tipo: bloque `authenticity` + campos |
| `scripts/analizar-autenticidad.py` · `test-autenticidad.py` · `manage-references.py` | `pnpm autenticidad` (integridad siempre encendida; `--sin-integridad` la quita) · `test:autenticidad` (`integrity_reasons_include/exclude` opcionales en el ground truth) · `refs:*` |
| `tests/unit/test_{pdf_forensics,pixel_forensics,fingerprint_store,authenticity_stage,bedrock_vision_stage,rule_on_fail}.py` | Pruebas, todas gratis |
| `tests/unit/test_pdf_integrity.py` · `test_pdf_forensics_overlay.py` · `test_pixel_forensics_ela.py` · `test_field_presets.py` · `test_authenticity_integrity.py` | Capa 0 (cada manipulación reproducida con pypdf/Pillow), orden de pintado, ELA, `$presets`, y el contrato de la etapa con capas bajo demanda (un tipo solo-integridad jamás rasteriza ni llama a un modelo) |
