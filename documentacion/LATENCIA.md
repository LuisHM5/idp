# Latencia del pipeline: dónde se va el minuto

Análisis medido (no estimado) de por qué un documento tarda ~1 minuto desde que
el cliente hace `POST /documents` hasta que el resultado está disponible, con el
benchmark de modelos que decide la palanca principal.

**Fecha del análisis:** 2026-09-23 · cuenta `886031931562`, `us-east-1`,
stacks `DocumentValidationStack` (prod) y `DocumentValidationStack-Dev`.

**Actualización 2026-10-07:** benchmark de modelos baratos (§10) y mejoras
cuantificadas (§11). Al 2026-10-07 ninguna palanca de §8 está aplicada: el
modelo desplegado sigue siendo `minimax.minimax-m2.5`.

---

## 1. Veredicto en cinco líneas

1. **La arquitectura no es el cuello de botella.** S3 + SQS + Step Functions +
   DynamoDB suman **~10s** de los ~60: el resto es una sola llamada a Bedrock.
2. El **85-90% del tiempo** es la etapa `extraction` del enrichment Lambda, que
   para un documento de 1-2 páginas es **exactamente UNA llamada `Converse`
   no-streaming**. Su wall clock es `TTFT + tokens_salida / throughput`.
3. El modelo desplegado (`minimax.minimax-m2.5`) rinde **25-86 tokens/s
   efectivos** y es **muy inestable**: el mismo CURP de 1 página tardó 6.7s,
   12.0s y 23.9s en tres corridas idénticas.
4. Medido sobre los mismos documentos, **Haiku 4.5 es 3.4-4.2x más rápido que
   MiniMax** (CURP: 2.85s vs 11.98s · pedimento de 2 páginas: 8.79s vs 30.22s)
   **sin perder un solo campo** — y en el pedimento extrajo 3 campos correctos
   que MiniMax dejó vacíos. Cuesta +$0.004 a +$0.013 por documento.
5. Los ~10s de plomería también son recuperables casi sin riesgo: ventana de
   batching de SQS, dos pasos de Step Functions cuyo resultado se descarta, y
   el primer poll del cliente.

**Lo que agregó la ronda del 2026-10-07 (§10 y §11):**

6. **Ningún modelo barato sirve hoy como reemplazo.** Nova 2 Lite y GLM 4.7
   Flash son tan rápidos como Haiku y cuestan como MiniMax o menos, pero pierden
   los arreglos o devuelven la extracción vacía en pedimentos (§10.3).
7. **Haiku 4.5 sigue siendo el único rápido y estable** (2.2-8.2x más rápido que
   MiniMax en extracción, a 2.6-4.2x el costo), **pero no es el techo de
   calidad**: en `normal_pend.pdf` inventó los números de factura copiando el
   ejemplo del hint del catálogo, 3 de 3 corridas. MiniMax los extrajo bien.
8. **El envoltorio `{value, confidence}` por campo es el 18-47% del JSON de
   salida** (31-47% en documentos de 1-3 páginas). Es la mejora de formato más grande disponible y aplica a cualquier
   modelo (§11.1).
9. **Con un modelo rápido, el OCR pasa a ser la mitad del tiempo** en
   expedientes grandes: 15.3s de `pageLoad` contra 15.4s de extracción (§11.3).

---

## 2. Cómo se midió

| Fuente | Qué aportó |
|---|---|
| Tablas de tracking DynamoDB (dev 121 docs, prod 7) | `Tracking.Timings` por etapa y `ProcessingSeconds` reales |
| `document-validation-costs[-dev]` | tokens entrada/salida y páginas OCR por documento |
| Historial de Step Functions (`get-execution-history`) | duración exacta de cada estado del workflow |
| CloudWatch Logs del enrichment Lambda | `Init Duration` (cold start) y `Duration` reales |
| `aws lambda list-event-source-mappings` | ventana de batching de SQS |
| `pnpm bench:latencia` (§6) | comparación controlada entre modelos sobre documentos propios |

---

## 3. Anatomía del minuto

| Tramo | Tiempo medido | Evidencia |
|---|---|---|
| `POST /documents` → URL prefirmada | ~0.2s | Lambda Node 256 MB, `upload-lambda/index.mjs` |
| `PUT` del cliente a S3 | red del cliente | va directo a S3, no pasa por Lambda |
| S3 → SQS → consumidor | **0-5s** | `MaximumBatchingWindowInSeconds=5` en el event source mapping |
| `InitMetadataEntry` | 0.3s | historial SFN |
| `MultiModelClassificationStep` + update | **1.9s** | historial SFN (prod, 2026-09-14) |
| `MultiModelProcessingStep` + update | **1.6s** | historial SFN |
| **`EnrichmentStep`** | **17-220s** | historial SFN — 95%+ del total |
| `CopyToProcessed` + `DeleteFromRaw` + EventBridge | ~1.2s | historial SFN |
| Primer poll del cliente | **5s fijos** | `documentacion/api-docs/API_USO.md:313-323` |

Cold start del enrichment Lambda: **317-386 ms** (`Init Duration` en CloudWatch).
No es un problema — los imports pesados ya están diferidos (`handler.py:575`,
`:585`, `:596`).

### Los dos pasos upstream son trabajo descartado

`handler.py:750-755` enruta a `_run_expediente()` y **retorna antes** de leer
`processingResult`: para todo tipo field-centric, la extracción upstream nunca
se usa. De la clasificación solo sobrevive `fallback_classification`
(`handler.py:669`), que se usa únicamente si el archivo no produjo documentos —
el segmenter re-clasifica de todas formas (`handler.py:548-556`).

Los dos pasos ya se auto-bypassean del lado del modelo
(`model-invoke-lambda/index.py:281-294` y `:305-318`), así que esos 3.5s son
overhead de Lambda + 2 `UpdateItem` a DynamoDB, no llamadas a Bedrock.
**Excepción:** un upload SIN tipo declarado (`raw/{uuid}/file`) sí paga una
llamada de visión real cuyo resultado el segmenter descarta.

---

## 4. Dentro del enrichment Lambda

De `tracking.timings` de documentos reales (ambiente dev, ms → s):

| Documento | total | pageLoad | extraction | tokens salida | tok/s efectivos |
|---|---:|---:|---:|---:|---:|
| INE (1 pág) | 17.9s | 0.9s | **17.0s** | 574 | 34 |
| CURP (1 pág) | 28.3s | 0.6s | **27.7s** | 723 | 26 |
| FACTURA_FLETE (1 pág) | 34.1s | 2.9s | **31.2s** | 3 256 | 104 |
| PEDIMENTO_ERP (31 pág escaneadas) | 38.3s | **16.0s** | 22.3s | 1 837 | 82 |
| PEDIMENTO_ERP (113 pág) | 72.3s | 13.2s | **59.1s** | 3 768 | 64 |
| PEDIMENTO_ERP (139 pág) | 196.1s | 17.8s | **178.3s** | 13 833 | 78 |

Las etapas son **estrictamente secuenciales** (`expediente.py:448-598`):
`pageLoad` termina por completo —todas las páginas, todo el OCR— antes de que
arranque la segmentación, y la autenticidad corre después de la barrera de
extracción aunque solo necesite los campos de su propio documento.

### Por qué la extracción tarda tanto

Para un documento de 1 página con tipo declarado hay **exactamente 1 llamada a
Bedrock**:

| Etapa | Llamadas | Por qué |
|---|---:|---|
| Segmentación | 0 | tipo declarado → segmentos determinísticos (`segmenter.py:1020-1029`) |
| Localizador de páginas | 0 | solo si el tipo declara `page_selection` y sus marcadores fallan |
| Extracción | **1** | 1 chunk (`_MAX_CHUNK_CHARS = 28_000`, `text_extractor.py:36`) × 1 partición |
| Autenticidad | 0 | solo tipos con bloque `authenticity` (hoy `CARTA_NOM35`) |

Es decir: **no queda paralelismo por explotar en el caso común**. `CHUNK_WORKERS=4`
no sirve cuando hay una sola llamada, y `ConverseStream` tampoco ayudaría porque
el JSON del tool call debe estar completo antes de parsearse. Los únicos dos
factores son **tokens de salida** y **throughput del modelo**.

Dos agravantes concretos:

- **Tokens de razonamiento.** 574 tokens de salida para un INE cuyo JSON real
  son ~150. No hay `additionalModelRequestFields` en ningún punto del repo, así
  que hoy el reasoning de MiniMax **no se puede bajar ni apagar**.
- **El caso común nunca cachea.** `use_cache = len(chunks) > 1`
  (`text_extractor.py:775`): el documento de 1 página reenvía todo el árbol de
  campos sin `cachePoint` en cada llamada.

---

## 5. Escaneados: OCR de páginas que no se usan

`pdf_pages.load_pages` (`:165-194`) hace OCR de **todas** las páginas escaneadas
antes de que `page_selector` decida cuáles importan — una llamada sync a
Textract por página (`pdf_pages.py:144`), con `TEXTRACT_WORKERS=8`. Ya está
documentado en `document-validation-stack.ts:373-376`: *"a 31-page scan pays 31
Textract calls even when 3 pages end up going to the model"*.

Es el origen de los 13-18s de `pageLoad` en los pedimentos escaneados.

---

## 6. Benchmark de modelos

Script nuevo: `scripts/bench-latencia.py` (`pnpm bench:latencia`). Corre el
pipeline **real en local** (`scripts/procesar-docs.py` con `--model`) una vez por
documento × modelo × repetición, y compara segundos por etapa, tokens, costo con
las tarifas de `pipeline/pricing.py`, y **si los campos extraídos cambian**
respecto al modelo base.

### 6.1 CURP — 1 página, texto nativo, sin OCR (3 repeticiones)

| Modelo | mediana | min-max | tok salida | tok/s | $/doc | campos ok | vs base |
|---|---:|---|---:|---:|---:|---|---|
| `minimax.minimax-m2.5` (actual) | **11.98s** | 6.67-23.91 | 590 | 50.9 | $0.0013 | 13/13 | — |
| `us.anthropic.claude-haiku-4-5-...` | **2.85s** | 2.81-2.88 | 320 | 113.9 | $0.0050 | 13/13 | **0/13 distintos** |
| `moonshotai.kimi-k2.5` | 6.52s | 5.57-6.61 | 188 | 28.6 | $0.0018 | 13/13 | 3/13 distintos |

Lo importante no es solo la mediana:

- **Haiku 4.5 es 4.2x más rápido y extrae exactamente lo mismo** (0 de 13 campos
  cambian). Cuesta +$0.0037 por documento.
- **Haiku es estable** (2.81-2.88s, ±1%); **MiniMax varía 3.6x entre corridas
  idénticas** (6.67-23.91s). Esa varianza es la que hace que el p95 del sistema
  se dispare, no la mediana.
- Kimi K2.5 es más barato que Haiku y 1.8x más rápido que MiniMax, pero **cambió
  3 campos** (`ESTATUS_CURP`, `FECHA_EMISION`, `TIPO_CERTIFICACION`) — hay que
  revisar cuáles están bien antes de considerarlo.

### 6.2 PEDIMENTO_ERP — 2 páginas, texto nativo (3 repeticiones)

| Modelo | mediana | min-max | tok salida | tok/s | $/doc | campos ok | vs base |
|---|---:|---|---:|---:|---:|---|---|
| `minimax.minimax-m2.5` (actual) | **30.22s** | 18.07-58.50 | 1 586 | 52.9 | $0.0042 | 61/61 | — |
| `us.anthropic.claude-haiku-4-5-...` | **8.79s** | 8.74-8.81 | 1 220 | 139.7 | $0.0170 | 61/61 | 3/61 distintos |
| `moonshotai.kimi-k2.5` | 17.50s | 6.95-18.14 | 681 | 42.7 | $0.0067 | 60/61 | 0/61 distintos |

Se repite el patrón del CURP, más marcado: **Haiku es 3.4x más rápido y otra vez
estable** (8.74-8.81s, ±0.4%), mientras MiniMax se mueve 3.2x entre corridas
idénticas (18.07-58.50s).

Los "3 campos distintos" de Haiku **no son un error suyo: son campos que MiniMax
dejó vacíos y Haiku sí extrajo**, verificados contra el texto del documento:

| Campo | MiniMax | Haiku | En el documento |
|---|---|---|---|
| `Referencia` | vacío | `6TCALKTN` | sí — `CODIGO DE ACEPTACION: 6TCALKTN` |
| `CUADRO_LIQUIDACION.CONCEPTOS[0].TASA` | vacío | `330` | sí — renglón `PRV … 330` |
| `CUADRO_LIQUIDACION.CONCEPTOS[1].TASA` | vacío | `16` | sí — renglón `IVAPRV … 16` |

Es decir: en este documento el modelo más rápido también fue el más completo.

### 6.3 Histórico (gratis, `pnpm bench:latencia -- --historico`)

Corridas locales ya guardadas en `resultados/` — fechas y catálogos distintos,
así que no es un experimento controlado, pero apunta al mismo lado:

| Documento | MiniMax M2.5 | Kimi K2.5 | Haiku 4.5 |
|---|---:|---:|---:|
| Pedimento ERP 2 pág | 22.2s | 7.0s | 10.6s |
| Expediente 139 pág | **425.2s** | **46.0s** | — |
| Escaneado 31 pág | 46.6s | 35.6s | 38.6s |

El expediente de 139 páginas es el caso extremo: 425s contra 46s.

### 6.4 Cómo reproducirlo

```bash
pnpm bench:latencia -- --historico                       # gratis, lee resultados/
pnpm bench:latencia -- --doc test-docs/curp.pdf --tipo CURP --repeticiones 3
pnpm bench:latencia -- --docs test-docs/e2e-suite/PEDIMENTO_ERP --tipo PEDIMENTO_ERP --dry-run
```

`--dry-run` imprime la matriz y una cota de costo sin llamar a Bedrock;
`--max-costo-usd N` aborta si se pasa. El costo real de los benchmarks de §6.1 y
§6.2 fue **$0.108** (18 corridas), contra una cota estimada de $0.20.

**Antes de adoptar cualquier modelo:** "campos distintos" no es accuracy. Hay que
correr `pnpm test:e2e:suite` y el ground truth de
`tests/integration/expected_pedimentos.json`, y recalibrar el estimador de costos
(`pnpm costos:validar`, `documentacion/COSTOS.md` §9).

---

## 7. Hallazgos secundarios (no son de latencia, pero salieron del mismo análisis)

1. **El catálogo se recarga en CADA invocación.** `bin/app.ts:48` pone
   `forceReloadCatalog: true` en dev **y en prod** → `handler.py:724-726`
   invalida el caché siempre: re-parseo de 531 KB de `config/catalogs.json`
   (`document_catalog.py:642`) + un `Scan` a la tabla de catálogo (`:681`) +
   otro `Scan` a `FallbackModelsTable` (`model_fallback.py:66`).
2. **`PEDIMENTO_ERP` está a 441 caracteres de duplicar sus llamadas.** Su bloque
   de esquema mide 9 559 chars contra el umbral de split de 10 000
   (`text_extractor.py:61`): un hint más y las llamadas por chunk se duplican en
   silencio.
3. **JSON truncado aceptado sin aviso.** El split por overflow de `max_tokens`
   solo dispara con `len(pages) > 1` (`text_extractor.py:747`). Con MiniMax
   clampeado a 8 192 tokens de salida (`bedrock.py:347-350`), una página densa
   que agote el presupuesto devuelve JSON truncado que se parsea y se acepta.
4. **No existe notificación push.** El evento terminal de EventBridge lleva
   `documentId`/`classification` pero **no** el estado de validación
   (`base-document-processing.js:176-187`), así que aun escuchando EventBridge el
   cliente tiene que hacer `GET`. `GET /documents/{id}` ya responde `202` +
   `Retry-After: 5` (`query-lambda/index.mjs:620-650`), pero ese 5 es fijo.
5. **Escrituras finales en serie.** Tras el pipeline, `handler.py:689-701` hace
   S3 `put_object` + 3 escrituras a DynamoDB secuenciales, cada una construyendo
   su propio cliente boto3 (a diferencia de `bedrock.client()`, que sí cachea).

---

## 8. Palancas, ordenadas por (ahorro / riesgo)

| # | Palanca | Ahorro | Riesgo | Dónde |
|---|---|---|---|---|
| 1 | Modelo de extracción según §6 | **-71 a -76%** del tiempo de extracción (medido en 2 tipos) | medio: exige validar campos contra el ground truth | `BEDROCK_MODEL_ID`, o `model_ids` por tipo en el catálogo |
| 2 | `maximumBatchingWindowInSeconds = 0` | hasta -5s (≈-2.5s prom.) | bajo | escape hatch en `document-validation-stack.ts` (hardcodeado en `queued-s3-adapter.js:194-198`) |
| 3 | `Choice` que salta clasificación+procesamiento con tipo declarado | -3.5s | medio: hay que inyectar un `classificationResult` sintético | override de `createStandardProcessingWorkflow` en `multi-model-document-processing.ts` |
| 4 | Push (estado de validación en el evento / webhook por app) | -5s percibidos + el overshoot del backoff | bajo | `document-validation-stack.ts`, `AppsTable` |
| 5 | `additionalModelRequestFields` para bajar reasoning | proporcional a los tokens de salida | bajo | `pipeline/bedrock.py` |
| 6 | Caché de prompt también con 1 chunk | TTFT y tokens de entrada | bajo | `text_extractor.py:775` |
| 7 | `TEXTRACT_WORKERS` 8 → 12-16 | -5 a -10s en escaneados | bajo: techo de throttling | `document-validation-stack.ts:383` (ya es env var) |
| 8 | `page_selection` en más tipos | hasta -81% de páginas a extracción | bajo | catálogos, ver `ARQUITECTURA.md:130` |
| 9 | OCR en dos pasadas | el grueso de `pageLoad` | alto: rompe el fail-open | `pdf_pages.py` + `page_selector.py` |

El plan de implementación por fases está en
`~/.claude/plans/algo-que-no-me-dazzling-pelican.md`.

---

## 9. Velocidad de los modelos: qué medir, y los datos

### 9.1 Los parámetros que de verdad mueven la aguja

La latencia de una llamada se descompone así:

```
wall clock ≈ arranque + (tokens_de_salida / throughput)
             └─ TTFT + fase de razonamiento (si el modelo piensa)
```

| Métrica | Qué es | Por qué importa aquí | Cómo se mide en este repo |
|---|---|---|---|
| **TTFT** (*time to first token*) | del request al primer token | Bedrock **solo lo emite con streaming**; este pipeline usa `Converse` no-streaming (`bedrock.py:515`), así que hoy no existe para nosotros | requeriría migrar a `ConverseStream`; métrica CloudWatch `TimeToFirstToken` |
| **TTFAT** (*time to first **answer** token*) | del request al primer token **útil**, después de pensar | **es LA métrica de un modelo de razonamiento**: explica por qué MiniMax tarda 15-20s antes de escribir nada | publicado por Artificial Analysis (§9.2); localmente se ve como el "arranque" del ajuste de §9.3 |
| **Throughput de salida** (tok/s) | tokens generados por segundo | multiplica el tamaño del JSON extraído | columna `tok/s` de `pnpm bench:latencia` |
| **InvocationLatency** | del request al **último** token | es la métrica que SÍ aplica a este pipeline (no-streaming) | CloudWatch, namespace `AWS/Bedrock` |
| **Tokens de salida por documento** | verbosidad + tokens de razonamiento | un modelo que piensa 400 tokens de más paga esos 400 tokens de latencia **y** de costo | `usage.extraction.usage.outputTokens` en cada resultado |
| **Varianza entre corridas idénticas** (CV) | dispersión del mismo trabajo repetido | define el **p95** del sistema, que es lo que sufre el usuario — no la mediana | `--repeticiones N` en el benchmark |
| **Determinismo del tamaño de salida** | ¿emite los mismos tokens ante la misma entrada? | un conteo estable es señal de que no hay razonamiento variable de por medio | columna `tokensSalida` por repetición |
| **`max_tokens` del modelo** | techo de salida por llamada | MiniMax está clampeado a 8 192 (`bedrock.py:347-350`); si se agota, el JSON se trunca (§7.3) | `_KNOWN_MODEL_MAX_TOKENS` |
| **Ventana de contexto** | cuánto texto entra por llamada | decide cuántos chunks → cuántas llamadas (`_MAX_CHUNK_CHARS = 28_000`) | catálogo del modelo |
| **Cuotas TPM/RPM y throttling** | techo de concurrencia | limita `CHUNK_WORKERS`/`EXTRACT_WORKERS`; un throttle se paga como reintento con backoff de 2-16s (`bedrock.py:512-524`) | CloudWatch `EstimatedTPMQuotaUsage`, `InvocationThrottles` |
| **Soporte de prompt caching** | reutilizar el prefijo del prompt | baja TTFT y tokens de entrada; hoy solo se usa con >1 chunk (`text_extractor.py:775`) | `cost_calibration.json` → `supportsPromptCache` |

### 9.2 Datos publicados (Artificial Analysis, consulta del 2026-09-23)

| Modelo | Salida (tok/s) | TTFT publicado | $/1M in · out | Nota |
|---|---:|---:|---|---|
| **Claude 4.5 Haiku (sin razonamiento)** | 98.4 | **519 ms** | $1.00 · $5.00 | es el modo en que lo usa este pipeline |
| Claude 4.5 Haiku (con razonamiento) | 165.0 | **14 940 ms** | $1.00 · $5.00 | el mismo modelo pensando: 29x más lento en arrancar |
| **MiniMax-M2.5** (el desplegado) | **sin dato** | **sin dato** | $0.30 · $1.20 | Artificial Analysis no publica velocidad para este modelo |
| MiniMax-M3 | 152.4 | 797 ms | $0.30 · $1.20 | **mismo precio que M2.5** — candidato obvio a probar si está en Bedrock |
| **Kimi K2.5** | **sin dato** | **sin dato** | $0.60 · $2.75 | tampoco publica velocidad |
| Kimi K2.7 Code | 51.6 | 1 319 ms | $0.95 · $4.00 | referencia de la familia |

Para MiniMax-M2.5, la página de proveedores de Artificial Analysis sí mide por
proveedor (ninguno es Bedrock): **93.9-139.8 tok/s** en los más rápidos, y un
**tiempo al primer token de respuesta de 15.4-23.1s** — mientras el primer token
*crudo* llega en 0.42-0.56s. Esa brecha **es** la fase de razonamiento, y es
exactamente lo que se ve en las mediciones de §6.

**Dos lecturas incómodas pero útiles:**

1. Los benchmarks publicados **no cubren los dos modelos que este sistema usa
   hoy**. Por eso `pnpm bench:latencia` no es un lujo: es la única fuente.
2. La fila "Haiku con razonamiento" es la advertencia: **activar razonamiento en
   cualquier modelo multiplica el arranque por ~29x**. Si algún día se configura
   `additionalModelRequestFields` (§8, palanca 5), es para BAJARLO, no subirlo.

### 9.3 Lo medido en este repo (manda sobre lo publicado)

Ajuste lineal `extracción_s = arranque + tokens_salida / throughput` sobre las 6
corridas de §6 (2 documentos × 3 repeticiones) de cada modelo. Los `tok/s` de las
tablas de §6 son **brutos** (incluyen el arranque dentro del promedio); esta
tabla los separa, por eso los números son más altos:

| Modelo | Arranque | Throughput real | CV por documento | Tokens de salida (3 reps) |
|---|---:|---:|---:|---|
| `minimax.minimax-m2.5` | 0.12s | **44 tok/s** | **58% y 62%** | 570/608/590 · 1586/1602/1517 |
| `us.anthropic.claude-haiku-4-5-...` | 0.71s | **152 tok/s** | **1% y 0%** | 320/320/320 · 1220/1220/1220 |
| `moonshotai.kimi-k2.5` | 3.24s | 64 tok/s | 9% y 44% | 198/188/166 · 773/654/681 |

Tres cosas que sólo se ven midiendo:

- **Haiku rinde 152 tok/s en Bedrock, más que los 98 publicados**, y su arranque
  (0.71s) coincide con el TTFT publicado de 519 ms más nuestra propia red.
- **Haiku emite el MISMO número de tokens en cada repetición** (320, 320, 320).
  MiniMax varía ±5%: está pensando distinto cada vez, y ese pensamiento es el
  que se cobra en segundos.
- **El "arranque" de MiniMax sale bajo (0.12s) sólo porque el ajuste reparte su
  razonamiento dentro del throughput**: no hay forma de separarlos sin streaming.
  Su varianza (58-62%) es la huella de esa fase variable.

### 9.4 Palancas de velocidad que dependen de la plataforma, no del modelo

- **Latency-optimized inference de Bedrock** (`performanceConfig.latency = "optimized"`):
  a la fecha de consulta la lista publicada cubre Claude 3.5 Haiku, Nova Pro y
  Llama 3.1 70B/405B en `us-east-2`/`us-west-2` — **no** Haiku 4.5 ni los modelos
  de terceros que usamos. Verificar antes de contar con ello; si el modelo
  elegido entra, es un cambio de un parámetro.
- **`TimeToFirstToken` de CloudWatch** existe pero sólo para APIs de streaming.
  Si se quiere medir TTFT server-side hay que migrar a `ConverseStream` — lo que
  **no** reduce el wall clock aquí (el JSON del tool call debe llegar completo
  antes de parsearse), así que sería puro instrumento, no optimización.
- **Prompt caching**: baja tokens de entrada y arranque. `cost_calibration.json`
  ya registra `supportsPromptCache: false` para MiniMax M2.5.

### Fuentes

- [Artificial Analysis — MiniMax-M2.5, proveedores](https://artificialanalysis.ai/models/minimax-m2-5/providers)
- [Amazon Bedrock — métricas de runtime en CloudWatch](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html)
- [AWS — nuevas métricas de TTFT y consumo estimado de cuota](https://aws.amazon.com/blogs/machine-learning/improve-operational-visibility-for-inference-workloads-on-amazon-bedrock-with-new-cloudwatch-metrics-for-ttft-and-estimated-quota-consumption)
- [Amazon Bedrock — latency-optimized inference](https://docs.aws.amazon.com/bedrock/latest/userguide/latency-optimized-inference.html)
- Datos de Haiku 4.5, MiniMax-M3 y Kimi: consulta al MCP de Artificial Analysis, 2026-09-23.

---

## 10. Benchmark de modelos baratos (2026-10-07)

Pregunta: ¿hay un modelo barato que se acerque a Haiku 4.5 en velocidad y
calidad? Haiku se tomó como referencia de comparación. 72 corridas con
`pnpm bench:latencia`, costo reportado por el script $1.08 (sobrestimado: tarifa
los modelos nuevos como Haiku). Resultados crudos en
`bench-latencia/20261007-*/`.

### 10.1 Candidatos y tarifas

Tarifas de AWS Price List, `us-east-1`, standard, consultadas el 2026-10-07.

| Modelo | Índice de inteligencia | $/1M entrada | $/1M salida | Resultado del preflight |
|---|---:|---:|---:|---|
| `minimax.minimax-m2.5` (desplegado) | 22.8 | 0.30 | 1.20 | corre |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | 15.4 | 1.10 | 5.50 | corre |
| `zai.glm-4.7-flash` | 14.9 | 0.07 | 0.40 | corre |
| `us.amazon.nova-2-lite-v1:0` | 8.7 | 0.33 | 2.75 | corre solo con perfil `us.` |
| `zai.glm-4.7` | 22.2 | 0.60 | 2.20 | corre |
| `us.zai.glm-5.3` | 44.8 | sin SKU publicado | sin SKU publicado | corre solo con perfil `us.`; 7.5-18.0s en CURP, tan lento como MiniMax: **descartado** |
| `us.moonshotai.kimi-k3` | 43.6 | 3.30 | 16.50 | **descartado**: 3x el precio de Haiku y rechaza el campo `temperature` que `bedrock.py:785` siempre manda |

Índice de inteligencia: Artificial Analysis Intelligence Index, consultado el
2026-10-07. Variantes publicadas: Haiku 4.5 y Nova 2 Lite sin razonamiento;
GLM 4.7 y GLM 4.7 Flash con razonamiento; GLM 5.3 y Kimi K3 en modo "Max".
El índice no predijo la calidad de extracción (§10.3): GLM 4.7 (22.2) devolvió
la salida vacía, y los dos de índice más alto ni siquiera pasaron el preflight
por precio o velocidad.

### 10.2 Velocidad y precio

Segundos de la etapa `extraction`, mediana de 3 repeticiones (min-max). `$` es
solo Bedrock, tokens medidos × tarifa real.

| Documento | MiniMax M2.5 (índice 22.8) | Haiku 4.5 (15.4) | Nova 2 Lite (8.7) | GLM 4.7 Flash (14.9) | GLM 4.7 (22.2) |
|---|---|---|---|---|---|
| `curp.pdf` (1 pág) | 15.6s (9.2-16.9) · $0.0014 | 2.7s (2.7-2.8) · $0.0050 | 1.5s (1.4-2.1) · $0.0016 | 1.8s (1.2-2.6) · $0.0002 | no medido |
| `2607-0973-6100971_P.PDF-1.pdf` (2 pág) | 21.9s (13.3-26.8) · $0.0041 | 8.5s (8.5-8.5) · $0.0170 | 5.0s (4.5-5.5) · $0.0053 | 7.1s (6.6-7.1) · $0.0009 | 8.3s (8.0-11.8) · $0.0067 |
| `normal_pend.pdf` (3 pág) | 20.7s (17.3-49.8) · $0.0045 | 9.4s (9.4-9.5) · $0.0191 | 0.9s, **salida vacía** | 6.9s (6.7-**192.0**) · $0.0010 | 11.6s, **salida vacía** |
| `FLETE.pdf` (esquema >10K, 2 llamadas) | 34.8s (13.1-38.9) · $0.0051 | 4.2s (4.1-4.3) · $0.0198 | 6.9s (5.1-8.3) · $0.0091 | 10.8s (9.8-11.3) · $0.0014 | 5.3s (5.1-7.8) · $0.0077 |
| `26 07 3435 6041353 CG.pdf` (139 pág, 1 rep) | 78.7s · $0.0296* | 15.4s · $0.0777* | 2.0s, **salida vacía** | no medido | no medido |

\* Incluye $0.015 de Textract (10 páginas) y, en Haiku, escritura de caché.

Haiku contra MiniMax en extracción: 5.8x, 2.6x, 2.2x, 8.2x y 5.1x más rápido,
a 3.5x, 4.2x, 4.2x, 3.9x y 2.6x el costo. Su variación entre corridas idénticas
fue de 0-1%; la de MiniMax, de 24-50%.

Las cifras de MiniMax y Haiku coinciden con las de §6 dentro de la variación de
MiniMax (CURP 15.6s contra 11.98s; pedimento de 2 páginas 21.9s contra 30.22s;
Haiku 2.7s y 8.5s contra 2.85s y 8.79s).

### 10.3 Calidad

"Distinto de Haiku" no es un error por sí solo; lo verificado contra el texto
del documento se marca como tal.

| Modelo | Qué pasó |
|---|---|
| **Haiku 4.5** | Completo y determinista. **Error verificado en `normal_pend.pdf`:** devolvió `FACTURAS[].Factura` = `35079805` y `35079806`, 3 de 3 corridas. El documento dice `517010` y `517043`; `35079805` solo aparece en el hint del catálogo (`'COVE2689JP013 / 35079805'`) y `35079806` no aparece en ningún lado. |
| **MiniMax M2.5** | Facturas correctas en `normal_pend.pdf`. Deja vacíos `Referencia` y las dos `TASA` del cuadro de liquidación (ya verificado en §6.2 que sí están en el documento). En `FLETE.pdf` deja vacíos `IMPORTE`, `TARIFA` y `NOTAS`, y una de tres corridas perdió casi toda la fila de conceptos. |
| **Nova 2 Lite** | **Tool call vacío** (`toolInput: {}`, 16 tokens de salida) en `normal_pend.pdf` 3 de 3 y en las 2 llamadas del expediente de 139 páginas. En el pedimento de 2 páginas quedó en `REVIEW_REQUIRED` con `TASA` = 2 y 1 (Haiku: 330 y 16) y `GI_Total` = 11027 (Haiku: 17389). En `FLETE.pdf` intercambia domicilio de cliente y destinatario respecto a Haiku (sin verificar quién acierta). |
| **GLM 4.7 Flash** | En `normal_pend.pdf` repite la misma factura en las dos filas, cruza importes entre filas, y una corrida tardó 192s. En `FLETE.pdf` pierde la fila de conceptos y da otro cliente. En CURP deja `FOLIO` vacío y mete el nombre completo en `NOMBRES`. |
| **GLM 4.7** | **Ignora el tool call forzado**: termina con `stopReason: end_turn` y texto libre, así que la extracción sale vacía en `normal_pend.pdf` y en `FLETE.pdf` (3 de 3). |

Las fallas de Nova 2 Lite y GLM 4.7 son de compatibilidad con cómo el pipeline
pide la salida (`toolChoice` forzado, `bedrock.py:786-797`), no de lectura del
documento. No se investigó la causa; mientras no se resuelva, no son candidatos.

### 10.4 Conclusión

- No hay atajo barato: de los cinco candidatos nuevos, ninguno extrae un
  pedimento de 3 páginas de forma usable.
- La decisión real sigue siendo MiniMax contra Haiku: 2.2-8.2x menos tiempo de
  extracción y variación casi nula, a cambio de +$0.004 a +$0.05 por documento.
- Antes de adoptar Haiku hay que quitar del hint de `FACTURAS` el ejemplo con
  un número de factura realista y volver a medir: tal como está, Haiku lo copia.
- Nova 2 Lite rinde 164-286 tok/s a un tercio del precio de Haiku. Si se
  resuelve el tool call vacío, merece otra medición.

---

## 11. Mejoras cuantificadas (2026-10-07)

Estimadas sobre las salidas reales del benchmark (`_prompt_output.json`), sin
implementar nada. Los porcentajes de formato son por caracteres del JSON, no por
tokens medidos.

### 11.1 Formato de salida

| Documento | JSON de salida | Sin `{value, confidence}` | Arreglos en columnas | Ambos |
|---|---:|---:|---:|---:|
| CURP (0 filas) | 474 chars | -46% | 0% | -46% |
| Pedimento 2 pág (3 filas) | 1 759 | -38% | -1% | no calculado |
| `normal_pend.pdf` (4 filas) | 2 171 | -31% | -12% | no calculado |
| `FLETE.pdf` (1 fila) | 1 749 | -33% | 0% | no calculado |
| Expediente 139 pág (24 filas) | 5 482 | -18% | -36% | -51% |

- **El envoltorio por campo** (`text_extractor.py:431-434`) es la palanca grande
  en documentos chicos. Quitarlo implica decidir de dónde sale la confianza por
  campo que hoy consume el resultado.
- **El formato columnar** (nombres de columna una vez, no por fila) solo paga
  cuando hay muchas filas: 36% con 24 filas, nada con 1-3.
- Como el tiempo es proporcional a los tokens de salida, en el expediente de 139
  páginas la mitad del JSON equivale a cerca de 7s menos con Haiku y cerca de
  40s menos con MiniMax.

### 11.2 Lo que no apareció en esta ronda

- **Truncamiento por `max_tokens`:** 0 de 72 corridas. El expediente de 139
  páginas hoy genera 3 399-4 112 tokens de salida en 2 llamadas, contra los
  13 833-16 662 de §4 y §6.3: la selección de páginas y la confianza por fila ya
  lo bajaron. El riesgo de §7.3 sigue en el código, pero ningún documento de
  prueba lo dispara.
- **Split por esquema grande:** `FLETE.pdf` hace 2 llamadas concurrentes y con
  Haiku termina en 4.2s. Duplica tokens de entrada (12 482), no latencia.
- **Razonamiento de MiniMax:** 626-733 tokens de salida para un CURP cuyo JSON
  mide 519 caracteres; Haiku usa 320 para 474. Sigue sin poder apagarse.

### 11.3 Carga de páginas

En el expediente de 139 páginas `pageLoad` tardó 15.3-16.0s en las tres corridas.
El texto nativo de las 139 páginas con pypdf tarda 1.2s medido aparte, así que
unos 14s son Textract sobre 10 páginas escaneadas. Con Haiku eso es la mitad del
total (30.7s). Medido en local, donde `TEXTRACT_WORKERS` vale 4; el Lambda usa 8.

### 11.4 Orden sugerido

| # | Mejora | Efecto medido o estimado | Qué toca |
|---|---|---|---|
| 1 | Quitar el ejemplo realista del hint de `FACTURAS` y re-medir Haiku | requisito para adoptar Haiku | catálogo `PEDIMENTO_ERP` |
| 2 | Extracción en Haiku 4.5 | extracción 2.2-8.2x más rápida; +$0.004 a +$0.05 por documento | `BEDROCK_EXTRACTOR_MODEL_ID` (env var, sin código) |
| 3 | Plomería de §8 (#2, #3, #4) | ~10s fijos por documento | CDK y API |
| 4 | Quitar el envoltorio por campo; columnas en arreglos | -18% a -51% del JSON de salida | `text_extractor.py` |
| 5 | OCR solo de las páginas seleccionadas, o más workers | hasta ~14s en expedientes con escaneos | `pdf_pages.py`, `page_selector.py` |
| 6 | Investigar el tool call vacío de Nova 2 Lite | abriría un modelo rápido a un tercio del precio de Haiku | `bedrock.py` |

Estimación para el pedimento de 2 páginas, de punta a punta: hoy unos 32s de
mediana (21.9s de extracción + ~10s de plomería) con picos arriba de 60s; con
#2, unos 18s; con #2 y #3, unos 11s.
