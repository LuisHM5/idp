# Comparativa de precios — modelos candidatos en Amazon Bedrock

> Generado 2026-08-07, **expandido y cruzado con inteligencia el 2026-08-10**.
> Fuente de precios: AWS Pricing API (MCP `aws-billing` → `aws-pricing`,
> `get_pricing_from_api`). Fuente de inteligencia: MCP `artificial-analysis`
> (`list_models` / `get_model`, índice "Intelligence Index" de Artificial
> Analysis — más alto es más inteligente). Región: **us-east-1
> (N. Virginia)**, misma región que usa el stack desplegado (ver
> `AGENTS.md`). Tier **on-demand estándar** (no batch, no flex, no priority)
> salvo donde se indica lo contrario.
>
> **Desde 2026-10-15 el modelo frontera (base de texto y de visión) es
> Claude Haiku 5.5** — ver §0 abajo. Las secciones §0.1 en adelante son el
> análisis original de agosto, que usaba como baseline al modelo de entonces,
> **Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, ver
> `COSTOS.md` §1); se conservan como registro histórico — se usaba como **techo de precio** ($1.10 input /
> $5.50 output por 1M tokens, verificado en AWS Pricing API), **no como
> techo de inteligencia**. Es decir: se buscan modelos que cuesten _lo mismo
> o menos_ que Haiku 4.5, sin importar si son más o menos inteligentes —
> y luego se filtra por cuáles superan la inteligencia de Haiku 4.5 según
> Artificial Analysis (Intelligence Index = **29.9** para Claude Haiku 4.5,
> variante "Reasoning"/adaptive thinking, que es como corre este pipeline).

## 0. Modelo frontera: Claude Haiku 5.5 (2026-10-15)

`us.anthropic.claude-haiku-5-5` es el modelo base (`BEDROCK_MODEL_ID`) y el de
visión (`BEDROCK_VISION_MODEL_ID`), con effort `low` (`BEDROCK_EFFORT`). Es la
referencia contra la que se compara cualquier candidato nuevo: un modelo
tiene que ser más inteligente que Haiku 5.5 **al effort al que corre aquí** o
más barato, y aun así pasar la prueba contra ground truth de
`MODELOS_TEXTO.md` §3.

### 0.a Tarifas completas (Bedrock, us-east-1, por 1M tokens)

| Modelo | Input | Output | Input batch | Output batch | Cache write 5m | Cache write 1h | Cache read |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Claude Haiku 5.5** | **$0.11** | **$0.55** | N/A | N/A | $0.1375 | $0.22 | $0.011 |
| MiniMax M2.5 (costo completo) | $0.36 | $1.44 | — | — | sin caché | sin caché | sin caché |

- Fuente: consola de Bedrock, 2026-10-15. Para MiniMax el SKU `standard` del
  Price List API sigue diciendo $0.30 / $1.20; `pipeline/pricing.py` usa el
  costo completo ($0.36 / $1.44) para no sub-reportar.
- Las tarifas de Haiku 5.5 son para prompts de **hasta 100K tokens**; arriba
  de eso cobra 5x ($0.55 / $2.75). Los chunks de extracción (~28K
  caracteres) nunca llegan ahí.
- Haiku 5.5 usa un tokenizer nuevo: el mismo texto cuenta **~30% más
  tokens** que en Haiku 4.5. Ya está descontado al comparar costos por
  documento, no por token.
- Caché: desde 512 tokens de prefijo (Haiku 4.5 pedía 4,096), así que el
  prefijo de schema+reglas de casi cualquier tipo ya se cachea; el pipeline
  usa el TTL de 5 min (`cachePoint` `default`).

**Contra MiniMax M2.5** (el modelo desplegado hasta hoy): MiniMax cuesta
**3.3x** más en input y **2.6x** más en output; con la mezcla típica de
extracción (3 de input por 1 de output) son $0.22 contra $0.63 por millón,
**2.9x**. Ejemplo real — `test-docs/portal/constancias/España.pdf`
(`CONSTANCIA_SITUACION_FISCAL_PORTAL`, 2026-10-15): MiniMax facturó 16,916 /
2,883 tokens = **$0.0102** y tardó 73.5 s en extracción. Los mismos tokens a
tarifa de Haiku 5.5 son $0.0035 ($0.0045 si el tokenizer cuenta 30% más).

### 0.b Inteligencia (Artificial Analysis Intelligence Index v4.3.2, 15-oct-2026)

| Modelo / effort | Índice |
|---|---:|
| Claude Haiku 5.5 (max) | 43 |
| Claude Haiku 5.5 (xhigh) | 41 |
| Claude Haiku 5.5 (high) | 38 |
| Claude Haiku 5.5 (medium, default del modelo) | 34 |
| **Claude Haiku 5.5 (low, el de este pipeline)** | **29** |
| Claude Haiku 4.5 | 17 |
| Claude Haiku 4.5 (sin razonamiento) | 15 (estimado por AA) |

Incluso en `low`, Haiku 5.5 está muy por encima de Haiku 4.5 (17) y de MiniMax
M2.5 (22.8 en la misma escala v4, `MODELOS_TEXTO.md` §4). Ojo: los índices de
§0.1 en adelante (29.9 para Haiku 4.5, 34.5 para MiniMax) son de una versión
anterior del índice y **no se comparan** con esta tabla.

### 0.c Por qué `low` y cuándo subirlo

- Haiku 5.5 razona por defecto (effort `medium`). La extracción es una
  llamada con herramienta forzada: en ese caso el modelo responde con la
  herramienta directo, sin razonar, así que el effort casi no cambia ni
  latencia ni costo ahí. Sí pesa en las llamadas de texto libre
  (segmentación, recovery de Textract, localizador de páginas).
- `low` es el más barato y rápido. Subirlo por tipo con `model_effort` en el
  catálogo, o por etapa con `BEDROCK_<ETAPA>_EFFORT`, solo si una medición
  contra ground truth muestra que el tipo pierde campos en `low`.
- Cada nivel arriba gasta más tokens de salida (el razonamiento se cobra como
  output y cuenta contra `maxTokens`).

### 0.d Requisitos para invocarlo

1. Model ID del catálogo: `anthropic.claude-haiku-5-5`. `get-foundation-model`
   reporta `inferenceTypesSupported: INFERENCE_PROFILE`, por eso el pipeline lo
   invoca por el perfil `us.anthropic.claude-haiku-5-5` (también existe
   `global.`). Las tres formas tienen tarifa en `pricing.py` y las tres reciben
   el request adaptado.
2. **Acceso habilitado en la cuenta** (consola de Bedrock → Model access).
   Al 2026-10-15 `get-foundation-model-availability` devuelve
   `agreementAvailability: NOT_AVAILABLE` y cada llamada responde
   `AccessDeniedException ... is not available for this account` con las
   tres formas del ID (probado el mismo día); el
   pipeline cae a MiniMax por la cadena de fallback y lo reporta en
   `tracking.modelFallbacks`.
3. Request sin `temperature`/`topP` ni prefill, y con
   `output_config.effort` — lo arma `pipeline/bedrock.py::_adapt_request`
   solo para este modelo; MiniMax, Kimi y GLM reciben el request de siempre.

## 0.1 Hallazgo principal (agosto 2026, baseline Haiku 4.5)

Ninguno de los candidatos originales de este documento (Nova Pro/Lite/Micro,
Llama 3.3 70B, Qwen3 VL 235B, NVIDIA Nemotron Nano) **supera la inteligencia
de Haiku 4.5** — todos están muy por debajo (Intelligence Index 4.4–20.9 vs
29.9 de Haiku). Son más baratos, pero también claramente menos capaces; no
son "igual o más inteligentes" como pidió el usuario.

Cruzando el catálogo de Artificial Analysis contra el catálogo _en vivo_ de
Amazon Bedrock (`AmazonBedrock` → atributo `model`, us-east-1) sí aparecen
**4 modelos nuevos, ya disponibles en Bedrock, que superan la inteligencia
de Haiku 4.5 y cuestan menos** (verificado con AWS Pricing API, no
estimado):

| Modelo                             | Intelligence Index (AA) | vs Haiku 4.5 (29.9) | Precio in/out por 1M | vs techo de precio Haiku |
| ---------------------------------- | ----------------------: | ------------------: | -------------------: | -----------------------: |
| **GLM 5** (Z AI)                   |                    40.6 |                +36% |        $1.00 / $3.20 |   0.77x (23% más barato) |
| **Kimi K2.5** (Moonshot AI)        |                    36.0 |                +20% |        $0.60 / $3.00 |   0.55x (45% más barato) |
| **MiniMax M2.5**                   |                    34.5 |                +15% |        $0.30 / $1.20 |   0.25x (75% más barato) |
| **Kimi K2 Thinking** (Moonshot AI) |                    33.5 |                +12% |        $0.60 / $2.50 |   0.51x (49% más barato) |
| **Claude Haiku 4.5 (actual)**      |                **29.9** |                   — |    **$1.10 / $5.50** |                **1.00x** |

`Kimi K2.5` ya estaba en la lista original de candidatos por precio — lo
nuevo aquí es confirmar que **también es más inteligente** que Haiku 4.5
(dato que no estaba disponible sin el MCP de Artificial Analysis).

✅ **GLM 5, Kimi K2.5 y MiniMax M2.5 sí soportan visión** (input texto +
imagen, confirmado — no fallan en el paso de visión de este pipeline). La
inferencia por convención de nombres (`V`/`VL` como sufijo) que se usó
inicialmente para marcarlos como "no confirmado" era incorrecta para los
tres — Z AI, Moonshot AI y MiniMax no siguen esa convención de forma
consistente en toda su línea. **Sólo Kimi K2 Thinking sigue sin
confirmar** — no se ha verificado si acepta input de imagen; no asumir que
lo hereda de Kimi K2.5 sólo por ser de la misma familia.

GPT-5.6 Luna y Terra siguen siendo los más inteligentes del catálogo
completo (52.3 y 56.6), pero **sólo existen en AWS GovCloud (US-West)** —
no utilizables en este stack (us-east-1) sin migrar de región (ver §1 y
nota original más abajo).

### 0.1 ¿Hay algo parecido de NVIDIA o Google? (revisado 2026-08-10)

**NVIDIA:** un solo modelo de NVIDIA cumple intel ≥ Haiku y precio ≤ techo
de Haiku — **Nemotron 3 Ultra 550B A55B** (intel 38.3, $0.60/$2.75, 0.53x
el costo de Haiku). Pero **no está en el catálogo en vivo de Bedrock
todavía** (verificado 2026-08-10 contra `AmazonBedrock` → atributo
`model`) — sólo están disponibles sus hermanos menores ("Nemotron 3 Super
120B A12B", intel 25.7; "Nemotron Nano 3 30B", intel 14.5; "Nemotron Nano
2", intel 8.7), todos por debajo de Haiku. Es un candidato a **vigilar**,
no uno usable hoy.

**Google:** Google **no es partner de Amazon Bedrock** — ningún modelo
Gemini aparece en el catálogo de `AmazonBedrock` (sólo existen vía Vertex
AI / Google Cloud, una integración de proveedor completamente aparte, no
un cambio de `BEDROCK_MODEL_ID`). Aun así, dos sí cumplen intel+precio si
algún día se integra Vertex AI:

| Modelo | Intelligence Index | Input $/1M | Output $/1M | vs techo Haiku | Visión |
| --- | ---: | ---: | ---: | ---: | :---: |
| Gemini 3 Flash Preview (Reasoning) | 38.7 | $0.50 | $3.00 | 0.49x | ✅ nativa (todo Gemini es multimodal) |
| Gemini 3.5 Flash-Lite | 37.4 | $0.30 | $2.50 | 0.35x | ✅ nativa |

Gemini 3.5 Flash-Lite sería, en pura relación precio/inteligencia, uno de
los mejores candidatos de toda la comparativa — pero usarlo implica añadir
un proveedor nuevo al pipeline (Google Cloud SDK, auth GCP, cliente
distinto), no sólo cambiar el modelo. Precio reportado por Artificial
Analysis es pricing directo de Google (no hay equivalente en Bedrock que
verificar, porque no existe ahí).

## 1. Tabla maestra — inteligencia (Artificial Analysis) vs precio (AWS Bedrock, us-east-1)

Todos los precios de esta tabla son on-demand estándar, verificados vía AWS
Pricing API el 2026-08-07 (grupo original) y 2026-08-10 (grupo nuevo,
marcado 🆕). El Intelligence Index es de Artificial Analysis (variante
"Reasoning"/adaptive donde el modelo la ofrece, para ser comparable con
Haiku 4.5 corriendo en modo reasoning). Ordenado por inteligencia
descendente.

| Modelo                               | Bedrock model ID                              | Proveedor   | Intelligence Index | Input $/1M | Output $/1M | vs. Haiku 4.5 (precio) |              Input imagen              | Output |
| ------------------------------------ | --------------------------------------------- | ----------- | -----------------: | ---------: | ----------: | ---------------------: | :------------------------------------: | :----: |
| GPT-5.6 Terra ⚠️ GovCloud            | `openai.gpt-5.6-terra`                        | OpenAI      |               56.6 |      $3.30 |      $19.80 |                  3.25x |            ✅ texto+imagen             | texto  |
| GPT-5.6 Luna ⚠️ GovCloud             | `openai.gpt-5.6-luna`                         | OpenAI      |               52.3 |      $1.32 |       $7.92 |                  1.30x |            ✅ texto+imagen             | texto  |
| **GLM 5** 🆕                         | `zai.glm-5`                                   | Z AI        |               40.6 |      $1.00 |       $3.20 |                  0.77x |            ✅ texto+imagen             | texto  |
| Gemini 3 Flash Preview (Reasoning) 🆕 ⚠️ no Bedrock | — (sólo Vertex AI) | Google | 38.7 |      $0.50 |       $3.00 |                  0.49x |            ✅ texto+imagen             | texto  |
| Nemotron 3 Ultra 550B A55B 🆕 ⚠️ no Bedrock | — (aún no en catálogo) | NVIDIA | 38.3 |      $0.60 |       $2.75 |                  0.53x | ⚠️ no confirmado (probable sólo texto) | texto  |
| Gemini 3.5 Flash-Lite 🆕 ⚠️ no Bedrock | — (sólo Vertex AI)          | Google      |               37.4 |      $0.30 |       $2.50 |                  0.35x |            ✅ texto+imagen             | texto  |
| **Kimi K2.5**                        | `moonshotai.kimi-k2.5`                        | Moonshot AI |               36.0 |      $0.60 |       $3.00 |                  0.55x |            ✅ texto+imagen             | texto  |
| **MiniMax M2.5** 🆕                  | `minimax.minimax-m2.5`                        | MiniMax     |               34.5 |      $0.30 |       $1.20 |                  0.25x |            ✅ texto+imagen             | texto  |
| **Kimi K2 Thinking** 🆕              | `moonshotai.kimi-k2-thinking`                 | Moonshot AI |               33.5 |      $0.60 |       $2.50 |                  0.51x | ⚠️ no confirmado (probable sólo texto) | texto  |
| **Claude Haiku 4.5 (actual)**        | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Anthropic   |           **29.9** |  **$1.10** |   **$5.50** |              **1.00x** |            ✅ texto+imagen             | texto  |
| DeepSeek R1 🆕                       | `DeepSeek R1` (verificar ID exacto)           | DeepSeek    |               20.4 |      $1.35 |       $5.40 |                  1.12x |             ❌ sólo texto              | texto  |
| Qwen3 VL 235B A22B Instruct          | `qwen.qwen3-vl-235b-a22b-instruct`            | Qwen        |               20.9 |      $0.53 |       $2.66 |                  0.48x |          ✅ texto+imagen (VL)          | texto  |
| DeepSeek V3.2 🆕                     | `deepseek.v3.2`                               | DeepSeek    |               25.1 |      $0.62 |       $1.85 |                  0.47x |             ❌ sólo texto              | texto  |
| NVIDIA Nemotron 3 Super 120B A12B 🆕 | `nvidia.nemotron-super-3-120b`                | NVIDIA      |               25.7 |      $0.20 |       $0.80 |                  0.17x |             ❌ sólo texto              | texto  |
| NVIDIA Nemotron Nano 12B v2 VL       | `nvidia.nemotron-nano-12b-v2`                 | NVIDIA      |                8.8 |      $0.20 |       $0.60 |                  0.15x |          ✅ texto+imagen (VL)          | texto  |
| Llama 4 Maverick 17B 🆕              | `meta.llama4-maverick-17b`                    | Meta        |               14.5 |      $0.24 |       $0.97 |                  0.20x |            ✅ texto+imagen             | texto  |
| NVIDIA Nemotron 3 Nano 30B A3B 🆕    | `nvidia.nemotron-nano-3-30b`                  | NVIDIA      |               14.5 |      $0.06 |       $0.24 |                  0.05x |             ❌ sólo texto              | texto  |
| Amazon Nova 2 Lite                   | `amazon.nova-2-lite-v1:0`                     | Amazon      |               11.8 |      $0.33 |       $2.75 |                  0.38x |            ✅ texto+imagen             | texto  |
| Llama 3.3 70B Instruct               | `us.meta.llama3-3-70b-instruct-v1:0`          | Meta        |                9.3 |      $0.72 |       $0.72 |                  0.44x |             ❌ sólo texto              | texto  |
| Amazon Nova Pro                      | `amazon.nova-pro-v1:0`                        | Amazon      |                7.5 |      $0.80 |       $3.20 |                  0.67x |            ✅ texto+imagen             | texto  |
| Qwen3 235B A22B 2507 Instruct 🆕     | `qwen.qwen3-235b-a22b-2507`                   | Qwen        |               18.4 |      $0.22 |       $0.88 |                  0.18x |         ❌ sólo texto (no-VL)          | texto  |
| Amazon Nova Micro                    | `amazon.nova-micro-v1:0`                      | Amazon      |                4.4 |     $0.035 |       $0.14 |                  0.03x |             ❌ sólo texto              | texto  |

Notas de la tabla:

- **🆕** = modelo agregado en esta expansión (2026-08-10), no estaba en la
  versión anterior de este documento.
- **⚠️ no Bedrock** = cumple intel ≥ Haiku y precio ≤ techo de Haiku, pero
  **no es usable en este pipeline hoy**: Gemini (Google no es partner de
  Bedrock, requeriría integrar Vertex AI como proveedor nuevo) o Nemotron
  3 Ultra 550B A55B (todavía no aparece en el catálogo en vivo de
  Bedrock — candidato a vigilar, no a usar). Ver §0.1.
- Confirmado en el catálogo en vivo de `AmazonBedrock` (atributo `model`,
  84 valores, us-east-1) el 2026-08-10: `GLM 5`, `Kimi K2.5`,
  `Kimi K2 Thinking`, `MiniMax M2.5`, `DeepSeek v3.2`, `R1` (DeepSeek R1),
  `Qwen3 235B A22B 2507`, `NVIDIA Nemotron 3 Super 120B A12B`,
  `Nemotron Nano 3 30B`, `Llama 4 Maverick 17B` — todos con SKU real de
  precio en `us-east-1`, no estimados.
- Modelos que aparecían como candidatos en `model recomendation.md` pero
  **no están en el catálogo actual de Bedrock** (verificado 2026-08-10):
  `DeepSeek V4 Flash`, `MiniMax M3`, `Kimi K2.6`, `Qwen3.6 Plus`,
  `Nemotron 3 Ultra 550B A55B`, `Gemini 3 Flash` (Google no es partner de
  Bedrock — sólo Vertex AI). Se usaron sus equivalentes _sí_ disponibles en
  Bedrock (p. ej. MiniMax M2.5 en vez de M3, Kimi K2.5/K2 Thinking en vez
  de K2.6, Nemotron 3 Super/Nano en vez de Ultra).
- Columna "Input imagen": soporte de visión (input texto **e** imagen).
  Confirmado por nombre/documentación pública para Anthropic, Nova,
  Qwen3-**VL**, Nemotron-**VL**, Llama 4, GPT-5.6. **GLM 5, Kimi K2.5 y
  MiniMax M2.5 también confirmados** — soportan visión sin fallar, pese a
  no llevar sufijo `V`/`VL` (esa convención de nombres no aplica de forma
  consistente en toda la línea de Z AI/Moonshot AI/MiniMax, así que no
  sirve como señal confiable por sí sola). **Sólo Kimi K2 Thinking sigue
  sin confirmar** — ninguno de los dos MCP usados (`artificial-analysis`,
  `aws-billing`) expone modalidad de input, así que esto no puede
  verificarse por esa vía; sólo queda confirmar contra la documentación de
  Moonshot AI o probar directamente antes de descartar/elegir por este
  criterio. La columna "Output" es texto en todos los casos — es lo único
  que le importa a este pipeline (ver instrucción original del usuario).
- El Intelligence Index de Artificial Analysis no es directamente
  comparable 1:1 con "capacidad de extracción de campos de un pedimento" —
  es un índice agregado de benchmarks generales (razonamiento, código,
  matemáticas, etc.). Sigue siendo la señal más objetiva disponible sin
  correr los candidatos contra `tests/integration/expected_pedimentos.json`
  (ver §6).

## 2. Candidatos recomendados (igual o más inteligentes que Haiku 4.5, precio ≤ techo de Haiku)

Aplicando el filtro pedido — **inteligencia ≥ Haiku 4.5 (29.9) Y precio ≤
techo de Haiku 4.5 ($1.10 in / $5.50 out)** — sobre el catálogo _disponible
en Bedrock en us-east-1 hoy_, quedan exactamente estos 4, de mejor a peor
relación precio/inteligencia:

1. **MiniMax M2.5** — 34.5 (+15% vs Haiku), $0.30/$1.20 (**75% más barato**
   que Haiku). Mejor relación precio/inteligencia del grupo.
2. **Kimi K2 Thinking** — 33.5 (+12%), $0.60/$2.50 (49% más barato).
3. **Kimi K2.5** — 36.0 (+20%), $0.60/$3.00 (45% más barato). Ya estaba en
   la lista original por precio; ahora se confirma que también es más
   inteligente.
4. **GLM 5** — 40.6 (+36%, el más inteligente del grupo que sigue bajo el
   techo de precio), $1.00/$3.20 (23% más barato).

**GLM 5, Kimi K2.5 y MiniMax M2.5 tienen visión confirmada** (input texto
+ imagen) — de los 4, son los tres listos para probarse contra el paso de
visión de este pipeline sin bloqueos conocidos. **Sólo Kimi K2 Thinking
sigue sin confirmar** (ver §1 y §5) — verificar antes de adoptarlo.

Si se acepta salir de us-east-1 (GovCloud), **GPT-5.6 Luna** (52.3,
+75% de inteligencia, con visión confirmada) sería el candidato más fuerte
en inteligencia pura, a 1.30x el precio de Haiku — pero implica migrar
región, algo fuera del alcance de esta comparativa.

Si se acepta salir de Bedrock por completo, **Gemini 3.5 Flash-Lite**
(intel 37.4, $0.30/$2.50, 0.35x el precio de Haiku, visión nativa) es la
mejor relación precio/inteligencia de *toda* la comparativa — mejor que
MiniMax M2.5 en inteligencia a un precio casi igual de bajo. El obstáculo
no es el modelo, es que Google no está en Bedrock: implica añadir Vertex
AI como proveedor nuevo al pipeline. Ver §0.1.

## 3. Tiers alternativos (flex / priority) — grupo nuevo

Igual que en la comparativa original, algunos modelos nuevos también
ofrecen tiers **flex** (más barato, sin SLA de latencia) y **priority**
(más caro, con SLA). GLM 5, Kimi K2.5, Kimi K2 Thinking y MiniMax M2.5 los
tienen todos en us-east-1:

| Modelo           | Tier     | Input $/1M | Output $/1M |
| ---------------- | -------- | ---------: | ----------: |
| GLM 5            | flex     |      $0.50 |       $1.60 |
| GLM 5            | standard |      $1.00 |       $3.20 |
| GLM 5            | priority |      $1.75 |       $5.60 |
| Kimi K2.5        | flex     |      $0.30 |       $1.50 |
| Kimi K2.5        | standard |      $0.60 |       $3.00 |
| Kimi K2.5        | priority |      $1.05 |       $5.25 |
| Kimi K2 Thinking | flex     |      $0.30 |       $1.25 |
| Kimi K2 Thinking | standard |      $0.60 |       $2.50 |
| Kimi K2 Thinking | priority |      $1.05 |       $4.38 |
| MiniMax M2.5     | flex     |      $0.15 |       $0.60 |
| MiniMax M2.5     | standard |      $0.30 |       $1.20 |
| MiniMax M2.5     | priority |     $0.525 |       $2.10 |

Ver la comparativa original (Qwen3 VL 235B, Kimi K2.5 previo, NVIDIA
Nemotron Nano) en la versión anterior de este documento para esos tiers —
no cambiaron.

## 4. Costo estimado por documento (mezcla real de tokens de este pipeline)

Misma metodología que la versión anterior: **6,600 input / 950 output
tokens** por documento (mezcla medida para un pedimento nativo de 3
páginas, `COSTOS.md` §0 Caso A). Sólo costo del _modelo_; no incluye
Textract, Lambda ni el piso fijo de infraestructura
(`COSTOS.md` §6, $0.0085/página).

| Modelo                               |  $/documento | $/mes (2,430 docs) | Intelligence Index |
| ------------------------------------ | -----------: | -----------------: | -----------------: |
| Amazon Nova Micro                    |     $0.00036 |              $0.88 |                4.4 |
| NVIDIA Nemotron 3 Nano 30B A3B 🆕    |     $0.00062 |              $1.52 |               14.5 |
| NVIDIA Nemotron 3 Super 120B A12B 🆕 |     $0.00208 |              $5.05 |               25.7 |
| Qwen3 235B A22B 2507 🆕              |     $0.00229 |              $5.56 |               18.4 |
| Llama 4 Maverick 17B 🆕              |     $0.00251 |              $6.09 |               14.5 |
| NVIDIA Nemotron Nano 2 VL            |     $0.00189 |              $4.59 |                8.8 |
| Amazon Nova 2 Lite                   |     $0.00479 |             $11.64 |               11.8 |
| Llama 3.3 70B Instruct               |     $0.00544 |             $13.21 |                9.3 |
| Qwen3 VL 235B A22B Instruct          |     $0.00603 |             $14.64 |               20.9 |
| DeepSeek V3.2 🆕                     |     $0.00585 |             $14.21 |               25.1 |
| **MiniMax M2.5 🆕**                  | **$0.00312** |          **$7.58** |           **34.5** |
| **Kimi K2 Thinking 🆕**              | **$0.00634** |         **$15.39** |           **33.5** |
| Kimi K2.5                            |     $0.00681 |             $16.55 |               36.0 |
| Amazon Nova Pro                      |     $0.00832 |             $20.22 |                7.5 |
| **GLM 5 🆕**                         | **$0.00964** |         **$23.41** |           **40.6** |
| **Claude Haiku 4.5 (actual)**        | **$0.01249** |         **$30.34** |           **29.9** |
| DeepSeek R1 🆕                       |     $0.01404 |             $34.10 |               20.4 |
| GPT-5.6 Luna ⚠️ GovCloud only        |     $0.01624 |             $39.45 |               52.3 |
| GPT-5.6 Terra ⚠️ GovCloud only       |     $0.04059 |             $98.63 |               56.6 |

Filas en **negrita** = los 4 candidatos que cumplen "igual o más
inteligente que Haiku 4.5 Y precio ≤ techo de Haiku" (§2). Nótese que
**DeepSeek R1 cuesta más que Haiku 4.5 y es menos inteligente** — está
dominado por Haiku en ambos ejes, no es un candidato viable pese a estar
en el catálogo de Bedrock.

## 5. Lo que esta comparativa NO cubre

- **Soporte real de vision para Kimi K2 Thinking.** GLM 5, Kimi K2.5 y
  MiniMax M2.5 ya están confirmados (input texto+imagen, sin fallos). Para
  Kimi K2 Thinking, ninguno de los dos MCP usados (`artificial-analysis`,
  `aws-billing`) expone modalidad de input de forma confiable — no hay
  campo de modalidad en el schema de ninguno de los dos, se verificó
  explícitamente. Confirmar contra la documentación de Moonshot AI, o
  probar directamente, antes de considerarlo como reemplazo de Haiku 4.5.
- **Soporte de structured output / tool use en Bedrock Converse**, igual
  que en la versión anterior — sigue sin investigarse
  (`https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html`).
  Relevante porque el pipeline pide campos JSON exactos del catálogo.
- **Calidad de extracción real.** El Intelligence Index de Artificial
  Analysis es un promedio de benchmarks generales (razonamiento, código,
  matemáticas, instrucciones) — no mide específicamente extracción de
  campos de documentos aduaneros. Un modelo más "inteligente" en este
  índice no garantiza mejor exactitud contra
  `tests/integration/expected_pedimentos.json`. Cualquier candidato de §2
  necesita correrse contra `pnpm test:pedimentos` antes de considerarse
  viable, igual que se advertía en la versión anterior de este documento.
- **Disponibilidad real sin cross-region inference profile / model
  access.** Los model ID de la tabla maestra para los 4 candidatos nuevos
  (`zai.glm5`, `moonshotai.kimi-k2.5`, `moonshotai.kimi-k2-thinking`,
  `minimax.minimax-m2.5`) se derivaron del campo `usagetype` de los SKU de
  AWS Pricing API, no de una llamada a `bedrock:ListFoundationModels` —
  confirmar el ID exacto (sufijos de versión tipo `-v1:0`) y que el
  modelo esté habilitado (model access) en la cuenta, en la consola de
  Bedrock, antes de usarlos en código.
- **Precio de GLM 5, Kimi K2.5/K2 Thinking, MiniMax M2.5 fuera de Bedrock.**
  Artificial Analysis reporta pricing directo del proveedor para algunos de
  estos modelos que **no coincide** con el de Bedrock en varios casos (ver
  ejemplo de GPT-5.6 Luna abajo) — donde ambas fuentes coincidieron
  exactamente (GLM 5, Kimi K2.5, Kimi K2 Thinking, MiniMax M2.5, NVIDIA
  Nemotron Nano 12B v2 VL) se usó el precio de Bedrock como autoritativo
  para esta tabla, consistente con que el pipeline corre en Bedrock.

## 6. Fuente de los datos

Precios de los primeros 10 modelos (tabla original) obtenidos en vivo el
2026-08-07; precios de los 9 modelos agregados en esta expansión (🆕)
obtenidos en vivo el 2026-08-10 — ambos vía MCP `aws-billing` →
`aws-pricing`, operación `get_pricing_from_api`, service code
`AmazonBedrock` (modelos de terceros/Amazon) y `AmazonBedrockService`
(Claude, vía Bedrock Mantle), filtrando `location: "US East (N. Virginia)"`.
No son estimaciones — son los mismos SKU y `pricePerUnit` que factura AWS,
igual que `documentacion/COSTOS.md` §1 verificó para Claude Haiku 4.5
contra Cost Explorer al centavo.

Intelligence Index de todos los modelos obtenido en vivo el 2026-08-10 vía
MCP `artificial-analysis` → `list_models` / `get_model`. Donde el proveedor
ofrece variantes "Reasoning"/adaptive y "Non-reasoning", se usó la variante
Reasoning para ser comparable con cómo corre Haiku 4.5 en este pipeline
(adaptive thinking). Los precios que reporta Artificial Analysis (pricing
directo del proveedor, no necesariamente Bedrock) se usaron sólo como
referencia cruzada — donde había una entrada equivalente confirmada en
Bedrock, el precio de Bedrock es el que aparece en las tablas de este
documento, porque es el que factura realmente este pipeline.

⚠️ **Nota GPT-5.6 Luna/Terra:** el precio de Bedrock GovCloud ($1.32/$7.92
y $3.30/$19.80) es muy distinto del precio directo de OpenAI que reporta
Artificial Analysis ($0.20/$1.20 y $2.00/$12.00) — son canales de
distribución distintos con pricing distinto. Esta tabla usa el precio de
Bedrock GovCloud (verificado previamente contra AWS Pricing API, ver
versión anterior de este documento) porque es el único canal relevante
para un stack en AWS, aunque GovCloud en sí no es usable desde us-east-1
sin migrar de región.

## 7. Recomendación final — calidad/precio

> **Actualizado 2026-08-10**: MiniMax M2.5 confirmó soporte de visión
> (texto+imagen, sin fallar) — entra a esta comparación de pleno derecho.
> Sólo Kimi K2 Thinking sigue sin confirmar y queda fuera.

Restringiendo a lo que es realmente usable *hoy* en este pipeline (en
Bedrock us-east-1, con visión confirmada) quedan tres candidatos frente al
baseline: **GLM 5**, **Kimi K2.5** y **MiniMax M2.5**. Todo lo demás está
descartado por alguna razón dura: Kimi K2 Thinking (visión sin confirmar),
Nemotron 3 Ultra/Gemini (no están en Bedrock), GPT-5.6 (GovCloud only).

Comparar sólo por Intelligence Index agregado esconde algo relevante para
esta tarea específica — desglosando los benchmarks de Artificial Analysis
que más se parecen a "extraer campos exactos de un documento" (no
matemáticas/código, que aquí no aplican):

| Benchmark | Qué mide | Claude Haiku 4.5 | GLM 5 | Kimi K2.5 | MiniMax M2.5 |
| --- | --- | ---: | ---: | ---: | ---: |
| GPQA (conocimiento/razonamiento) | precisión en preguntas de nivel experto | 0.672 | 0.820 | **0.879** | 0.848 |
| HLE (razonamiento difícil) | precisión en tareas de razonamiento exigente | 0.104 | 0.293 | **0.307** | 0.205 |
| IFBench (seguir instrucciones exactas) | qué tan literal sigue el formato pedido | 0.543 | 0.723 | 0.702 | **0.716** |
| Tau2 (tool-use / salida estructurada) | fiabilidad en llamadas a herramientas / formato rígido | 0.547 | **0.982** | 0.959 | 0.953 |
| Precio por documento (mezcla real) | costo directo | $0.01249 | $0.00964 | $0.00681 | **$0.00312** |

**Elegiría MiniMax M2.5.** Razones, en orden de peso:

1. **Es, por lejos, el más barato de los tres** — $0.00312/doc: 54% más
   barato que Kimi K2.5, 68% más barato que GLM 5, 75% más barato que
   Haiku 4.5. Nada más en la tabla se le acerca en precio.
2. **Ese ahorro casi no cuesta calidad en los benchmarks que importan
   aquí.** Frente a Kimi K2.5 (el segundo más barato): GPQA 0.848 vs 0.879
   (-3.5%), IFBench 0.716 vs 0.702 (MiniMax **gana**), Tau2 0.953 vs 0.959
   (prácticamente empatados). La única caída real es HLE (0.205 vs 0.307,
   -33%) — y HLE mide razonamiento de nivel "examen más difícil del mundo"
   en dominios generales, el benchmark menos parecido a "leer un pedimento
   y llenar un JSON" de toda la tabla.
3. **Tau2 (0.953) sigue casi 2x el de Haiku 4.5 (0.547)** — misma lectura
   que con Kimi K2.5/GLM 5: buena señal de fiabilidad en salida
   estructurada, no una prueba directa de `output_config.format` en
   Bedrock Converse (sigue sin verificarse, ver §5).
4. Si el criterio fuera "máxima inteligencia general sin ver el costo
   extra", **GLM 5** (Intelligence Index 40.6, el más alto de los tres, y
   el que gana en Tau2) seguiría siendo la opción — y si se quiere más
   margen específicamente en razonamiento difícil (HLE), **Kimi K2.5** es
   el punto medio. MiniMax M2.5 es la apuesta cuando el costo por documento
   pesa tanto como la inteligencia, que es como está planteada esta
   comparativa desde el principio (techo de precio de Haiku).

**Orden de respaldo si MiniMax M2.5 no funciona bien en la prueba real:**
1. MiniMax M2.5 (mejor precio/calidad) → 2. Kimi K2.5 (mejor equilibrio,
más margen en HLE) → 3. GLM 5 (mayor inteligencia agregada y mejor Tau2,
al precio más alto de los tres) → 4. Claude Haiku 4.5 (baseline conocido).

**Lo que esto NO reemplaza:** ninguno de los tres se ha corrido contra
`tests/integration/expected_pedimentos.json`, y el soporte de
`output_config.format`/structured output en Bedrock Converse sigue sin
confirmarse para ninguno de los tres (§5) — esta recomendación es la mejor
apuesta *por especificaciones publicadas*, no un reemplazo de la prueba
real antes de mover producción. Dado lo barato que es MiniMax M2.5, es el
primer candidato razonable para esa prueba.

### 7.1 Refinamiento — enfocado específicamente en extracción + validación

La comparación de arriba usa GPQA/HLE como proxy de "inteligencia
general". Para el caso de uso concreto de este pipeline —
**extracción de campos de un documento + validación contra el
catálogo**, no razonamiento abierto — hay tres benchmarks de Artificial
Analysis más cercanos a la tarea real:

| Benchmark | Por qué aplica a extracción+validación |
| --- | --- |
| **LCR** (Long Context Retrieval) | encontrar el dato correcto dentro de un contexto largo — análogo directo a "leer el documento y sacar el campo correcto" |
| **Tau2** (tool-use / salida estructurada) | sostener un formato rígido en una llamada estructurada — análogo directo a devolver el JSON exacto del catálogo |
| **IFBench** (seguir instrucciones al pie de la letra) | análogo directo a las reglas de validación (p. ej. "si el campo está vacío, no inventes un valor") |

| | Claude Haiku 4.5 | GLM 5 | Kimi K2.5 | MiniMax M2.5 |
| --- | ---: | ---: | ---: | ---: |
| LCR (encontrar el dato) | **0.737** | 0.707 | 0.730 | 0.720 |
| Tau2 (formato rígido) | 0.547 | **0.982** | 0.959 | 0.953 |
| IFBench (seguir reglas) | 0.543 | 0.723 | 0.702 | **0.716** |
| Promedio de los 3 | 0.609 | **0.804** | 0.797 | 0.796 |
| $/documento | $0.01249 | $0.00964 | $0.00681 | **$0.00312** |

**Hallazgo:** en este compuesto — el más relevante para la tarea real —
GLM 5, Kimi K2.5 y MiniMax M2.5 quedan **prácticamente empatados** (0.804
/ 0.797 / 0.796, <1% de diferencia entre ellos), y los tres superan a
Haiku 4.5 (0.609) por un margen que sí es real. Con la calidad
esencialmente empatada entre los tres candidatos, el precio deja de ser
un desempate menor y pasa a ser el criterio que decide: **elegir MiniMax
M2.5 no sacrifica precisión medible frente a GLM 5/Kimi K2.5 en LCR,
Tau2 ni IFBench**, y sí ahorra 54–68% frente a ellos.

Matiz importante para la parte de **validación** específicamente: si las
reglas de negocio del catálogo requieren razonamiento más allá de "sigue
el formato" (casos ambiguos, cruces de campos, interpretación de códigos),
GPQA vuelve a ser relevante — ahí Kimi K2.5 (0.879) le saca ventaja a
MiniMax M2.5 (0.848), y sería el respaldo natural si MiniMax falla en
casos de validación complejos.

**Límite real de este refinamiento:** ninguno de LCR/Tau2/IFBench mide
extracción visual de documentos ni tasa de alucinación en campos
vacíos/ilegibles, que es el riesgo que más importa en "validación" para
este pipeline — eso no lo cubre ningún benchmark de Artificial Analysis,
sólo `pnpm test:pedimentos` contra `expected_pedimentos.json` lo
confirma.
