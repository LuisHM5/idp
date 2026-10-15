# ¿Cómo funciona Document Validator? — Explicación general (high level)

Documento para entender el sistema completo sin leer código: qué hace, cómo fluye un documento, cómo decide si algo es válido y cómo se usa.

---

## 1. ¿Qué hace el sistema en una frase?

> Recibe un PDF (un CFDI, un pedimento o un expediente aduanal completo de decenas de páginas), **encuentra qué documentos contiene, extrae sus campos, los valida con reglas configurables** y devuelve un resultado con evidencia y nivel de confianza — **sin inventar jamás un dato que no esté en el documento**.

---

## 2. Arquitectura general

Todo es serverless en AWS, sin servidores que administrar: el archivo se sube a
S3 y eso invoca directamente a la Lambda que lo procesa; el resultado queda en
DynamoDB/S3 y se avisa por EventBridge. El diagrama y el detalle de cada pieza
viven en un solo lugar: [`ARQUITECTURA.md`](ARQUITECTURA.md).

---

## 3. El pipeline de expedientes (el corazón del sistema)

Un "expediente" aduanal es UN PDF con MUCHOS documentos adentro (pedimento + facturas + acuses + permisos...). El pipeline lo desarma y procesa cada uno:

```mermaid
flowchart TD
    A["PDF (1 a 100+ páginas)"] --> B{"¿La página tiene<br/>texto digital?"}
    B -->|"Sí (gratis)"| C["pypdf<br/>texto nativo"]
    B -->|"No (escaneada)"| D["Textract OCR<br/>solo esas páginas"]
    C --> E["Texto de cada página"]
    D --> E
    E --> F["🔍 SEGMENTACIÓN<br/>Bedrock divide el PDF en documentos:<br/>'p1-2 factura agencia, p3-7 pedimento...'"]
    F --> G["📥 EXTRACCIÓN por documento<br/>Bedrock llena el schema del catálogo:<br/>campos simples + tablas (partidas, conceptos)"]
    G --> H["🔧 RECUPERACIÓN determinística<br/>regex rescata referencias de pedimento<br/>que el modelo dejó vacías"]
    H --> I["⚖️ MOTOR DE REGLAS<br/>regex, fórmulas, sumas,<br/>reglas cruzadas entre documentos"]
    I --> J["📊 RESOLUCIÓN<br/>estado por campo, por documento<br/>y del expediente completo"]
```

**Ejemplo real** (`pedimentos.pdf`, 86 páginas): el sistema detectó 23 documentos — factura de la agencia aduanal, 3 copias del pedimento, factura del proveedor extranjero, CFDIs de terceros, comprobantes de pago SPEI, acuses VUCEM — y validó que **todos pertenecen al mismo pedimento `260734356041248`** y al mismo importador.

### ¿Por qué es costo-eficiente?

| Recurso | Expediente de 86 páginas | Costo |
|---|---|---|
| pypdf (texto nativo) | 68 páginas digitales | **$0.00** — es gratis |
| Textract OCR | solo las 18 páginas escaneadas | ~$0.03 |
| Bedrock Haiku 4.5 (segmentar + extraer; con Haiku 5.5 es ~10x menos por token) | todo el texto | ~$0.32 |
| **Total** | | **~$0.35 USD** |

- El texto de PDFs digitales es **gratis** (pypdf); Textract solo toca páginas escaneadas — subir PDFs digitales en vez de escaneos reduce ~10x el costo.
- Un CFDI de 1 página cuesta ~$0.02.

---

## 4. El motor de reglas (validación configurable)

Las reglas viven en la carpeta **`catalogs/`** (un JSON por tipo) — se agregan o cambian **sin tocar código ni redesplegar**:

```mermaid
flowchart LR
    subgraph Catalogo["📋 Catálogo JSON (por tipo de documento)"]
        A["extraction_fields<br/>(qué extraer)"]
        B["extraction_hints<br/>(dónde buscar)"]
        C["validation_rules<br/>(cómo validar)"]
        D["cross_document_rules<br/>(validar ENTRE documentos)"]
    end
    Catalogo -->|"pnpm sync<br/>(hot-reload)"| E[("DynamoDB")]
    Catalogo -->|"pnpm build + deploy<br/>(empaquetado)"| F["Lambda"]
```

**Tipos de regla disponibles** (todas en JSON):

| Categoría | Reglas | Ejemplo |
|---|---|---|
| Formato | `regex`, `regex_each`, `presence` | RFC válido, pedimento de 15 dígitos |
| Fechas | `date_expiry`, `date_max_age` | permiso no vencido |
| Matemáticas | `custom_formula`, `cfdi_total`, `sum_array` | subtotal + IVA = total (con tolerancia) |
| Entre documentos | `cross_document_match`, `cross_document_amount` | el CFDI referencia al pedimento del expediente |
| Negocio | `expected_value_by_app`, `negative_keyword`, `minimum_items` | el RFC receptor es TU empresa |

**Reglas de expediente completo** (automáticas): mismo pedimento en todos los documentos, RFC del importador consistente, CFDIs duplicados (distingue copia legítima de doble cobro), conciliación de gastos vs. cuenta del agente.

---

## 5. Estados y política anti-invención

### El principio nº 1: nunca entregar un dato inventado como válido

```mermaid
flowchart TD
    A["¿El dato está impreso<br/>en el documento?"] -->|No| B["null + NOT_FOUND<br/>(jamás se inventa)"]
    A -->|Sí| C["extractedValue<br/>= texto EXACTO del documento"]
    C --> D["normalizedValue<br/>= copia normalizada determinística<br/>(monto→número, fecha→ISO)<br/>NUNCA sustituye al original"]
    D --> E{"¿Pasa reglas +<br/>confianza ≥ umbral?"}
    E -->|Sí| F["✅ VALIDATED"]
    E -->|"Falla regla"| G["❌ INVALID"]
    E -->|"Confianza baja"| H["⚠️ LOW_CONFIDENCE"]
```

### Estados por campo

`VALIDATED` · `INVALID` · `LOW_CONFIDENCE` · `NOT_FOUND` · `NOT_APPLICABLE` · `REVIEW_REQUIRED`

### Estados por documento y expediente

```mermaid
stateDiagram-v2
    [*] --> APPROVED: todo validado
    [*] --> PARTIALLY_APPROVED: unos campos bien,<br/>otros fallaron
    [*] --> REVIEW_REQUIRED: dudas o baja confianza<br/>(un humano decide)
    [*] --> REJECTED: reglas críticas fallaron
    [*] --> PROCESSING_ERROR: no se pudo procesar
```

> El sistema es **conservador a propósito**: ante la duda marca `REVIEW_REQUIRED` en vez de aprobar. Un dato dudoso nunca sale como bueno.

### Cada campo devuelve evidencia completa

```json
{
  "extractedValue": "26 07 3435 6041248",
  "normalizedValue": "260734356041248",
  "status": "VALIDATED",
  "confidence": 0.98,
  "checks": [
    { "rule": "presence", "valid": true },
    { "rule": "regex", "valid": true, "message": "15 dígitos" }
  ]
}
```

---

## 6. Flujo de uso completo (lo que haces tú)

```mermaid
sequenceDiagram
    actor U as Tú
    participant S3 as S3 source/
    participant P as Pipeline
    participant R as Resultado

    U->>S3: 1. Subir PDF<br/>(pnpm test-e2e o aws s3 cp)
    Note over S3,P: automático (0 intervención)
    S3->>P: dispara procesamiento
    P->>P: segmenta + extrae + valida<br/>(1 pág ≈ 30s · 86 págs ≈ 6-8 min)
    P->>R: resumen → EventBridge<br/>detalle completo → S3 results/
    U->>R: 2. pnpm resultado -- --id <documentId>
    R-->>U: estados + campos + evidencia
```

### Comandos del día a día

| Quiero… | Comando |
|---|---|
| Subir un documento (<4 MB) | `pnpm test-e2e --file doc.pdf --no-wait` |
| Subir uno grande (≥4 MB) | `aws s3 cp doc.pdf s3://<bucket>/raw/$(uuidgen)/doc.pdf` |
| Ver el resultado | `pnpm resultado -- --id <documentId> --wait` |
| Probar local sin el stack | `pnpm procesar -- doc.pdf --importer MI_RFC` |
| Cambiar reglas sin redeploy | editar el tipo bajo `catalogs/base/` o `catalogs/custom/` + `pnpm sync` |
| Crear un tipo nuevo | copiar `catalogs/_TEMPLATE.json` → `catalogs/base/<dominio>/<TIPO>.json` o `catalogs/custom/<integración>/<TIPO>.json` + `pnpm sync:one` |
| Desplegar cambios de código | `pnpm build && pnpm run deploy` |

---

## 7. Ejemplo real: ¿dónde queda mi número de pedimento?

Procesando `pedimentos.pdf` (86 páginas), el número `260734356041248` queda en:

```
resultado
└── expediente
    └── documents[]
        └── { documentType: "PEDIMENTO_ADUANAL", pageStart: 3, pageEnd: 7 }
            └── fields
                └── NUMERO_PEDIMENTO_NORMALIZADO
                    ├── extractedValue:  "260734356041248"
                    ├── normalizedValue: "260734356041248"
                    ├── status:          "VALIDATED"     ← úsalo solo si dice esto
                    └── confidence:      0.98
```

Tu aplicación consumidora: busca el documento con `documentType == "PEDIMENTO_ADUANAL"` y lee `fields.NUMERO_PEDIMENTO_NORMALIZADO.normalizedValue` **solo cuando `status == "VALIDATED"`**.

---

## 8. Mapa de archivos del proyecto

```
documents-validator/
├── catalogs/                          ← ⭐ TUS REGLAS (un JSON por tipo, fuente de verdad)
├── src/
│   ├── document-validation-stack.ts      ← infraestructura (CDK)
│   └── document-processing/resources/enrichment-lambda/
│       ├── pipeline/                     ← pipeline de expedientes (OCR, segmentar, extraer)
│       ├── validators/engine.py          ← motor de reglas dinámico
│       └── config/catalogs.json        ← copia generada del catálogo (NO editar)
├── scripts/                              ← CLI: subir, procesar, ver resultado, catálogo
├── tests/                                ← tests unitarios (enfocados en PEDIMENTO_ADUANAL) + integración con docs reales
└── documentacion/
    ├── COMO_FUNCIONA.md                  ← este documento
    └── GUIA_DESPLIEGUE_Y_USO.md          ← guía operativa paso a paso
```

---

## 9. Límites y decisiones conocidas

| Tema | Situación |
|---|---|
| Archivos por API | Sin tope de tamaño real: `POST /documents` devuelve una URL prefirmada y el archivo va con un `PUT` directo a S3, nunca por API Gateway/Lambda. Mismos dos pasos para cualquier tamaño |
| Páginas por documento (expedientes/pedimentos) | **Si el tipo declarado selecciona sus páginas** (`page_selection`): no se trunca — el costo lo acota el selector, con un tope de salvaguarda muy superior (`max_source_pages`, 500). **Si no** (tipo sin selección, apagada, o subida sin tipo declarado): máximo 50 (`MAX_PAGES`, configurable); el excedente se trunca antes de correr Textract/Bedrock para acotar costo — el original completo queda intacto en S3, resultado marcado `reviewRequired` + `truncated` |
| Tiempo | 1 página ≈ 30 s · 27 págs escaneadas ≈ 4 min · 86 págs ≈ 6-8 min (límite Lambda: 15 min) |
| Páginas `OTRO` | COVE, DODA, SPEI internos no tienen tipo en el catálogo → `REVIEW_REQUIRED` (agregables vía JSON) |
| Reglas cross-documento | Solo viven en el JSON empaquetado (cambiarlas requiere `pnpm build && pnpm run deploy`) |
| Precisión | Ningún modelo garantiza 0 errores; por eso: confianza por campo + reglas determinísticas + abstención (`NOT_FOUND`) + revisión humana |
```
