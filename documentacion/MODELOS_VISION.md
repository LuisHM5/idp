# Modelos con visión, costo-eficientes, a nivel de Haiku 4.5

Qué modelos pueden **ver imágenes/PDFs** con una inteligencia comparable a
Claude Haiku 4.5 y cuestan menos, cuáles se pueden usar **hoy** en este stack
(Bedrock `us-east-1`) y cuáles no — con la visión **probada contra Bedrock**, no
inferida del nombre.

**Fecha del análisis:** 2026-09-24 · cuenta `886031931562`, `us-east-1`.

---

## 1. Veredicto

1. **En Bedrock, hoy, sin trámites, solo Kimi K2.5 supera a Haiku en
   inteligencia con visión y cuesta menos**: índice 19.4-23.5 contra 15.4-16.9, a
   0.55x del precio. Pero es **8x más lento** en la prueba de visión (11.3s contra
   1.4s).
2. **GPT-6 Luna es la mejor opción sobre el papel**: índice 18.3-37.3 a
   $0.10/$0.50 (precio de lista de OpenAI, ~0.09x de Haiku). Ya está en el catálogo
   de `us-east-1` con `IMAGE`, pero **esta cuenta no tiene acceso**
   (`AccessDeniedException`). GPT-5.6 Luna está en la misma situación.
3. **MiniMax M2.5, GLM 5 y Kimi K2 Thinking NO tienen visión.** Bedrock rechaza
   tanto `image` como `document`. Esto contradice `models_pricing.md` y
   `model recomendation.md`, que dan a MiniMax M2.5 y GLM 5 como "visión
   confirmada": es falso.
4. **Consecuencia en producción:** la clasificación upstream de los uploads
   **sin tipo declarado** corre sobre MiniMax y falla. Ya pasó una vez
   (2026-09-01, ver §6).
5. La etapa de visión del expediente (autenticidad) **no está afectada**: tiene
   su propia cadena de modelo, que por diseño nunca cae al modelo base (§3).

---

## 2. Criterio: qué es "nivel Haiku"

| | Valor |
|---|---|
| Inteligencia de referencia | **Claude Haiku 4.5**: Intelligence Index **15.4** (sin razonamiento) / **16.9** (con razonamiento) |
| Techo de precio | **$1.10 input / $5.50 output** por 1M tokens (Bedrock `us-east-1`, `pipeline/pricing.py`) → **$2.20 blended** (3:1) |
| Filtro | visión ✅ · índice ≥ 15.4 en alguna variante · blended ≤ $2.20 |

> ⚠️ **Artificial Analysis cambió de escala.** `models_pricing.md` (2026-08-10)
> daba a Haiku 4.5 un índice de **29.9**; hoy el mismo modelo sale con **16.9**.
> Todos los modelos bajaron parecido, así que las comparaciones *relativas* de
> aquel documento siguen en pie, pero **no mezcles números de los dos
> documentos**.

"Blended" = `(3 × input + output) / 4`, la métrica de Artificial Analysis.
Refleja bien este pipeline, donde el input domina (prompt + OCR/imagen) y el
output es un JSON corto.

---

## 3. Dónde necesita visión este repo

Casi todo el pipeline es **solo texto** (corre sobre el OCR de Textract o el
texto nativo del PDF). Solo **dos** puntos le mandan una imagen o un archivo al
modelo:

| Punto | Qué manda | Modelo que usa | Variable |
|---|---|---|---|
| Etapa `vision` del expediente (`pipeline/authenticity.py`, solo tipos con bloque `authenticity`; hoy `CARTA_NOM35`) | página + recortes de firma/sello/logo | `model_ids.vision` del catálogo → `BEDROCK_VISION_MODEL_ID` → **Haiku 4.5** | nunca cae a `BEDROCK_MODEL_ID` ni a la lista de fallback (`bedrock.py:39-47`, `:255-264`) ✅ |
| Clasificación upstream (`model-invoke-lambda/index.py:364-372`) de un upload **sin tipo** (`raw/{uuid}/file`) | el archivo completo como bloque `image` (png/jpg) o `document` (pdf) | `MODEL_ID` = `BEDROCK_CLASSIFICATION_MODEL_ID` o, si no existe, **`BEDROCK_MODEL_ID`** (`document-validation-stack.ts:163-166`) | hoy desplegado = `minimax.minimax-m2.5` ❌ (§6) |

Es decir: para mover la visión a un modelo más barato, las perillas son
`BEDROCK_VISION_MODEL_ID` (o `model_ids.vision` por tipo) y
`BEDROCK_CLASSIFICATION_MODEL_ID`. **No hay que tocar código.**

---

## 4. Candidatos en Bedrock `us-east-1`

### 4.1 A nivel de Haiku o por encima, más baratos

| Modelo | ID en Bedrock | Intel. (AA) | $ in / out por 1M | Blended | vs Haiku | Visión probada | ¿Usable hoy? |
|---|---|---:|---:|---:|---:|---|---|
| **Claude Haiku 4.5** (referencia) | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | 15.4-16.9 | $1.10 / $5.50 | $2.20 | 1.00x | ✅ 1.4s | ✅ |
| **Kimi K2.5** | `moonshotai.kimi-k2.5` | 19.4-23.5 | $0.60 / $3.00 | $1.20 | **0.55x** | ✅ 11.3s | ✅ on-demand |
| **GPT-6 Luna** | `us.openai.gpt-6-luna` | 18.3-37.3 | $0.10 / $0.50 ¹ | $0.20 ¹ | **~0.09x** ¹ | catálogo: `TEXT,IMAGE` | ❌ `AccessDeniedException`: habilitar acceso |
| **GPT-5.6 Luna** | `us.openai.gpt-5.6-luna` | 15.5-37.3 | $0.20 / $1.20 ¹ | $0.45 ¹ | ~0.20x ¹ | catálogo: `TEXT,IMAGE` | ❌ `AccessDeniedException` |
| **Grok 4.3** | — | 14.0-24.9 | $1.25 / $2.50 | $1.56 | 0.71x ² | no probada ³ | ❌ Converse: *model identifier is invalid* |
| **Gemma 4 31B** | — | 13.9-19.0 | $0.14 / $0.40 | $0.21 | 0.09x | no probada ³ | ❌ ídem |
| **Gemma 4 26B A4B** | — | 13.1-16.7 | $0.13 / $0.40 | $0.20 | 0.09x | no probada ³ | ❌ ídem |

¹ La Price List API de AWS **todavía no publica SKU** para los modelos de OpenAI
en Bedrock. El precio es el de lista de OpenAI según Artificial Analysis. Hay que
confirmarlo antes de meterlo a `pricing.py`.
² El input de Grok 4.3 es **más caro** que el de Haiku ($1.25 contra $1.10); solo
gana en blended por su output barato.
³ Tienen SKU de precio en `us-east-1`, pero **no aparecen** en
`list-foundation-models` y `Converse` rechaza el ID. La visión se toma de
OpenRouter/models.dev, no de una prueba.

El rango de inteligencia va de la variante sin razonamiento a la de esfuerzo
máximo. Para visión/clasificación importa sobre todo el extremo bajo: son tareas
cortas donde el razonamiento largo solo agrega latencia.

### 4.2 Con visión pero por debajo de Haiku (baratos y rápidos)

Sirven para tareas **simples** de visión, como clasificar un documento o leer un
folio, no para juzgar autenticidad:

| Modelo | ID | Intel. | $ in / out | Blended | Visión probada |
|---|---|---:|---:|---:|---|
| Amazon Nova 2 Lite | `us.amazon.nova-2-lite-v1:0` | 8.7-13.4 | $0.33 / $2.75 | $0.94 | ✅ 0.9s |
| Qwen3 VL 235B A22B | `qwen.qwen3-vl-235b-a22b` | 9.9-13.4 | $0.53 / $2.66 | $1.06 | ✅ 1.2s |
| Llama 4 Maverick 17B | `us.meta.llama4-maverick-17b-instruct-v1:0` | 10.0 | $0.24 / $0.97 | $0.42 | ✅ 0.6s |
| Mistral Large 3 | `mistral.mistral-large-3-675b-instruct` | 9.3 | $0.50 / $1.50 | $0.75 | ✅ 0.9s |
| Llama 4 Scout 17B | `us.meta.llama4-scout-17b-instruct-v1:0` | 8.1 | $0.17 / $0.66 | $0.29 | catálogo `IMAGE`, no probada |
| NVIDIA Nemotron Nano 12B v2 VL | `nvidia.nemotron-nano-12b-v2` | 5.8-7.5 | $0.20 / $0.60 | $0.30 | ✅ 0.7s |
| Ministral 3 14B | `mistral.ministral-3-14b-instruct` | 6.0 | $0.20 / $0.20 | $0.20 | catálogo `IMAGE`, no probada |
| Gemma 3 27B | `google.gemma-3-27b-it` | 4.9 | $0.23 / $0.38 | $0.27 | ✅ 3.2s |

Con visión pero **más caros** que Haiku: Kimi K3 ($3.00/$16.50), Grok 4.6
($2.20/$6.60), Pixtral Large ($2.00/$6.00), Nova 2 Pro, GPT-5.4/5.5/Sol y toda la
familia Claude mayor que Haiku.

### 4.3 Sin visión en Bedrock (no usar en `vision` ni en clasificación)

Confirmado con **error real de Bedrock**: `minimax.minimax-m2.5`, `zai.glm-5` y
`moonshot.kimi-k2-thinking`. Rechazan el bloque `image`, y MiniMax también el
bloque `document`.

Declarados `TEXT` en `list-foundation-models`: MiniMax M2/M2.1, GLM 4.7 y 4.7
Flash, DeepSeek V3.2/R1, gpt-oss (todos), Qwen3 de texto (32B, Next, Coder),
Nemotron Super/Nano de texto, Devstral 2, Llama 3.x (excepto 3.2 11B/90B) y
Nova Micro.

---

## 5. Fuera de Bedrock (solo referencia)

Modelos con visión, a nivel de Haiku o por encima y más baratos, que **no están
en Bedrock `us-east-1`**. Usarlos implicaría otro proveedor/API. La visión viene
de OpenRouter/models.dev y **no se probó**.

| Modelo | Intel. (AA) | $ in / out | Blended |
|---|---:|---:|---:|
| GLM 5.3 Flash (Z AI) | 41.8 | $0.15 / $0.50 | $0.24 |
| Qwen3.8-Flash-Next (Alibaba) | 39.8 | $0.15 / $0.47 | $0.23 |
| MiMo-V2.6-Pro (Xiaomi) | 46.3 | $0.44 / $0.87 | $0.54 |
| DeepSeek V4.1 Flash | 24.7-39.5 | $0.30 / $1.20 | $0.53 |
| MiniMax-M3 | 29.2 | $0.30 / $1.20 | $0.53 |
| Gemini 3.5 Flash-Lite (Google) | 22.2 | $0.30 / $2.50 | $0.85 |
| Ling-3.0-flash-VL (InclusionAI) | 24.6 | $0.075 / $0.22 | $0.11 |
| Step 3.7 Flash (StepFun) | 19.5 | $0.20 / $1.15 | $0.44 |

Ojo: **MiniMax-M3 sí tiene visión** (a diferencia de M2.5). Si Bedrock lo agrega,
sería el reemplazo natural del modelo base, con visión incluida.

---

## 6. Hallazgo: la clasificación de uploads sin tipo falla con MiniMax

Las cuatro Lambdas `idp-classification-*` e `idp-processing-*` (dev y prod)
están desplegadas con `MODEL_ID=minimax.minimax-m2.5`. El motivo es que
`BEDROCK_CLASSIFICATION_MODEL_ID` no está definido y el stack cae a
`BEDROCK_MODEL_ID` (`document-validation-stack.ts:166`).

- Un upload con tipo (`raw/{TYPE}/...`) **no se ve afectado**: se salta la
  llamada al modelo (`index.py:281-294`).
- Un upload **sin tipo** manda el archivo como `image`/`document`
  (`index.py:364-365`) → `ValidationException`.
- **Pasó en producción/dev:** `idp-classification-...d95f3697-secured`,
  2026-09-01 17:23 UTC,
  `ValidationException: This model doesn't support documents.`
- `LATENCIA.md` §3 dice que ese caso "paga una llamada de visión real". En
  realidad **falla** mientras el modelo base sea MiniMax.

**Arreglo (sin código):** definir `BEDROCK_CLASSIFICATION_MODEL_ID` con un
modelo con visión y volver a desplegar. Clasificar es una tarea fácil, así que
basta un modelo barato de §4.2: `us.amazon.nova-2-lite-v1:0` (0.9s, $0.94
blended) o Haiku 4.5 si se prefiere no arriesgar.

---

## 7. Recomendación

| Uso | Hoy | Siguiente paso |
|---|---|---|
| Etapa `vision` (autenticidad) | Mantener **Haiku 4.5**: juzga firma/sello/impresión, y es rápido y estable | Pedir acceso a **GPT-6 Luna** en Bedrock → correr la suite de autenticidad de `CARTA_NOM35` con `BEDROCK_VISION_MODEL_ID=us.openai.gpt-6-luna`. Si empata, cuesta ~1/10 |
| Clasificación upstream | **Arreglar ya** (§6): `BEDROCK_CLASSIFICATION_MODEL_ID=us.amazon.nova-2-lite-v1:0` | — |
| Alternativa más barata sin trámite | Kimi K2.5 (0.55x, más inteligente) | Solo donde la latencia no importe: tardó 8x más que Haiku en la prueba y en `LATENCIA.md` §6.1 cambió 3 campos del CURP |

Antes de cambiar el modelo de cualquier etapa: `pricing.py` tiene que tener su
tarifa (`tests/unit/test_pricing_coverage.py`). Además hay que correr
`pnpm test:e2e:suite` y recalibrar `pnpm costos:validar` (ver `COSTOS.md` §9).

---

## 8. Cómo se verificó

| Fuente | Qué aportó |
|---|---|
| MCP `artificial-analysis` + su API v2 | Intelligence Index, precio de lista y velocidad. **No expone modalidad de input**: ni el MCP ni la API tienen el dato |
| `openrouter.ai/api/v1/models` y `models.dev/api.json` | `input_modalities`, cruzado por nombre contra los 469 modelos base de AA |
| `aws bedrock list-foundation-models` (`us-east-1`) | `inputModalities` oficial de cada modelo en Bedrock |
| AWS Price List API (`AmazonBedrock`, `AmazonBedrockFoundationModels`, `AmazonBedrockService`) | tarifas on-demand `standard` de `us-east-1` |
| **Prueba empírica** `Converse` | imagen PNG sintética con un folio aleatorio de 8 caracteres y dos importes; se pidió leer el folio y sumar el total. Todos los modelos marcados ✅ devolvieron folio y total correctos; los ❌ respondieron `ValidationException`. Costo total < $0.01 |
| CloudWatch Logs `idp-classification-*` (90 días) | la falla real de §6 |

La prueba de visión es **binaria**: confirma que el modelo ve la imagen y
lee texto limpio. **No mide calidad** en documentos reales (escaneos, sellos,
firmas). Para eso hay que usar la suite de autenticidad o
`pnpm bench:latencia`.
