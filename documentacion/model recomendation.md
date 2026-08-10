us.anthropic.claude-haiku-4-5-20251001-v1:0
qwen.qwen3-vl-235b-a22b-instruct
amazon.nova-pro-v1:0

# BEDROCK_MODEL_ID=amazon.nova-pro-v1:0

# BEDROCK_MODEL_ID=us.meta.llama3-3-70b-instruct-v1:0

Amazon Nova 2 Lite 0,33 USD 2,75 USD
Amazon Nova Micro 0,035 USD 0,14 USD

bedrockmantle
gpt-5.6 Luna

Nova Lite 2: amazon.nova-2-lite-v1:0
Nova Micro: amazon.nova-micro-v1:0

GTP-5.6 Luna
GTP-5.6 Terra

# Chinos

Qwn 3 VL 235B A22B. qwen.qwen3-vl-235b-a22b
Kimi 2.5K: moonshotai.kimi-k2.5

NVIDIA NEMOTRON 12B v2 VL BF16: nvidia.nemotron-nano-12b-v2

https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html

# Comparativa inteligencia vs costo (2026-08-10, ver documentacion/models_pricing.md)

Baseline: Claude Haiku 4.5 = Intelligence Index 29.9 (Artificial Analysis),
techo de precio $1.10 in / $5.50 out por 1M tokens (Bedrock, us-east-1).
Ninguno de los modelos de arriba (Nova, Llama 3.3, Qwen3 VL, Nemotron Nano)
supera esa inteligencia — todos entre 4.4 y 20.9. Kimi K2.5 sí la supera
(36.0) y ya estaba en la lista.

Nuevos candidatos que SÍ superan la inteligencia de Haiku 4.5 y cuestan
menos, confirmados en el catálogo de Bedrock us-east-1:

GLM 5 (Z AI): zai.glm-5 — intel 40.6 (+36%), $1.00 in / $3.20 out (0.77x) — visión ✅ confirmada
Kimi K2.5: moonshotai.kimi-k2.5 — intel 36.0 (+20%), $0.60 in / $3.00 out (0.55x) — visión ✅ confirmada
MiniMax M2.5: minimax.minimax-m2.5 — intel 34.5 (+15%), $0.30 in / $1.20 out (0.25x) [mejor relación precio/inteligencia] — visión ⚠️ sin confirmar
Kimi K2 Thinking: moonshotai.kimi-k2-thinking — intel 33.5 (+12%), $0.60 in / $2.50 out (0.51x) — visión ⚠️ sin confirmar

GLM 5 y Kimi K2.5 sí soportan visión (input texto+imagen, sin fallar) —
la sospecha inicial de "probablemente sólo texto" por convención de
nombres (V/VL) era incorrecta para estos dos. Kimi K2 Thinking y
MiniMax M2.5 siguen sin confirmar — ningún MCP consultado
(artificial-analysis, aws-billing) expone modalidad de input, así que hay
que verificarlo contra la documentación del proveedor o probando
directamente.

Más inteligente que todo lo anterior (GPT-5.6 Luna, intel 52.3; GPT-5.6
Terra, intel 56.6) pero sólo existen en AWS GovCloud (US-West), no en
us-east-1 — no usables sin migrar de región.

Evaluados pero descartados por ser menos inteligentes que Haiku (aunque
más baratos): DeepSeek V3.2, DeepSeek R1, Qwen3 235B A22B 2507, NVIDIA
Nemotron 3 Super 120B A12B, NVIDIA Nemotron 3 Nano 30B A3B, Llama 4
Maverick 17B. DeepSeek R1 además sale MÁS caro que Haiku ($1.35/$5.40) —
descartado en ambos ejes.

No en el catálogo de Bedrock todavía (existen según Artificial Analysis
pero no aparecen en AmazonBedrock/us-east-1 al 2026-08-10): DeepSeek V4
Flash, MiniMax M3, Kimi K2.6, Qwen3.6 Plus, Nemotron 3 Ultra 550B A55B.
Gemini 3 Flash tampoco — Google no es partner de Bedrock (sólo Vertex AI).
