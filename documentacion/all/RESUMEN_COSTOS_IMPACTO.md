# Resumen Simple de Costos e Impacto — Documents Validator Serverless

**Arquitectura:** 100% Serverless (sin VPC)  
**Región:** México Central (mx-central-1)  
**Baseline:** 1,240 documentos/mes  
**Fecha:** Junio 3, 2026

---

## 💰 Cuánto voy a pagar

### Tabla simple de costos mensuales

| Documentos/mes | Sin optimizar | Optimizado | Ahorro |
|---------------:|-------------:|-----------:|-------:|
| 1,000 | $19.16 | $6.38 | 67% |
| **1,240** | **$21.42** | **$6.80** | **68%** |
| 2,000 | $28.61 | $7.55 | 74% |
| 5,000 | $56.95 | $11.06 | 81% |
| 10,000 | $104.19 | $14.80 | 86% |

### Fórmula rápida

**Sin optimizar:**
```
Costo = $9.71 fijo + ($0.01055 × documentos)
```

**Optimizado (recomendado):**
```
Costo = $1.21 fijo + ($0.00117 × documentos)
```

**Ejemplo:** 3,000 documentos/mes
- Sin optimizar: $9.71 + $31.65 = **$41.36/mes**
- Optimizado: $1.21 + $3.51 = **$4.72/mes** (89% ahorro)

---

## 📊 ¿Qué cuesta en cada documento?

### Desglose por componente

| Componente | Costo sin opt. | Costo opt. | ¿Qué es? |
|------------|---------------:|-----------:|----------|
| **Bedrock (IA)** | $0.00542 | $0.00177 | Claude para extraer datos |
| **Textract (OCR)** | $0.00400 | $0.00019 | Backup cuando imagen borrosa |
| **Lambda** | $0.00018 | $0.00018 | Código de validación |
| **Step Functions** | $0.00032 | $0.00032 | Orquestar flujo |
| **Otros** | $0.00074 | $0.00074 | S3, DynamoDB, APIs |
| **Total/doc** | **$0.01066** | **$0.00320** | **70% ahorro** |

### Costos fijos mensuales

| Componente | Sin opt. | Opt. | Descripción |
|------------|----------:|-----:|-------------|
| KMS (encriptación) | $5.00 | $1.00 | 5 llaves → 1 llave |
| CloudWatch Dashboard | $3.00 | $0.00 | Panel opcional |
| CloudWatch Alarms | $1.50 | $0.00 | Alertas opcionales |
| DynamoDB Storage | $0.21 | $0.21 | Base de datos |
| **Total fijo/mes** | **$9.71** | **$1.21** | **87% ahorro** |


---

## 📈 Análisis de Impacto: ¿Qué pasa si aumenta el uso?

### 1️⃣ Impacto de TOKENS Bedrock (entrada/salida)

**Uso actual:** 3,580 tokens entrada + 270 tokens salida por documento

| Escenario | Tokens entrada | Tokens salida | Costo/doc sin cache | Costo/doc con cache | Impacto mensual @ 1,240 docs |
|-----------|---------------:|--------------:|--------------------:|--------------------:|-----------------------------:|
| **Prompts cortos (-50%)** | 1,790 | 135 | $0.00271 | $0.00088 | **-$3.36/mes** ✅ |
| **Actual (baseline)** | 3,580 | 270 | $0.00542 | $0.00177 | — |
| **Prompts largos (+50%)** | 5,370 | 405 | $0.00813 | $0.00266 | **+$3.36/mes** ⚠️ |
| **Muy largos (+100%)** | 7,160 | 540 | $0.01084 | $0.00354 | **+$6.72/mes** ⚠️ |

**💡 Conclusión:** 
- Con prompt caching, el impacto se reduce 67%
- Cada 1,000 tokens extra = +$0.40/mes @ 1,240 docs
- **Recomendación:** Mantener prompts <4,000 tokens e implementar caching

---

### 2️⃣ Impacto de ALMACENAMIENTO S3 (GB)

**Uso actual:** 100 KB por documento

| Tamaño/doc | Total storage @ 1,240 docs | Costo S3 storage/mes | Costo S3 requests/mes | Total/mes | Δ vs actual |
|-----------:|---------------------------:|---------------------:|----------------------:|----------:|------------:|
| **50 KB** | 62 MB | $0.001 | $0.01 | $0.011 | -50% |
| **100 KB (actual)** | 124 MB | $0.003 | $0.02 | $0.023 | — |
| **500 KB** | 620 MB | $0.015 | $0.02 | $0.035 | +52% |
| **1 MB** | 1.24 GB | $0.030 | $0.02 | $0.050 | +117% |
| **5 MB** | 6.2 GB | $0.150 | $0.02 | $0.170 | +639% |

**Escalando a 10,000 docs:**

| Tamaño/doc | Total storage | Costo storage/mes | Impacto |
|-----------:|--------------:|------------------:|---------|
| 100 KB | 1 GB | $0.02 | Mínimo |
| 500 KB | 5 GB | $0.12 | Bajo |
| 1 MB | 10 GB | $0.24 | Bajo |
| 5 MB | 50 GB | $1.20 | Moderado |
| 10 MB | 100 GB | $2.40 | Alto |

**💡 Conclusión:**
- S3 storage es **extremadamente barato** ($0.024/GB/mes)
- Incluso con documentos de 5MB, el costo es <$2/mes @ 10K docs
- **No es un factor crítico de optimización**

---

### 3️⃣ Impacto de REQUESTS API (consultas)

**Uso actual:** 2 queries API por documento

| Queries/doc | Total requests/mes @ 1,240 | Costo API Gateway | Costo Lambda | Total/mes | Δ vs actual |
|------------:|---------------------------:|------------------:|-------------:|----------:|------------:|
| **1 (mínimo)** | 1,240 | $0.005 | $0.002 | $0.007 | -50% |
| **2 (actual)** | 2,480 | $0.009 | $0.004 | $0.013 | — |
| **5 (intensivo)** | 6,200 | $0.023 | $0.009 | $0.032 | +146% |
| **10 (muy intensivo)** | 12,400 | $0.046 | $0.018 | $0.064 | +392% |
| **50 (extremo)** | 62,000 | $0.228 | $0.089 | $0.317 | +2,338% |

**Escalando a 10,000 docs:**

| Queries/doc | Total requests/mes | Costo total/mes | Impacto |
|------------:|-------------------:|----------------:|---------|
| 2 (actual) | 20,000 | $0.09 | Mínimo |
| 5 | 50,000 | $0.23 | Mínimo |
| 10 | 100,000 | $0.46 | Bajo |
| 50 | 500,000 | $2.31 | Moderado |

**💡 Conclusión:**
- APIs serverless son **casi gratis**
- Incluso con 10 queries/doc, solo añade $0.46/mes @ 10K docs
- API Gateway + Lambda tienen pricing muy competitivo
- **No limites queries por costo**, limítalas por experiencia de usuario

---

### 4️⃣ Impacto de TRANSFERENCIA DE DATOS (webhooks salientes)

**Uso actual:** 50% documentos envían webhook con 5 KB payload

| Payload/webhook | Webhooks/mes @ 1,240 | Total GB transferido | Costo transfer/mes | ¿Se cobra? |
|----------------:|---------------------:|---------------------:|-------------------:|------------|
| **1 KB** | 620 | 0.0006 GB | $0.00 | ❌ Free |
| **5 KB (actual)** | 620 | 0.003 GB | $0.00 | ❌ Free |
| **50 KB** | 620 | 0.031 GB | $0.00 | ❌ Free |
| **500 KB** | 620 | 0.31 GB | $0.00 | ❌ Free |
| **5 MB** | 620 | 3.1 GB | $0.00 | ❌ Free |
| **50 MB** | 620 | 31 GB | $0.00 | ❌ Free |

**Escalando a 10,000 docs con 5,000 webhooks/mes:**

| Payload/webhook | Total GB | Costo/mes | ¿Cuándo se cobra? |
|----------------:|---------:|----------:|-------------------|
| 5 KB | 0.024 GB | $0.00 | Nunca (< 100GB) |
| 50 KB | 0.24 GB | $0.00 | Nunca |
| 500 KB | 2.4 GB | $0.00 | Nunca |
| 5 MB | 24 GB | $0.00 | Nunca |
| 25 MB | 120 GB | **$1.70** | ✅ Sí (> 100GB) |

**💡 Conclusión:**
- **Primeros 100 GB/mes son GRATIS**
- Solo pagas $0.085/GB después de 100GB
- Para superar 100GB necesitas: 
  - 20,000 webhooks × 5MB cada uno
  - O 200,000 webhooks × 500KB cada uno
- **Prácticamente no cobrable para uso normal**

---

### 5️⃣ Impacto de TEXTRACT FALLBACK RATE (% docs con OCR)

**Uso actual:** 10% documentos requieren Textract AnalyzeID ($0.04/página)

| Fallback % | Docs con Textract @ 1,240 | Costo Textract/mes | Δ vs actual | Causa típica |
|-----------:|--------------------------:|-------------------:|------------:|--------------|
| **0% (ideal)** | 0 | $0.00 | **-$4.96** ✅ | Bedrock OCR perfecto |
| **2% (optimizado)** | 25 | $1.00 | **-$3.96** ✅ | Prompts mejorados |
| **5%** | 62 | $2.48 | **-$2.48** ✅ | Buena calidad imagen |
| **10% (actual)** | 124 | $4.96 | — | Baseline |
| **20%** | 248 | $9.92 | **+$4.96** ⚠️ | Imágenes de mala calidad |
| **50%** | 620 | $24.80 | **+$19.84** 🔴 | Escaneos muy borrosos |

**Escalando a 10,000 docs:**

| Fallback % | Docs con Textract | Costo/mes | Impacto |
|-----------:|------------------:|----------:|---------|
| 0% | 0 | $0.00 | **-$40.00** |
| 2% | 200 | $8.00 | **-$32.00** |
| 10% (actual) | 1,000 | $40.00 | — |
| 20% | 2,000 | $80.00 | **+$40.00** |
| 50% | 5,000 | $200.00 | **+$160.00** |

**💡 Conclusión:**
- **Textract es EL FACTOR MÁS CARO** (23-41% del costo total)
- Reducir fallback de 10% → 2% ahorra **$3.96/mes @ 1,240 docs**
- Usar DetectDocumentText ($0.0015/página) en lugar de AnalyzeID ahorra **97%**
- **PRIORIDAD #1 de optimización**

**Estrategias de optimización:**
1. ✅ Mejorar prompts Bedrock para extraer mejor sin Textract
2. ✅ Usar DetectDocumentText + Bedrock para extracción estructurada
3. ✅ Implementar caché de resultados Textract
4. ✅ Validar calidad de imagen antes de procesar

