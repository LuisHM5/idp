# Modelos ganadores calidad precio

1. minimax.minimax-m2.5
2. moonshotai.kimi-k2.5
3. zai.glm-5
4. us.anthropic.claude-haiku-4-5-20251001-v1:0

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

# Respuesta a "¿hay algo parecido de Google o NVIDIA?" (2026-08-10)

NVIDIA: sólo Nemotron 3 Ultra 550B A55B cumple (intel 38.3, $0.60 in /
$2.75 out, 0.53x) — pero no está en Bedrock todavía, sólo vigilar.
Los Nemotron que SÍ están en Bedrock hoy (Super 120B, Nano 3 30B, Nano 2)
están todos por debajo de Haiku en inteligencia.

Google: dos cumplen intel+precio, ambos con visión nativa —
Gemini 3 Flash (Reasoning): intel 38.7, $0.50 in / $3.00 out (0.49x)
Gemini 3.5 Flash-Lite: intel 37.4, $0.30 in / $2.50 out (0.35x) — mejor
relación precio/inteligencia de toda la comparativa
Pero Google no está en Bedrock — usar cualquiera de los dos implica meter
Vertex AI como proveedor nuevo (SDK y auth distintos), no cambiar sólo el
model ID.

# Veredicto calidad/precio — ACTUALIZADO (2026-08-10, detalle en §7 de models_pricing.md)

MiniMax M2.5 confirmó visión → entra al juego. Ahora son 3: GLM 5, Kimi
K2.5, MiniMax M2.5.

Elegiría **MiniMax M2.5**: es, por lejos, el más barato ($0.00312/doc —
54% menos que Kimi K2.5, 68% menos que GLM 5), y ese ahorro casi no cuesta
calidad en lo que importa aquí: GPQA 0.848 (vs 0.879 Kimi, -3.5%), IFBench
0.716 (le GANA a Kimi K2.5, 0.702), Tau2 0.953 (casi empatado con Kimi
K2.5, 0.959, y casi 2x Haiku 4.5). Sólo cae fuerte en HLE (0.205 vs 0.307
Kimi) — pero HLE mide razonamiento tipo "examen más difícil del mundo",
lo menos parecido a leer un pedimento de toda la tabla.

Orden de respaldo si MiniMax M2.5 falla en la prueba real:

1. MiniMax M2.5 (mejor precio/calidad)
2. Kimi K2.5 (mejor equilibrio, más margen en HLE)
3. GLM 5 (mayor inteligencia agregada, mejor Tau2, el más caro de los 3)
4. Claude Haiku 4.5 (baseline conocido)

Pendiente antes de mover producción: correr contra
tests/integration/expected_pedimentos.json y confirmar
output_config.format / structured output en Bedrock Converse — ninguno de
los 3 se ha probado en la tarea real todavía. Por lo barato que es,
MiniMax M2.5 es el primer candidato razonable para esa prueba.

# Refinamiento enfocado en extracción+validación (2026-08-10, detalle en §7.1 de models_pricing.md)

En vez de GPQA/HLE (inteligencia general), usar los 3 benchmarks más
parecidos a la tarea real: LCR (encontrar el dato en contexto largo),
Tau2 (sostener formato JSON rígido), IFBench (seguir reglas al pie de la
letra).

Promedio de los 3: Haiku 4.5 = 0.609, GLM 5 = 0.804, Kimi K2.5 = 0.797,
MiniMax M2.5 = 0.796 — GLM5/Kimi/MiniMax quedan prácticamente empatados
(<1% entre ellos), los 3 muy por encima de Haiku.

Con calidad empatada entre los 3 candidatos, el precio decide: MiniMax
M2.5 sigue siendo la mejor elección (mismo nivel en LCR/Tau2/IFBench,
54-68% más barato).

Matiz para validación con reglas de negocio complejas (no sólo formato):
ahí Kimi K2.5 gana en GPQA (0.879 vs 0.848) — sería el respaldo si
MiniMax falla en casos de validación difíciles.

Ningún benchmark mide alucinación en campos vacíos/ilegibles ni
extracción visual real — sólo pnpm test:pedimentos lo confirma.
