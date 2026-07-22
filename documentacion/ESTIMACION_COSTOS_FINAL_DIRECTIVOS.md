# 💰 Costo de Infraestructura — Resumen Ejecutivo

**Proyecto:** Validador de Documentos (IDP) — Arquitectura **100% Serverless**
**Regiones evaluadas:** `mx-central-1` (México) y `us-east-1` (Virginia)
**Precios:** reales, AWS Price List API (jun-2026) · **Modelo IA:** Claude Haiku 4.5
**Sin EC2, sin servidores, sin VPC, sin costo fijo de infraestructura.**

---

## 1) El número final

| Escenario (volumen mensual) | Costo mensual (México) | Costo por documento |
|---|---:|---:|
| **Baseline — 1,240 docs/mes** | **≈ $16 USD/mes** | $0.013 / doc |
| Piloto — 1,000 docs/mes | ≈ $14 USD/mes | $0.014 / doc |
| Crecimiento — 5,000 docs/mes | ≈ $46 USD/mes | $0.009 / doc |
| Escala — 10,000 docs/mes | ≈ $86 USD/mes | $0.009 / doc |

> 💡 **Pagas solo por lo que usas.** El costo crece de forma **lineal y predecible** con el volumen, y el **costo por documento baja** a mayor escala. Sin IVA (agregar 16% en México).

**Regla de bolsillo:**

```
Costo mensual (México) ≈  $6 fijo  +  $0.008 × (documentos del mes)
```

---

## 2) Costo total por volumen y región

| Documentos/mes | 🇲🇽 mx-central-1 | 🇺🇸 us-east-1 | $/doc |
|---:|---:|---:|---:|
| 1,000 | $13.99 | $13.96 | $0.0140 |
| **1,240** | **$15.91** | **$15.87** | **$0.0128** |
| 2,000 | $21.98 | $21.92 | $0.0110 |
| 5,000 | $45.94 | $45.80 | $0.0092 |
| 10,000 | $85.89 | $85.60 | $0.0086 |
| 50,000 | $405.45 | $404.01 | $0.0081 |

> México cuesta **~0.3% más** que Virginia. La diferencia es mínima porque los dos rubros más caros (IA y Textract) tienen **precio global**.

---

## 3) ¿En qué se va el dinero? (baseline 1,240 docs/mes, México)

| Componente | Costo/mes | % del total | ¿Crece con el volumen? |
|---|---:|---:|---|
| 🧠 **Bedrock (IA — Claude Haiku 4.5)** | $6.11 | **38%** | ✅ Sí — el principal |
| 📄 **Textract (OCR, solo 10% de docs)** | $3.10 | **19%** | ✅ Sí |
| 🔧 **Costos fijos** (ver sección 5) | $6.00 | **38%** | ❌ No (se diluye al escalar) |
| ⚙️ Step Functions (orquestación) | $0.39 | 2% | ✅ Sí |
| ⚡ Lambda (cómputo) | $0.26 | 2% | ✅ Sí |
| 🗄️ DynamoDB (base de datos) | $0.00 | <1% | ✅ Sí (casi gratis) |
| 📦 S3 (almacenamiento) | $0.02 | <1% | ✅ Sí (casi gratis) |

> **El 57% del costo variable es IA + OCR.** Lambda, DynamoDB y S3 juntos son **menos del 3%**.

---

## 4) 🚀 Qué pasa si **sube el volumen** (detonación de costos)

A más documentos, suben las peticiones a Lambda, las lecturas/escrituras y el GB almacenado.
Aquí está el detalle real por servicio:

### ⚡ Lambda — peticiones y cómputo
| Docs/mes | Invocaciones | Costo/mes (MX) |
|---:|---:|---:|
| 1,240 | ~11,400 | $0.26 |
| 10,000 | ~92,000 | $2.12 |
| 50,000 | ~460,000 | $10.60 |

- Precio real: **$0.21 / millón de peticiones** + **$0.0000175 / GB-seg** (MX).
- Las primeras **1,000,000 peticiones/mes son gratis** (free tier) → en volúmenes bajos Lambda es prácticamente $0.

### 🗄️ DynamoDB — uso (peticiones) + almacenamiento (GB)
| Docs/mes | Uso (lecturas+escrituras) | Almacenamiento agregado | Costo/mes (MX) |
|---:|---:|---:|---:|
| 1,240 | ~10K operaciones | +0.01 GB/mes | ~$0.00 |
| 10,000 | ~80K operaciones | +0.08 GB/mes | $0.04 |
| 50,000 | ~400K operaciones | +0.40 GB/mes | $0.18 |

- Precio real (On-Demand, MX): **$0.655 / millón de escrituras**, **$0.1315 / millón de lecturas**.
- Almacenamiento: **$0.2625 / GB-mes**, con los **primeros 25 GB gratis** → el storage es **$0** durante mucho tiempo.
- 👉 Aun a 50,000 docs/mes, DynamoDB cuesta **menos de $0.20/mes**.

### 📦 S3 — uso (peticiones) + almacenamiento (GB)
| Docs/mes | Peticiones (PUT+GET) | Almacenamiento agregado | Costo/mes (MX) |
|---:|---:|---:|---:|
| 1,240 | ~8,700 | +0.13 GB/mes | $0.016 |
| 10,000 | ~70,000 | +1.05 GB/mes | $0.13 |
| 50,000 | ~350,000 | +5.25 GB/mes | $0.63 |

- Precio real (MX): almacenamiento **$0.02415 / GB-mes**, PUT **$5.25 / millón**, GET **$0.42 / millón**.
- 👉 Cada documento ocupa ~105 KB. Incluso acumulando un año, el storage es de **centavos**.

> ✅ **Conclusión clave para directivos:** aunque suba el volumen, **Lambda + DynamoDB + S3 nunca son el problema** (juntos <3%). El costo siempre lo dominan **la IA (Bedrock) y el OCR (Textract)**, que escalan linealmente con cada documento procesado.

---

## 5) 💵 Costos fijos (no dependen del volumen)

| Concepto | Costo/mes | Necesario |
|---|---:|---|
| 🔐 KMS (1 llave de cifrado) | $1.00 | Sí (seguridad) |
| 🔑 Secrets Manager (credenciales) | $0.40 | Sí |
| 📊 CloudWatch Dashboard | $3.00 | Opcional |
| 🚨 CloudWatch Alarms | $0.60 | Recomendado |
| **Total fijo base** | **≈ $6.00** | |

- Si se quitan Dashboard y alarmas (ej. ambiente de bajo volumen): el fijo baja a **~$1.40/mes**.
- Cada llave KMS adicional cuesta **$1/mes** (hoy se usa 1 consolidada).
- A **10,000 docs/mes** los fijos son solo el **7%** del total; a **50,000**, apenas **1.5%**.

---

## 6) ⚠️ Costos que podrían aparecer (hoy NO incluidos)

| Concepto | Costo aprox. | Cuándo aplica |
|---|---:|---|
| **IVA (México)** | +16% | Siempre, sobre el total |
| AWS Support (Business) | $100/mes | Si se requiere soporte 24/7 |
| WAF (firewall de APIs) | $5/mes + uso | Si se exponen APIs públicas |
| Ambientes Dev/Staging | ~30% del prod | Si se replica el entorno |
| Route 53 / dominio | ~$0.50/mes | Si hay dominio propio |
| Disaster Recovery (multi-región) | +$70-100/mes | Solo enterprise / compliance |
| Bedrock Provisioned | ~$240/mes fijo | Solo si supera **45,000 docs/mes** sostenidos |

---

## 7) 📉 Palancas de ahorro (si se busca optimizar)

1. **Prompt caching en Bedrock** → hasta **20% menos** en IA (el rubro #1).
2. **Reducir el fallback de Textract** del 10% al 2% → ahorra ~**$2.5/mes** @ 1,240 docs (más a mayor escala).
3. **TTL en DynamoDB** (borrado automático) → mantiene el storage en $0 indefinidamente.
4. **Quitar Dashboard** en ambientes no críticos → **-$3/mes**.

> Con estas palancas, el baseline puede bajar de **~$16 a ~$11/mes**.

---

## 📌 Mensaje para la dirección

> La plataforma corre **100% serverless**: **sin servidores que mantener y sin costo fijo de infraestructura**.
> El gasto es **proporcional al uso** (~**$0.013 por documento**, bajando a **~$0.009** a escala).
> El **80% del costo es la inteligencia artificial y el OCR** que procesan cada documento; la infraestructura de datos (Lambda, base de datos y almacenamiento) es **marginal (<3%)** incluso multiplicando el volumen.
> **Riesgo de costo bajo y totalmente predecible.**

---

*Precios obtenidos del AWS Price List API (jun-2026). On-Demand, sin descuentos por compromiso. Bedrock y Textract se facturan a precio global (cross-region inference desde EE.UU., ya que no existe endpoint en mx-central-1). Cifras sin IVA. Revisar mensualmente contra AWS Cost Explorer con datos reales de consumo.*
