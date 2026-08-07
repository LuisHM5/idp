# Comparativa de precios — modelos candidatos en Amazon Bedrock

> Generado 2026-08-07 a partir de `documentacion/model recomendation.md`
> (lista de modelos candidatos) cruzado con **precios reales en vivo**
> obtenidos vía la AWS Pricing API (MCP `aws-billing` → `aws-pricing`,
> `get_pricing_from_api`). Región: **us-east-1 (N. Virginia)**, misma región
> que usa el stack desplegado (ver `AGENTS.md`). Tier **on-demand estándar**
> (no batch, no flex, no priority) salvo donde se indica lo contrario.
> Metodología igual a la ya usada en `documentacion/COSTOS.md` §0/§1.
>
> El modelo actualmente en producción es **Claude Haiku 4.5**
> (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, ver
> `pipeline/pricing.py` y `COSTOS.md` §1) — se incluye aquí como línea base
> para comparar el resto.

## 1. Tabla de precios (on-demand estándar, us-east-1)

| Modelo | Bedrock model ID | Proveedor | Input $/1M tok | Output $/1M tok | vs. Haiku 4.5 |
| --- | --- | --- | ---: | ---: | ---: |
| Amazon Nova Micro | `amazon.nova-micro-v1:0` | Amazon | $0.035 | $0.14 | 0.03x |
| NVIDIA Nemotron Nano 12B v2 VL | `nvidia.nemotron-nano-12b-v2` | NVIDIA | $0.20 | $0.60 | 0.15x |
| Amazon Nova 2 Lite | `amazon.nova-2-lite-v1:0` | Amazon | $0.33 | $2.75 | 0.38x |
| Llama 3.3 70B Instruct | `us.meta.llama3-3-70b-instruct-v1:0` | Meta | $0.72 | $0.72 | 0.44x |
| Qwen3 VL 235B A22B Instruct | `qwen.qwen3-vl-235b-a22b-instruct` | Qwen | $0.53 | $2.66 | 0.48x |
| Kimi K2.5 | `moonshotai.kimi-k2.5` | Moonshot AI | $0.60 | $3.00 | 0.55x |
| Amazon Nova Pro | `amazon.nova-pro-v1:0` | Amazon | $0.80 | $3.20 | 0.67x |
| **Claude Haiku 4.5 (actual)** | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Anthropic | **$1.10** | **$5.50** | **1.00x** |
| GPT-5.6 Luna ⚠️ | `openai.gpt-5.6-luna` | OpenAI | $1.32 | $7.92 | 1.30x |
| GPT-5.6 Terra ⚠️ | `openai.gpt-5.6-terra` | OpenAI | $3.30 | $19.80 | 3.25x |

⚠️ **GPT-5.6 Luna/Terra sólo existen en `AWS GovCloud (US-West)`** — la
AWS Pricing API no devuelve ningún SKU para ellos en `us-east-1` (ni en
ninguna región comercial). El stack de este proyecto corre en `us-east-1`,
así que estos dos modelos **no están disponibles** para este pipeline sin
migrar a GovCloud. Se incluyen sólo por completitud de la lista original.

Orden alfabético de referencia rápida (`model recomendation.md`
→ nombre en el catálogo de precios de AWS, cuando difiere):

- `qwen.qwen3-vl-235b-a22b-instruct` → catálogo de precios lo lista como
  **"Qwen3 VL 235B A22B"** (sin el sufijo `-instruct`).
- `nvidia.nemotron-nano-12b-v2` → catálogo de precios lo lista como
  **"NVIDIA Nemotron Nano 2 VL"**; es el mismo modelo que "NVIDIA NEMOTRON
  12B v2 VL BF16" en `model recomendation.md`.
- `amazon.nova-2-lite-v1:0` → catálogo de precios lo lista como
  **"Nova 2.0 Lite"**. El precio confirma exactamente la nota manual del
  archivo original ("Amazon Nova 2 Lite 0,33 USD 2,75 USD").
- `amazon.nova-micro-v1:0` → **"Nova Micro"**; también confirma la nota
  manual original ("Amazon Nova Micro 0,035 USD 0,14 USD").

## 2. Cache (sólo modelos con pricing de cache separado)

| Modelo | Cache read $/1M | Cache write $/1M (5 min) | Cache write $/1M (1h) |
| --- | ---: | ---: | ---: |
| Claude Haiku 4.5 | $0.11 | $1.375 | $2.20 |

Ninguno de los otros modelos de la tabla 1 expone SKUs de prompt caching
en `us-east-1` bajo `AmazonBedrock` — si el candidato no aparece aquí, no
tiene esa opción de ahorro (o AWS aún no la factura por separado ahí).

## 3. Tiers alternativos (flex / priority) — sólo donde existen

Algunos modelos ofrecen tiers de servicio distintos al estándar: **flex**
(más barato, latencia no garantizada) y **priority** (más caro, SLA de
latencia). Claude Haiku 4.5, Nova Pro/Micro/2 Lite y Llama 3.3 70B **no**
tienen estas variantes en el catálogo — sólo standard y batch.

| Modelo | Tier | Input $/1M | Output $/1M |
| --- | --- | ---: | ---: |
| Qwen3 VL 235B A22B | flex | $0.26 | $1.33 |
| Qwen3 VL 235B A22B | standard | $0.53 | $2.66 |
| Qwen3 VL 235B A22B | priority | $0.93 | $4.66 |
| Kimi K2.5 | flex | $0.30 | $1.50 |
| Kimi K2.5 | standard | $0.60 | $3.00 |
| Kimi K2.5 | priority | $1.05 | $5.25 |
| NVIDIA Nemotron Nano 2 VL | flex | $0.10 | $0.30 |
| NVIDIA Nemotron Nano 2 VL | standard | $0.20 | $0.60 |
| NVIDIA Nemotron Nano 2 VL | priority | $0.35 | $1.05 |

## 4. Costo estimado por documento (mezcla real de tokens de este pipeline)

Usando la mezcla de tokens **medida** para un pedimento nativo de 3
páginas (Caso A, `COSTOS.md` §0: ~6,483–6,742 input / ~883–1,054 output
con Claude Haiku 4.5) — se toma **6,600 input / 950 output tokens** como
representativo. Esto es sólo el costo del *modelo* (extracción LLM); no
incluye Textract, Lambda ni el piso fijo de infraestructura (ver
`COSTOS.md` §6 para el costo total por página ya acordado, $0.0085/página).

| Modelo | $/documento (6,600 in / 950 out) | $/mes (2,430 docs, sólo LLM) |
| --- | ---: | ---: |
| Amazon Nova Micro | $0.00036 | $0.88 |
| NVIDIA Nemotron Nano 2 VL | $0.00189 | $4.59 |
| Amazon Nova 2 Lite | $0.00479 | $11.64 |
| Llama 3.3 70B Instruct | $0.00544 | $13.21 |
| Qwen3 VL 235B A22B Instruct | $0.00603 | $14.64 |
| Kimi K2.5 | $0.00681 | $16.55 |
| Amazon Nova Pro | $0.00832 | $20.22 |
| **Claude Haiku 4.5 (actual)** | **$0.01249** | **$30.34** |
| GPT-5.6 Luna ⚠️ GovCloud only | $0.01624 | $39.45 |
| GPT-5.6 Terra ⚠️ GovCloud only | $0.04059 | $98.63 |

El valor de Claude Haiku 4.5 en esta tabla ($0.01249/doc) coincide con lo
medido realmente en producción ($0.0123/doc, `COSTOS.md` §0 Hallazgo 1),
lo que confirma que la mezcla de tokens usada aquí es representativa y que
estos números son comparables entre sí, no sólo estimaciones aisladas.

## 5. Lo que esta comparativa NO cubre

- **Soporte de vision / documentos multimodales.** Este pipeline extrae
  campos leyendo imágenes de páginas escaneadas (Textract OCR de respaldo
  + visión del modelo, ver `AGENTS.md` → `pdf_pages.py`). La AWS Pricing
  API no expone qué modelos aceptan input de imagen ni a qué costo por
  imagen (el atributo `modality` existe en el catálogo pero ningún SKU de
  los 10 modelos de la tabla 1 lo usa) — esto no se puede responder con
  datos de precios, sólo con la documentación de cada modelo. De los
  nombres se infiere que Qwen3 VL y NVIDIA Nemotron Nano **VL** son
  vision-language explícitamente; Nova Pro/2 Lite son multimodales según
  documentación pública de AWS; Nova Micro y Llama 3.3 70B Instruct son
  **texto-only** en sus versiones base. Esto es conocimiento general, no
  verificado vía MCP — confirmar antes de descartar/elegir un modelo por
  este criterio.
- **Soporte de structured output / tool use en Bedrock Converse.**
  `model recomendation.md` deja la liga
  `https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html`
  como pendiente — cuál de estos modelos soporta salida estructurada
  confiable (relevante porque el pipeline pide campos JSON exactos del
  catálogo) sigue sin investigar y no es algo que la Pricing API responda.
- **Calidad de extracción.** Precio barato no implica exactitud aceptable
  contra el ground truth de `tests/integration/expected_pedimentos.json`
  — cualquier candidato por debajo de Haiku 4.5 en precio necesita
  correrse contra `pnpm test:pedimentos` antes de considerarse viable.
- **Disponibilidad real sin cross-region inference profile.** Algunos
  IDs de la lista (`us.anthropic...`, `us.meta...`) ya son perfiles
  cross-region; otros (`amazon.nova-pro-v1:0`, `qwen.qwen3-vl-...`,
  `moonshotai.kimi-k2.5`, `nvidia.nemotron-nano-12b-v2`) no traen prefijo
  `us.` en la lista original — confirmar en la consola de Bedrock que el
  modelo está habilitado (model access) en la cuenta antes de probar.

## 6. Fuente de los datos

Todos los precios de este documento se obtuvieron en vivo el 2026-08-07
vía MCP `aws-billing` → herramienta `aws-pricing`, operación
`get_pricing_from_api`, service code `AmazonBedrock` (modelos de
terceros/Amazon) y `AmazonBedrockService` (Claude, vía Bedrock Mantle),
filtrando `location: "US East (N. Virginia)"`. No son estimaciones — son
los mismos SKUs y `pricePerUnit` que factura AWS, igual que
`documentacion/COSTOS.md` §1 verificó para Claude Haiku 4.5 contra Cost
Explorer al centavo.
