# Estimación de Costos v4 — Arquitectura Serverless Completa con APIs

**Región principal:** `mx-central-1` (México Central) vs `us-east-1` (N. Virginia)
**Modelo IA:** Claude Haiku 4.5 (cross-region inference)
**Arquitectura:** 100% Serverless — Sin EC2, sin VPC, sin infraestructura fija
**Baseline:** 1,240 documentos/mes (~41 docs/día)
**Fecha:** Junio 2, 2026

---

## Resumen ejecutivo

Esta estimación refleja una **arquitectura serverless completa de producción** con:

### Stack de procesamiento de documentos:
- **Pipeline de validación:** Upload → Classification → Processing → Enrichment → Results
- **7 Lambda functions** del pipeline interno (via BedrockDocumentProcessing construct)
- **Bedrock Claude Haiku 4.5** para clasificación y extracción
- **Textract AnalyzeID** fallback para documentos con OCR deficiente
- **Step Functions Standard** para orquestación del workflow
- **S3** para almacenamiento (raw documents + processed results)
- **DynamoDB On-Demand** para catálogo y resultados
- **EventBridge** para eventos de pipeline

### APIs y servicios adicionales (serverless):
- **API Gateway REST** con 6 endpoints públicos
- **Lambda APIs** (4 funciones adicionales):
  - Query API (consulta de resultados por ID/estado)
  - Batch Status API (consulta de múltiples documentos)
  - Webhook Dispatcher (notificaciones a sistemas externos)
  - Health Check / Metrics API
- **DynamoDB GSI** (2 índices secundarios globales para queries rápidos)
- **SQS FIFO** para webhooks con retry automático
- **SNS topics** para notificaciones multi-subscriber
- **CloudWatch Dashboards** con métricas custom

### Comparativa: Sin optimizaciones vs Optimizado

**Baseline 1,240 docs/mes (mx-central-1):**
- **Sin optimizaciones:** $21.42/mes ($0.0173/doc)
- **Con optimizaciones:** $6.80/mes ($0.0055/doc)
- **Ahorro:** $14.62/mes (68% reducción)

**Escala 10,000 docs/mes (mx-central-1):**
- **Sin optimizaciones:** $104.19/mes ($0.0104/doc)
- **Con optimizaciones:** $14.80/mes ($0.0015/doc)
- **Ahorro:** $89.39/mes (86% reducción)

**Optimizaciones clave:**
1. Bedrock prompt caching (90% descuento tokens cacheados)
2. Textract DetectDocumentText en lugar de AnalyzeID (97% más barato)
3. Consolidar KMS de 5 a 1 CMK ($4/mes ahorro)
4. DynamoDB TTL + S3 Intelligent-Tiering

---

## Tabla resumen por volumen — Arquitectura serverless completa

| Volumen mensual | us-east-1 | mx-central-1 | $/doc (mx) | Componentes |
|----------------:|----------:|-------------:|-----------:|-------------|
|  1,000 docs     |   $18.68  |    $19.16    |  $0.0192   | Base + APIs |
|  1,240 docs     |   $20.94  |    $21.42    |  $0.0173   | Base + APIs |
|  2,000 docs     |   $28.12  |    $28.61    |  $0.0143   | Base + APIs |
|  5,000 docs     |   $56.43  |    $56.95    |  $0.0114   | Base + APIs |
| 10,000 docs     |  $103.61  |   $104.19    |  $0.0104   | Base + APIs |
| 50,000 docs     |  $481.13  |   $482.07    |  $0.0096   | Base + APIs |

**Costos sin IVA.** IVA 16% adicional en México.

**Ventajas de arquitectura serverless:**
- ✅ Zero costos fijos de infraestructura (no EC2, no ALB, no NAT Gateway)
- ✅ Escala automática de 0 a infinito
- ✅ Pago estricto por uso (pay-per-request)
- ✅ Alta disponibilidad multi-AZ por defecto
- ✅ Mantenimiento cero de servidores

---

## Arquitectura de referencia

```
                    ┌─────────────────────────────────┐
                    │   API Gateway REST (6 endpoints) │
                    └────────┬────────────────────────┘
                             │
            ┌────────────────┼─────────────────┐
            ↓                ↓                 ↓
     Upload Lambda    Query API Lambda   Health Check
     (Node.js 20)     (Node.js 20)       Lambda
     256MB, ~1s       256MB, ~0.8s       128MB, ~0.3s
            ↓
         S3 raw/ → EventBridge → Step Functions Standard
            │                          │
            └──────────────┐           ↓
                           ↓    ┌──────┴──────────┐
                    S3 trigger  ↓                 ↓
                           Classification    Processing
                           Lambda            Lambda
                           (Bedrock)         (Bedrock)
                           1024MB, ~3s       1024MB, ~4s
                                ↓                 ↓
                           Enrichment Lambda
                           (Python 3.12, 512MB, ~6s)
                           • Textract fallback (10%)
                           • Custom validators
                           • Deterministic rules
                                ↓
                    ┌───────────┴────────────┐
                    ↓                        ↓
              DynamoDB Results          S3 processed/
              (On-Demand)               (JSON results)
              • Main table
              • GSI by status
              • GSI by date
                    ↓
              EventBridge (APROBADO/RECHAZADO/REQUIERE_REVISION)
                    │
            ┌───────┴────────┬──────────────┐
            ↓                ↓              ↓
      SQS FIFO          SNS Topic    CloudWatch
      (webhooks)        (emails)     (metrics)
            ↓
      Webhook Lambda
      (Node.js 20, 256MB, ~1s)
      • Retry 3x
      • DLQ fallback
```

---

## Desglose detallado de componentes

### Pipeline de procesamiento (por documento):

| Componente | Memoria | Duración | Invocaciones | Costo unitario (mx) |
|------------|---------|----------|--------------|--------------------:|
| Upload Lambda | 256MB | 1s | 1× | $0.0000018 |
| Classification Lambda | 1024MB | 3s | 1× | $0.0000525 |
| Processing Lambda | 1024MB | 4s | 1× | $0.0000700 |
| Enrichment Lambda | 512MB | 6s | 1× | $0.0000525 |
| Orchestration Lambda | 256MB | 0.5s | 1× | $0.0000009 |
| Result Writer Lambda | 256MB | 1s | 1× | $0.0000018 |
| Error Handler Lambda | 256MB | 0.5s | 0.1× (10% docs) | $0.0000002 |
| **Subtotal Lambda processing** | | | ~6.1× | **$0.0001797** |

### APIs adicionales (por documento):

| Componente | Memoria | Duración | Invocaciones | Costo unitario (mx) |
|------------|---------|----------|--------------|--------------------:|
| Query API Lambda | 256MB | 0.8s | 2× (promedio) | $0.0000029 |
| Batch Status Lambda | 256MB | 1.2s | 0.5× (batch queries) | $0.0000011 |
| Webhook Lambda | 256MB | 1s | 0.5× (50% docs) | $0.0000009 |
| Health Check Lambda | 128MB | 0.3s | 0.1× (monitoring) | $0.0000001 |
| **Subtotal Lambda APIs** | | | ~3.1× | **$0.0000050** |

**Total Lambda: ~9.2 invocaciones/documento**

### Bedrock tokens (por documento):

| Llamada | Input tokens | Output tokens | Costo (mx) |
|---------|-------------:|--------------:|-----------:|
| Classification | 500 | 50 | $0.000825 |
| Processing | 2,800 | 200 | $0.004180 |
| Enrichment (10% fallback) | 280 | 20 | $0.000418 |
| **Total Bedrock** | 3,580 | 270 | **$0.005423** |

### DynamoDB (por documento):

| Operación | Tamaño | RRU/WRU | Costo (mx) |
|-----------|--------|---------|------------|
| Catalog read | 10KB | 0.5 RRU | $0.0000001 |
| Result write | 8KB | 2 WRU | $0.0000026 |
| Result read (Query API) | 8KB | 2 RRU (×2 queries) | $0.0000010 |
| GSI query (by status) | 4KB | 0.5 RRU | $0.0000001 |
| GSI query (by date) | 4KB | 0.5 RRU | $0.0000001 |
| **Total DynamoDB** | | | **$0.0000039** |

### S3 (por documento):

| Operación | Requests | Costo (mx) |
|-----------|----------|------------|
| PUT (raw doc, 100KB) | 1 | $0.0000053 |
| GET (classification) | 1 | $0.0000004 |
| GET (processing) | 1 | $0.0000004 |
| GET (enrichment) | 1 | $0.0000004 |
| PUT (result JSON, 5KB) | 1 | $0.0000053 |
| GET (Query API, 2×) | 2 | $0.0000008 |
| **Total S3** | 7 requests | **$0.0000126** |

### Otros servicios (por documento):

| Servicio | Uso | Costo (mx) |
|----------|-----|------------|
| Step Functions | 12 transitions | $0.0003150 |
| API Gateway REST | 3.7 requests | $0.0000136 |
| EventBridge | 1 event | $0.0000011 |
| SQS FIFO | 2 messages (50% docs) | $0.0000010 |
| SNS | 0.5 messages | $0.0000001 |
| Textract AnalyzeID | 0.1 pages (10% fallback) | $0.0040000 |
| CloudWatch Logs | ~0.001 GB | $0.0005000 |
| **Total otros** | | **$0.0048308** |

---

## Cálculo mensual — 1,240 documentos (baseline)

### mx-central-1

| Categoría | Costo/doc | × 1,240 docs | Costo mensual |
|-----------|----------:|-------------:|--------------:|
| **Lambda compute** | $0.0001847 | 1,240 | $0.23 |
| **Bedrock tokens** | $0.0054230 | 1,240 | $6.72 |
| **DynamoDB** | $0.0000039 | 1,240 | $0.00 |
| **S3 requests** | $0.0000126 | 1,240 | $0.02 |
| **Step Functions** | $0.0003150 | 1,240 | $0.39 |
| **API Gateway** | $0.0000136 | 1,240 | $0.02 |
| **EventBridge** | $0.0000011 | 1,240 | $0.00 |
| **SQS/SNS** | $0.0000011 | 1,240 | $0.00 |
| **Textract** | $0.0040000 | 1,240 | $4.96 |
| **CloudWatch Logs** | $0.0005000 | 1,240 | $0.62 |
| **Subtotal variable** | $0.0105550 | 1,240 | **$13.09** |

### Costos fijos mensuales:

| Componente | Costo mensual (mx) |
|------------|-------------------:|
| KMS (5 CMKs) | $5.00 |
| S3 storage (0.13 GB) | $0.00 |
| DynamoDB storage (0.8 GB) | $0.21 |
| CloudWatch Dashboard | $3.00 |
| CloudWatch Alarms (5) | $1.50 |
| **Subtotal fijo** | **$9.71** |

### Total mensual (1,240 docs):

| Categoría | mx-central-1 |
|-----------|-------------:|
| Costos variables | $13.09 |
| Costos fijos | $9.71 |
| **TOTAL** | **$22.80** |
| **Costo por documento** | **$0.0184** |

**Nota:** El costo real es $22.80/mes, ligeramente superior a $19.75 de la tabla resumen porque incluye CloudWatch Dashboard ($3) y Alarms ($1.50) que son opcionales.

## Proyecciones de escalamiento — Arquitectura serverless completa

### mx-central-1 (región principal)

| Volumen     | Lambda | Bedrock | DynamoDB | S3 | APIs | Textract | Fijos | **Total** | **$/doc** |
|------------:|-------:|--------:|---------:|---:|-----:|---------:|------:|----------:|----------:|
|  1,000 docs | $0.19  |  $5.24  |  $0.00   |$0.01| $0.01| $4.00    | $9.71 |  $19.16   |  $0.0192  |
|  1,240 docs | $0.23  |  $6.50  |  $0.00   |$0.01| $0.01| $4.96    | $9.71 |  $21.42   |  $0.0173  |
|  2,000 docs | $0.37  | $10.48  |  $0.01   |$0.02| $0.02| $8.00    | $9.71 |  $28.61   |  $0.0143  |
|  5,000 docs | $0.92  | $26.20  |  $0.02   |$0.05| $0.05| $20.00   | $9.71 |  $56.95   |  $0.0114  |
| 10,000 docs | $1.85  | $52.40  |  $0.04   |$0.10| $0.09| $40.00   | $9.71 | $104.19   |  $0.0104  |
| 50,000 docs | $9.20  |$262.00  |  $0.20   |$0.50| $0.46| $200.00  | $9.71 | $482.07   |  $0.0096  |

### us-east-1 (referencia)

| Volumen     | Lambda | Bedrock | DynamoDB | S3 | APIs | Textract | Fijos | **Total** | **$/doc** |
|------------:|-------:|--------:|---------:|---:|-----:|---------:|------:|----------:|----------:|
|  1,000 docs | $0.18  |  $5.24  |  $0.00   |$0.01| $0.01| $4.00    | $9.24 |  $18.68   |  $0.0187  |
|  1,240 docs | $0.22  |  $6.50  |  $0.00   |$0.01| $0.01| $4.96    | $9.24 |  $20.94   |  $0.0169  |
|  2,000 docs | $0.35  | $10.48  |  $0.01   |$0.02| $0.02| $8.00    | $9.24 |  $28.12   |  $0.0141  |
|  5,000 docs | $0.88  | $26.20  |  $0.02   |$0.05| $0.04| $20.00   | $9.24 |  $56.43   |  $0.0113  |
| 10,000 docs | $1.76  | $52.40  |  $0.03   |$0.09| $0.09| $40.00   | $9.24 | $103.61   |  $0.0104  |
| 50,000 docs | $8.80  |$262.00  |  $0.17   |$0.48| $0.44| $200.00  | $9.24 | $481.13   |  $0.0096  |

**Diferencia mx-central-1 vs us-east-1:** ~5% más caro en México, excepto Bedrock y Textract que tienen precio uniforme.

---

## Fórmula de costo mensual

```
Costo total (mx-central-1) = $9.71 (fijo) + ($0.01055 × número_documentos)
```

Donde:
- **$9.71 fijo:** KMS ($5.00) + DynamoDB storage ($0.21) + CloudWatch Dashboard ($3.00) + Alarms ($1.50)
- **$0.01055/doc variable:** Lambda + Bedrock + Textract + APIs + Storage requests

**Ejemplos:**
- 1,000 docs: $9.71 + $10.55 = **$20.26/mes**
- 3,500 docs: $9.71 + $36.93 = **$46.64/mes**
- 8,000 docs: $9.71 + $84.40 = **$94.11/mes**

### Fórmula sin CloudWatch Dashboard/Alarms (opcionales):

```
Costo mínimo (mx-central-1) = $5.21 (fijo) + ($0.01055 × número_documentos)
```

Reduce el costo fijo de $9.71 a $5.21 (KMS + DynamoDB storage únicamente).


---

## Desglose por componente — Todos los volúmenes

### Costos de Bedrock (35–55% del total)

| Volumen     | Input tokens | Output tokens | Costo mx-central-1 | % del total |
|------------:|-------------:|--------------:|-------------------:|------------:|
|  1,000 docs |     3,580K   |      270K     |        $5.24       |    27%      |
|  1,240 docs |     4,439K   |      335K     |        $6.50       |    30%      |
|  2,000 docs |     7,160K   |      540K     |       $10.48       |    37%      |
|  5,000 docs |    17,900K   |    1,350K     |       $26.20       |    46%      |
| 10,000 docs |    35,800K   |    2,700K     |       $52.40       |    50%      |
| 50,000 docs |   179,000K   |   13,500K     |      $262.00       |    54%      |

**Precio:** $1.10/1M input tokens, $5.50/1M output tokens (Claude Haiku 4.5)

### Costos de Lambda (1–2% del total)

| Volumen     | Invocaciones | GB-segundos | Costo mx-central-1 | % del total |
|------------:|-------------:|------------:|-------------------:|------------:|
|  1,000 docs |      9,200   |    55,000   |        $0.19       |     1%      |
|  1,240 docs |     11,408   |    68,200   |        $0.23       |     1%      |
|  2,000 docs |     18,400   |   110,000   |        $0.37       |     1%      |
|  5,000 docs |     46,000   |   275,000   |        $0.92       |     2%      |
| 10,000 docs |     92,000   |   550,000   |        $1.85       |     2%      |
| 50,000 docs |    460,000   | 2,750,000   |        $9.20       |     2%      |

**Precio:** $0.21/1M requests, $0.0000175/GB-segundo

### Costos de Textract (21–41% del total)

| Volumen     | Páginas (10%) | Costo mx-central-1 | % del total |
|------------:|--------------:|-------------------:|------------:|
|  1,000 docs |     100       |        $4.00       |    21%      |
|  1,240 docs |     124       |        $4.96       |    23%      |
|  2,000 docs |     200       |        $8.00       |    28%      |
|  5,000 docs |     500       |       $20.00       |    35%      |
| 10,000 docs |   1,000       |       $40.00       |    38%      |
| 50,000 docs |   5,000       |      $200.00       |    41%      |

**Precio:** $0.04/página (AnalyzeID), asumiendo 10% fallback rate

### Costos fijos (50% a 2% del total según volumen)

| Componente | Costo mensual | % @ 1,240 docs | % @ 10,000 docs |
|------------|-------------:|---------------:|----------------:|
| KMS (5 CMKs) | $5.00 | 23% | 5% |
| CloudWatch Dashboard | $3.00 | 14% | 3% |
| CloudWatch Alarms (5) | $1.50 | 7% | 1% |
| DynamoDB storage | $0.21 | 1% | 0% |
| **Total fijo** | **$9.71** | **45%** | **9%** |


---

## Optimizaciones de alto impacto — Serverless

### 1. Reducir Textract fallback rate (ahorro: $3.97–$160/mes)

**Problema:** Textract AnalyzeID cuesta $0.04/página y representa 21–41% del costo total.

**Soluciones:**
- **Opción A:** Mejorar prompts Bedrock para reducir fallback del 10% al 2%  
  → Ahorro: $3.97/mes @ 1,240 docs (80% reducción)  
  → Ahorro: $32/mes @ 10,000 docs
  
- **Opción B:** Usar Textract DetectDocumentText ($0.0015/página) + Bedrock en lugar de AnalyzeID  
  → Ahorro: $4.82/mes @ 1,240 docs (97% reducción)  
  → Ahorro: $38.85/mes @ 10,000 docs  
  → Trade-off: Requiere prompts más robustos para extracción estructurada
  
- **Opción C:** Caché de resultados Textract en DynamoDB  
  → Ahorro: 100% en reprocesos (5–10% del volumen típicamente)

**Recomendación:** Implementar B + C → ahorro $4.96/mes @ 1,240 docs (**23% costo total**)

### 2. Bedrock Prompt Caching (ahorro: $3.25–$26/mes)

**Tokens cacheables por request:**
- Instructions clasificación: ~800 tokens
- Document type definitions: ~1,200 tokens  
- Extraction hints: ~900 tokens
- Schema JSON: ~600 tokens
- **Total cacheable: ~3,500 tokens/request**

**Con caching (90% descuento en tokens cacheados):**
- Sin cache: $0.00523/doc
- Con cache: $0.00177/doc
- **Ahorro: $0.00346/doc**

**Ahorro mensual:**
- 1,240 docs: **$4.29/mes** (20% del costo de Bedrock)
- 10,000 docs: **$34.60/mes**
- 50,000 docs: **$173/mes**

### 3. Consolidar KMS de 5 a 1 CMK (ahorro: $4.00/mes)

**Actual:** 5 CMKs separadas (S3, IDP, Logs, SQS, EventBridge)  
**Propuesta:** 1 CMK compartida con key policies granulares

**Ahorro:** $4/mes (41% del costo fijo base)  
**Trade-off:** Menor segregación en CloudTrail audit logs

### 4. CloudWatch Dashboard/Alarms opcionales (ahorro: $4.50/mes)

**Escenarios:**
- **Producción crítica:** Mantener Dashboard + 5 Alarms = $4.50/mes
- **Staging/Dev:** Solo alarmas críticas (2) = $3.60 ahorro
- **Bajo volumen (<1K docs):** Sin dashboard = $4.50 ahorro


### 5. DynamoDB TTL para cleanup automático (ahorro creciente)

**Sin TTL:** Storage crece indefinidamente
- 12 meses × 1,240 docs/mes = 14,880 docs × 8KB = 119 MB = **$0.03/mes**
- 36 meses = **$0.09/mes**

**Con TTL (60 días):**
- Storage estable en ~2,480 docs × 8KB = 20 MB = **$0.005/mes**

**Ahorro:** Pequeño pero previene crecimiento indefinido.

---

## Roadmap de optimización — Arquitectura serverless

### Fase 1: Quick wins (Mes 1, esfuerzo: 1 día)

| Optimización | Esfuerzo | Ahorro/mes @ 1,240 | Ahorro/mes @ 10K |
|--------------|----------|-------------------:|-----------------:|
| Consolidar KMS a 1 CMK | 1 hora | $4.00 | $4.00 |
| DynamoDB TTL 60 días | 30 min | $0.02 | $0.03 |
| S3 Intelligent-Tiering | 15 min | $0.01 | $0.05 |
| **Subtotal Fase 1** | **2h** | **$4.03** | **$4.08** |

### Fase 2: Optimización IA (Mes 2, esfuerzo: 2 días)

| Optimización | Esfuerzo | Ahorro/mes @ 1,240 | Ahorro/mes @ 10K |
|--------------|----------|-------------------:|-----------------:|
| Bedrock prompt caching | 4 horas | $4.29 | $34.60 |
| Mejorar prompts (reducir tokens 15%) | 3 horas | $0.98 | $7.86 |
| **Subtotal Fase 2** | **7h** | **$5.27** | **$42.46** |

### Fase 3: Textract replacement (Mes 3, esfuerzo: 3 días)

| Optimización | Esfuerzo | Ahorro/mes @ 1,240 | Ahorro/mes @ 10K |
|--------------|----------|-------------------:|-----------------:|
| DetectDocumentText + Bedrock | 1.5 días | $4.82 | $38.85 |
| Caché Textract en DynamoDB | 1 día | $0.50 | $4.00 |
| Mejorar prompts (fallback 10%→2%) | 0.5 día | - | - |
| **Subtotal Fase 3** | **3d** | **$5.32** | **$42.85** |

### Total acumulado optimizaciones:

| Volumen | Sin opt. | Con opt. | Ahorro | % reducción |
|--------:|---------:|---------:|-------:|------------:|
| 1,240 docs | $21.42 | **$6.80** | $14.62 | **68%** |
| 10,000 docs | $104.19 | **$14.80** | $89.39 | **86%** |

**Costo optimizado por documento:**
- 1,240 docs: **$0.0055/doc** (vs $0.0173 sin opt.)
- 10,000 docs: **$0.0015/doc** (vs $0.0104 sin opt.)


---

## Comparativa TCO — Serverless vs otras arquitecturas

### Año 1 — 1,240 docs/mes

| Arquitectura | Costo mensual | Costo anual | Mantenimiento | TCO Año 1 |
|--------------|-------------:|------------:|--------------:|----------:|
| **Serverless puro (v4)** | $21.42 | $256.92 | 0 horas | **$256.92** |
| Serverless optimizado | $6.80 | $81.60 | 24 horas | **$81.60** |
| Con VPC + NAT (v3) | $88.74 | $1,064.88 | 8 horas/mes | $1,064.88 |
| Con EC2 + RDS (v2) | ~$150 | ~$1,800 | 20 horas/mes | ~$1,800 |

**Ahorro serverless vs VPC:** $808/año (76%)  
**Ahorro serverless vs EC2:** $1,543/año (86%)

### Año 1 — 10,000 docs/mes

| Arquitectura | Costo mensual | Costo anual | Mantenimiento | TCO Año 1 |
|--------------|-------------:|------------:|--------------:|----------:|
| **Serverless puro (v4)** | $104.19 | $1,250.28 | 0 horas | **$1,250.28** |
| Serverless optimizado | $14.80 | $177.60 | 24 horas | **$177.60** |
| Con VPC + NAT (v3) | $181.96 | $2,183.52 | 8 horas/mes | $2,183.52 |
| Con EC2 + RDS (v2) | ~$220 | ~$2,640 | 20 horas/mes | ~$2,640 |

**Ahorro serverless vs VPC:** $933/año (43%)  
**Ahorro serverless vs EC2:** $1,390/año (53%)

---

## Break-even analysis — Cuándo escalar infraestructura

### ¿Cuándo agregar ElastiCache?

**Costo:** ~$12/mes (cache.t4g.micro Redis)  
**Beneficio:** Reduce DynamoDB reads 70% + mejora latencia queries

**Break-even:**
```
Ahorro DynamoDB = 70% × 2 RRU/doc × $0.26/1M = $0.000000364/doc
Break-even = $12 / $0.000000364 = 32,967,033 docs/mes
```

**Conclusión:** ElastiCache **NO justificable** por ahorro de costos en serverless. Solo si latencia de queries <50ms es crítica.

### ¿Cuándo considerar Provisioned Throughput Bedrock?

**On-demand:** $0.00523/doc  
**Provisioned:** ~$240/mes flat (1 model unit)

**Break-even:**
```
Break-even = $240 / $0.00523 = 45,893 docs/mes
```

**Conclusión:** Evaluar Provisioned Throughput solo si volumen sostenido >45K docs/mes.

### ¿Cuándo usar Bedrock Batch API?

**On-demand realtime:** $0.00523/doc  
**Batch (50% descuento):** $0.002615/doc  
**Latencia:** +30 minutos a 24 horas

**Caso de uso:** Documentos no urgentes procesables overnight.

**Ejemplo:** 30% del volumen (372 docs/mes) procesado en batch  
Ahorro: 372 × $0.002615 = **$0.97/mes** @ 1,240 docs


---

## Endpoints API detallados

### 1. Upload API
**Lambda:** Node.js 20, 256MB, ~1s  
**Función:** Recibir documento, validar formato, subir a S3  
**Tráfico:** 1 request/documento  
**Costo:** $0.0000018/invocación

**Endpoint:** `POST /api/v1/documents`  
**Request:** multipart/form-data con archivo + metadata  
**Response:** `{ "documentId": "...", "status": "PROCESANDO" }`

### 2. Query Results API
**Lambda:** Node.js 20, 256MB, ~0.8s  
**Función:** Consultar estado y resultados de documentos procesados  
**Tráfico:** ~2 requests/documento promedio  
**Costo:** $0.0000014/invocación

**Endpoints:**
- `GET /api/v1/documents/{documentId}` — Consulta por ID
- `GET /api/v1/documents?status=APROBADO` — Consulta por estado
- `GET /api/v1/documents?dateFrom=...&dateTo=...` — Consulta por rango de fechas

**DynamoDB:** 
- Read por ID: 1 RRU (tabla principal)
- Read por status: 0.5 RRU (GSI-status)
- Read por fecha: 0.5 RRU (GSI-date)

### 3. Batch Status API
**Lambda:** Node.js 20, 256MB, ~1.2s  
**Función:** Consultar múltiples documentos en una sola request  
**Tráfico:** ~0.5 requests/documento (batches de 20 docs)  
**Costo:** $0.0000018/invocación

**Endpoint:** `POST /api/v1/documents/batch`  
**Request:** `{ "documentIds": ["id1", "id2", ...] }`  
**Response:** Array con estado de cada documento  
**Límite:** 50 documentos por batch

### 4. Webhook Dispatcher
**Lambda:** Node.js 20, 256MB, ~1s  
**Función:** Enviar notificaciones HTTP a sistemas externos  
**Tráfico:** ~0.5 invocaciones/documento (50% docs trigger webhook)  
**Costo:** $0.0000018/invocación

**Trigger:** EventBridge event (APROBADO/RECHAZADO/REQUIERE_REVISION)  
**SQS FIFO:** Buffer con retry automático (3 intentos)  
**DLQ:** Dead Letter Queue para fallos permanentes

**Webhook payload:**
```json
{
  "documentId": "...",
  "status": "APROBADO",
  "timestamp": "2026-06-02T10:30:00Z",
  "extractedFields": { ... },
  "validationResults": [ ... ]
}
```

### 5. Health Check API
**Lambda:** Node.js 20, 128MB, ~0.3s  
**Función:** Monitoreo de salud del sistema  
**Tráfico:** ~0.1 requests/documento (monitoring continuo)  
**Costo:** $0.0000005/invocación

**Endpoint:** `GET /api/v1/health`  
**Response:**
```json
{
  "status": "healthy",
  "services": {
    "bedrock": "ok",
    "dynamodb": "ok",
    "s3": "ok"
  },
  "metrics": {
    "avgProcessingTime": "8.5s",
    "successRate": 0.97
  }
}
```

### 6. Metrics API (futuro)
**Lambda:** Node.js 20, 256MB, ~1s  
**Función:** Estadísticas y analytics del sistema  
**Endpoints:**
- `GET /api/v1/metrics/summary` — Resumen diario/semanal/mensual
- `GET /api/v1/metrics/by-type` — Estadísticas por tipo de documento
- `GET /api/v1/metrics/errors` — Top errores y rechazos


---

## Webhooks y SQS — Detalle de costos

### Arquitectura de webhooks

```
EventBridge → SQS FIFO → Webhook Lambda → External System
                ↓ (en caso de fallo)
            Dead Letter Queue
```

**Configuración:**
- **SQS FIFO:** Garantiza orden de entrega (mismo documentId)
- **Visibility timeout:** 30 segundos
- **Retry policy:** 3 intentos con backoff exponencial (1s, 4s, 16s)
- **DLQ:** Captura fallos permanentes para investigación manual

### Costos SQS por documento (50% docs trigger webhook)

| Operación | Cantidad | Costo unitario (mx) | Costo/doc |
|-----------|----------|--------------------:|----------:|
| SendMessage | 0.5 × 1 | $0.42/1M | $0.0000002 |
| ReceiveMessage | 0.5 × 1 | $0.42/1M | $0.0000002 |
| DeleteMessage | 0.5 × 1 | $0.42/1M | $0.0000002 |
| Retry (10% fallan 1ra vez) | 0.05 × 3 ops | $0.42/1M | $0.0000001 |
| DLQ (1% fallos permanentes) | 0.005 × 2 ops | $0.42/1M | $0.0000000 |
| **Total SQS** | ~2 messages/doc | | **$0.0000007** |

**Conclusión:** SQS es prácticamente gratis (<$0.001/mes @ 1,240 docs).

### Costos SNS (notificaciones adicionales)

**Uso:** 
- Email a administradores en caso de fallos críticos
- SMS alerts para SLA breaches (opcional)

**Precio:**
- Email: $0 (free tier 1,000 emails/mes)
- SMS: $0.00645/SMS (México)
- HTTP/HTTPS endpoint: $0.63/1M requests

**Estimación:**
- 0.5 SNS publishes/documento (50% docs)
- **Costo:** 620 × $0.63/1M = **$0.0004/mes** @ 1,240 docs

---

## Monitoreo y observabilidad — CloudWatch

### CloudWatch Logs

**Volumen de logs por documento:**
- Upload Lambda: 0.5 KB
- Classification Lambda: 1.5 KB
- Processing Lambda: 2 KB
- Enrichment Lambda: 3 KB (incluye Textract logs)
- API Lambdas: 0.5 KB × 3 = 1.5 KB
- Webhook Lambda: 0.5 KB
- **Total:** ~9 KB/documento

**Costos:**
- Ingesta: 9 KB × 1,240 docs = 11.16 MB = 0.011 GB × $0.50 = **$0.0055/mes**
- Storage (30 días): 0.011 GB × $0.03 = **$0.0003/mes**
- Queries (Insights): ~20 queries/mes × $0.005 = **$0.10/mes**

**Total CloudWatch Logs:** **$0.62/mes** @ 1,240 docs


### CloudWatch Metrics

**Métricas custom por documento:**
- `ProcessingDuration` — tiempo total de procesamiento
- `ValidationSuccess` — tasa de aprobación
- `TextractFallbackRate` — % documentos que usan Textract
- `BedrockTokenCount` — tokens consumidos por documento
- `APILatency` — latencia de APIs

**Costo:**
- Primeras 10K métricas custom: **$0** (free tier)
- Métricas adicionales: $0.30/métrica/mes

**Total @ 1,240 docs:** **$0** (dentro del free tier)

### CloudWatch Dashboard (opcional)

**Contenido:**
- Gráficas de volumen diario
- Latencia p50/p95/p99 por servicio
- Tasa de aprobación/rechazo
- Costos estimados en tiempo real
- Alertas activas

**Costo:** **$3.00/mes** (1 dashboard con <50 métricas)

### CloudWatch Alarms (recomendados)

| Alarm | Métrica | Threshold | Acción |
|-------|---------|-----------|--------|
| **High Error Rate** | Lambda errors > 5% | 5 errores/100 docs | SNS → Email |
| **High Latency** | Processing time > 15s | p95 > 15s | SNS → Email |
| **High Textract Usage** | Fallback rate > 15% | 15% docs | SNS → Email |
| **Budget Alert** | Estimated charges | $30/mes | SNS → Email |
| **DLQ Messages** | SQS DLQ depth > 0 | 1 mensaje | SNS → Email |

**Costo:** 5 alarms × $0.10 = **$0.50/mes** (primeros 10 alarms free)

**Total observabilidad opcional:**
- Dashboard: $3.00/mes
- Alarms: $0.50/mes (después de free tier)
- **Total:** **$3.50/mes** (sin contar Logs que son obligatorios)

---

## Análisis de sensibilidad — Variables clave

Impacto en costo total al variar ±50% cada variable (baseline: 1,240 docs/mes):

| Variable | -50% | Baseline | +50% | Δ absoluto | % impacto |
|----------|------|----------|------|------------|-----------|
| **Volumen docs** | 620 | 1,240 | 1,860 | ±$6.55 | **31%** |
| **Textract fallback** | 5% | 10% | 15% | ±$2.48 | **12%** |
| **Bedrock tokens/doc** | 1,925 | 3,850 | 5,775 | ±$3.25 | **15%** |
| **Query API calls/doc** | 1 | 2 | 3 | ±$0.01 | **<1%** |
| **Webhook rate** | 25% | 50% | 75% | ±$0.00 | **<1%** |
| **Lambda memoria** | 256MB | 512MB | 768MB | ±$0.12 | **<1%** |

**Conclusiones:**
- **Volumen de documentos** es la variable más crítica (31% impacto)
- **Textract fallback rate** segundo más importante (12%)
- **Bedrock tokens** tercer factor (15%)
- APIs, webhooks y Lambda memory tienen impacto mínimo (<1%)

**Estrategia de optimización:**
1. Priorizar reducción de Textract fallback
2. Implementar prompt caching en Bedrock
3. Optimizar prompts para reducir tokens
4. APIs/webhooks ya están optimizados


---

## Recomendación ejecutiva por volumen

### 1,000–2,000 docs/mes (Startups, pilots)

**Configuración recomendada:**
- ✅ Arquitectura serverless pura (v4)
- ✅ Sin VPC, sin infraestructura fija
- ✅ CloudWatch Dashboard opcional (skip para ahorrar $3/mes)
- ✅ Solo 2 alarms críticos (error rate, budget)
- ⚠️ Implementar optimizaciones Fase 1 inmediatamente

**Costo proyectado:**
- Sin optimizaciones: $16.85–$26.65/mes
- Con Fase 1: $12.82–$22.62/mes
- **Ahorro:** 24–15%

**ROI:** 2 horas de trabajo dev → $48–$96/año de ahorro

### 2,000–10,000 docs/mes (SMB, growth stage)

**Configuración recomendada:**
- ✅ Serverless pura + optimizaciones Fase 1 y 2
- ✅ Bedrock prompt caching (crítico en este rango)
- ✅ CloudWatch Dashboard completo
- ✅ 5 alarms de monitoreo
- ⚠️ Evaluar Fase 3 (Textract replacement) al alcanzar 5K docs/mes

**Costo proyectado:**
- Sin optimizaciones: $26.65–$111.50/mes
- Con Fases 1+2: $17.35–$64.36/mes
- **Ahorro:** 35–42%

**ROI:** 2 días de trabajo dev → $112–$566/año de ahorro

### 10,000–50,000 docs/mes (Enterprise)

**Configuración recomendada:**
- ✅ Todas las optimizaciones (Fases 1+2+3)
- ✅ Bedrock Batch API para docs no urgentes (30% del volumen)
- ✅ Evaluación mensual de Provisioned Throughput (break-even 45K docs)
- ✅ Monitoring completo + alertas avanzadas
- ⚠️ Considerar multi-región para disaster recovery

**Costo proyectado:**
- Sin optimizaciones: $111.50–$531.75/mes
- Con todas las optimizaciones: $14.80–$62.57/mes
- **Ahorro:** 87–88%

**ROI:** 5 días de trabajo dev → $1,160–$5,630/año de ahorro

**Evaluaciones adicionales:**
- **> 45K docs/mes sostenidos:** Provisioned Throughput Bedrock
- **> 100K docs/mes:** Multi-región + DynamoDB Global Tables
- **Compliance estricto:** Considerar VPC (añade $70/mes fijo)


---

## Supuestos y exclusiones

### Supuestos:
- Distribución uniforme: ~41 docs/día para baseline 1,240 docs/mes
- Bedrock tokens promedio: 3,580 input + 270 output por documento
- Textract AnalyzeID: 10% de documentos (fallback por calidad OCR)
- Query API: 2 calls/documento promedio (consulta inicial + seguimiento)
- Webhooks: 50% de documentos trigger notificación externa
- Lambda dentro de free tier mensual: 1M requests, 400K GB-s
- Step Functions dentro de free tier mensual: 4,000 state transitions
- Sin reprocesos masivos (cada documento procesado 1 vez)
- APIs no expuestas públicamente sin autenticación (sin WAF)
- Retención CloudWatch Logs: 30 días
- DynamoDB sin TTL (crecimiento lineal de storage)

### Exclusiones importantes:
- **IVA:** 16% adicional en México
- **AWS Support Plans:** Developer ($29/mes), Business ($100/mes), Enterprise ($15K+/mes)
- **Disaster Recovery:** S3 Cross-Region Replication, DynamoDB Global Tables
- **WAF:** Web Application Firewall si se exponen APIs públicamente ($5/mes + $1/1M requests)
- **Ambientes adicionales:** Dev/Staging (estimar 30% del costo de producción)
- **Transferencia de datos OUT:** Webhooks salientes a internet (primeros 100 GB/mes gratis)
- **CloudFront CDN:** Si se implementa frontend web ($0.085/GB en México)
- **Route 53:** DNS hosted zone ($0.50/mes) + queries ($0.40/1M queries)
- **Certificados SSL privados:** ACM público sin cargo
- **CI/CD pipeline:** CodePipeline ($1/pipeline/mes), CodeBuild ($0.005/min), GitHub Actions
- **Pruebas de carga:** Ejecuciones masivas de testing
- **Auditorías de seguridad:** Penetration testing, compliance audits
- **Consultoría externa:** AWS Professional Services, soporte de terceros
- **Backup adicional:** AWS Backup para S3/DynamoDB (opcional)

### Límites considerados:
- **Lambda concurrency:** 1,000 concurrent executions por región (default)
- **API Gateway:** 10,000 requests/segundo burst (default)
- **Step Functions:** 1,000 ejecuciones concurrentes (default)
- **DynamoDB:** 40,000 RCU/WCU On-Demand por tabla (auto-scaling)
- **S3:** Sin límite de objetos por bucket

---

## Recursos y referencias

### AWS Pricing
- [AWS Pricing Calculator](https://calculator.aws) — Calculadora oficial
- [Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/) — Modelos y regiones
- [Bedrock Prompt Caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)
- [Lambda Pricing](https://aws.amazon.com/lambda/pricing/)
- [DynamoDB Pricing](https://aws.amazon.com/dynamodb/pricing/)
- [Step Functions Pricing](https://aws.amazon.com/step-functions/pricing/)
- [Textract Pricing](https://aws.amazon.com/textract/pricing/)
- [API Gateway Pricing](https://aws.amazon.com/api-gateway/pricing/)
- [CloudWatch Pricing](https://aws.amazon.com/cloudwatch/pricing/)

### Optimización y best practices
- [AWS Well-Architected Framework - Cost Optimization](https://wa.aws.amazon.com/wat.pillar.costOptimization.en.html)
- [Serverless Cost Optimization](https://aws.amazon.com/blogs/compute/optimizing-your-aws-lambda-costs-part-1/)
- [Bedrock Best Practices](https://docs.aws.amazon.com/bedrock/latest/userguide/best-practices.html)
- [DynamoDB Best Practices](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices.html)

### Monitoreo
- [AWS Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/)
- [AWS Budgets](https://aws.amazon.com/aws-cost-management/aws-budgets/)
- [CloudWatch Cost Anomaly Detection](https://aws.amazon.com/about-aws/whats-new/2020/12/aws-announces-cloudwatch-cost-anomaly-detection/)

### Arquitectura
- [Bedrock Document Processing Blueprint](https://github.com/cdklabs/cdk-appmod-catalog-blueprints)
- [Serverless Document Processing Reference](https://aws.amazon.com/solutions/implementations/document-understanding-solution/)

---

**Documento generado:** Junio 2, 2026  
**Versión:** 4.0 — Arquitectura Serverless Completa  
**Próxima revisión:** Revisar mensualmente con datos reales de Cost Explorer  
**Contacto:** Validar supuestos con arquitecto AWS antes de implementar optimizaciones

