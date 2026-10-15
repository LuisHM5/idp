# Modelos para las etapas de puro texto: ranking costo, velocidad e inteligencia

Qué modelo conviene para las etapas que **no requieren visión** (segmentación,
extracción, locator y recovery, que corren sobre el OCR de Textract o el texto
nativo del PDF), entre los que se pueden invocar en Bedrock `us-east-1`. Para las
etapas con imagen, ver `MODELOS_VISION.md`.

**Fecha del análisis:** 2026-10-08 · cuenta `886031931562`, `us-east-1`.

> **Actualización 2026-10-15 — Claude Haiku 5.5 es el modelo base y el de
> visión.** `BEDROCK_MODEL_ID` y `BEDROCK_VISION_MODEL_ID` =
> `us.anthropic.claude-haiku-5-5`, effort `low` (`BEDROCK_EFFORT`). Es el
> modelo frontera de referencia: ver `models_pricing.md` §0 (tarifas completas,
> Intelligence Index por effort y comparación con MiniMax). El código ya manda
> el request que acepta (sin `temperature`, sin prefill, con
> `output_config.effort` — `pipeline/bedrock.py::_adapt_request`). **Requisito
> manual:** la cuenta todavía responde `AccessDeniedException`
> (`agreementAvailability: NOT_AVAILABLE`); hay que habilitar el acceso al
> modelo en la consola de Bedrock. Mientras tanto cada llamada cae a MiniMax por
> la cadena de fallback. Las mediciones de abajo son del 2026-10-08, con
> Haiku 4.5; repetir §3 con Haiku 5.5 en cuanto haya acceso.

---

## 1. Veredicto

1. **Principal: Kimi K2.5** (`moonshotai.kimi-k2.5`). 19 de 19 campos contra
   ground truth, la extracción más rápida entre los que acertaron todo (6.0 s) y
   $0.0064 por documento. Es además el de mayor índice de inteligencia entre los
   que la cuenta puede usar hoy (23.5) y el que más evidencia propia acumula
   después de MiniMax.
2. **Respaldo: Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`).
   19 de 19, 7.8 s y la latencia más estable de todo lo medido (variación de 0 a
   1% entre repeticiones). Cuesta 2.6 veces lo que Kimi ($0.0166) y es menos
   inteligente en el papel (15.4), por eso queda segundo.
3. **MiniMax M2.5, el modelo desplegado hoy, salió mal en esta prueba**: 9 de 19
   campos, 194 s y $0.0179, el más caro de la corrida. La primera llamada chocó
   con el tope de 8,192 tokens de salida y el pipeline tuvo que repartir el
   trabajo en dos llamadas más. Es **una sola corrida**; en octubre, con el mismo
   archivo como `PEDIMENTO_ERP`, acertó las facturas en 3 de 3. Lo que sí es
   consistente es su lentitud y su variación (58 a 62% entre repeticiones).
4. **Candidatos baratos a seguir midiendo:** Qwen3 Next 80B (19 de 19 por
   $0.0024) y DeepSeek V3.2 (19 de 19 por $0.0069). Solo tienen esta medición,
   así que no entran al top 2 todavía.
5. **Los mejores en el papel no se pueden usar hoy.** Claude Haiku 5.5
   ($0.11 / $0.55, índice 29.4 a 43.4), GPT-6 Luna y GLM 5.3 responden
   `AccessDeniedException`: la cuenta no los tiene habilitados. Haiku 5.5 sería
   el candidato natural a principal en cuanto haya acceso (§6).

---

## 2. Criterio

| Orden | Criterio | De dónde sale |
|---|---|---|
| 1 | **Filtro eliminatorio:** salida vacía, ignorar el tool call forzado o no poder invocarse | prueba de hoy y `LATENCIA.md` §10 |
| 2 | **Exactitud contra ground truth** | prueba de hoy (§3) |
| 3 | **Costo real por documento** | tokens facturados × tarifa on-demand de la Price List API de AWS |
| 4 | **Latencia** (mediana y variación) | prueba de hoy y benchmarks de `bench-latencia/` |
| 5 | **Inteligencia** (desempate) | Artificial Analysis, consultado hoy |

El índice de inteligencia va al final a propósito: Mistral Large 3 (9.3) y Qwen3
Next 80B (9.6 a 11.2) acertaron los 19 campos, y Kimi K2 Thinking (22.0) no
devolvió nada utilizable. Para esta tarea, seguir el tool call forzado pesa más
que el razonamiento general.

---

## 3. La prueba contra ground truth (2026-10-08)

Una corrida por modelo de `test-docs/pedimentos docs/docs/normal_pend.pdf`
(3 páginas nativas, sin Textract) declarado como `PEDIMENTO_ADUANAL`, evaluada
con `evaluate_expectation` de `scripts/test-pedimentos.py` contra
`tests/integration/expected_pedimentos.json`: 19 valores exactos y 9 campos
obligatorios presentes. Costo total de la corrida: **$0.11**. Salidas crudas en
`bench-latencia/20261008-texto-groundtruth/` (`_filas.json` trae el resumen).

Segundos y tokens son solo de las llamadas a Bedrock. `$/doc` usa la tarifa
on-demand real de cada modelo.

| # | Modelo | Aciertos (19) | Obligatorios (9) | Estado | s | Tokens in / out | $/doc | Qué falló |
|---:|---|---:|---:|---|---:|---:|---:|---|
| 1 | Kimi K2.5 | **19** | 9 | APPROVED | 6.0 | 7,069 / 735 | $0.0064 | — |
| 2 | Claude Haiku 4.5 | **19** | 9 | APPROVED | 7.8 | 8,643 / 1,282 | $0.0166 | — |
| 3 | Qwen3 Next 80B | **19** | 9 | APPROVED | 14.1 | 8,021 / 1,058 | $0.0024 | — |
| 4 | DeepSeek V3.2 | **19** | 9 | APPROVED | 15.8 | 7,751 / 1,158 | $0.0069 | — |
| 5 | GLM 5 | **19** | 9 | APPROVED | 12.3 | 7,518 / 915 | $0.0104 | — |
| 6 | Mistral Large 3 | **19** | 9 | APPROVED | 19.2 | 8,398 / 1,060 | $0.0058 | — |
| 7 | GLM 4.7 | **19** | 9 | APPROVED | 14.9 | 7,518 / 852 | $0.0064 | hoy nada; ver §4 |
| 8 | GLM 4.7 Flash | 17 | 9 | APPROVED | 5.5 | 7,518 / 855 | $0.0009 | puso el COVE como número de factura en las dos filas |
| 9 | Qwen3 VL 235B | 17 | 9 | APPROVED | 26.8 | 8,021 / 989 | $0.0069 | mismo error del COVE |
| 10 | gpt-oss-120b | 15 | 9 | REVIEW_REQUIRED | 8.3 | 6,547 / 2,251 | $0.0023 | no devolvió las facturas |
| 11 | MiniMax M2.5 | 9 | 8 | REVIEW_REQUIRED | 194.0 | 17,488 / 10,542 | $0.0179 | tope de 8,192 tokens en la 1.ª llamada; tipo de cambio, fletes, total y facturas incorrectos |
| 12 | Nemotron 3 Super 120B | 5 | 4 | REVIEW_REQUIRED | 10.1 | 8,909 / 1,230 | $0.0021 | ignoró el tool call (`end_turn`) |
| 13 | Kimi K2 Thinking | 5 | 4 | REVIEW_REQUIRED | 46.1 | 7,264 / 5,067 | $0.0170 | ignoró el tool call (`end_turn`) |
| 14 | Amazon Nova 2 Lite | 5 | 4 | REVIEW_REQUIRED | 0.9 | 7,762 / 16 | $0.0026 | tool call vacío (16 tokens) |
| — | Llama 4 Maverick 17B | — | — | PROCESSING_ERROR | — | — | — | `ValidationException`: no acepta `toolChoice.tool` |
| — | Claude Haiku 5.5 | — | — | PROCESSING_ERROR | — | — | — | `AccessDeniedException` |
| — | GPT-6 Luna | — | — | PROCESSING_ERROR | — | — | — | `AccessDeniedException` |
| — | GLM 5.3 | — | — | PROCESSING_ERROR | — | — | — | `AccessDeniedException` |

Los 5 aciertos de los modelos con salida vacía son campos que el pipeline
deriva sin el modelo (número de pedimento, año, aduana, patente, consecutivo).

Límites de esta prueba: **un documento, una repetición**. Sirve para separar a
los que siguen el contrato de los que no; no alcanza para ordenar con confianza
a los siete que sacaron 19.

---

## 4. Ranking completo

Combina la prueba de hoy con lo ya medido en el repo y en DynamoDB de dev. El
índice de inteligencia es el de Artificial Analysis consultado hoy; el rango va
de la variante sin razonamiento a la de máximo esfuerzo.

| # | Modelo | ID en Bedrock | $ in / out por 1M | Intel. | Aciertos hoy | Evidencia previa | Veredicto |
|---:|---|---|---:|---:|---:|---|---|
| 1 | **Kimi K2.5** | `moonshotai.kimi-k2.5` | $0.60 / $3.00 | 23.5 | 19/19 | CURP 13/13 y pedimento 60/61 (2026-09-23); 10 pedimentos con 99% de campos válidos (agosto); 7 docs en dev | **Principal** |
| 2 | **Claude Haiku 4.5** | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | $1.10 / $5.50 | 15.4 | 19/19 | 5 documentos × 3 repeticiones en octubre, todos válidos; variación 0-1% | **Respaldo** |
| 3 | Qwen3 Next 80B | `qwen.qwen3-next-80b-a3b` | $0.14 / $1.20 | 9.6-11.2 | 19/19 | ninguna | Medir más; el más barato que acertó todo |
| 4 | DeepSeek V3.2 | `deepseek.v3.2` | $0.62 / $1.85 | 16.0 | 19/19 | ninguna | Medir más |
| 5 | GLM 5 | `zai.glm-5` | $1.00 / $3.20 | 21.8-27.9 | 19/19 | 2 corridas de agosto: pedimento 63/63, INE 22/37 | Usable; más caro que Kimi sin ganarle en nada medido |
| 6 | Mistral Large 3 | `mistral.mistral-large-3-675b-instruct` | $0.50 / $1.50 | 9.3 | 19/19 | ninguna en texto | Usable pero lento (19 s) |
| 7 | GLM 4.7 | `zai.glm-4.7` | $0.60 / $2.20 | 22.2 | 19/19 | octubre: ignoró el tool call, salida vacía en este mismo archivo y en FLETE, 3 de 3 | No confiable: acierta o devuelve vacío según el catálogo |
| 8 | MiniMax M2.5 (desplegado) | `minimax.minimax-m2.5` | $0.30 / $1.20 | 22.8 | 9/19 | 83 docs en dev; octubre: 61/61 y 73/73 como `PEDIMENTO_ERP`; variación 58-62% | Barato por token, lento e inestable; dejar de ser el principal |
| 9 | GLM 4.7 Flash | `zai.glm-4.7-flash` | $0.07 / $0.40 | 10.6-14.9 | 17/19 | octubre: filas de factura repetidas, `FOLIO` vacío en CURP, una corrida de 196 s con 32,000 tokens | Descartado para extracción |
| 10 | Qwen3 VL 235B | `qwen.qwen3-vl-235b-a22b` | $0.53 / $2.66 | 9.9-13.4 | 17/19 | 1 doc viejo en dev, en revisión | Descartado: lento y con el error del COVE |
| 11 | gpt-oss-120b | `openai.gpt-oss-120b-1:0` | $0.15 / $0.60 | 10.2-11.6 | 15/19 | ninguna | Descartado: omite listas |
| 12 | Nemotron 3 Super 120B | `nvidia.nemotron-super-3-120b` | $0.15 / $0.65 | 12.8 | 5/19 | ninguna | Descartado: ignora el tool call |
| 13 | Kimi K2 Thinking | `moonshot.kimi-k2-thinking` | $0.60 / $2.50 | 22.0 | 5/19 | ninguna | Descartado: ignora el tool call y gasta 5,067 tokens |
| 14 | Amazon Nova 2 Lite | `us.amazon.nova-2-lite-v1:0` | $0.33 / $2.75 | 8.7-13.4 | 5/19 | octubre: tool call vacío en este archivo 3 de 3 y en el expediente de 139 páginas | Descartado para extracción |
| 15 | Llama 4 Maverick 17B | `us.meta.llama4-maverick-17b-instruct-v1:0` | $0.24 / $0.97 | 10.0 | error | ninguna en texto | Incompatible: no acepta tool call forzado |

### 4.1 Mejores en el papel, sin acceso en la cuenta

| Modelo | ID | $ in / out por 1M | Intel. | Estado |
|---|---|---:|---:|---|
| **Claude Haiku 5.5** | `us.anthropic.claude-haiku-5-5` | $0.11 / $0.55 ¹ | 29-43 | Modelo base desde 2026-10-15; `AccessDeniedException` hasta habilitar el acceso en la cuenta. Publicado el 2026-10-07 |
| GPT-6 Luna | `us.openai.gpt-6-luna` | $0.10 / $0.50 ² | 29.9-38.1 | `AccessDeniedException` |
| GLM 5.3 | `us.zai.glm-5.3` | $1.40 / $4.40 ² | 34.3-44.8 | `AccessDeniedException` hoy; el 2026-10-07 sí respondió en el preflight de CURP |
| GPT-5.6 Luna | `us.openai.gpt-5.6-luna` | $0.20 / $1.20 ² | 32.1-37.3 | sin acceso (probado 2026-09-24) |

¹ Price List API de AWS, `us-east-1`, hasta 100K tokens de prompt; arriba de eso
$0.55 / $2.75.
² Precio de lista del proveedor según Artificial Analysis; AWS no publica SKU.

### 4.2 En el catálogo, no medidos

Solo datos públicos; ninguno supera a los medidos en el papel, así que no se
gastó en probarlos.

| Modelo | $ in / out por 1M | Intel. |
|---|---:|---:|
| Kimi K3 | $3.30 / $16.50 | 30.1-43.6 (más caro que Haiku 4.5; rechaza `temperature`) |
| Grok 4.7 | $2.20 / $6.60 | 42.2-46.4 (más caro que Haiku 4.5) |
| DeepSeek R1 | $1.35 / $5.40 | 13.1 |
| Qwen3 Coder Next | $0.50 / $1.20 | 9.2 |
| Magistral Small 1.2 | $0.50 / $1.50 | 8.6 |
| Devstral 2 | $0.40 / $2.00 | 8.6 |
| Qwen3 32B | $0.15 / $0.60 | 7.3-8.6 |
| gpt-oss-20b | $0.07 / $0.30 | 9.0-10.0 |
| Nemotron Nano 3 30B | $0.06 / $0.24 | 6.8-8.9 |
| Amazon Nova Micro | $0.035 / $0.14 | 5.9 |
| MiniMax M2.1 / M2 | $0.30 / $1.20 | por debajo de M2.5 |

---

## 5. Lo que ya decían tus datos

### 5.1 Latencia y costo en benchmarks controlados

Medianas de la etapa de extracción (`LATENCIA.md` §6 y §10; 3 repeticiones).

| Documento | MiniMax M2.5 | Kimi K2.5 | Haiku 4.5 |
|---|---|---|---|
| CURP, 1 página | 12.0-15.6 s · $0.0013 | 6.5 s · $0.0018 | 2.7-2.9 s · $0.0050 |
| Pedimento, 2 páginas | 21.9-30.2 s · $0.0042 | 17.5 s · $0.0067 | 8.5-8.8 s · $0.0170 |
| `normal_pend.pdf` como ERP | 20.7 s (17.3-49.8) · $0.0045 | no medido | 9.4 s · $0.0191 |
| Expediente, 139 páginas | 78.7 s · $0.0296 | 46.0 s · $0.0427 ³ | 15.4 s · $0.0777 |

³ Corrida histórica no controlada.

| Modelo | Arranque | Throughput | Variación entre repeticiones |
|---|---:|---:|---|
| MiniMax M2.5 | 0.12 s | 44 tok/s | 58% y 62% |
| Kimi K2.5 | 3.24 s | 64 tok/s | 9% y 44% |
| Haiku 4.5 | 0.71 s | 152 tok/s | 1% y 0% |

Cuando MiniMax tiene una corrida normal es el más barato por documento; lo que
lo baja en el ranking es que sus corridas malas son frecuentes y caras.

### 5.2 DynamoDB de dev

Tabla `document-validation-costs-dev`, 104 documentos del 2026-08-17 al
2026-10-08. No son los mismos archivos por modelo, así que sirve para volumen,
no para comparar calidad.

| Modelo (extracción) | Docs | Tokens in | Tokens out | Costo recalculado | $/doc |
|---|---:|---:|---:|---:|---:|
| MiniMax M2.5 | 83 | 1,101,308 | 291,008 | $0.680 | $0.0082 |
| Kimi K2.5 | 7 | 210,866 | 16,821 | $0.177 | $0.0253 |
| Haiku 4.5 | 1 | 23,388 | 4,985 | $0.053 | $0.0531 |

El costo se recalculó con la tarifa vigente: los registros de Kimi de septiembre
están guardados con la tarifa por defecto ($1.10 / $5.50) y los de MiniMax
anteriores al 2026-09-04 con otra versión de tarifas. En la tabla de tracking,
la extracción con MiniMax promedia 47.5 s sobre 75 documentos.

### 5.3 Benchmarks públicos cercanos a la tarea

IFBench mide seguir instrucciones al pie de la letra, LCR encontrar el dato en
un contexto largo y Tau2 sostener el uso de herramientas.

| Modelo | Intel. | IFBench | LCR | Tau2 |
|---|---:|---:|---:|---:|
| Kimi K2.5 | 23.5 | 0.70 | 0.78 | 0.96 |
| MiniMax M2.5 | 22.8 | 0.72 | 0.73 | 0.95 |
| GLM 4.7 | 22.2 | 0.68 | 0.71 | 0.96 |
| Kimi K2 Thinking | 22.0 | 0.68 | 0.72 | 0.93 |
| GLM 5 (sin razonamiento) | 21.8 | 0.55 | 0.44 | 0.97 |
| DeepSeek V3.2 | 16.0 | 0.49 | 0.46 | 0.79 |
| Claude Haiku 4.5 | 15.4 | 0.42 | 0.50 | 0.32 |
| Qwen3 Next 80B | 9.6 | 0.40 | 0.53 | 0.22 |
| Mistral Large 3 | 9.3 | 0.36 | 0.36 | 0.25 |

Haiku 4.5 sale último de los tres grandes en los benchmarks y primero en
estabilidad en tus documentos; Kimi K2 Thinking sale bien aquí y falla en la
prueba real. Por eso el ranking se apoya en lo medido.

---

## 6. Recomendación

| Uso | Hoy | Siguiente paso |
|---|---|---|
| Modelo base de texto (`BEDROCK_MODEL_ID`) | **Hecho 2026-10-15: Claude Haiku 5.5** (`us.anthropic.claude-haiku-5-5`), por encima de Kimi K2.5: más inteligente (29-43 contra 23.5) y 5x más barato por token | Con acceso habilitado, correr `pnpm test:pedimentos -- --model us.anthropic.claude-haiku-5-5` sobre los 4 archivos del expected y `pnpm test:e2e:suite` |
| Cadena de fallback (`model_fallback.py`) | MiniMax → Kimi → GLM 5 → Haiku 5.5. Haiku 5.5 va al final porque el segmentador usa el ÚLTIMO de la lista como su default y reintento; cuando Haiku es el que falla, el cambio va a MiniMax | Reordenar a Kimi primero si MiniMax sigue lento como respaldo |
| Costo esperado | Kimi cuesta unos $0.0025 más por pedimento que una corrida normal de MiniMax ($2.50 por cada 1,000) y tarda menos en todo lo medido | Recalibrar `pnpm costos:validar` después del cambio |
| Claude Haiku 5.5 | Pedir acceso en la cuenta (consola de Bedrock → Model access) | Resuelto en código: `bedrock.py::_adapt_request` quita `temperature` y el prefill y agrega `output_config.effort` solo para ese modelo. Con acceso, repetir la prueba de §3 |
| Qwen3 Next 80B y DeepSeek V3.2 | No adoptar todavía | Faltan sus tarifas en `pricing.py` y repeticiones sobre CURP, FLETE y un expediente largo |

Antes de cambiar el modelo de cualquier etapa: `pricing.py` tiene que tener su
tarifa (`tests/unit/test_pricing_coverage.py`).

---

## 7. Cómo se verificó

| Fuente | Qué aportó |
|---|---|
| `aws bedrock list-foundation-models` y `list-inference-profiles` (`us-east-1`) | catálogo de modelos de texto al 2026-10-08 |
| AWS Price List API (`AmazonBedrock`, `AmazonBedrockFoundationModels`, `AmazonBedrockService`) | tarifas on-demand `standard` de `us-east-1` |
| MCP `artificial-analysis` (550 modelos más consultas puntuales) | Intelligence Index, IFBench, LCR, Tau2 y precio de lista |
| `bench-latencia/20261007-*`, `bench-latencia/{curp,ped2}`, `LATENCIA.md`, `COSTOS.md`, `data/resultados/` | latencia, tokens y calidad ya medidos |
| DynamoDB dev (`document-validation-costs-dev` y la tabla de tracking) | volumen, tokens y tiempos por modelo |
| **Prueba de hoy** (`bench-latencia/20261008-texto-groundtruth/prueba_texto.py`) | exactitud contra ground truth de 18 modelos; llama a `process_pdf` con `model_id` y la cadena de fallback apagada, para que un modelo que falla se vea como falla |

No se modificó código, catálogos ni configuración desplegada.
