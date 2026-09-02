# Costos de procesamiento — Documents Validator

> Generado 2026-07-28 a partir de datos reales: 14 ejecuciones guardadas en
> `s3://<bucket-dev>/results/*.json` (27-28 jul 2026), cruzadas contra AWSt
> Cost Explorer (cuenta 886031931562, us-east-1) y precios obtenidos en vivo
> con la AWS Pricing API. No son estimaciones de documentación — son
> `usage` real de Bedrock (`extractionMeta.usage` en cada resultado) y
> `pages.totalPages` / `pages.ocrPages` real por documento.

## 0-bis. Selección de páginas de interés (2026-08-28) — medido, aún no desplegado

Antes de este cambio, un PDF subido **con el tipo declarado** se extraía
completo: las 86 páginas de un expediente iban a Bedrock aunque el pedimento
viviera en 15 de ellas. `pipeline/page_selector.py` manda solo las páginas
cuyo texto cumple los marcadores que el tipo declara en `page_selection`.

Medido con `pnpm paginas` (offline, texto nativo de pypdf, sin llamar a AWS)
sobre `test-docs/pedimentos docs/docs`:

| Documento | Páginas | A Bedrock | Ahorro |
| --- | ---: | ---: | ---: |
| `26 07 3435 6041248 CG.pdf` (expediente digital) | 86 | 16 | **81 %** |
| `pedimento-normal.pdf` / `pedimentos.pdf` (mismo expediente) | 86 | 16 | **81 %** |
| `normal_pend.pdf` | 3 | 3 | 0 % (fail-open: nada que ahorrar) |
| CFDIs sueltos de 1-2 páginas | 1-2 | igual | 0 % (fail-open) |
| **Total de la carpeta** | **348** | **138** | **60 %** |

### Ahorro de páginas ≠ ahorro de tokens

**No los confundas.** Descartar el 81 % de las páginas de ese expediente
recorta solo ~**67 % de los tokens**, porque las páginas que vale la pena
conservar son justamente las densas en texto (el pedimento y sus anexos),
mientras que las descartadas —acuses VUCEM, portadas— son ligeras.

Cita la cifra de **páginas** para volumen de OCR, y la de **tokens** para
costo. Medido sobre `26 07 3435 6041248 CG.pdf` con el tipo declarado:

|                       | input | output | chunks | costo Bedrock |
| --------------------- | ----: | -----: | -----: | ------------: |
| Sin selección         | 57,512 |  5,400 |      6 |     $0.0930 |
| Con selección         | 18,719 |  1,800 |      2 |     $0.0305 |
| **Ahorro**            | **67 %** | **67 %** | 6→2 | **67 %** |

El output importa más de lo que parece: cuesta **5× el input** ($5.50 vs
$1.10 por millón) y escala con **chunks**, no con caracteres — cada chunk
emite un JSON completo del schema. Por eso el ahorro se calcula distinto
para cada lado.

### Cómo se reporta el ahorro (y qué es dato vs. estimación)

Los tokens que se cobran son **exactos**: salen del `usage` que devuelve
Bedrock y ya reflejan solo las páginas filtradas, así que `cost_audit.py` los
valora bien sin cambio alguno. Lo que no existe es el contrafactual — nadie
pagó la llamada cara — así que se estima y se reporta **separado**, nunca
mezclado con lo facturado:

```jsonc
"pageSelection": {
  "pagesConsidered": 86, "selected": [...], "skipped": [...],
  "savings": {
    "charsSent": 50109, "charsSkipped": 105096,     // hechos exactos
    "billedInputTokens": 18719,                      // lo que Bedrock cobró
    "billedOutputTokens": 1800,
    "chunksSent": 2, "estimatedChunksWithoutSelection": 6,
    "estimatedInputTokensSaved": 39260,              // derivado, ver abajo
    "estimatedInputSavingPct": 67.7,
    "estimatedOutputTokensSaved": 3600
  }
}
```

- **Input** se estima con la densidad tokens/carácter *de la llamada que sí
  corrió*, aplicada a los caracteres descartados. Es sólido porque el texto de
  página es lo único que cambia entre filtrar y no filtrar: el prefijo de
  schema e instrucciones es idéntico byte a byte.
- **Output** se estima por chunks evitados (`_chunk_pages` es matemática de
  texto, gratis y exacta).

Validado contra el contrafactual real corriendo el mismo documento con
`PAGE_SELECTION_ENABLED=false`: **la estimación de costo quedó a 0.6 % del
valor verdadero** (0.8 % en input tokens).

El agregado por documento vive en `expediente.usage.pageSelection` y se
persiste como `PageSelection` en la tabla de costos, con
`estimatedCostAvoidedUsd` valorado a las tarifas del modelo que corrió — así
`pnpm costos -- --id <id>` y `pnpm reporte:costos` muestran lo ahorrado sin
recalcular nada.

### El truncado ciego y la selección son excluyentes

`_MAX_PAGES` (50 por defecto) trunca el PDF **antes** que cualquier otra
etapa. Dejar los dos guardas activos anula el selector: en una subida de 68
páginas solo podría elegir entre las primeras 50, y un pedimento en la página
63 se perdería igual que antes — justo lo que la selección existe para evitar.

Por eso `process_pdf` elige uno de los dos (`_page_cap_for`):

| Situación | Tope |
| --- | --- |
| Tipo declarado **con** selección activa | **No se trunca.** Solo aplica `page_selection.max_source_pages` (500 por defecto) como salvaguarda contra un archivo capaz de agotar la Lambda — no es un límite de costo, el costo lo acota el selector |
| Tipo sin `page_selection`, o con `enabled: false`, o `PAGE_SELECTION_ENABLED=false` | `MAX_PAGES`, **idéntico a hoy** |
| Subida **sin tipo declarado** | `MAX_PAGES` — no se sabe qué selector aplica hasta que el segmenter clasifica, y clasificar exige leer las páginas |

Medido con `test-docs/long-docs.pdf` (68 págs, 59 nativas): antes se truncaba
a 50 y el resultado salía con `truncated` + `reviewRequired`; ahora se
procesan las 68 y se extraen 26 — sin warning y sin perder páginas.

### Cuándo el selector ni se molesta

Dos salidas rápidas, antes de puntuar nada, en las que se procesa todo:
`min_pages` (5) porque un documento corto es una sola llamada a Bedrock de
todos modos —filtrarlo no ahorra ni una llamada, y sí arriesga perder una
página—, y `expected_pages` (6 para el pedimento) porque una subida que no es
más grande que UN pedimento **es** el pedimento, no un expediente que lo
contiene. Por eso en la tabla de arriba los CFDIs de 1-2 páginas y
`normal_pend.pdf` salen con 0 % de ahorro: es lo correcto, no una falla de
calibración.

### Los límites del documento corrigen dos errores, no solo el costo

Declarar dónde empieza y acaba el documento (`start_markers`, `end_markers`,
`page_of_pattern`) resultó más una corrección que un ahorro. Medido contra el
scoring con `follow_pages`:

| Documento | Antes | Ahora | Qué pasaba |
| --- | --- | --- | --- |
| `long-docs.pdf` | 3–28 | 3–27 | se arrastraba el DODA de la página 28 |
| expediente 86 págs | 4–19 | 3–18 | **se perdía la página 3**, portada escaneada del pedimento sin texto que puntuar |

La segunda es la importante: esa página se estaba descartando y su contenido
nunca llegaba al modelo. `page_of_pattern` la recupera deduciendo el rango
desde el "Página 2 de 5" de la página siguiente.

### Cuando el tamaño es variable: `expected_pages: "auto"`

Para un tipo cuyo largo es impredecible, cualquier rango declarado sería
mentira. `"auto"` quita el atajo por tamaño y, **cuando los marcadores fallan**,
cambia "procesar todo" por una llamada barata a Bedrock que ubica el rango
(`pipeline/page_locator.py`, servicio `BedrockPageLocator` en la tabla de
costos).

La economía: la llamada manda el digest de ~450 caracteres por página que ya
usa el segmenter, no el texto completo. Sobre el expediente de 86 páginas eso
es ~$0.012 contra los ~$0.09 de extraerlo entero. Solo se paga cuando el
camino gratis ya se rindió, y **su costo aparece como servicio propio** — no
escondido dentro de `BedrockExtraction`, para que se pueda ver si se está
pagando solo.

`PEDIMENTO_ADUANAL` **no** lo usa: declara `expected_pages: 6`, así que su
camino es 100 % determinístico y nunca gasta esta llamada.

Tres advertencias para no leer de más estos números:

1. **Es ahorro de Bedrock, no de Textract.** El filtro corre *después* de
   `load_pages`, así que hoy el OCR se sigue pagando por cada página
   escaneada. Eso es la Fase 2 (OCR bajo demanda).
2. **El ahorro es del caso "tipo declarado"**, que es el común en producción
   (`raw/{TIPO}/{uuid}/`): ahí el archivo entero es UN segmento y el filtro
   descarta todo lo que no es del tipo. Con auto-clasificación el segmenter ya
   partió el archivo por documento, así que cada segmento es casi todo
   relevante y la selección suele salir en fail-open — correcto, pero sin
   ahorro.
3. **Un PDF 100 % escaneado no ahorra nada todavía**, porque el selector
   necesita texto. Verificado contra texto de Textract real (guardado en un
   `_prompt_input.json` de una corrida previa): los marcadores **sí** aciertan
   sobre OCR (scores 7 y 3 sobre un umbral de 3), aun cuando el ruido de OCR
   tumbó uno de ellos — pero un pedimento escaneado es casi todo páginas de
   pedimento, así que la selección excede `max_ratio` y cae en fail-open.

## 0. Recálculo 2026-08-04 (vía MCP AWS — Cost Explorer + Pricing API)

Datos nuevos desde el último corte: **54 resultados** en
`results/*.json` (29-jul a 04-ago 2026, 1 error de procesamiento sin
`documents`), **598 páginas**, **100 sub-documentos clasificados**.
Precios re-verificados en vivo con `aws-pricing` MCP — **sin cambios**
respecto a la tabla de la sección 1 (Input $1.10/1M, Output $5.50/1M,
Cache read $0.11/1M, Cache write $1.375/1M, Textract sync $0.0015/pág).

### Hallazgo 1 — el costo/página del pedimento nativo cayó ~4.7x

Mismo archivo de prueba que el "Caso A" original (`normal_pend.pdf`, 3
páginas nativas, sin OCR), 18 ejecuciones nuevas:

|                   | 2026-07-28 (antes) | 2026-08-04 (ahora) |
| ----------------- | ------------------ | ------------------ |
| Input tokens/doc  | ~20,631            | ~6,483–6,742       |
| Output tokens/doc | ~6,454             | ~883–1,054         |
| Costo/doc         | $0.0582            | $0.0123            |
| **Costo/página**  | **$0.0194**        | **$0.0041**        |

Caída de ~3.1x en input y ~7x en output para el **mismo PDF**. Coincide en
el tiempo con la migración del DSL de catálogo (`catalogs/PEDIMENTO_ADUANAL.json`
al nuevo formato `fields` consolidado, ver memoria `catalog-dsl-v4-migration`)
y con los cambios locales pendientes en `pipeline/expediente.py` /
`pipeline/pdf_pages.py` / `pipeline/result_common.py` (working tree, aún sin
commitear) — prompt/salida más compactos. No se investigó línea por línea
cuál cambio específico lo causó; queda como pendiente si se quiere atribuir
con precisión.

### Hallazgo 2 — la mitad de los sub-documentos clasificados ya no tienen extracción LLM (costo $0)

De los 100 sub-documentos clasificados en la ventana, **48 tienen
`extractionMeta: null`** — se clasifican pero no se extraen campos:
`OTRO` (28), `PERMISO_CERTIFICADO_SANITARIO` (4), `CFDI_GASTO_TERCERO` (7),
`FACTURA_LOGISTICA` (5), `FACTURA_AGENCIA_ADUANAL` (3), más
`FACTURA_HONORARIOS_XML` (1, pipeline determinístico `xml_document.py`,
nunca llama a Bedrock). Razón confirmada en el repo: sus catálogos viven
ahora en `catalogs/save/` (archivados durante la migración del DSL v4,
ver [[catalog-dsl-v4-migration]] en memoria) — el código no encuentra
entrada de catálogo activa para esos tipos, así que `extract_document` no
se invoca. Esto es **temporal** (migración en curso, no arquitectura
final): cuando esos tipos se reactiven con el DSL nuevo, su costo dejará
de ser $0.

### Tabla por tipo (ventana 29-jul → 04-ago, sólo costo Bedrock/LLM)

| Tipo                                                                                                                             |    docs | páginas |   costo LLM |    $/página |
| -------------------------------------------------------------------------------------------------------------------------------- | ------: | ------: | ----------: | ----------: |
| PEDIMENTO_ADUANAL                                                                                                                |      46 |     511 |     $1.2947 |     $0.0025 |
| PEDIMENTO_ADUANAL_V1                                                                                                             |       1 |       2 |     $0.0714 |     $0.0357 |
| CONSTANCIA_SITUACION_FISCAL                                                                                                      |       4 |       8 |     $0.0698 |     $0.0087 |
| FACTURA_HONORARIOS                                                                                                               |       1 |       1 |     $0.0138 |     $0.0138 |
| OTRO / PERMISO_CERTIFICADO_SANITARIO / CFDI_GASTO_TERCERO / FACTURA_LOGISTICA / FACTURA_AGENCIA_ADUANAL / FACTURA_HONORARIOS_XML |      48 |      75 |     $0.0000 |     $0.0000 |
| **TOTAL**                                                                                                                        | **100** | **598** | **$1.4498** | **$0.0024** |

El `$0.0025/página` de PEDIMENTO_ADUANAL incluye 14 ejecuciones repetidas
del mismo PDF de 27 páginas escaneadas (declarado explícitamente como
`PEDIMENTO_ADUANAL` al subir → 1 solo documento de 27 pág, no expediente
segmentado — ver regla de `declared_type` en memoria `pedimentos-pipeline`),
que amortiza el prompt entre más páginas y sale más barato por página que
el caso de 3 páginas nativas.

### Grounding contra dólares reales (Cost Explorer, sólo los servicios de este proyecto)

La cuenta 886031931562 tiene **otros experimentos corriendo en paralelo**
(el 08-03 se ve gasto de Mistral-Large-3, Nova Lite/Pro y Qwen3 bajo el
servicio genérico "Amazon Bedrock" — $0.154 ese día, ajeno a este pipeline,
que sólo usa `us.anthropic.claude-haiku-4-5-...`). Filtrando sólo
`Claude Haiku 4.5 (Amazon Bedrock Edition)` + `Amazon Textract` (usage type
`SyncTextPagesProcessed`, se excluyó `AsyncFormsPagesProcessed`/
`AsyncTablesPagesProcessed` del 29-jul por ser Analyze API, que este
pipeline no usa):

|                                | Bottom-up (S3, por-documento) | Cost Explorer (dólares reales facturados) |
| ------------------------------ | ----------------------------: | ----------------------------------------: |
| Bedrock Haiku 4.5              |                       $1.4498 |                                   $1.9237 |
| Textract Sync                  |             $0.7620 (508 pág) |                         $0.9795 (653 pág) |
| **Total**                      |                   **$2.2118** |                               **$2.9032** |
| **$/página** (÷ 598 pág de S3) |                   **$0.0037** |                               **$0.0049** |

La brecha (~31%) es real y tiene dos causas identificadas, mismo patrón
que ya advertía la sección 2 original: (1) la llamada de
clasificación/segmentación del PDF (antes de extraer por-documento) gasta
tokens de Bedrock que **no** quedan registrados en ningún
`extractionMeta.usage` por-documento — sólo se ve al comparar contra Cost
Explorer; (2) ~145 páginas de Textract facturadas no corresponden a
ningún resultado guardado en `results/` (reintentos/pruebas sin JSON
final), igual que documentaba la sección 2 para el periodo anterior.

**Costo/página blended actual: $0.0037–$0.0049 (antes $0.0144)** — la
caída es real (Hallazgo 1) pero está inflada por el Hallazgo 2 (mitad de
los tipos en $0 mientras dura la migración del catálogo); no usar este
número blended para proyectar el costo cuando todos los tipos vuelvan a
tener extracción activa — para eso, el número representativo es el de
PEDIMENTO_ADUANAL nativo puro: **$0.0041/página**.

### Costo real por documento (Bedrock + Textract, incl. los últimos subidos)

Se agregaron a la muestra 5 documentos `INE` subidos el 04-ago (imagen
JPEG/PNG, pipeline de imagen nuevo — 2 de 5 fallaron con
`unreadable PDF: Stream has ended unexpectedly`, sin costo porque el
error ocurre antes de llamar a Bedrock/Textract; vale la pena revisar
esos 2 casos si se van a subir más imágenes así). Costo real por tipo
(Bedrock + Textract, atribuido documento-por-documento):

| Tipo                                                                                                                             |   n | costo/doc (avg) | páginas | $/página |
| -------------------------------------------------------------------------------------------------------------------------------- | --: | --------------: | ------: | -------: |
| PEDIMENTO_ADUANAL (mezcla 3pág nativo + 27/50pág escaneado)                                                                      |  46 |         $0.0421 |     511 |  $0.0038 |
| PEDIMENTO_ADUANAL_V1                                                                                                             |   1 |         $0.0714 |       2 |  $0.0357 |
| CONSTANCIA_SITUACION_FISCAL                                                                                                      |   4 |         $0.0175 |       8 |  $0.0087 |
| FACTURA_HONORARIOS                                                                                                               |   1 |         $0.0138 |       1 |  $0.0138 |
| **INE** (imagen, 100% OCR)                                                                                                       |   3 |     **$0.0080** |       3 |  $0.0080 |
| OTRO / CFDI_GASTO_TERCERO / FACTURA_LOGISTICA / PERMISO_CERTIFICADO_SANITARIO / FACTURA_AGENCIA_ADUANAL / FACTURA_HONORARIOS_XML |  48 |         $0.0000 |      75 |  $0.0000 |

Casos de referencia puros (un solo tipo, sin mezclar con expedientes):

- **`normal_pend.pdf`, 3 pág nativas, sin OCR:** $0.0123/doc → $0.0041/página.
- **`pedimiento-scan.pdf`, 27 pág, 100% OCR (declarado, 1 solo doc):** ~$0.0988/doc → $0.0037/página.
- **INE, 1 pág imagen, 100% OCR:** $0.0080/doc → $0.0080/página.

## 0.1 Costo real en PROD (no dev) — recalculado 2026-08-04

**Sólo existe el stack `DocumentValidationStack-Dev` desplegado** (no hay
un stack `DocumentValidationStack` de prod corriendo en la cuenta —
`aws cloudformation list-stacks` sólo lo muestra a él). Por eso este
número no es "medido" como la sección 0 — es la sección 0 (que sí es
100% real) **más** el costo fijo de lo que activa
`src/document-validation-stack.ts` cuando `environment === "prod"`
(línea 110, `isProd`), calculado con precios reales vía `aws-pricing` MCP.

**Bedrock/Textract cuestan exactamente lo mismo en prod que en dev** — el
precio de AWS no depende del nombre del stack. Lo que prod agrega es
infraestructura fija adicional, gateada por `isProd` (confirmado leyendo
el código, líneas 552-667): Lambda usa el mismo `memorySize`/`timeout` en
ambos ambientes, así que no hay diferencia de cómputo — sólo:

| Extra sólo-prod                                                                                | Precio real (verificado con `aws-pricing` MCP, us-east-1)                                                                                                                  |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| WAFv2 Web ACL                                                                                  | $5.00/mes                                                                                                                                                                  |
| Regla rate-based (`RateLimitPerIp`)                                                            | $1.00/mes                                                                                                                                                                  |
| 3× AWS Managed Rule Group (`CommonRuleSet`, `KnownBadInputsRuleSet`, `AmazonIpReputationList`) | $1.00/mes **cada una** (se facturan como "Rule", no hay SKU separado para managed groups gratuitos de AWS — la sección 5 original sólo contaba 1 regla y subestimaba esto) |
| WAF request processing                                                                         | $0.60 / millón de requests (Tier0) — centavos al volumen actual                                                                                                            |
| API Gateway Usage Plan + API Key                                                               | $0 fijo (sólo se cobra el request, misma tarifa API Gateway de siempre)                                                                                                    |
| Dominio custom `idp.alsuper.com` + ACM (BYO cert)                                              | $0 (CNAME externo, cert importado)                                                                                                                                         |

**Piso fijo WAF corregido: $9.00/mes** (antes se documentó ~$6/mes —
faltaban 3 de las 4 reglas).

Cost Explorer registró ~$5.94/mes al prorratear la infraestructura de la
**cuenta completa**, pero no es correcto asignar ese importe como piso fijo
del proyecto: la mayor parte era almacenamiento de otras claves KMS de la
cuenta. La configuración actual crea **cero CMK en dev** (cifrado default de
AWS) y **una sola CMK compartida en prod**, incluido el bucket. Lambda,
DynamoDB, SQS, S3, CloudWatch, Step Functions y API Gateway son
principalmente consumo variable al volumen de este sistema.

### Piso fijo total y costo mensual realista en prod

| Ambiente                               |             Piso fijo atribuible/mes |
| -------------------------------------- | -----------------------------------: |
| Dev                                    |       $0 fijo (cifrado default AWS) |
| Prod                                   | ~$10.00 (1 clave KMS + $9 de WAFv2) |
| Prod + dev desplegados simultáneamente |                            ~$10.00 |

### Proyección acordada: 2,430 documentos / 7,290 páginas al mes

Supuesto: pedimentos de tres páginas procesados con el tipo declarado, por
lo que se omiten la clasificación visual, la extracción redundante del
blueprint y la segmentación por modelo. El costo de Bedrock + Textract usa
las ejecuciones más recientes del mismo pedimento de tres páginas
(~$0.01365/documento, incluyendo una página OCR). El resto del runtime se
prorratea conservadoramente desde la ventana real de 58 ejecuciones.

**AWS Business Support+ se excluye expresamente**: es un cargo compartido de
la cuenta y no forma parte del costo por página acordado para este proyecto.

| Componente                                                                       | Costo mensual | Costo/página |
| -------------------------------------------------------------------------------- | ------------: | -----------: |
| Bedrock + Textract                                                               |        $33.17 |     $0.00455 |
| Lambda, S3, SQS, CloudWatch, DynamoDB, Step Functions y API Gateway              |        ~$3.50 |    ~$0.00048 |
| Piso fijo de prod: 1 clave KMS + WAFv2                                           |        $10.00 |     $0.00137 |
| **Subtotal técnico antes de IVA**                                                |    **$46.67** | **$0.00640** |
| IVA 16%                                                                          |         $7.47 |     $0.00102 |
| **Total esperado con IVA**                                                       |    **$54.14** | **$0.00743** |
| **Presupuesto conservador: IVA + 15% de contingencia sobre el consumo variable** |    **$60.52** |  **$0.0083** |

El valor recomendado para presupuesto es **$0.0083 USD/página**, equivalente
a **$60.52 USD/mes** para 7,290 páginas. La diferencia contra el esperado de
$0.00743/página cubre variación de tokens, reintentos y errores de proceso.
Mantener dev desplegado ya no suma un piso fijo de KMS; solo agrega su consumo
de pruebas.

## 1. Modelo desplegado y precio real

Pipeline usa `us.anthropic.claude-haiku-4-5-20251001-v1:0` (perfil
cross-region "us."), que factura a tarifa **Regional** de us-east-1 — se
verificó que el monto de Cost Explorer coincide **al centavo** con
`tokens × precio Regional`, no con la tarifa "Global" (que es ~9% más
barata).

| Concepto                                            | Precio real (us-east-1, Regional) |
| --------------------------------------------------- | --------------------------------- |
| Input tokens                                        | $1.10 / 1M                        |
| Output tokens                                       | $5.50 / 1M                        |
| Cache read                                          | $0.11 / 1M                        |
| Cache write (5 min)                                 | $1.375 / 1M                       |
| Textract Sync `DetectDocumentText` (por página OCR) | $0.0015 / página                  |

## 2. Costo real medido por documento

**Caso A — `normal_pend.pdf`, 3 páginas nativas (pypdf layout), sin OCR**
(13 ejecuciones reales, 27-28 jul):

- Promedio: 20,631 input tok + 6,454 output tok (sin cache: doc de 1 solo chunk)
- Costo Bedrock: **$0.0582/doc → $0.0194/página**

**Caso B — expediente de 50 páginas (32 nativas + 18 escaneadas/OCR),
extracción multi-chunk con prompt caching:**

- 61,183 input + 63,150 output + 77,280 cache-read + 51,520 cache-write tok
- Bedrock: $0.4940 + Textract (18 pág × $0.0015) = $0.027
- Total: **$0.5210/doc → $0.0104/página**

**Blended (las 14 ejecuciones reales, 89 páginas totales):**

- Total: $1.2775 → **~$0.0144/página**
- **Promedio real de páginas por documento: 89 / 14 = 6.4 páginas/doc**
  (mediana 3 — el promedio lo jala hacia arriba el expediente de 50 páginas;
  si tu mezcla real de documentos es más parecida a CFDIs/pedimentos
  sueltos, 3 páginas/doc es más representativo que 6.4)

Un expediente grande sale más barato por página que uno chico: el
prompt-caching amortiza el prefijo del catálogo/prompt entre chunks, y el
output (campos extraídos) no crece linealmente con las páginas.

**Rango real observado: $0.010–$0.019/página, blended ~$0.014/página.**

### Validación cruzada (Cost Explorer, no solo cálculo)

Para 2026-07-14 → 2026-07-28: Bedrock Haiku 4.5 = $16.6933 exactos —
coincide con `3.007258M in × $1.10 + 2.330075M out × $5.50 + 1.465509M
cache-read × $0.11 + 0.297228M cache-write × $1.375` = $16.6933. Textract =
$1.0935 = 729 páginas × $0.0015 exacto.

**Importante:** ese mismo periodo la cuenta registra ~3x más tokens de
Bedrock y 216 páginas Textract solo el 27-jul (no 18) — hubo más
ejecuciones reales ese día (otros tipos de documento, reintentos, scripts
de prueba) que no dejaron un JSON en `results/` para poder atribuirles
páginas exactas. Por eso **no** se reporta un "$/página a nivel de cuenta":
sería inventar el denominador. Los números de la sección 2 son los
confiables porque están atados 1:1 a tokens y páginas de un documento real.

## 3. Si migraras de modelo (solo referencia — no es tu costo real)

Precios on-demand reales, us-east-1, Regional, por 1M tokens:

| Modelo                      | Input | Output | Cache read | Cache write 5m |
| --------------------------- | ----- | ------ | ---------- | -------------- |
| **Haiku 4.5 (el que usas)** | $1.10 | $5.50  | $0.11      | $1.375         |
| Sonnet 4.5                  | $3.30 | $16.50 | $0.33      | $4.125         |
| Sonnet 5                    | $2.20 | $11.00 | $0.22      | $2.75          |
| Opus 4.5 / 5                | $5.50 | $27.50 | $0.55      | $6.875         |

Mismo volumen de tokens, Sonnet 4.5 ≈ 3x el costo/página; Opus ≈ 5x.

### 3.1 Latencia por modelo (medida, no estimada)

El costo no es lo único que cambia al cambiar de modelo: **la latencia la manda
la SALIDA**, que se genera token a token. Mismo PDF
(`test-docs/pedimentos-screens/2607-0973-6100971_P.PDF-1.pdf`, 2 páginas
nativas), mismo catálogo `PEDIMENTO_ERP`, mismo código, 2026-09-02:

| Modelo | Duración | Tokens entrada | Tokens salida | Extracción |
| --- | --- | --- | --- | --- |
| `moonshotai.kimi-k2.5` | **7.0 s** | 7,735 | **667** | correcta |
| `us.anthropic.claude-haiku-4-5-...` | **10.6 s** | 9,353 | 1,237 | correcta |
| `minimax.minimax-m2.5` | **58.5 s** | 7,610 | 1,616 | correcta |

Los tres sacaron la misma factura (`35079805`, acuse `COVE2689JP013 / 35079805`,
21,178.80). minimax además es **errático**: 14.6 s, 17 s, 27 s y 58.5 s sobre la
MISMA entrada en corridas distintas del mismo día. Y no soporta prompt caching
(ni kimi), así que en un documento multi-chunk cada llamada paga el prefijo de
schema+hints completo sin descuento — Haiku 4.5 es el único de los tres que lo
aprovecha, y el único con tarifa verificada en `pipeline/pricing.py` (por eso
todo costo calculado con los otros dos sale marcado ⚠ "tarifa aproximada").

Se fija por tipo en el catálogo (`model_ids: {"extractor": "..."}`) o global con
`BEDROCK_MODEL_ID` / `BEDROCK_EXTRACTOR_MODEL_ID` — ver
`pipeline/bedrock.py::_model_candidates` para el orden de precedencia.

## 4. Infraestructura de soporte (medida, mismo periodo, dev)

> **Corte histórico, no usar como piso fijo actual.** Cost Explorer mezclaba
> las claves KMS de toda la cuenta: el cargo de KMS indicado abajo no era
> atribuible íntegramente al proyecto. La corrección vigente está en la
> sección 0.1: $0 fijo de KMS para dev y ~$10/mes de piso fijo para prod.

Costo real del resto del stack en 2026-07-14 → 2026-07-28 (KMS, Lambda,
Step Functions, DynamoDB, SQS, S3, API Gateway, CloudWatch — todo lo que
no es Bedrock/Textract):

| Servicio                 | Costo real (14 días)   | Nota                                                                                                                                                   |
| ------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| AWS KMS                  | $3.09 (5,613 requests) | El más alto de los "extras" — la key propia del bucket se invoca por request de S3/Lambda; vale la pena revisar si se puede cachear o reducir llamadas |
| Amazon SQS               | $0.069                 | Cola raw→enrichment                                                                                                                                    |
| AWS Lambda               | $0.167                 | Upload + Enrichment + Query lambdas                                                                                                                    |
| Amazon S3                | $0.012                 | Almacenamiento + requests                                                                                                                              |
| CloudWatch (+Events)     | $0.019                 | Logs + EventBridge                                                                                                                                     |
| DynamoDB                 | $0.0015                | Catálogo + tracking table                                                                                                                              |
| API Gateway              | $0.0003                | Volumen de pruebas, bajísimo                                                                                                                           |
| Step Functions           | $0.0005                | Orquestación del pipeline                                                                                                                              |
| **Total infra (sin IA)** | **~$3.38**             | En 2 semanas de pruebas dev                                                                                                                            |

Esto es overhead prácticamente fijo por request, no por página — a este
volumen es ruido comparado con Bedrock ($16.69) y Textract ($1.09), excepto
KMS que destaca y merece revisión.

_(Se detectaron también ~$0.037 en RDS y algo de Glue/CloudTrail/CloudFormation
en la cuenta ese periodo — no corresponden a la arquitectura de este
proyecto, probablemente son otros recursos en la misma cuenta AWS; no se
incluyen en el total de arriba.)_

## 5. Extras que SOLO existen en producción (no están en dev)

> **Corte histórico.** La verificación vigente confirmó $9/mes de WAF
> ($5 por Web ACL + 4 reglas/grupos a $1 cada uno). Usar la sección 0.1 para
> el piso fijo y la sección 6 para el costo por página acordado.

Según `src/document-validation-stack.ts`, el ambiente `prod` agrega
API keys + WAFv2 + dominio custom, gateado por `environment === "prod"`.
Esto es costo **fijo adicional**, no depende de páginas procesadas:

| Concepto (prod only)                                       | Precio real (us-east-1)                                                                                             |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| WAFv2 Web ACL                                              | $5.00 / mes                                                                                                         |
| Por regla (ej. la regla rate-based propia)                 | $1.00 / mes                                                                                                         |
| Requests procesados por WAF (tier base)                    | $0.60 / millón                                                                                                      |
| AWS Managed Rule Groups (reglas gestionadas de AWS)        | no confirmado en esta consulta — la consola de WAF típicamente suma cuota extra por grupo administrado, revisar ahí |
| API Gateway REST (misma tarifa que dev, ya corre en ambos) | $3.50 / millón de requests (primeros 333M/mes)                                                                      |
| Certificado ACM importado (BYO)                            | $0 (import es gratis)                                                                                               |
| Dominio custom / Route 53                                  | $0 en AWS si el DNS externo solo apunta con CNAME (no hay hosted zone)                                              |

**Piso fijo mensual en prod por WAF: ~$6/mes** (Web ACL + 1 regla) más el
managed rule group sin confirmar, independiente de cuántos documentos
proceses. A los volúmenes de request de esta app (pruebas: decenas/cientos
al mes) el cargo por-request de WAF y API Gateway es centavos, así que el
costo por página en prod es esencialmente el mismo de la sección 2
($0.010–$0.019/página) **más** ese piso fijo de WAF que no escala con uso.

## 6. Resumen vigente — 2,430 documentos / 7,290 páginas al mes

> Actualizado 2026-08-17. Esta sección reemplaza las proyecciones anteriores
> para el volumen acordado. Los casos históricos de las secciones 1–5 se
> conservan para explicar cómo cambió el costo con la optimización del DSL.

| Unidad                                                             |                   Costo vigente |
| ------------------------------------------------------------------ | ------------------------------: |
| Bedrock + Textract por pedimento de 3 páginas                      |             ~$0.01365/documento |
| Subtotal técnico antes de IVA                                      |    $46.67/mes — $0.00640/página |
| Total esperado con IVA                                             |    $54.14/mes — $0.00743/página |
| **Presupuesto recomendado con IVA y 15% de contingencia variable** | **$60.52/mes — $0.0083/página** |

### COSTO RECOMENDADO POR PÁGINA EN PROD

# **$0.0083 USD / página**

Este costo incluye Bedrock, Textract, Lambda, DynamoDB, S3, API Gateway,
KMS, SQS, CloudWatch, Step Functions, WAFv2, IVA y 15% de contingencia
sobre el consumo variable. **No incluye AWS Business Support+** ni el costo
de mantener dev activo en paralelo con prod.

La cifra depende de que el documento se envíe con su tipo declarado y de
que la mezcla productiva sea representativa del pedimento actual de tres
páginas. Si se reactivan los catálogos archivados, cambia la mezcla de tipos
o se omite `documentType`, se debe volver a medir antes de usar este valor.

### Colchón presupuestario ampliado

# **$0.0131 USD / página**

Para planeación financiera puede reservarse **$0.0131/página**, equivalente
a **$95.50 USD/mes** para 7,290 páginas. Este importe es un colchón ampliado
por encima del presupuesto técnico recomendado de $0.0083/página; no es el
costo esperado y no atribuye AWS Business Support+ al proyecto.

## 7. Auditoría de costos por documento (tabla DynamoDB, 2026-08-05)

Todo lo anterior en este documento son cortes puntuales calculados a mano
(S3 `results/*.json` cruzado contra Cost Explorer). Desde 2026-08-05 el
enrichment Lambda escribe, **por cada documento procesado**, un registro con
el costo real desglosado por servicio a una tabla dedicada:
`document-validation-costs[-dev]` (PK `DocumentId`, `RETAIN` — no se borra
con `pnpm run destroy`). Separada de la tabla de tracking a propósito: es un
dato de auditoría/finanzas, no algo por lo que se busquen documentos.

**Qué desglosa cada registro** (`enrichment-lambda/cost_audit.py` +
`pipeline/pricing.py`, tarifas de la sección 1 de este documento):

| Servicio                                                      | Qué mide                                                                                                                                                     | Cuándo es $0                                                                                                |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `BedrockSegmentation`                                         | Tokens de `segmenter.segment_pages` (clasifica rangos de página en un expediente)                                                                            | Tipo declarado explícitamente al subir (`raw/{TIPO}/...`) — no hay llamada al modelo                        |
| `BedrockExtraction`                                           | Tokens sumados de TODOS los chunks/segmentos de `text_extractor.extract_document`                                                                            | Segmento sin campos de catálogo (ej. `OTRO`)                                                                |
| `BedrockClassificationUpstream` / `BedrockProcessingUpstream` | Tokens de los pasos de clasificación/extracción del blueprint (Lambda `model-invoke-lambda`, corren ANTES del enrichment Lambda)                             | Bypass de clasificación manual o de extracción diferida — el caso normal para `PEDIMENTO_ADUANAL` declarado |
| `TextractOcr`                                                 | Páginas OCR × $0.0015                                                                                                                                        | Documento 100% texto nativo (sin páginas escaneadas)                                                        |
| `LambdaCompute`                                               | Aproximado: duración de la fase de procesamiento (no la invocación completa — excluye descarga S3 y las escrituras finales a S3/DynamoDB) × memoria asignada | Nunca — siempre hay algo de cómputo                                                                         |

Este registro **cierra una brecha que las secciones 0/2 de este documento ya
documentaban**: antes, los tokens de `segmenter.py` solo se logueaban
(`logger.info`, que nunca se veía porque el root logger no tiene level
configurado) y los del paso de clasificación/procesamiento del blueprint se
descartaban por completo en `model-invoke-lambda/index.py` — ninguno de los
dos llegaba a ningún lado calculable. Ambos se capturan y propagan ahora, así
que el número que se ve por-documento debería acercarse más al de Cost
Explorer que antes (aunque la brecha de ~31% de la sección 0 se midió antes
de este cambio — no está re-validada contra Cost Explorer todavía).

### Atributo `PageSelection` (2026-08-28)

Cuando la selección de páginas filtró algo (§0-bis), el registro lleva además
un atributo `PageSelection` con **lo que se ahorró**, para poder responder
"cuánto nos ahorró esto" semanas después sin recalcular nada:

| Campo | Qué es |
| --- | --- |
| `pagesConsidered` / `pagesExtracted` / `pagesSkipped` | páginas vistas, extraídas y descartadas |
| `charsSent` / `charsSkipped` | caracteres de texto enviados vs. descartados — **exacto** |
| `chunksSent` / `estimatedChunksWithoutSelection` | llamadas a Bedrock hechas vs. las que habría habido |
| `billedInputTokens` / `billedOutputTokens` | **lo que Bedrock cobró de verdad** |
| `estimatedInputTokensSaved` / `estimatedOutputTokensSaved` | tokens evitados (derivados) |
| `estimatedInputSavingPct` | % de input tokens ahorrado |
| `estimatedCostAvoidedUsd` | $ evitados, a las tarifas del modelo que corrió |

**Vive FUERA de `Services` a propósito.** Ahí solo van montos facturados, y
casi todo esto valora una llamada que nunca se hizo. `estimatedCostAvoidedUsd`
**nunca** se suma a `TotalCostUsd` — si se sumara, la tabla sobrestimaría el
gasto real. Un test lo verifica (`tests/unit/test_page_selection_cost_record.py`).

Se guarda vía `_decimalize()`: el bloque trae floats
(`estimatedInputSavingPct`, `estimatedCostAvoidedUsd`) y boto3 rechaza un
float en cualquier parte del item, **en tiempo de escritura** — es decir, en
producción, no en desarrollo. El mismo test serializa el item con el
`TypeSerializer` de boto3 para que esa falla salga aquí y no en CloudWatch.

Se lee con `pnpm costos`:

- `pnpm costos -- --id <id>` → tabla facturado / evitado / sin selección, por
  input tokens, output tokens, llamadas a Bedrock y caracteres.
- `pnpm costos` (agregado) → páginas no extraídas, tokens y costo evitados del
  corte completo, más "sin selección el corte habría costado ~$X en vez de $Y".

También aparece en los reportes:

| Reporte | Dónde |
| --- | --- |
| `pnpm costos -- --id <id>` | tabla facturado / evitado / sin selección por documento |
| `pnpm costos` | agregado del corte + "habría costado ~$X en vez de $Y" |
| `pnpm reporte:costos` | sección **Ahorro por selección de páginas** en el `.md` y el `.html`, hoja `SeleccionPaginas` en el `.xlsx`, y columnas `PageSel.*` por documento en la hoja `Documentos` |
| `pnpm reporte` | tarjeta en el dashboard que **respeta los filtros** (se recalcula en el navegador con el corte activo) y se oculta sola si ningún documento del corte tuvo selección |

En todos, `evitado` va en su propia columna, nunca sumado al costo total.

**Qué NO se trackea por documento, deliberadamente:** KMS, S3, SQS,
CloudWatch, API Gateway, DynamoDB. Ninguno es atribuible a una invocación
individual desde dentro del propio Lambda (no hay un conteo de tokens/páginas
por-request como con Bedrock/Textract), y la sección 4 ya midió que juntos
son ruido frente a Bedrock+Textract a este volumen (~$3.38 en dos semanas
contra ~$18 de Bedrock+Textract en la misma ventana) — calcular un reparto
por-documento de eso sería precisión inventada, no una medición real.

**Cómo consultarlo:**

```
pnpm costos                       # resumen: total + desglose por tipo + últimos documentos
pnpm costos -- --id <documentId>  # detalle de un documento (prefijo basta)
pnpm costos -- --env prod
```

`PricingVersion` (`pipeline/pricing.py`, hoy `2026-08-05`) queda grabado en
cada registro para que una futura repreciación de AWS no vuelva
incomparables los registros viejos contra los nuevos sin decirlo.

**Alcance:** solo documentos procesados DESPUÉS de este cambio tienen
registro — no hay backfill retroactivo de los resultados que ya estaban en
S3/tracking antes de 2026-08-05 (los datos de `usage`/`pages` sí existen ahí
para quien quiera calcularlos a mano, igual que hizo este documento en las
secciones anteriores).
