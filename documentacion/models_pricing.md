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
> El modelo actualmente en producción es **Claude Haiku 4.5**
> (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, ver `pipeline/pricing.py`
> y `COSTOS.md` §1) — se usa aquí como **techo de precio** ($1.10 input /
> $5.50 output por 1M tokens, verificado en AWS Pricing API), **no como
> techo de inteligencia**. Es decir: se buscan modelos que cuesten _lo mismo
> o menos_ que Haiku 4.5, sin importar si son más o menos inteligentes —
> y luego se filtra por cuáles superan la inteligencia de Haiku 4.5 según
> Artificial Analysis (Intelligence Index = **29.9** para Claude Haiku 4.5,
> variante "Reasoning"/adaptive thinking, que es como corre este pipeline).

## 0. Hallazgo principal

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

✅ **GLM 5 y Kimi K2.5 sí soportan visión** (input texto + imagen,
confirmado — no fallan en el paso de visión de este pipeline). La
inferencia por convención de nombres (`V`/`VL` como sufijo) que se usó
inicialmente para marcarlos como "no confirmado" era incorrecta para estos
dos casos — Z AI y Moonshot AI no siguen esa convención de forma
consistente en toda su línea. **Kimi K2 Thinking y MiniMax M2.5 siguen sin
confirmar** — no se ha verificado si aceptan input de imagen; no asumir
que heredan el soporte de Kimi K2.5/GLM 5 sólo por ser de la misma familia.

GPT-5.6 Luna y Terra siguen siendo los más inteligentes del catálogo
completo (52.3 y 56.6), pero **sólo existen en AWS GovCloud (US-West)** —
no utilizables en este stack (us-east-1) sin migrar de región (ver §1 y
nota original más abajo).

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
| **Kimi K2.5**                        | `moonshotai.kimi-k2.5`                        | Moonshot AI |               36.0 |      $0.60 |       $3.00 |                  0.55x |            ✅ texto+imagen             | texto  |
| **MiniMax M2.5** 🆕                  | `minimax.minimax-m2.5`                        | MiniMax     |               34.5 |      $0.30 |       $1.20 |                  0.25x | ⚠️ no confirmado (probable sólo texto) | texto  |
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
  Qwen3-**VL**, Nemotron-**VL**, Llama 4, GPT-5.6. **GLM 5 y Kimi K2.5
  también confirmados** — soportan visión sin fallar, pese a no llevar
  sufijo `V`/`VL` (esa convención de nombres no aplica de forma consistente
  en toda la línea de Z AI/Moonshot AI, así que no sirve como señal
  confiable por sí sola). **Kimi K2 Thinking y MiniMax M2.5 siguen sin
  confirmar** — ninguno de los dos MCP usados (`artificial-analysis`,
  `aws-billing`) expone modalidad de input, así que esto no puede
  verificarse por esa vía; sólo queda confirmar contra la documentación de
  cada proveedor o probar directamente antes de descartar/elegir por este
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

**GLM 5 y Kimi K2.5 tienen visión confirmada** (input texto + imagen) —
de los 4, son los dos listos para probarse contra el paso de visión de
este pipeline sin bloqueos conocidos. **Kimi K2 Thinking y MiniMax M2.5
siguen sin confirmar** (ver §1 y §5) — verificar antes de adoptarlos.

Si se acepta salir de us-east-1 (GovCloud), **GPT-5.6 Luna** (52.3,
+75% de inteligencia, con visión confirmada) sería el candidato más fuerte
en inteligencia pura, a 1.30x el precio de Haiku — pero implica migrar
región, algo fuera del alcance de esta comparativa.

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

- **Soporte real de vision para Kimi K2 Thinking y MiniMax M2.5.** GLM 5
  y Kimi K2.5 ya están confirmados (input texto+imagen, sin fallos). Para
  los otros dos, ninguno de los dos MCP usados (`artificial-analysis`,
  `aws-billing`) expone modalidad de input de forma confiable — no hay
  campo de modalidad en el schema de ninguno de los dos, se verificó
  explícitamente. Confirmar contra la documentación de Moonshot AI /
  MiniMax, o probar directamente, antes de considerar cualquiera de los
  dos como reemplazo de Haiku 4.5.
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
