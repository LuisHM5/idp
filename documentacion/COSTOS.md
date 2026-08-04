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

Cost Explorer registró ~$5.94/mes al prorratear la infraestructura de la
**cuenta completa**, pero no es correcto asignar ese importe como piso fijo
del proyecto: la mayor parte era almacenamiento de otras claves KMS de la
cuenta. El stack desplegado crea dos claves KMS propias (la compartida del
pipeline y la propia del bucket), a $1/mes cada una. Lambda, DynamoDB, SQS,
S3, CloudWatch, Step Functions y API Gateway son principalmente consumo
variable al volumen de este sistema.

### Piso fijo total y costo mensual realista en prod

| Ambiente | Piso fijo atribuible/mes |
| --- | ---: |
| Dev | ~$2.00 (2 claves KMS) |
| Prod | ~$11.00 (2 claves KMS + $9 de WAFv2) |
| Prod + dev desplegados simultáneamente | ~$13.00 |

### Proyección acordada: 2,430 documentos / 7,290 páginas al mes

Supuesto: pedimentos de tres páginas procesados con el tipo declarado, por
lo que se omiten la clasificación visual, la extracción redundante del
blueprint y la segmentación por modelo. El costo de Bedrock + Textract usa
las ejecuciones más recientes del mismo pedimento de tres páginas
(~$0.01365/documento, incluyendo una página OCR). El resto del runtime se
prorratea conservadoramente desde la ventana real de 58 ejecuciones.

**AWS Business Support+ se excluye expresamente**: es un cargo compartido de
la cuenta y no forma parte del costo por página acordado para este proyecto.

| Componente | Costo mensual | Costo/página |
| --- | ---: | ---: |
| Bedrock + Textract | $33.17 | $0.00455 |
| Lambda, S3, SQS, CloudWatch, DynamoDB, Step Functions y API Gateway | ~$3.50 | ~$0.00048 |
| Piso fijo de prod: 2 claves KMS + WAFv2 | $11.00 | $0.00151 |
| **Subtotal técnico antes de IVA** | **$47.67** | **$0.00654** |
| IVA 16% | $7.63 | $0.00105 |
| **Total esperado con IVA** | **$55.30** | **$0.0076** |
| **Presupuesto conservador: IVA + 15% de contingencia sobre el consumo variable** | **$61.68** | **$0.0085** |

El valor recomendado para presupuesto es **$0.0085 USD/página**, equivalente
a **$61.68 USD/mes** para 7,290 páginas. La diferencia contra el esperado de
$0.0076/página cubre variación de tokens, reintentos y errores de proceso.
Si dev permanece desplegado junto con prod, sumar aproximadamente $2/mes por
sus dos claves KMS, más su consumo de pruebas.

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

> **Corte histórico, no usar como piso fijo actual.** Cost Explorer mezclaba
> las claves KMS de toda la cuenta: el cargo de KMS indicado abajo no era
> atribuible íntegramente al proyecto. La corrección vigente está en la
> sección 0.1: ~$2/mes de KMS para dev y ~$11/mes de piso fijo para prod.

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

> Actualizado 2026-08-04. Esta sección reemplaza las proyecciones anteriores
> para el volumen acordado. Los casos históricos de las secciones 1–5 se
> conservan para explicar cómo cambió el costo con la optimización del DSL.

| Unidad | Costo vigente |
| --- | ---: |
| Bedrock + Textract por pedimento de 3 páginas | ~$0.01365/documento |
| Subtotal técnico antes de IVA | $47.67/mes — $0.00654/página |
| Total esperado con IVA | $55.30/mes — $0.0076/página |
| **Presupuesto recomendado con IVA y 15% de contingencia variable** | **$61.68/mes — $0.0085/página** |

### COSTO RECOMENDADO POR PÁGINA EN PROD

# **$0.0085 USD / página**

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
por encima del presupuesto técnico recomendado de $0.0085/página; no es el
costo esperado y no atribuye AWS Business Support+ al proyecto.
