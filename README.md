# Document Validator

Sistema extensible de validación y extracción inteligente de documentos mexicanos usando Amazon Bedrock + validación determinística configurable.

> 🧭 **¿Cómo funciona? (explicación general con diagramas)**: [`documentacion/COMO_FUNCIONA.md`](documentacion/COMO_FUNCIONA.md)
>
> 📘 **Guía completa de despliegue y operación** (subir, actualizar, eliminar total/parcial, catálogos, reglas, troubleshooting): [`documentacion/GUIA_DESPLIEGUE_Y_USO.md`](documentacion/GUIA_DESPLIEGUE_Y_USO.md)

---

## Pipeline de Pedimentos / Expedientes Aduanales

Los tipos aduanales (los que declaran `"pipeline": "expediente"` en `catalogs/`) se procesan con un pipeline dedicado (`pipeline/` dentro de la enrichment lambda) diseñado para PDFs multi-documento, escaneados o digitales:

```
PDF → texto por página (pypdf layout / Textract solo páginas escaneadas)
    → segmentación (Bedrock clasifica rangos de páginas por tipo)
    → extracción por documento (schema del catálogo: escalares + arrays)
    → recuperación determinística (referencias de pedimento por regex)
    → motor de reglas dinámico (fórmulas, sumas, regex, cross-documento)
    → resolución por campo y por documento + checks de expediente
```

Características clave:

- **Costo-eficiente**: el texto nativo del PDF es gratis; Textract solo se usa en páginas sin capa de texto. Un expediente de 86 páginas cuesta ~$0.35 USD (~$0.02 un CFDI de 1 página).
- **Anti-invención**: el LLM copia valores textuales o devuelve `null`; la normalización (montos, fechas, número de pedimento 15 dígitos, UUIDs con guiones Unicode) es determinística y nunca sustituye el valor extraído (`extractedValue` vs `normalizedValue`).
- **Estados por campo**: `VALIDATED`, `INVALID`, `LOW_CONFIDENCE`, `NOT_FOUND`, `NOT_APPLICABLE`, `REVIEW_REQUIRED` — con confianza combinada (OCR × LLM).
- **Estados por documento/expediente**: `APPROVED`, `REJECTED`, `PARTIALLY_APPROVED`, `REVIEW_REQUIRED`, `PROCESSING_ERROR`.
- **Reglas dinámicas** (JSON, sin redeploy): `regex`, `regex_each`, `presence`, `minimum_items`, `custom_formula`, `cfdi_total`, `sum_array`, `cross_document_match`, `cross_document_amount`, `date_expiry`, `date_max_age`, `negative_keyword`, `expected_value_by_app`, `page_ranges_no_overlap`, `boolean_true`.
- **Checks de expediente**: mismo pedimento en todos los documentos (match por consecutivo de 7 dígitos), RFC del importador esperado (ignora RFCs genéricos `XEXX`/`XAXX`), CFDIs duplicados (distingue copias legítimas de doble cobro), conciliación de gastos y de valores contra facturas extranjeras.

### Tests

```bash
pnpm test:unit          # motor de reglas + normalizadores, offline (pytest)
pnpm test:pedimentos    # pipeline real contra test-docs/pedimentos docs/docs (AWS: Textract + Bedrock)
pnpm test:pedimentos -- --file B2401416 --save /tmp/resultados
```

Las expectativas (ground truth) viven en `tests/integration/expected_pedimentos.json`. Un run completo de los 8 documentos cuesta ~$1 USD y tarda ~15 min.

> La carpeta `catalogs/` (un archivo por tipo + `_shared.json`) es la única fuente de verdad; `pnpm build` la combina en `config/catalogs.json` dentro de la Lambda. Guía: `catalogs/README.md`.

---

## Cómo Funciona

```
┌─────────┐     ┌───────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Upload │────▶│    S3     │────▶│  Bedrock          │────▶│  Lambda          │────▶│ EventBridge │
│  (API)  │     │  raw/     │     │  Clasificación +  │     │  Validación      │     │ Notificación│
└─────────┘     └───────────┘     │  Extracción       │     │  Determinística  │     └─────────────┘
                                  └──────────────────┘     └──────────────────┘
```

1. **Upload**: Documento llega via API Gateway (base64 JSON) → se guarda en S3
2. **Clasificación**: Bedrock identifica el tipo (INE, CSF, PASAPORTE, etc.)
3. **Extracción**: Bedrock extrae campos específicos usando `extraction_hints` como guía
4. **Validación**: Lambda Python aplica reglas determinísticas (regex, fechas, presencia)
5. **Custom**: Opcionalmente ejecuta scripts Node.js para lógica avanzada
6. **Resultado**: Emite evento a EventBridge con status APROBADO/RECHAZADO/REQUIERE_REVISION

### ¿Cómo sabe Bedrock dónde buscar cada campo?

El prompt de extracción incluye **extraction_hints** por campo:

```
Si es INE, extrae:
NOMBRE_COMPLETO, CURP, VIGENCIA, ...

Pistas para localizar campos en INE:
  - NOMBRE_COMPLETO: Nombre completo del titular, en la parte frontal superior
  - CURP: Clave de 18 caracteres, en el reverso debajo del código de barras
  - VIGENCIA: Año en formato YYYY, junto a la foto en el frente
```

Estos hints se configuran en el JSON sin tocar código.

---

## Requisitos

- Node.js 20+
- pnpm 10+
- AWS CLI configurado
- AWS CDK (`npm install -g aws-cdk`)

---

## Instalación

```bash
cd documents-validator
pnpm install
```

---

## Build y Deploy

```bash
# Compilar TypeScript
pnpm build

# Ver qué se va a desplegar (diff contra lo actual)
pnpm run diff

# Desplegar
pnpm run deploy

# Desplegar con DynamoDB catalog habilitado (hot-reload)
# Editar src/index.ts: enableDynamoCatalog: true
pnpm build && pnpm run deploy
```

---

## Scripts Disponibles

| Comando | Descripción |
|---------|-------------|
| `pnpm build` | Sincronizar catálogos + parchear blueprint + compilar TypeScript |
| `pnpm run deploy` | Desplegar stack a AWS |
| `pnpm run diff` | Ver cambios pendientes vs AWS |
| `pnpm synth` | Generar CloudFormation template |
| `pnpm run destroy` / `pnpm run destroy:full` | Eliminar stack (full = también la tabla DynamoDB) |
| `pnpm setup:python` | Crear `.venv` con dependencias de test |
| `pnpm test:unit` | Tests offline del motor de reglas (pytest) |
| `pnpm test:pedimentos` | Pipeline real contra los docs de pedimentos (AWS) |
| `pnpm test-e2e --file doc.pdf` | Probar el stack desplegado end-to-end |
| `pnpm sync` | Subir **toda** la carpeta `catalogs/` a DynamoDB (hot-reload, sin redeploy) |
| `pnpm sync:one -- --type X` | Subir **un** tipo (o `--file`, o un nombre difuso; `--dry-run` para validar) |
| `pnpm preview-prompts` | Ver los prompts que recibirá Bedrock (corre `pnpm build` antes) |

---

## Agregar un Nuevo Tipo Documental

Todo vive en la carpeta **`catalogs/`** (un archivo por tipo). Tres pasos:

```bash
# 1. Copia la plantilla con el nombre del tipo
cp catalogs/_TEMPLATE.json catalogs/ACTA_NACIMIENTO.json

# 2. Edita catalogs/ACTA_NACIMIENTO.json:
#    { "ACTA_NACIMIENTO": { "description": "...", "extraction_fields": [...],
#      "extraction_hints": {...}, "required_fields": [...], "validation_rules": [...] } }

# 3. Aplícalo
pnpm sync:one -- --type ACTA_NACIMIENTO     # inmediato (DynamoDB, sin redeploy), o
pnpm run build && pnpm run deploy           # permanente (empaquetado en la Lambda)
```

Guía detallada de los campos: **`catalogs/README.md`**. Para tipos aduanales
multi-documento agrega `"pipeline": "expediente"` al entry.

---

## Agregar Extraction Hints

Los hints le dicen a Bedrock **dónde y cómo** encontrar cada campo en el documento.
Se editan en `extraction_hints` del archivo del tipo en `catalogs/`:

```json
"extraction_hints": {
  "RFC": "RFC de 12 o 13 caracteres, aparece en la parte superior junto a 'RFC:'",
  "VIGENCIA": "Fecha de vencimiento en formato DD/MM/YYYY, parte inferior derecha",
  "FIRMA_PRESENTE": "Responder SI si hay firma manuscrita visible en la parte inferior, NO si no"
}
```

Luego `pnpm sync:one -- --type MI_TIPO` (hot-reload) o `pnpm run build && pnpm run deploy`.

---

## Agregar Validación Custom (Node.js)

Para lógica que va más allá de regex/fechas/presencia (APIs externas, cálculos, cruces),
crea un archivo en `custom_validators/` (p. ej. `custom_validators/validate_rfc_sat.mjs`)
y referéncialo con `"custom_validator": "validate_rfc_sat"` en el tipo. Ejemplo:

```javascript
export async function validate(entityMap, context) {
  const rfc = entityMap["RFC"];
  if (!rfc) return [];

  // Tu lógica custom aquí (APIs, cálculos, etc.)
  const isValid = rfc.length >= 12;

  return [{
    field: "RFC",
    rule: "custom:validate_rfc_sat",
    valid: isValid,
    message: isValid ? "RFC verificado" : "RFC inválido"
  }];
}
```

Luego redesplegar: `pnpm build && pnpm run deploy`

---

## Tipos de Reglas de Validación

| rule_type | Qué hace | Requiere `pattern` | Ejemplo |
|-----------|----------|-------------------|---------|
| `regex` | Valida campo contra expresión regular | Sí | RFC, CURP, código postal |
| `date_expiry` | Verifica que la fecha sea futura | No | VIGENCIA |
| `presence` | Verifica que el campo exista y no esté vacío | No | Campos obligatorios |
| `boolean_true` | Verifica valor afirmativo (SI, TRUE, 1, PRESENTE) | No | FIRMA_PRESENTE |

### Patrones Regex Comunes

| Campo | Patrón | Descripción |
|-------|--------|-------------|
| RFC persona física | `^[A-Z&Ñ]{4}\d{6}[A-Z0-9]{3}$` | 13 caracteres |
| RFC persona moral | `^[A-Z&Ñ]{3}\d{6}[A-Z0-9]{3}$` | 12 caracteres |
| RFC ambos | `^[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}$` | 12 o 13 caracteres |
| CURP | `^[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d$` | 18 caracteres |
| Código postal | `^\d{5}$` | 5 dígitos |
| Email | `^[\w.-]+@[\w.-]+\.\w+$` | Formato email |

---

## Estructura del Proyecto

```
documents-validator/
├── src/
│   ├── index.ts                         # CDK Stack (infraestructura)
│   ├── document-catalog.ts              # Lee JSON config para generar prompts
│   ├── prompt-generator.ts              # Genera prompts Bedrock con hints
│   └── document-processing/resources/
│       ├── enrichment-lambda/
│       │   ├── handler.py               # Motor de validación (NO modificar)
│       │   ├── document_catalog.py      # Carga config JSON + DynamoDB
│       │   ├── config/
│       │   │   ├── catalogs.json        # ← catálogo empaquetado (GENERADO, no editar)
│       │   │   └── schema.json          # JSON Schema para autocompletado
│       │   ├── validators/
│       │   │   ├── base.py              # Utilidades compartidas (regex, fechas)
│       │   │   ├── identification.py    # INE, Pasaporte, Cédula
│       │   │   ├── fiscal.py            # Constancia Situación Fiscal
│       │   │   └── internal_formats.py  # Formatos internos
│       │   └── custom_validators/
│       │       ├── README.md            # Guía de custom validators
│       │       └── example_curp_checkdigit.mjs  # Ejemplo funcional
│       └── upload-lambda/
│           ├── index.mjs                # API de upload
│           └── package.json
├── catalogs/                           # ← FUENTE DE VERDAD: un JSON por tipo
│   ├── _shared.json                    #   field_sets + reglas cross-documento
│   ├── _TEMPLATE.json                  #   plantilla para tipos nuevos
│   └── <TYPE_ID>.json                  #   un tipo por archivo
├── scripts/
│   ├── sync-catalogs.mjs               # build: combina catalogs/ → config/catalogs.json
│   ├── sync-schema.mjs                 # pnpm sync / sync:one → DynamoDB
│   └── preview-prompts.mjs             # CLI: ver prompts generados
├── package.json
└── tsconfig.json
```

---

## Configuración del Stack

En `src/index.ts`:

```typescript
new BedrockDocumentProcessingStack(app, "DocValidator", {
  // Habilitar DynamoDB para hot-reload de tipos (sin redeploy)
  enableDynamoCatalog: true,

  // Recargar catálogo en cada invocación (para cambios inmediatos en DynamoDB)
  forceReloadCatalog: true,
});
```

---

## Prioridad de Carga del Catálogo

Cuando la Lambda se ejecuta, carga tipos en este orden (último gana):

1. **JSON empaquetado** (`config/catalogs.json`, generado de `catalogs/`) — siempre disponible, desplegado con la Lambda
2. **DynamoDB** (si `CATALOG_TABLE` está configurado) — sobreescribe tipos del JSON (es lo que escribe `pnpm sync`)

Esto permite:
- Tener una base estable en JSON (versionada en git)
- Hacer cambios rápidos en DynamoDB sin redeploy
- DynamoDB gana si hay conflicto de type_id

---

## Flujo de Trabajo Recomendado

### Para desarrollo normal:
1. Editar el archivo del tipo en `catalogs/` (o `cp catalogs/_TEMPLATE.json catalogs/NUEVO.json`)
2. `pnpm build`
3. `pnpm preview-prompts` (verificar que los prompts se ven bien)
4. `pnpm run deploy`

### Para cambios urgentes en producción (sin redeploy):
1. Editar el archivo en `catalogs/`
2. `pnpm sync` (todo) o `pnpm sync:one -- --type NUEVO_TIPO` — efecto inmediato

### Para validación custom:
1. Crear `custom_validators/mi_validador.mjs`
2. Referenciarlo con `"custom_validator": "mi_validador"` en el tipo
3. `pnpm build && pnpm run deploy`

---

## Resultado de Validación (Canonical Result)

Cada documento procesado produce:

```json
{
  "documentId": "550e8400-e29b-41d4-a716-446655440000",
  "documentClassification": "INE",
  "extractedEntities": {
    "NOMBRE_COMPLETO": "Juan Pérez López",
    "CURP": "PELJ850101HDFRPN09",
    "VIGENCIA": "2029"
  },
  "validation": {
    "status": "APROBADO",
    "checks": [
      {"field": "CURP", "rule": "regex", "valid": true, "message": "CURP matches pattern"},
      {"field": "VIGENCIA", "rule": "date_expiry", "valid": true, "message": "Document has not expired"},
      {"field": "NOMBRE_COMPLETO", "rule": "presence", "valid": true, "message": "Required field present"}
    ],
    "summary": "All 3 checks passed"
  },
  "processedAt": "2025-05-05T10:30:00Z"
}
```

Status posibles:
- **APROBADO**: Todas las validaciones pasaron
- **RECHAZADO**: Al menos una validación falló
- **REQUIERE_REVISION**: No se aplicaron reglas (tipo desconocido o sin reglas)
