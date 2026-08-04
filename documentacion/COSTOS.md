# Costos de procesamiento — Documents Validator

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

| | 2026-07-28 (antes) | 2026-08-04 (ahora) |
| --- | --- | --- |
| Input tokens/doc | ~20,631 | ~6,483–6,742 |
| Output tokens/doc | ~6,454 | ~883–1,054 |
| Costo/doc | $0.0582 | $0.0123 |
| **Costo/página** | **$0.0194** | **$0.0041** |

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

| Tipo | docs | páginas | costo LLM | $/página |
| --- | ---: | ---: | ---: | ---: |
| PEDIMENTO_ADUANAL | 46 | 511 | $1.2947 | $0.0025 |
| PEDIMENTO_ADUANAL_V1 | 1 | 2 | $0.0714 | $0.0357 |
| CONSTANCIA_SITUACION_FISCAL | 4 | 8 | $0.0698 | $0.0087 |
| FACTURA_HONORARIOS | 1 | 1 | $0.0138 | $0.0138 |
| OTRO / PERMISO_CERTIFICADO_SANITARIO / CFDI_GASTO_TERCERO / FACTURA_LOGISTICA / FACTURA_AGENCIA_ADUANAL / FACTURA_HONORARIOS_XML | 48 | 75 | $0.0000 | $0.0000 |
| **TOTAL** | **100** | **598** | **$1.4498** | **$0.0024** |

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

| | Bottom-up (S3, por-documento) | Cost Explorer (dólares reales facturados) |
| --- | ---: | ---: |
| Bedrock Haiku 4.5 | $1.4498 | $1.9237 |
| Textract Sync | $0.7620 (508 pág) | $0.9795 (653 pág) |
| **Total** | **$2.2118** | **$2.9032** |
| **$/página** (÷ 598 pág de S3) | **$0.0037** | **$0.0049** |

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

### Las tres unidades, de lo medido

| Unidad                                                   | Costo (solo IA: Bedrock + Textract)      |
| -------------------------------------------------------- | ---------------------------------------- |
| **Por página**                                           | $0.010–$0.019 (blended real **$0.0144**) |
| **Por documento** (con el promedio real de 6.4 pág/doc)  | 6.4 × $0.0144 ≈ **$0.092/doc**           |
| **Por documento típico de 3 páginas** (mediana, sin OCR) | **$0.058/doc**                           |

### Piso fijo mensual (no depende de cuántos documentos proceses)

| Ambiente | Piso fijo/mes   | De qué sale                                                                                                       |
| -------- | --------------- | ----------------------------------------------------------------------------------------------------------------- |
| Dev      | **~$7.24/mes**  | Infraestructura de soporte (sección 4: $3.38 medidos en 14 días → prorateado a 30)                                |
| Prod     | **~$13.24/mes** | Lo mismo de dev **+ $6/mes de WAFv2** (Web ACL $5 + 1 regla $1) — sin contar el managed rule group, no confirmado |

### Total a pagar por mes = piso fijo + (documentos/mes × $0.092/doc)

Ejemplos con distintos volúmenes (ajusta `documentos/mes` a tu caso real —
esto es aritmética sobre los números medidos, no un dato nuevo):

| Documentos/mes | Páginas/mes (× 6.4) | IA (Bedrock+Textract) | Total dev (+ $7.24 piso) | Total prod (+ $13.24 piso) |
| -------------- | ------------------- | --------------------- | ------------------------ | -------------------------- |
| 50             | 320                 | $4.61                 | **$11.85**               | **$17.85**                 |
| 200            | 1,280               | $18.43                | **$25.67**               | **$31.67**                 |
| 1,000          | 6,400               | $92.16                | **$99.40**               | **$105.40**                |
| 5,000          | 32,000              | $460.80               | **$468.04**              | **$474.04**                |

A bajo volumen el piso fijo (KMS + WAF) pesa proporcionalmente más; a partir
de ~1,000 documentos/mes el costo se vuelve casi puramente variable (~$0.092/doc).

### Puntos clave

- Costo real de IA: **$0.0144/página**, **$0.092/documento** (promedio real 6.4 pág/doc).
- Piso fijo: **$7.24/mes en dev**, **$13.24/mes en prod** — no escala con volumen.
- KMS es el rubro de infra a vigilar (dominó el piso fijo de dev).
- Si cambiaras a Sonnet 4.5/Opus, el componente variable ($/página, $/doc) sube ~3x/~5x — el piso fijo no cambia.
