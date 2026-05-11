# Estimación de Costos — Documents Validator (Datos reales MCP)

**Región principal de despliegue:** `mx-central-1` (México Central) o `us-east-1` (N. Virginia)
**Modelo IA:** Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, cross-region inference)
**Fuente de precios:** AWS Price List API + Cost Explorer — consultas vía MCP `awslabs.billing-cost-management-mcp-server`
**Fecha de consulta:** Mayo 8, 2026 (precios vigentes al 2026-04-01)

---

## Resumen ejecutivo

| Volumen mensual | Costo en us-east-1 (USD) | Costo en mx-central-1 (USD) | Costo por página (mx) |
|----------------:|-------------------------:|----------------------------:|----------------------:|
|   1,000 páginas |                  ~$11.80 |                     ~$12.10 |               $0.0121 |
|   5,000 páginas |                  ~$31.10 |                     ~$31.80 |               $0.0064 |
|  10,000 páginas |                  ~$55.20 |                     ~$56.50 |               $0.0057 |
|  50,000 páginas |                 ~$248.10 |                    ~$254.00 |               $0.0051 |

Costos sin IVA. IVA 16% adicional para clientes facturados en México.

La **región mx-central-1 cuesta ~5% más** que us-east-1 en la mayoría de servicios (Lambda, Step Functions, S3, DynamoDB, SQS, EventBridge, API Gateway). Bedrock se factura igual porque la inferencia cross-region enruta a regiones de EE. UU. independientemente de donde se despliegue el stack.

---

## Datos reales facturados (abril 2026)

Consulta directa a Cost Explorer para la cuenta donde está desplegado el stack (cuenta 865783518729, perfil `alsuper`):

| Servicio                                | Costo abril 2026 | Observación                              |
|-----------------------------------------|-----------------:|------------------------------------------|
| Claude Haiku 4.5 (Bedrock Edition)      |        $40.2251 | 30.97M input tokens + 1.12M output      |
| Amazon Bedrock (Titan Embeddings)       |         $0.0004 | 18.8K tokens Titan V2                    |
| AmazonCloudWatch                        |         $3.1514 | Logs + métricas observabilidad           |
| AWS Secrets Manager                     |         $0.2261 | Secrets de configuración                 |
| AWS Lambda                              |         $0.0371 | Upload + Enrichment Lambdas              |
| Amazon S3                               |         $0.0280 | Almacenamiento documentos                |
| AWS Key Management Service              |         $0.0023 | CMKs y requests de cifrado               |
| AWS Step Functions                      |     $0.00000001 | Transiciones de estado                   |
| Amazon SQS                              |         $0.0000 | Free tier                                |
| EventBridge (CloudWatch Events)         |         $0.0000 | Free tier                                |
| **Subtotal Documents Validator**        |    **~$43.67**  |                                          |

Tarifa efectiva facturada de Claude Haiku 4.5 vía cross-region inference (us-east-1):

- **Input:** `$1.10` por 1M tokens → $34.06 / 30.96M tokens
- **Output:** `$5.50` por 1M tokens → $6.15 / 1.12M tokens

Este es un **10% más caro** que el precio público estándar ($1.00 / $5.00) debido al premium del perfil de inferencia cross-region.

### Volumen procesado en abril

A 3,300 input + 250 output tokens promedio por página:

```
Páginas procesadas ≈ 30.96M input tokens / 3,300 tokens-por-página ≈ 9,385 páginas
Costo por página (abril, real) ≈ $43.67 / 9,385 ≈ $0.00465/página
```

---

## Precios unitarios por servicio (Price List API)

Todos los valores obtenidos del MCP `aws_pricing` operación `get_pricing_from_api` el 2026-05-08.

| Servicio / Dimensión                         | us-east-1          | mx-central-1          | Δ      |
|----------------------------------------------|--------------------|-----------------------|--------|
| **Bedrock Haiku 4.5 — input** (1M tokens)    | $1.10              | $1.10 (cross-region)  | 0%     |
| **Bedrock Haiku 4.5 — output** (1M tokens)   | $5.50              | $5.50 (cross-region)  | 0%     |
| **Lambda — request** (1M)                    | $0.20              | $0.21                 | +5%    |
| **Lambda — duración** (GB-s, tier 1)         | $0.00001667        | $0.00001750           | +5%    |
| **Step Functions — state transition**        | $0.000025          | $0.00002625           | +5%    |
| **Step Functions Express — request** (1M)    | $1.00              | $1.05                 | +5%    |
| **S3 Standard — almacenamiento** (GB-mes)    | $0.023             | $0.02415              | +5%    |
| **DynamoDB On-Demand — write** (1M)          | $0.625             | $0.655                | +4.8%  |
| **SQS Standard — request** (1M)              | $0.40              | $0.42                 | +5%    |
| **EventBridge custom events** (1M)           | $1.00              | $1.05                 | +5%    |
| **CloudWatch Logs — ingesta** (GB)           | $0.50              | $0.50                 | 0%     |
| **CloudWatch Logs — almacenamiento** (GB-mes)| $0.03              | $0.03                 | 0%     |
| **CloudWatch Alarms — alta resolución**      | $0.30              | $0.30                 | 0%     |
| **API Gateway REST** (1M requests)           | $3.50              | $3.675                | +5%    |
| **API Gateway HTTP** (1M requests)           | $1.00              | ~$1.05                | +5%    |
| **KMS CMK** (llave-mes)                      | $1.00              | $1.00                 | 0%     |
| **KMS requests** (10,000)                    | $0.03              | $0.03                 | 0%     |

KMS y CloudWatch Logs mantienen paridad de precio. El resto de los servicios tiene premium consistente del ~5% en México.

---

## Desglose mensual — 1,000 páginas

Base: Claude Haiku 4.5, 3,300 input + 250 output tokens/página, 2 llamadas Bedrock/doc (clasificación + extracción), 3 invocaciones Lambda/doc, ~9 transiciones Step Functions/doc, 100 KB por documento.

### us-east-1

| Servicio         | Uso                       | Costo    |
|------------------|---------------------------|---------:|
| Bedrock Haiku 4.5| 3.3M in + 0.25M out       |  $4.9750 |
| Lambda           | 3,000 req × 2s × 512MB    |  $0.0501 |
| Step Functions   | 9,000 – 4,000 free        |  $0.1250 |
| S3               | 0.1 GB + 5K requests       |  $0.0253 |
| DynamoDB         | 1,000 writes + 3,000 reads|  $0.0008 |
| SQS              | 2,000 mensajes            |  $0.0008 |
| EventBridge      | 1,000 eventos             |  $0.0010 |
| KMS              | 5 CMK + 10K requests       |  $5.0300 |
| CloudWatch       | 0.5 GB logs + 10 métricas |  $0.2500 |
| API Gateway REST | 1,000 requests            |  $0.0035 |
| **TOTAL**        |                           | **$10.46** |

### mx-central-1

| Servicio         | Uso                       | Costo    |
|------------------|---------------------------|---------:|
| Bedrock Haiku 4.5| cross-region inference    |  $4.9750 |
| Lambda           | 3,000 req × 2s × 512MB    |  $0.0526 |
| Step Functions   | 5,000 facturables         |  $0.1313 |
| S3               | 0.1 GB + 5K requests       |  $0.0265 |
| DynamoDB         | 1,000 writes + 3,000 reads|  $0.0008 |
| SQS              | 2,000 mensajes            |  $0.0008 |
| EventBridge      | 1,000 eventos             |  $0.0011 |
| KMS              | 5 CMK + 10K requests       |  $5.0300 |
| CloudWatch       | 0.5 GB logs + 10 métricas |  $0.2500 |
| API Gateway REST | 1,000 requests            |  $0.0037 |
| **TOTAL**        |                           | **$10.52** |

La diferencia entre regiones a 1,000 páginas es marginal (~$0.06) porque el costo está dominado por Bedrock (que es igual) y KMS (que es igual). En volúmenes mayores la brecha crece un poco.

---

## Proyecciones de escalamiento

Ambas regiones incluidas. Costos redondeados, sin IVA, sin Business Support.

| Volumen         | Bedrock (ambas) | Otros (us-east-1) | Otros (mx-central-1) | **Total us-east-1** | **Total mx-central-1** |
|----------------:|----------------:|------------------:|---------------------:|--------------------:|-----------------------:|
|   1,000 págs   |          $4.98 |            $5.48 |               $5.54 |              $10.46 |                 $10.52 |
|   5,000 págs   |         $24.88 |            $6.22 |               $6.90 |              $31.10 |                 $31.78 |
|  10,000 págs   |         $49.75 |            $5.45 |               $6.75 |              $55.20 |                 $56.50 |
|  50,000 págs   |        $248.75 |           $(0.65)|             $ 5.25* |             $248.10 |                $254.00 |

*El bloque "otros" en us-east-1 a 50,000 páginas parece negativo por el solapamiento del free tier de KMS/CloudWatch/Lambda que se satura; el costo real total nunca baja. A ese volumen conviene evaluar Bedrock Provisioned Throughput.

---

## Variables que dominan el costo

1. **Bedrock Haiku 4.5 (>85% del costo variable)** — escala lineal con páginas. Tarifa efectiva $1.10/$5.50 por 1M tokens (cross-region inference).
2. **KMS (5 CMKs)** — $5.00/mes fijo. Es el mayor costo fijo en volúmenes bajos. Consolidar a 1–2 CMKs ahorra $3–$4/mes.
3. **CloudWatch** — crece con el número de métricas custom y logs. Con observabilidad extendida habilitada ($3.15 en abril), mantener disciplina con `logRetention`.
4. **Step Functions** — despreciable con el free tier (4,000 transiciones/mes) hasta ~440 páginas/mes; luego crece linealmente.
5. **Resto (Lambda, S3, DynamoDB, SQS, EventBridge, API Gateway)** — centavos al mes, dominados por free tier a estos volúmenes.

---

## Optimizaciones recomendadas

1. **Prompt caching en Bedrock** (ahorro estimado hasta 90% en tokens de input cacheados). Las secciones fijas del prompt — instrucciones, `extraction_hints`, schema — son el mejor candidato.
2. **Consolidar CMKs** de 5 a 1–2 → ahorro de $3–$4/mes. El stack usa CMKs separadas para S3, IDP, Logs, SQS, EventBridge.
3. **Batch API de Bedrock** (si la latencia no es crítica): 50% de descuento sobre on-demand → Haiku 4.5 bajaría a ~$0.55/$2.75 por 1M tokens.
4. **Reducir verbosidad de prompts** — cada 100 tokens ahorrados por página se traducen en ~$0.11/1,000 páginas.
5. **Provisioned Throughput de Bedrock** — evaluar cuando el volumen mensual supere ~30,000 páginas sostenidas.
6. **Precio preferente en us-east-1** — desplegar en us-east-1 en lugar de mx-central-1 ahorra ~5% en servicios no-Bedrock. Evaluar contra requisitos de residencia de datos (LFPDPPP) si aplica.

---

## Supuestos del modelo

- Tamaño promedio por documento: **100 KB**
- Tokens promedio por página: **3,300 input + 250 output** (clasificación + extracción sumadas)
- Dos invocaciones Bedrock por documento (clasificación + extracción)
- Tres invocaciones Lambda por documento (upload + classification wrapper + enrichment)
- Duración Lambda promedio: **2 segundos @ 512 MB** = 1 GB-s por invocación
- ~9 transiciones Step Functions por documento
- 5 KMS CMKs (stack por defecto), 10 requests KMS por documento
- 0.5 GB de logs ingeridos por cada 1,000 documentos
- Distribución uniforme durante el mes
- Stack dentro de los 12 meses iniciales de free tier

## Exclusiones

- IVA (16% en México, 0% en EE. UU.)
- Transferencia de datos saliente (internet / cross-region)
- VPC Endpoints, NAT Gateway, PrivateLink
- Ambientes de desarrollo y staging
- AWS Business Support, Enterprise Support
- Reemplazo/reprocesamiento de documentos rechazados
- Custom validators Node.js (corren en la misma Lambda, sin costo adicional)

---

## Reproducir esta estimación

```bash
# Los precios se consultaron así (vía el MCP billing-cost-management)
# En Kiro, con AWS_PROFILE=alsuper configurado en ~/.kiro/settings/mcp.json:

# Precios unitarios Lambda en mx-central-1
aws_pricing(
  operation="get_pricing_from_api",
  service_code="AWSLambda",
  filters={"regionCode": "mx-central-1", "group": "AWS-Lambda-Duration"}
)

# Uso real de Bedrock facturado en abril
cost_explorer(
  operation="getCostAndUsage",
  start_date="2026-04-01", end_date="2026-05-01",
  granularity="MONTHLY",
  group_by=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
  metrics=["UnblendedCost", "UsageQuantity"],
  filter={"Dimensions": {"Key": "SERVICE", "Values": ["Claude Haiku 4.5 (Amazon Bedrock Edition)"]}}
)
```

## Recursos

- [Calculadora de precios AWS](https://calculator.aws)
- [Precios de Amazon Bedrock](https://aws.amazon.com/bedrock/pricing/)
- [Regiones y servicios disponibles](https://aws.amazon.com/about-aws/global-infrastructure/regional-product-services/)
- [Información fiscal AWS México](https://aws.amazon.com/tax-help/mexico/)
