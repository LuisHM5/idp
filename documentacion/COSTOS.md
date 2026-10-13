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

## 0-ter. Extracción determinística e híbrida por catálogo (2026-10-13)

`PEDIMENTO_ERP` y `CERTIFICADO_POLIZA` intentan sus `text_source` sobre el texto
pypdf/Textract antes de Bedrock. Si todos los campos esperados quedan resueltos,
la línea `usage.extraction` lleva cero tokens; si hay huecos, sólo el sub-schema
faltante se factura. En certificados, un perfil desconocido o ambiguo restaura
el schema completo para no confiar en coincidencias parciales. Esto no elimina
el costo de Textract de una página escaneada ni una clasificación automática:
para evitar esta última la carga debe declarar `documentType`.

El envelope publica dos números deliberadamente separados:

- `aiExecuted`/`aiUsage`: ejecución y tokens **reales facturados** que Bedrock
  reportó (el OCR de Textract no cuenta como IA generativa);
- `deterministicSavings`: contrafactual **estimado** contra una llamada con el
  schema completo. La entrada usa la densidad tokens/caracteres de la llamada
  parcial real cuando existe; la salida serializa los valores determinísticos
  con la forma que habría generado el modelo y aplica 4 caracteres/token.
- `aiResponseSavings`: contrafactual **estimado** del protocolo compacto contra
  el JSON legacy. `billed*` sigue siendo consumo real; `estimatedLegacy*` y
  `estimated*Saved` nunca se agregan a la factura. Un ahorro negativo indica
  que hubo fallback y se pagaron la llamada compacta fallida más legacy.

`documents[].deterministicExtraction` conserva `strategy`, `profile`,
`fallbackFields` y el cálculo por documento. El estimado nunca se suma a
`usage` ni se presenta como factura real. En la medición desplegada del
pedimento de 17 páginas, el camino híbrido tardó 160.75 s y facturó 38,804
tokens de entrada + 10,058 de salida; después de completar el mapper y usar
`fallback: review`, el mismo caso terminó en 2.47 s con 0/0 tokens. Los cuatro
certificados de calibración se verificaron localmente sin llamadas de modelo;
su costo/latencia desplegados deben medirse después de sincronizar el catálogo.

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
el tiempo con la migración del DSL de catálogo (`catalogs/base/aduanas/PEDIMENTO_ADUANAL.json`
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

## 0.1 Costo real en PROD (no dev) — verificado 2026-09-08

Ya están desplegados simultáneamente `DocumentValidationStack-Dev` y
`DocumentValidationStack`. La verificación en vivo cruzó los recursos físicos
de CloudFormation con WAFv2, KMS y Cost Explorer; no se atribuyó el total de la
cuenta por parecido de nombre. Prod tiene exactamente **1 Web ACL con 4 reglas**
y **1 CMK activa** pertenecientes al stack. En toda la cuenta sólo existe ese
Web ACL y, en `us-east-1`, esa CMK es la única clave administrada por el cliente
en estado `Enabled` (las anteriores están `PendingDeletion`). Esto permite
atribuir con seguridad el cargo fijo prorrateado, aun cuando Cost Explorer no
tenga granularidad `RESOURCE_ID` habilitada.

Al corte abierto 2026-09-01 → 2026-09-09, Cost Explorer devuelve **$2.162506**
de WAF fijo (Web ACL + reglas; se excluyen $0.000006 de 10 requests) y
**$0.236111** de almacenamiento de la CMK: **$2.398617 MTD**, marcado por AWS
como estimado mientras el mes sigue abierto. El run-rate mensual comprobado es
el mismo piso de **$10.00/mes** documentado abajo.

**Bedrock/Textract cuestan exactamente lo mismo en prod que en dev** — el
precio de AWS no depende del nombre del stack. Lo que prod agrega es
infraestructura fija adicional, gateada por `isProd` (confirmado leyendo
el código, líneas 552-667): Lambda usa el mismo `memorySize`/`timeout` en
ambos ambientes, así que no hay diferencia de cómputo — sólo:

| Extra sólo-prod                                                                                | Precio real (verificado contra precios públicos de AWS y Cost Explorer, us-east-1)                                                                                        |
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
aprovecha.

> **Corregido el 2026-09-04.** Esa frase decía además que Haiku era el único
> con tarifa verificada en `pipeline/pricing.py`, y era cierto — ése era el
> problema. `minimax.minimax-m2.5` es el **primero** de
> `model_fallback.DEFAULT_FALLBACK_MODELS`, o sea el que realmente corría, y al
> no estar en la tabla **todo** su costo se calculaba a tarifa de Haiku: un
> expediente real de 31 páginas se reportaba como $0.143 cuando costó $0.070.
> Ya están los tres (MiniMax `$0.30/$1.20`, Kimi K2.5 `$0.60/$3.00`, GLM 5
> `$1.00/$3.20` por millón), verificados contra la AWS Price List API
> (`AmazonBedrock`, us-east-1, `service_tier: standard`, vigentes desde
> 2026-08-01). `tests/unit/test_pricing_coverage.py` fija la invariante: todo
> modelo que pueda correr tiene que tener tarifa propia.
>
> **Los números de las secciones anteriores a esta fecha se midieron contra
> Cost Explorer con Haiku corriendo**, así que siguen siendo válidos para su
> época; lo que estaba mal era el cálculo de los documentos procesados con los
> modelos nuevos.

Se fija por tipo en el catálogo (`model_ids: {"extractor": "..."}`) o global con
`BEDROCK_MODEL_ID` / `BEDROCK_EXTRACTOR_MODEL_ID` — ver
`pipeline/bedrock.py::_model_candidates` para el orden de precedencia.

**Segmentación con más de un modelo en la misma invocación (2026-09-04).**
La latencia errática de minimax de arriba, sobre un expediente grande (31+
páginas, o cualquiera con 2+ tipos declarados), podía agotar el presupuesto
de salida de la llamada de segmentación (`max_tokens` fijo en 4096, ni
siquiera el default de `converse()`) sin escribir texto — el pipeline
colapsaba TODO el archivo a `documentClassification: "OTRO"` /
`documentCount: 0`, sin ninguna señal en la respuesta. `pipeline/segmenter.py`
ahora detecta esto (`stopReason`/respuesta vacía), reintenta una vez con el
modelo más confiable, y para expedientes grandes reparte la clasificación en
ventanas de páginas — ver `documentacion/GUIA_DESPLIEGUE_Y_USO.md` §5.6.1.
Consecuencia de costeo: `usage.segmentation` puede traer llamadas a MÁS DE UN
modelo en una sola invocación (el reintento, o ventanas distintas). Cuando
pasa, trae un `perModel: [...]` además del `modelId`/`usage` de siempre, y
`cost_audit.py::_segmentation_cost` sabe agregarlo tarifa por tarifa en vez
de aplicar la de un solo modelo a todo — el mismo tipo de error que el
párrafo de arriba ya documentó una vez, de otra causa.

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

### Atributo `ExtractionAudit` (2026-10-13)

La misma tabla dedicada de costos guarda ahora cómo se obtuvo la extracción,
sin inferirlo por el `TYPE_ID` ni por la duración:

| Campo | Qué registra |
|---|---|
| `AiExecuted` / `AiStages` | si corrió un modelo en cualquier etapa y cuáles |
| `Documents[].RequestedMode` / `EffectiveMode` | override solicitado y política resuelta |
| `Documents[].Strategy` | `model`, `hybrid` o `deterministic` realmente observado |
| `ResolvedFieldCount` / `FallbackFieldCount` | cuánto resolvió código y cuánto fue a IA |
| `Profile` / `ProfileStatus` | layout reconocido y estado de la decisión, cuando aplica |
| `DeterministicSavings.*` | input/output estimados evitados y costo contrafactual según el modelo que habría corrido |
| `Documents[].AiResponse*` | formato seleccionado/efectivo, fuente, fallback y número de llamadas compact/legacy |
| `AiResponseSavings.*` | tokens compactos facturados, contrafactual legacy y costo evitado estimado; puede ser negativo tras fallback |

Los costos de Bedrock realmente ejecutado siguen en `Services` y alimentan
`TotalCostUsd`. `ExtractionAudit.DeterministicSavings` y
`ExtractionAudit.AiResponseSavings` viven aparte y **nunca se
suma** al total: puro código tiene costo Bedrock $0; híbrido factura sólo el
fallback; modelo completo factura su usage completo. Textract conserva su costo
independiente en `Services.TextractOcr`, incluso cuando no hubo IA generativa.
`pnpm costos -- --id <id>` muestra este bloque y `pnpm reporte:costos` lo exporta
en las columnas `AiExecuted`, estrategia, perfil/estado, tokens y costo evitado.

**Qué NO se trackea por documento, deliberadamente:** KMS, S3, SQS,
CloudWatch, API Gateway, DynamoDB. Ninguno es atribuible a una invocación
individual desde dentro del propio Lambda (no hay un conteo de tokens/páginas
por-request como con Bedrock/Textract), y la sección 4 ya midió que juntos
son ruido frente a Bedrock+Textract a este volumen (~$3.38 en dos semanas
contra ~$18 de Bedrock+Textract en la misma ventana) — calcular un reparto
por-documento de eso sería precisión inventada, no una medición real.

`pnpm reporte` sí los muestra ahora en una sección de **Infraestructura del
stack**, sin repartirlos por documento: inventaría CloudFormation en vivo,
calcula el piso WAF/KMS desde los recursos efectivamente desplegados, comprueba
el fijo MTD con Cost Explorer sólo cuando la exclusividad del recurso está
demostrada y consulta el gasto variable por `CostStack` o por el cruce manual
descrito en la sección 8. El mismo reporte
separa las ejecuciones **Con IA / Sin IA / Sin datos** por uso real de servicios
Bedrock; no mantiene una lista hardcodeada de TYPE_IDs, por lo que XML/tabulares
determinísticos y futuros pipelines sin modelo entran automáticamente. En el
HTML, el filtro muestra los seis tipos principales por volumen y un botón
`+N ver todos`, además de un buscador por `TYPE_ID` o nombre legible; el universo
se reconstruye desde los catálogos activos y los datos en cada generación,
incluyendo tipos con cero documentos, así que un tipo nuevo se incorpora sin
modificar el reporte. Las gráficas HTML llevan leyendas persistentes: los
estados muestran color, nombre, cantidad y porcentaje; las series categóricas
repiten su muestra de color junto a la etiqueta, y las referencias explican por
separado corte actual, presupuesto, esperado, histórico y severidad del delta.

El alcance por defecto es el **año calendario completo**: del 1 de enero al 31
de diciembre del año actual (`--year` permite elegir otro). `--desde` y
`--hasta` siguen siendo overrides; si sólo se indica uno, el otro extremo es el
inicio o cierre de ese mismo año. Las tablas se cargan completas y después se
aplica esa ventana, por lo que el rango interactivo inicia mostrando todos los
documentos procesados dentro del periodo, no una muestra ni sólo el mes actual.

La infraestructura incluye una vista de **gasto fijo mes a mes**. Los meses
cerrados usan el cargo publicado por Cost Explorer, el mes en curso se destaca
como MTD (gastado hasta la fecha del reporte) frente a su cierre estimado, y los
meses futuros usan el run-rate del inventario actual. La proyección anual suma
real cerrado + estimación del mes abierto y de los meses restantes; no convierte
los cargos variables compartidos de S3/Lambda/DynamoDB en supuestos costos
fijos.

Una segunda vista, **Gasto real del reporte, mes a mes**, suma únicamente
procesamiento con auditoría de costo y fijo realmente atribuible. Muestra la
cobertura `documentos con costo / documentos procesados` y mantiene la
proyección en otra serie/columna: ningún valor futuro o estimado se presenta
como dinero ya gastado.

**Cómo consultarlo:**

```
pnpm costos                       # resumen: total + desglose por tipo + últimos documentos
pnpm costos -- --id <documentId>  # detalle de un documento (prefijo basta)
pnpm costos -- --env prod
```

`PricingVersion` (`pipeline/pricing.py`, hoy `2026-09-04`) queda grabado en
cada registro para que una futura repreciación de AWS no vuelva
incomparables los registros viejos contra los nuevos sin decirlo.

**Alcance:** solo documentos procesados DESPUÉS de este cambio tienen
registro — no hay backfill retroactivo de los resultados que ya estaban en
S3/tracking antes de 2026-08-05 (los datos de `usage`/`pages` sí existen ahí
para quien quiera calcularlos a mano, igual que hizo este documento en las
secciones anteriores).

## 8. Atribución de costos de infraestructura por stack

Cada despliegue aplica una etiqueta estable al stack y a los recursos para los
que CloudFormation soporta propagación:

- dev: `CostStack=DocumentValidationStack-Dev`
- prod: `CostStack=DocumentValidationStack`
- bootstrap compartido: `CostStack=CDKToolkit`, mediante
  la preparación automática de cada deploy

La etiqueta es únicamente metadata: no cambia nombres, referencias, permisos,
rutas ni comportamiento del pipeline. Las tablas de catálogo son recursos
importados, no administrados por los stacks, y por eso requieren etiquetado
directo. `deploy`, `deploy:dev` y `deploy:prod` las crean cuando faltan y
aseguran la etiqueta automáticamente; sobre una tabla existente nunca cambian
los datos ni el esquema. La tabla de fallback, si existe, es compartida y no se
debe cargar artificialmente a uno de los dos ambientes.

Para que Cost Explorer muestre importes, un administrador de la cuenta
pagadora debe activar la clave de asignación `CostStack`. Puede tardar hasta 24
horas en aparecer después de aplicarse y hasta otras 24 horas en quedar activa.
El backfill admite hasta 12 meses, pero únicamente recupera periodos durante
los cuales el recurso ya tenía la etiqueta. Los cargos de servicios que no
produzcan líneas facturables etiquetables permanecerán sin atribuir; por eso la
suma por stack no se fuerza a coincidir artificialmente con el total de la
cuenta.

Verificación del 2026-09-08: los recursos sí llevan la etiqueta, pero la cuenta
vinculada no puede consultar su estado de activación y una consulta filtrada por
`CostStack` devuelve cero grupos. Eso ya no detiene `pnpm reporte`: el reporte
cruza los tipos y IDs físicos de CloudFormation del ambiente seleccionado con
los cargos MTD por servicio de la cuenta. El cargo de cuenta se muestra como
contexto sólo si ese servicio existe en el stack y se suma como **atribuido al
stack** únicamente cuando el inventario demuestra exclusividad del recurso
(actualmente WAF y almacenamiento de la CMK). Así se evita cargar a dev costos
de prod o repartir arbitrariamente S3/Lambda/DynamoDB compartidos.

El `$0` fijo de un reporte generado sin argumentos es correcto y corresponde a
`--env dev`: ese stack no declara Web ACL ni clave KMS propia. No significa que
la cuenta completa cueste cero. En el corte 2026-09-01—2026-09-08, los cargos
de WAF (~$2.16 MTD) y almacenamiento KMS (~$0.24 MTD) pertenecen a prod. Para
verlos scopeados se usa `pnpm reporte -- --env prod`; allí el inventario da un
piso de **$10/mes** y el prorrateo real MTD aunque `CostStack` siga sin datos.

## 9. Costo estimado antes de procesar

`pnpm test:e2e:pedimento` y `pnpm test:e2e:suite` muestran el costo **antes** de
subir nada. Hasta 2026-09-14 ese número era páginas × un $/página promedio (el
histórico del tipo, o $0.0041 nativa / $0.0080 OCR de este documento): no sabía
qué modelo corre, cuántas páginas pasan por Textract, cuántas manda la selección
de páginas ni cuántas llamadas hace la extracción. Medido contra documentos
reales, erraba **72% mediano** por documento (hasta +1400% en un escaneado).

Hoy lo calcula `scripts/_cost_estimate.py` replicando lo que cobra
`cost_audit.build_cost_breakdown`, con el mismo código del enrichment Lambda y
sin ninguna llamada pagada (los clientes de Bedrock y Textract se bloquean):

| Pieza | Cómo se obtiene | Precisión |
|---|---|---|
| Modelo | `bedrock.explain_model_resolution("extractor", tipo)` con las variables del Lambda desplegado (`aws lambda get-function-configuration`) y el catálogo JSON + DynamoDB del ambiente. Hoy dev y prod: `minimax.minimax-m2.5` | exacto |
| Textract | páginas sin capa de texto utilizable (`pdf_pages.load_pages`, mismo umbral y detección de texto corrupto) × `pricing.textract_cost`. Todas pasan por OCR **antes** de la selección | exacto (36/36 documentos reales) |
| Páginas a la IA | `page_selector.select` sobre el texto nativo. Con páginas escaneadas no hay texto local: se usa cuántas suele elegir la selección en escaneados de ese tipo (calibrado; `pages.pdf`: 3 de 31) y el tope asume fail-open | exacto en nativos; estimado en escaneados |
| Llamadas | `text_extractor._chunk_pages` × `_field_tree_call_partitions` | exacto con el catálogo vigente |
| Tokens de entrada | caracteres reales del prompt fijo (`_build_field_tree_prompts`), del JSON del tool y del texto de las páginas ÷ caracteres por token del modelo (calibrado). Página OCR: caracteres por página calibrados por tipo | ±37% mediano |
| Tokens de salida | tokens por llamada calibrados por modelo y tipo (mediana; el tope usa p90) | ±34% mediano — depende de cuántas filas trae cada documento |
| Caché | solo modelos Anthropic y solo con más de un chunk (así lo manda el pipeline); los de terceros no tienen caché | regla del pipeline |
| Lambda | segundos base + por página OCR + por llamada × memoria desplegada | aproximado; < 5% del total |
| Segmentación / upstream | $0 para un tipo declarado (se verifica corriendo `segment_pages` con Bedrock bloqueado) | exacto |

### Validación contra lo cobrado (`pnpm costos:validar`)

Toma cada registro de la tabla de auditoría cuyo archivo original existe en
`test-docs/` (mismo nombre y número de páginas), baja su resultado de S3 y
compara servicio por servicio. Dos cuidados para que la comparación sea justa:

- **Re-tarifa los tokens reales** con `pipeline/pricing.py` vigente: los
  registros anteriores al 2026-09-04 guardaron MiniMax/Kimi a tarifa de Haiku
  (`pricingVersion 2026-08-05`, `approximatePricing: true`), así que su $
  guardado está inflado ~4×. 23 de los 36 registros comparados estaban así.
- **Calibración leave-one-out**: cada archivo se evalúa con constantes calculadas
  sin sus propios registros.

Corrida 2026-09-14 (dev + prod, 36 registros, 17 archivos):

| | Método anterior | Estimado actual |
|---|---:|---:|
| Error mediano del total por documento | 72% | **23%** |
| Textract exacto | — | 36/36 |
| Llamadas a Bedrock iguales a las reales | — | 27/36 (las demás corrieron con un catálogo anterior) |

Límites conocidos, medidos: un documento escaneado de un tipo **sin historial**
de OCR puede salir muy alto (`invoices-flete.pdf`, 31 páginas escaneadas y
`multi_instance`, +160% sin su propia calibración), porque ni el tamaño del
texto OCR ni la partición en instancias se pueden saber sin pagar el OCR. Los
tokens de salida varían con el contenido (`example.pdf`: 9 590 reales contra
~2 200 típicos). El catálogo cambió casi a diario en agosto-septiembre, y el
prompt de una corrida vieja no se puede reconstruir: los resultados guardan la
huella del catálogo (`tracking.schema.catalogVersions`), no el texto.

**Fuentes de calibración, en orden de confianza**: la selección de páginas guarda
en cada resultado los caracteres exactos de las páginas enviadas y descartadas
(`pageSelection.savings.charsSent/charsSkipped`), así que en esos documentos el
texto OCR por página y la densidad de tokens se miden sin suponer nada (pedimento
escaneado: 2 628 caracteres/página). Derivar el OCR de los tokens es solo
respaldo, y se descarta si da menos de 200 o más de 8 000 caracteres por página
(señal de un prompt de otra versión).

`scripts/cost_calibration.json` guarda las constantes y el error medido (el plan
lo muestra como "error típico"). Recalibra cuando cambie el modelo desplegado,
tras cambios grandes de catálogo o si el análisis de una corrida avisa que el
costo real se desvió más de 50%:

```bash
pnpm costos:validar                  # mide el error, no escribe nada
pnpm costos:validar -- --write       # además regenera scripts/cost_calibration.json
pnpm costos:validar -- -e prod --since 2026-09-10
```

Además, cada corrida del E2E guarda el estimado de cada archivo y el análisis lo
compara contra la auditoría del mismo documento (mismo catálogo, mismo momento):
esa es la validación que se acumula sola con el uso.

## 10. Etapa de autenticidad física — ESTIMADO, aún sin medir (2026-09-18)

> A diferencia del resto de este documento, **estas cifras no están medidas**:
> la etapa se agregó el 2026-09-18 y todavía no hay cartas procesadas en un stack
> desplegado. Son la estimación de `scripts/_cost_estimate.py` con tokens de
> visión por default. Reemplazar por lo medido en cuanto `pnpm costos` tenga
> registros con el servicio `BedrockAuthenticity`: sus tokens reales se fijan **a
> mano** en `scripts/cost_calibration.json` → `authenticity`
> (`locateInputTokens`, `inspectInputTokens`, `referenceImageTokens`…).
> `pnpm costos:validar -- --write` todavía **no** recalibra esta etapa.

**Primeras mediciones reales (2026-09-21, corridas locales con Haiku 4.5 en
visión) — sustituyen a la estimación de abajo donde difieren:**

| Carta | Llamadas de visión | Tokens entrada / salida | Visión | Total carta |
|---|---|---|---|---|
| Escaneo genuino, sin referencias (6 cartas) | 2 (clasificar ≤12 regiones + examinar 3 recortes) | 6,100–7,200 / 515–580 | **~$0.0090–0.0101** | no medido (faltó OCR + extracción en esas corridas) |
| Carta digital de Word, 4 imágenes insertadas, 6 referencias enviadas | 2 | 7,550 / 579 | **$0.0115** | **$0.0161** (extracción $0.0046; sin OCR: texto nativo) |

La primera llamada resultó más cara que lo estimado (3,300 tokens) porque ahora
lleva, además de la página, los recortes de las regiones candidatas: el modelo
ubicaba mal y se cambió a «el código propone regiones, el modelo las clasifica»
(ver `AUTENTICIDAD_Y_REFERENCIAS.md` §2). `pnpm procesar` ya incluye esta línea en
su total — antes la omitía y una `CARTA_NOM35` salía a menos de la mitad.

Solo la pagan los tipos que declaran el bloque `authenticity` (hoy:
`CARTA_NOM35`). De sus cuatro capas, **dos son gratis** (estructura del PDF y
pixeles corren en la Lambda, sin servicios externos) y una cuesta centavos de
DynamoDB (memoria). Lo que se factura es la **visión**: Converse cobra cada imagen
como tokens de ENTRADA (~ancho×alto/750 en los modelos Anthropic).

| Llamada | Qué manda | Tokens entrada (default) | Salida |
|---|---|---|---|
| `locate` | la página a 1568 px de lado largo + esquema | ~3,300 | ~150 |
| `inspect` | recortes de firma/sello/logo a resolución nativa | ~2,200 | ~350 |
| + por referencia registrada enviada | un recorte más (máx. `max_references_per_kind` × elementos) | ~320 c/u | — |

Por carta de una página, con Haiku 4.5 en visión y el extractor desplegado:

| Carta | Textract | Extracción | Autenticidad | Lambda | **Total** |
|---|---|---|---|---|---|
| Captura genuina (escaneada), sin referencias | $0.0015 | ~$0.0027 | ~$0.0088 (2 llamadas) | ~$0.0010 | **~$0.014** |
| Ídem, con 9 referencias enviadas (tope) | $0.0015 | ~$0.0027 | ~$0.0120 | ~$0.0010 | **~$0.019** |
| PDF digital (Word con firma pegada) | $0 (texto nativo) | ~$0.0025 | ~$0.0045 (**1** llamada) | ~$0.0009 | **~$0.008** |

La tercera fila es el guardrail de costo de la etapa: cuando la capa gratis ya
probó que el documento es digital, **el examen de recortes se omite**
(`vision.inspect_when_digital: false`) — preguntarle a un modelo si la firma
«parece» pegada no agrega información, solo costo. Se sigue pagando `locate`
porque hace falta saber QUÉ imagen incrustada está bajo la firma para poder
probar su reutilización en otra carta.

Guardrails por tipo, todos en el catálogo y todos fail-open (exceder uno analiza
menos y lo dice en `reasons`, nunca recorta en silencio): `pages.max_pages`,
`vision.max_references_per_kind`, `vision.inspect_crops`, `vision.enabled`.
Interruptor global: `AUTHENTICITY_ENABLED` (apagado manda a revisión, no aprueba).

**Capa 0 (integridad) y ELA: $0 (2026-09-24).** La capa `integrity`
(`pipeline/pdf_integrity.py`: guardados incrementales, fechas, editor en los
metadatos, anotaciones, formularios, texto sobre el escaneo, firma digital,
EXIF) y el análisis de nivel de error de la capa de pixeles no facturan nada en
Bedrock ni Textract: corren en la Lambda, en milisegundos, sobre bytes que ya
están en memoria. Y como las capas corren **bajo demanda** (solo si un campo del
tipo lee una señal suya), un tipo que declara únicamente
`"$presets": ["INTEGRIDAD_PDF"]` nunca rasteriza ni llama a un modelo: su costo
de autenticidad es exactamente cero. Dos palancas más en la capa de visión, sin
medir todavía: `vision.trigger: "on_suspicion"` omite la llamada `locate` cuando
ninguna capa gratis levantó un motivo no informativo (un archivo limpio no paga
modelo), y `vision.page_review: true` agrega UNA llamada más del tamaño de
`locate` — la página a 1568 px, **~3,300 tokens de entrada ESTIMADOS**, en la
misma línea `BedrockAuthenticity`.

En la auditoría (`pnpm costos`, §7) aparece como servicio propio,
**`BedrockAuthenticity`** — mismo criterio que `BedrockPageLocator`: una llamada
real y opcional nunca se esconde dentro de `BedrockExtraction`. La Lambda pasó de
1024 a **1536 MB** por los rasters de página (afecta el `LambdaCompute` de todos
los tipos en proporción a su duración: +50 % sobre una partida que es ~5-8 % del
costo por documento).

### `BedrockEmbeddings` — embeddings de referencias (2026-10-06)

Línea propia en la auditoría, separada de `BedrockAuthenticity`: no son tokens
de un modelo generativo sino una llamada `InvokeModel` a **Amazon Titan
Multimodal Embeddings G1** (`amazon.titan-embed-image-v1`), que se cobra **por
imagen**: **$0.00006** cada una (`pipeline/pricing.py`).

Cuándo se paga:

| Momento | Imágenes | Quién |
|---|---|---|
| Alta de una referencia (`POST /references/…/confirm` o `pnpm refs:register`) | 1 por referencia | References Lambda / CLI. **No** queda en la tabla de costos por documento: no hay documento |
| Documento de un proveedor con **más** referencias de un elemento que `vision.max_references_per_kind` (default 3) | 1 por elemento en esa situación | Enrichment Lambda: embebe el recorte del documento para elegir las más parecidas. Con 3 sellos o menos se mandan todos y **no se paga nada** |
| Documento **sin** `metadata.proveedor`, en un tipo con `authenticity.references.search.enabled` | 1 por elemento buscado | Enrichment Lambda: busca en el índice vectorial entre las referencias de todas las entidades del `scope` |
| `pnpm refs:reindex` (solo lo que no tenga vector del modelo actual, o todo con recálculo) / `pnpm refs:search` / `pnpm refs:eval` | 1 por recorte | Operador |

El embedding del recorte de un elemento se calcula **a lo más una vez por
documento**, así que el peor caso de una `CARTA_NOM35` (firma + sello + logo)
son 3 imágenes = **$0.00018** — dos órdenes de magnitud por debajo de la visión.

**Medido (2026-10-06):** embeber los 136 recortes de `test-docs/nom35/samples`
con `pnpm refs:eval` costó **~$0.008** (136 × $0.00006 = $0.0082).

El índice vectorial (S3 Vectors, `document-validation-references[-dev]`) guarda
un vector de 1024 dimensiones por referencia activa; su almacenamiento y sus
consultas no tienen línea en la auditoría por documento y **no están medidos**.

## 11. Verificación de tipo (`type_check`) — medido 2026-10-08

"¿El archivo ES el documento que se declaró?" se pregunta **dentro de la misma
llamada de extracción** (una llave `_DOCUMENTO` en el tool schema, ~30 tokens
de salida y ~120 de entrada por la regla del prompt): **no hay línea nueva en
la auditoría de costos** porque no hay llamada nueva — queda dentro de
`BedrockExtraction`. Sobre un documento típico (~900 tokens de salida con
MiniMax) es < 4 % de la salida, dentro del ruido entre dos corridas del mismo
archivo (776 vs 946 tokens en la constancia de abajo).

Dataset: los 10 archivos reales de la casilla "Estado de cuenta" del portal
(`ESTADO_CUENTA_PORTAL`, MiniMax M2.5, `pnpm procesar`): 9 estados de cuenta
legítimos (carátulas mexicanas, cartas de bancos extranjeros, wire
instructions) + 1 constancia de situación fiscal subida por error.

| | Resultado |
|---|---|
| Costo | $0.1145 total, **$0.0115/doc** (incluye visión de integridad y OCR; el estado de cuenta ya costaba eso antes) |
| Tiempo | 21 s/doc (12-52 s; MiniMax, sin cambio apreciable) |
| Legítimos | **9/9 `documentMatchesType: true`** — ningún falso positivo, incluidas las variantes extranjeras |
| Constancia | `false`, `rejectionConfidence 1.0`, `detectedDocument: "Constancia de Situación Fiscal del SAT"`, `confidence 0.05` |

Lo que enseñó la segunda corrida de la constancia: el modelo llenó `TITULAR`
con la razón social del contribuyente y `PAIS` con `MEX` (2 de 3 obligatorios,
confianza 0.95), así que la guardia "la mayoría de los obligatorios llenos ⇒
revisar en vez de rechazar" (0.5) dejó pasar el archivo ajeno a
`REQUIERE_REVISION`. El default quedó en **1.0 (todos)**: un documento
legítimo bien leído tiene todos sus obligatorios, y uno al que le falta alguno
iba a revisión de todos modos.

## Tráfico de pruebas reales

`pnpm test:real` genera costos auténticos y persiste documentos igual que una
integración externa. Sus uploads llevan `metadata.test-run-id` y
`metadata.test-suite=real-e2e`; no se altera `TotalCostUsd` ni se inventa un
descuento. `pnpm reporte` conserva esos cargos y abre por defecto con **todo**
el tráfico del periodo para que el total inicial coincida con todos los
documentos procesados. El filtro de tráfico permite aislar operativos, pruebas
o ambos grupos. Las corridas anteriores sin etiqueta se consideran operativas
porque no existe evidencia segura para reclasificarlas.
