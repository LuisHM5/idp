te# Costos de procesamiento — Documents Validator

> Generado 2026-07-28 a partir de datos reales: 14 ejecuciones guardadas en
> `s3://<bucket-dev>/results/*.json` (27-28 jul 2026), cruzadas contra AWS
> Cost Explorer (cuenta 886031931562, us-east-1) y precios obtenidos en vivo
> con la AWS Pricing API. No son estimaciones de documentación — son
> `usage` real de Bedrock (`extractionMeta.usage` en cada resultado) y
> `pages.totalPages` / `pages.ocrPages` real por documento.

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

| Tipo | n | costo/doc (avg) | páginas | $/página |
| --- | ---: | ---: | ---: | ---: |
| PEDIMENTO_ADUANAL (mezcla 3pág nativo + 27/50pág escaneado) | 46 | $0.0421 | 511 | $0.0038 |
| PEDIMENTO_ADUANAL_V1 | 1 | $0.0714 | 2 | $0.0357 |
| CONSTANCIA_SITUACION_FISCAL | 4 | $0.0175 | 8 | $0.0087 |
| FACTURA_HONORARIOS | 1 | $0.0138 | 1 | $0.0138 |
| **INE** (imagen, 100% OCR) | 3 | **$0.0080** | 3 | $0.0080 |
| OTRO / CFDI_GASTO_TERCERO / FACTURA_LOGISTICA / PERMISO_CERTIFICADO_SANITARIO / FACTURA_AGENCIA_ADUANAL / FACTURA_HONORARIOS_XML | 48 | $0.0000 | 75 | $0.0000 |

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

| Extra sólo-prod | Precio real (verificado con `aws-pricing` MCP, us-east-1) |
| --- | --- |
| WAFv2 Web ACL | $5.00/mes |
| Regla rate-based (`RateLimitPerIp`) | $1.00/mes |
| 3× AWS Managed Rule Group (`CommonRuleSet`, `KnownBadInputsRuleSet`, `AmazonIpReputationList`) | $1.00/mes **cada una** (se facturan como "Rule", no hay SKU separado para managed groups gratuitos de AWS — la sección 5 original sólo contaba 1 regla y subestimaba esto) |
| WAF request processing | $0.60 / millón de requests (Tier0) — centavos al volumen actual |
| API Gateway Usage Plan + API Key | $0 fijo (sólo se cobra el request, misma tarifa API Gateway de siempre) |
| Dominio custom `idp.alsuper.com` + ACM (BYO cert) | $0 (CNAME externo, cert importado) |

**Piso fijo WAF corregido: $9.00/mes** (antes se documentó ~$6/mes —
faltaban 3 de las 4 reglas).

Piso de infraestructura base (KMS + Lambda + DynamoDB + SQS + S3 +
CloudWatch + Step Functions + API Gateway, igual en dev y prod)
recalculado en vivo con Cost Explorer sobre los últimos 15 días
(21-jul → 05-ago): **$2.971 / 15 días → $0.198/día → ~$5.94/mes**
prorateado (antes $7.24/mes; bajó porque KMS tuvo menos actividad en
esta ventana — sigue siendo el rubro a vigilar).

### Piso fijo total y costo mensual realista en prod

| | Piso fijo/mes |
| --- | ---: |
| Infra base (dev y prod) | $5.94 |
| + WAFv2 (sólo prod) | $9.00 |
| **Total piso fijo PROD** | **$14.94** |

**Total mensual prod = $14.94 + (documentos/mes × costo/doc según tipo,
tabla de arriba).** Ejemplo con una mezcla típica (pedimentos, el tipo
dominante hoy, ~$0.04/doc si son escaneados o ~$0.012/doc si son
nativos):

| Documentos/mes | IA (a $0.012–$0.04/doc) | Total dev (+$5.94) | Total prod (+$14.94) |
| -------------- | ------------------------ | ------------------- | ---------------------- |
| 50 | $0.60–$2.00 | $6.54–$7.94 | $15.54–$16.94 |
| 200 | $2.40–$8.00 | $8.34–$13.94 | $17.34–$22.94 |
| 1,000 | $12.00–$40.00 | $17.94–$45.94 | $26.94–$54.94 |

A los volúmenes actuales de prueba, **el piso fijo de prod ($14.94) domina
el costo total** — el WAF + infraestructura pesan más que el procesamiento
de documentos en sí hasta llegar a varios cientos de documentos/mes. Esto
es distinto de dev, donde no hay WAF y el piso es ~$9/mes menor.

### $/página TOTAL en prod (variable + piso fijo amortizado)

Las secciones anteriores dan el costo variable por página ($0.0037–$0.0049)
y el piso fijo por mes ($14.94) por separado. Para un solo número de
"$/página real en prod" hace falta un volumen — el piso fijo no es por
página, es por tiempo, así que se reparte distinto según cuánto proceses.

**A tu ritmo real observado** (601 páginas en los 7 días de la ventana
29-jul→04-ago = ~86 páginas/día ≈ 2,576 páginas/mes si se mantiene):

- Piso fijo de esos 7 días: $14.94/mes ÷ 30 × 7 = $3.486
- Variable real (bottom-up, 601 páginas): $2.2357
- Total: $5.7217 / 601 páginas = **$0.0095/página** (con el ajuste de
  Cost Explorer de la sección 0 en vez del bottom-up: **$0.0107/página**)

**El piso fijo es ~61% del costo por página a este ritmo** — pesa más que
el procesamiento de IA en sí. Sensibilidad por volumen:

| Páginas/mes | Fijo/página | Variable/página | **Total/página** |
| ----------- | -----------: | ---------------: | -----------------: |
| 320 (~50 docs, bajo volumen) | $0.0467 | $0.0037 | **$0.0504** |
| **2,576 (ritmo real actual)** | **$0.0058** | **$0.0037** | **$0.0095** |
| 6,400 (~1,000 docs) | $0.0023 | $0.0037 | **$0.0061** |
| 32,000 (~5,000 docs) | $0.0005 | $0.0037 | **$0.0042** |

A partir de unos pocos miles de páginas/mes el costo converge al variable
puro porque el piso fijo se vuelve ruido — igual que en dev (sección 6),
sólo que en prod el piso de partida es $9/mes más alto por el WAF.

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

## 4. Infraestructura de soporte (medida, mismo periodo, dev)

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

## 6. Resumen — desglose x página / x documento / x mes

> Actualizado 2026-08-04 (secciones 0/0.1) — reemplaza los números de la
> primera versión (2026-07-28), que quedan como referencia histórica del
> "antes" en el Hallazgo 1.

### Las tres unidades, de lo medido ahora

| Unidad | Costo (Bedrock + Textract) |
| --- | --- |
| **Por página, blended real actual** | **$0.0037–$0.0049** (antes $0.0144 — ver Hallazgo 1; inflado a la baja por el Hallazgo 2 mientras dura la migración de catálogo) |
| **Por página, pedimento nativo puro** (el tipo que domina el volumen real) | **$0.0041** |
| Por documento — nativo 3 pág, sin OCR | **$0.0123** |
| Por documento — escaneado 27 pág, 100% OCR | **$0.0988** |
| Por documento — INE, 1 pág imagen, 100% OCR | **$0.0080** |
| Por documento — tipos sin catálogo activo hoy (OTRO, CFDI_GASTO_TERCERO, etc.) | **$0.0000** (temporal) |

### Piso fijo mensual (no depende de cuántos documentos proceses)

| Ambiente | Piso fijo/mes | De qué sale |
| --- | --- | --- |
| Dev | **~$5.94/mes** | Infraestructura de soporte, recalculada en vivo sobre 15 días reales (21-jul→05-ago) |
| Prod | **~$14.94/mes** | Lo mismo de dev **+ $9/mes de WAFv2** (Web ACL $5 + **4** reglas × $1 — 1 rate-based + **3 managed rule groups**, que la versión anterior de este doc no contaba) |

### $/página TOTAL en prod (variable + piso fijo amortizado) — el número final

El piso fijo no es por página, es por tiempo: se reparte distinto según
cuánto proceses. A tu ritmo real observado (~2,576 páginas/mes):
**$0.0095–$0.0107/página** (~61% de eso es el piso fijo, no el
procesamiento). Detalle completo en la sección 0.1.

| Páginas/mes | Total/página en prod |
| --- | ---: |
| 320 (bajo volumen) | $0.0504 |
| **2,576 (ritmo real actual)** | **$0.0095** |
| 6,400 | $0.0061 |
| 32,000 | $0.0042 |

### Puntos clave

- Costo real de IA **hoy**: **$0.0037–$0.0049/página** blended (antes
  $0.0144/página, caída real de ~3-4x — Hallazgo 1). El número puro del
  pedimento, que domina el volumen, es **$0.0041/página**.
- La mitad de los tipos clasificados hoy cuestan **$0 en LLM** porque su
  catálogo está archivado en `catalogs/save/` durante la migración del
  DSL v4 — es temporal, no una optimización permanente (Hallazgo 2).
- Piso fijo: **$5.94/mes en dev**, **$14.94/mes en prod** — no escala con
  volumen (corregido: el WAF de prod son 4 reglas = $9/mes, no $6/mes).
- **$/página TOTAL en prod, a tu ritmo real (~2,576 pág/mes): $0.0095–$0.0107**
  — el piso fijo pesa más que el procesamiento de IA hasta varios miles
  de páginas/mes.
- KMS sigue siendo el rubro de infra a vigilar (dominó el piso fijo de
  dev en ambas mediciones, jul y ago).
- Si cambiaras a Sonnet 4.5/Opus, el componente variable ($/página, $/doc)
  sube ~3x/~5x — el piso fijo no cambia.

---

### Desglose del costo total por página en prod

| Componente | $/página |
| --- | ---: |
| IA + Textract (Bedrock + OCR) | $0.0037–$0.0049 |
| Resto de infra (DynamoDB, S3, Lambda, API Gateway, KMS, SQS, CloudWatch, Step Functions) amortizado a tu ritmo | ~$0.0035 |
| WAF (solo prod) amortizado a tu ritmo | ~$0.0023 |
| **Total** | **$0.0095–$0.0107** |

## COSTO TOTAL REAL POR PÁGINA EN PROD

# **$0.0095 – $0.0107 / página**

*(a tu ritmo real de uso, ~2,576 páginas/mes — sección 0.1)*

Este es el único número que cuenta **todo**: Bedrock (IA) + Textract (OCR)
+ DynamoDB + S3 + Lambda + API Gateway + KMS + SQS + CloudWatch +
Step Functions + WAFv2 (piso fijo de prod). No es sólo IA — el $0.0037–
$0.0049/página de las secciones anteriores es nada más Bedrock+Textract;
el resto de la infraestructura (~61% del costo a este ritmo) se suma acá.

Baja si procesas más volumen al mes (el piso fijo se diluye — ver tabla de
sensibilidad arriba); sube si procesas menos. **$0.0095–$0.0107/página es
el número real a tu ritmo actual, no una proyección.**
