# Autenticidad física y referencias por proveedor

Guía completa de la **etapa de autenticidad** (`authenticity`): qué revisa, cómo
decide, cómo **dar de alta los sellos, firmas y logotipos válidos de cada
proveedor**, cómo se opera y cuáles son sus límites.

> **¿Solo quieres probarla o usarla con la carta NOM-035?** Empieza por la guía
> práctica: [`CARTA_NOM35.md`](CARTA_NOM35.md) — tres pasos, de gratis a caro, con
> lo que debe salir en cada uno.
>
> Primer tipo que la usa: `CARTA_NOM35` (la carta NOM-035 que entrega cada
> promotor). La etapa es **genérica**: cualquier tipo de documento que deba
> llegar *impreso, firmado con tinta, sellado y escaneado* la activa declarando
> un bloque en su catálogo — sin tocar código. La especificación del bloque está
> en [`catalogs/README.md`](../catalogs/README.md) → «Autenticidad física».

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

## 2. Las cuatro capas

```
                      ┌────────────────────────────────────────────────────┐
  PDF / JPG / PNG ──► │ 1 ESTRUCTURA   pdf_forensics.py    gratis, exacta  │──► CAPTURA_FISICA
                      │ 2 PIXELES      pixel_forensics.py  gratis, suave   │──► FONDO_NATURAL
                      │ 3 VISIÓN       vision_inspector.py ~$0.005-0.01    │──► *_PRESENTE, *_SIN_INSERCION,
                      │                                                    │    SELLO_CORRESPONDE_EMISOR,
                      │                                                    │    *_COINCIDE_REFERENCIA
                      │ 4 MEMORIA      fingerprint_store.py DynamoDB       │──► FIRMA_SELLO_NO_REUTILIZADOS,
                      └────────────────────────────────────────────────────┘    CARTA_NO_DUPLICADA
                                 │
                                 ▼
             campos `computed` del catálogo  ──►  motor de reglas (`boolean_true` + `on_fail`)
                                                   reject → RECHAZADO · review → REQUIERE_REVISION · info → aviso
```

| Capa | Qué responde | Cómo | ¿Puede rechazar? |
|---|---|---|---|
| **1 Estructura** | ¿El archivo es la *captura* de un papel o una *composición* digital? | Lee el PDF por dentro: caracteres de texto **visibles** vs invisibles (la capa OCR de un escaneo «buscable» es invisible), y dónde cae y cuánto cubre cada imagen. Sin modelo: no hay IA a la que engañar. | **Sí** — es determinista |
| **2 Pixeles** | ¿La hoja pasó por papel? | Ruido en las zonas en blanco. Una exportación digital tiene blanco 255 perfecto, desviación 0.000. | No (solo revisión). Un escáner con «limpieza de fondo» también deja blancos perfectos |
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

### Lo que NO detecta (límite honesto)

Quien **imprime** el Word con la firma pegada y **lo vuelve a escanear** pasa las
capas 1 y 2: el archivo *es* entonces una captura genuina. Para ese caso solo
quedan la capa 3 (halo rectangular, nitidez distinta, trama de puntos de un
«sello» que salió de una impresora → revisión humana) y la capa 4 (la misma tinta
bajo la carta de otro empleado → rechazo). Por eso la memoria entre cartas no es
un adorno: es la única defensa contra ese hueco.

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
| Ver qué diría la etapa de una carpeta de cartas | `pnpm autenticidad -- --dir <carpeta>` | $0, sin AWS |
| Entender por qué un archivo salió `DIGITAL` | `pnpm autenticidad -- --file x.pdf --detalle` | $0 |
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

---

## 8. Mapa de código

| Archivo | Rol |
|---|---|
| `enrichment-lambda/pipeline/authenticity.py` | Orquestador: lee el bloque del catálogo, corre las capas, escribe los campos `computed` y el bloque de auditoría |
| `…/pipeline/pdf_forensics.py` | Capa 1 (solo pypdf) |
| `…/pipeline/pixel_forensics.py` | Capa 2 y huellas de tinta (solo numpy) |
| `…/pipeline/vision_inspector.py` | Capa 3: prompts, esquemas y llamadas de visión |
| `…/pipeline/fingerprint_store.py` | Capa 4: tabla de huellas y lectura de referencias |
| `…/pipeline/page_raster.py` | Página/imagen → arreglo → JPEG (pypdfium2 + Pillow) |
| `…/pipeline/bedrock.py` | Bloques de imagen en Converse y la etapa `vision` |
| `…/validators/engine.py` | `on_fail` (reject/review/info) y `allow_empty` en `boolean_true` |
| `catalogs/base/recursos_humanos/CARTA_NOM35.json` | El tipo: bloque `authenticity` + campos |
| `scripts/analizar-autenticidad.py` · `test-autenticidad.py` · `manage-references.py` | `pnpm autenticidad` · `test:autenticidad` · `refs:*` |
| `tests/unit/test_{pdf_forensics,pixel_forensics,fingerprint_store,authenticity_stage,bedrock_vision_stage,rule_on_fail}.py` | Pruebas, todas gratis |
