# Guía de Despliegue y Uso — Document Validator

Guía operativa completa: subir, actualizar, eliminar (total o parcial), gestionar catálogos y reglas, probar y solucionar problemas.

> **Chuleta rápida**
>
> | Quiero...                                               | Comando                                                     |
> | ------------------------------------------------------- | ----------------------------------------------------------- |
> | Desplegar todo por primera vez                          | `pnpm install && pnpm build && pnpm run deploy`             |
> | Actualizar código o catálogo (con redeploy)             | `pnpm build && pnpm run deploy`                             |
> | Ver qué cambiaría antes de desplegar                    | `pnpm build && pnpm run diff`                               |
> | Actualizar TODO el catálogo SIN redeploy (hot-reload)   | `pnpm sync`                                                 |
> | Actualizar UN tipo SIN redeploy                         | `pnpm sync:one -- --type MI_TIPO`                           |
> | Validar el sync sin escribir                            | `pnpm sync -- --dry-run`                                    |
> | Eliminar el stack (conserva tabla DynamoDB)             | `pnpm run destroy`                                          |
> | Eliminar TODO (stack + tabla)                           | `pnpm run destroy:full`                                     |
> | **Procesar MIS documentos y solo revisar el resultado** | `pnpm procesar -- --dir /ruta/a/mis/docs --importer MI_RFC` |
> | Subir un documento al stack (<4 MB)                     | `pnpm test-e2e --file doc.pdf --no-wait`                    |
> | Subir un documento grande (≥4 MB)                       | `aws s3 cp doc.pdf s3://<bucket>/raw/$(uuidgen)/doc.pdf`    |
> | Ver el resultado de un documento subido                 | `pnpm resultado -- --id <documentId>`                       |
> | Tests offline del motor de reglas                       | `pnpm test:unit`                                            |
> | Tests reales de pedimentos (usa AWS, ~$1)               | `pnpm test:pedimentos`                                      |
> | Probar un documento contra el stack desplegado          | `pnpm test-e2e --file mi_doc.pdf`                           |

---

## 1. Requisitos previos

- **Node.js 20+** y **pnpm 10+** (`corepack enable` lo instala; versión pineada `pnpm@10.20.0`).
- **Python 3.12+** (para tests locales y bundling local de la Lambda).
- **AWS CLI** configurado (`aws configure`) con una cuenta que tenga acceso a **Amazon Bedrock** (modelo `us.anthropic.claude-haiku-4-5-20251001-v1:0` habilitado en us-east-1) y **Textract**.
- **AWS CDK v2**: `npm install -g aws-cdk` (o usar `npx cdk`, ya incluido vía `pnpm`).
- **Docker** (opcional): solo se usa como _fallback_ si no hay `pip3` local al empaquetar la Lambda.

Verificación rápida:

```bash
node -v && pnpm -v && python3 --version && aws sts get-caller-identity
```

## 2. Instalación inicial

```bash
git clone <repo> && cd documents-validator
pnpm install                 # dependencias Node/CDK
pnpm setup:python            # crea .venv e instala pypdf/boto3/pytest (para tests locales)
cp .env.example .env.production   # si no existe; revisa BEDROCK_MODEL_ID
```

`bin/app.ts` lee `.env.production` (las variables de entorno del sistema tienen prioridad). Variables relevantes:

| Variable                                     | Efecto                                                                                 | Default                                       |
| -------------------------------------------- | -------------------------------------------------------------------------------------- | --------------------------------------------- |
| `BEDROCK_MODEL_ID`                           | Modelo de clasificación/extracción del pipeline                                        | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `BEDROCK_EXTRACTOR_MODEL_ID`                 | Modelo del pipeline de pedimentos/re-extracción (permite usar uno más fuerte solo ahí) | igual a `BEDROCK_MODEL_ID`                    |
| `CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION` | Cuenta/región destino                                                                  | región `us-east-1`                            |
| `CATALOG_TABLE`                              | Nombre de la tabla DynamoDB del catálogo                                               | `document-validation-catalog`                 |

## 3. Primer despliegue

```bash
# Solo la primera vez por cuenta/región:
npx cdk bootstrap

pnpm build      # 1) sincroniza catálogos → Lambda  2) parchea el blueprint  3) compila TS
pnpm run diff       # opcional: revisar qué se va a crear
pnpm run deploy     # crea el stack "DocumentValidationStack"
```

Al terminar, la salida muestra:

- **`ApiEndpoint`** — URL del API de subida de documentos.
- **`CatalogTableName`** — tabla DynamoDB del catálogo hot-reload.

**Importante (primera vez con DynamoDB):** el stack _importa_ la tabla `document-validation-catalog` (no la crea, para que sobreviva a los destroys). Si no existe aún:

```bash
aws dynamodb create-table \
  --table-name document-validation-catalog \
  --attribute-definitions AttributeName=type_id,AttributeType=S \
  --key-schema AttributeName=type_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region us-east-1
```

### Verificar el despliegue

```bash
pnpm test-e2e --file "test-docs/pedimentos docs/docs/B2401416.pdf" --wait 60
pnpm track            # seguir la ejecución de Step Functions
```

## 4. Uso: enviar documentos y leer resultados

### 4.A Flujo completo con un ejemplo real (`pedimentos.pdf`, 86 páginas, 5.8 MB)

**Paso 1 — Subirlo.** Según el tamaño del archivo:

```bash
# Archivos < 4 MB → por el API (un solo comando):
pnpm test-e2e --file "test-docs/pedimentos docs/docs/B2401416.pdf" --no-wait

# Archivos ≥ 4 MB (como pedimentos.pdf) → directo a S3 (mismo trigger que usa el API):
DOCID=$(uuidgen)
aws s3 cp "test-docs/pedimentos docs/docs/pedimentos.pdf" \
  "s3://documentvalidationstack-bedrockdocumentprocessingd-z3hhejbrhaem/raw/$DOCID/pedimentos.pdf"
echo "Tu documentId: $DOCID"     # guárdalo para consultar el resultado
```

> El límite del API es ~4 MB reales (base64 + límite de payload de Lambda = 6 MB); por encima devuelve `HTTP 413`. La subida directa a `raw/` dispara exactamente el mismo pipeline.
>
> **Clasificación manual (opcional, más rápido/barato):** sube a `raw/{TIPO}/{uuid}/archivo.pdf` (p. ej. `raw/EXPEDIENTE_ADUANAL_MULTIDOCUMENTO/$DOCID/pedimentos.pdf`) y el paso de clasificación Bedrock se salta. Con el API: campo `documentType` en el body.

**Paso 2 — Ver el avance (opcional):**

```bash
pnpm track --latest --follow      # muestra en vivo: Clasificación → Extracción → Enrichment
```

**Paso 3 — Ver el resultado final:**

```bash
pnpm resultado                          # la ejecución más reciente
pnpm resultado -- --id $DOCID           # la tuya (con el prefijo del documentId basta)
pnpm resultado -- --id $DOCID --wait    # espera si sigue procesando (86 pág ≈ 6-8 min)
pnpm resultado -- --id $DOCID --save resultados/   # además guarda el JSON completo
```

La salida muestra: estado general del expediente, cada documento detectado con su rango de páginas/tipo/estado, los campos clave con confianza, los checks cruzados (mismo pedimento, RFC importador, UUIDs duplicados) y warnings. El JSON guardado trae el detalle completo de todos los campos (`extractedValue`/`normalizedValue`/`status`/`checks`).

**Alternativa sin stack** (mismo pipeline, corre local con tus credenciales): `pnpm procesar -- --file "test-docs/pedimentos docs/docs/pedimentos.pdf" --importer TU_RFC`.

### 4.0 Modo más simple: procesar documentos localmente (`pnpm procesar`)

Para probar con tus propios documentos **sin pasar por el API** (usa el mismo pipeline que la Lambda, con tus credenciales AWS; no necesita el stack desplegado):

```bash
pnpm procesar                                        # toda la carpeta test-docs/pedimentos docs/docs
pnpm procesar -- --file "mi expediente.pdf"          # un solo archivo
pnpm procesar -- --dir ~/Descargas/expedientes       # tu carpeta completa
pnpm procesar -- --dir ~/docs --importer OFU910626UQ0   # + checks cruzados de RFC importador
pnpm procesar -- --out /tmp/salida                   # dónde dejar los resultados
```

Salida (tú solo validas el resultado final):

- **Consola**: por documento — tipo detectado, estado (`APPROVED`/`REVIEW_REQUIRED`/...), campos clave con su confianza, arrays extraídos y checks fallidos.
- **`resultados/<archivo>.json`**: resultado completo (todos los campos con `extractedValue`/`normalizedValue`/`status`/`checks`).
- **`resultados/_resumen.md`**: tabla consolidada de toda la corrida para revisar de un vistazo.

### 4.0.1 ¿Dónde están las reglas de extracción y validación?

En la carpeta **`catalogs/`** — **un archivo por tipo** (`PEDIMENTO_ADUANAL.json`, etc.), más `catalogs/_shared.json` (field_sets + reglas cross-documento). Es la única fuente de verdad. Guía completa en `catalogs/README.md`.

```bash
pnpm sync                          # sube TODA la carpeta a DynamoDB (hot-reload, sin redeploy)
pnpm sync:one -- --type PEDIMENTO_ADUANAL   # sube un solo tipo
pnpm sync -- --dry-run             # valida el marshalling sin subir nada
```

No es obligatorio: la Lambda ya trae el catálogo empaquetado (`config/catalogs.json`, generado por `pnpm build` desde `catalogs/`). DynamoDB solo sirve para **cambiar reglas al vuelo sin redeploy**; DynamoDB gana sobre lo empaquetado en runtime.

### Enviar un documento (API REST)

```bash
curl -X POST "$API_ENDPOINT/upload" \
  -H "Content-Type: application/json" \
  -d '{
    "fileName": "expediente.pdf",
    "contentBase64": "'"$(base64 -w0 expediente.pdf)"'",
    "documentType": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"
  }'
```

- `documentType` es **opcional**: si se omite, Bedrock clasifica automáticamente; si se envía, la clasificación se salta (más rápido y barato).
- Formatos: `pdf`, `jpg`, `jpeg`, `png`.
- Respuesta: `{ "documentId": "...", ... }`. El resultado final se publica en **EventBridge** (broker `idp-broker`, source `document-validation-pipeline`).

### Estructura del resultado (tipos aduanales)

Los tipos del catálogo de pedimentos pasan por el pipeline de expedientes. El evento conserva el sobre clásico (`validation.status`: `APROBADO`/`RECHAZADO`/`REQUIERE_REVISION`) y agrega el detalle completo en `expediente`:

```jsonc
{
  "documentId": "…",
  "documentClassification": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
  "validation": {
    "status": "REQUIERE_REVISION",
    "summary": "expediente: REVIEW_REQUIRED (23 documents)",
  },
  "expediente": {
    "status": "REVIEW_REQUIRED", // APPROVED | REJECTED | PARTIALLY_APPROVED | REVIEW_REQUIRED | PROCESSING_ERROR
    "pages": { "totalPages": 86, "nativePages": 68, "ocrPages": 18 },
    "documents": [
      {
        "documentType": "PEDIMENTO_ADUANAL",
        "pageStart": 3,
        "pageEnd": 7,
        "status": "APPROVED",
        "confidence": 0.96,
        "fields": {
          "NUMERO_PEDIMENTO_NORMALIZADO": {
            "extractedValue": "26 07 3435 6041248", // SIEMPRE el texto original
            "normalizedValue": "260734356041248", // normalización determinística
            "confidence": 0.95,
            "status": "VALIDATED", // VALIDATED | INVALID | LOW_CONFIDENCE | NOT_FOUND | NOT_APPLICABLE | REVIEW_REQUIRED
            "checks": [{ "rule": "regex", "valid": true, "message": "…" }],
          },
        },
        "recoveredFields": { "PEDIMENTO_REFERENCE": "P0823 6001187" }, // recuperaciones regex auditables
      },
    ],
    "crossDocumentChecks": [
      {
        "rule": "same_pedimento",
        "valid": true,
        "message": "all documents reference pedimento consecutivo 6041248",
      },
    ],
    "warnings": [],
  },
}
```

Política anti-invención: `extractedValue` nunca se sustituye; si un dato no está en el documento, es `null` con estado `NOT_FOUND` — jamás un valor inventado.

## 5. Actualizar

### 5.1 Actualizar código (Lambdas, pipeline, motor de reglas)

```bash
pnpm build && pnpm run diff     # revisar cambios
pnpm run deploy
```

### 5.2 Actualizar el catálogo (fuente de verdad)

Edita el archivo del tipo en **`catalogs/`** (p. ej. `catalogs/PEDIMENTO_ADUANAL.json`); nunca `config/catalogs.json`, que es copia generada. Luego:

```bash
# Opción A — con redeploy (queda versionado en la Lambda):
pnpm build && pnpm run deploy

# Opción B — hot-reload SIN redeploy (efecto inmediato):
pnpm sync                              # toda la carpeta
pnpm sync:one -- --type PEDIMENTO_ADUANAL   # un solo tipo
```

> El sync a DynamoDB expande automáticamente los `field_sets` (p.ej. `COMMON_CFDI_FIELDS`, definidos en `catalogs/_shared.json`) y conserva todos los parámetros de reglas (`formula`, `tolerance`, `source`, `keywords`, `repeatable_fields`, …). Las `cross_document_validation_rules` (en `_shared.json`) solo viven en el JSON empaquetado: cambios en ellas requieren la Opción A.

### 5.3 Crear un tipo nuevo

```bash
cp catalogs/_TEMPLATE.json catalogs/MI_TIPO.json    # copia la plantilla
# edita MI_TIPO.json (ver catalogs/README.md)
pnpm sync:one -- --type MI_TIPO                     # aplica al vuelo (o build + deploy)
```

### 5.4 Actualizar solo reglas de un tipo

Las reglas viven en `validation_rules` del tipo (JSON). Tipos de regla soportados por el motor dinámico (sin tocar código):

`regex`, `regex_each`, `presence`, `boolean_true`, `date_expiry`, `date_max_age`, `minimum_items`, `custom_formula`, `cfdi_total`, `sum_array`, `cross_document_match`, `cross_document_amount`, `negative_keyword`, `expected_value_by_app`, `page_ranges_no_overlap`.

Ejemplo — agregar tolerancia a una validación de total:

```jsonc
{
  "field": "TOTAL",
  "rule_type": "cfdi_total",
  "formula": "SUBTOTAL - DESCUENTO + TOTAL_IMPUESTOS_TRASLADADOS - TOTAL_IMPUESTOS_RETENIDOS",
  "tolerance": 0.02,
  "message": "El total del CFDI no coincide",
}
```

Después: `pnpm sync` (inmediato) o `pnpm build && pnpm run deploy` (versionado).

### 5.5 Reglas de precedencia del catálogo en runtime

1. **DynamoDB** (si `CATALOG_TABLE` está configurada) — gana siempre. Es lo que escribe `pnpm sync`.
2. **JSON empaquetado** (`config/catalogs.json`, generado de `catalogs/`).
3. Tipo desconocido → `OTRO` (revisión manual).

Con `forceReloadCatalog: true` (default en `bin/app.ts`) los cambios en DynamoDB aplican en la siguiente invocación.

## 6. Eliminar

### 6.1 Eliminación parcial (tipos del catálogo)

Para **deshabilitar** un tipo sin borrarlo, pon `"enabled": false` en su archivo de `catalogs/` y corre `pnpm sync:one -- --type MI_TIPO`.

Para eliminar un tipo **definitivamente**: borra `catalogs/MI_TIPO.json`, corre `pnpm run build && pnpm run deploy`, y quítalo de DynamoDB con:
```bash
aws dynamodb delete-item --table-name document-validation-catalog \
  --key '{"type_id":{"S":"MI_TIPO"}}' --region us-east-1
```

### 6.2 Eliminación total

```bash
pnpm run destroy        # elimina el stack; CONSERVA la tabla DynamoDB y sus tipos
pnpm run destroy:full   # elimina stack + tabla DynamoDB (pide confirmación)
```

Notas:

- El bucket S3 de documentos lo gestiona el blueprint; si un destroy falla por bucket no vacío, vaciarlo primero (`aws s3 rm s3://<bucket> --recursive`).
- `pnpm run destroy:full` es **irreversible** para los tipos que solo existían en DynamoDB: exporta antes con `pnpm db-list-types`.

## 7. Pruebas

| Comando                                   | Qué prueba                                                                     | Requiere AWS            | Costo aprox. |
| ----------------------------------------- | ------------------------------------------------------------------------------ | ----------------------- | ------------ |
| `pnpm test:unit`                          | Motor de reglas, normalizadores, catálogo (67 tests)                           | No                      | $0           |
| `pnpm test:pedimentos`                    | Pipeline completo contra los 9 PDFs reales de `test-docs/pedimentos docs/docs` | Sí (Textract + Bedrock) | ~$1 USD      |
| `pnpm test:pedimentos -- --file B2401416` | Un solo documento                                                              | Sí                      | ~$0.02–0.35  |
| `pnpm test:pedimentos -- --save /tmp/res` | Igual + guarda los JSON completos de resultado                                 | Sí                      | —            |
| `pnpm test-local --type INE`              | Motor de validación con entidades simuladas                                    | No                      | $0           |
| `pnpm test-e2e --file doc.pdf`            | Pipeline desplegado end-to-end (API → S3 → SFN → EventBridge)                  | Sí (stack desplegado)   | centavos     |

La verdad de terreno de los tests de pedimentos vive en `tests/integration/expected_pedimentos.json` — al agregar documentos de prueba nuevos, añade ahí sus valores esperados.

## 8. Costos de operación (medidos)

| Escenario                                        | Costo aprox. |
| ------------------------------------------------ | ------------ |
| CFDI digital de 1 página                         | ~$0.02 USD   |
| Pedimento escaneado de 27 páginas (OCR completo) | ~$0.33 USD   |
| Expediente digital de 86 páginas (18 escaneadas) | ~$0.31 USD   |

Drivers: Textract $1.50/1000 páginas (solo páginas SIN capa de texto), Bedrock Haiku 4.5 (~$1/M tokens entrada, ~$5/M salida — confirmar en la calculadora AWS). El texto nativo de PDFs digitales es gratis: **subir PDFs digitales en vez de escaneos reduce ~10x el costo**.

## 9. Solución de problemas

| Síntoma                                                                | Causa probable                                                                                       | Solución                                                                                                                                                                                                                                                                                                                                    |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Unable to import module 'handler'... pypdf` en la Lambda              | Bundle sin dependencias                                                                              | Rehacer `pnpm build && pnpm run deploy`; verifica que el asset en `cdk.out` contenga `pypdf/`                                                                                                                                                                                                                                               |
| Synth falla en bundling y no hay Docker                                | Sin `pip3` local ni Docker                                                                           | Instalar python3-pip, o Docker para el fallback                                                                                                                                                                                                                                                                                             |
| `AccessDeniedException` de Bedrock                                     | Modelo no habilitado en la cuenta/región                                                             | Consola Bedrock → Model access → habilitar Claude Haiku 4.5 en us-east-1                                                                                                                                                                                                                                                                    |
| Task timed out (Lambda)                                                | Expediente muy grande                                                                                | La Lambda ya corre a 15 min / 1024 MB; para archivos mayores dividir el PDF o subir memoria en el stack                                                                                                                                                                                                                                     |
| Resultado `REQUIERE_REVISION` con segmentos `OTRO`                     | Páginas sin tipo en el catálogo (COVE, DODA, manifestación…)                                         | Normal y conservador; si quieres que aprueben, agrega esos tipos al catálogo JSON                                                                                                                                                                                                                                                           |
| Tipos nuevos no aplican                                                | Se editó la copia generada (`config/catalogs.json`)                                                  | Editar el archivo del tipo en `catalogs/` y correr `pnpm sync` (hot-reload) o `pnpm run build && pnpm run deploy`                                                                                                                                                                                                                            |
| `test-e2e` no encuentra el endpoint                                    | Nombre de stack distinto                                                                             | `pnpm test-e2e --stack DocumentValidationStack --endpoint https://...`                                                                                                                                                                                                                                                                      |
| `UPDATE_FAILED ... AWS::Logs::LogGroup ... was not found` al desplegar | Drift: un log group del stack fue borrado fuera de CloudFormation (limpiezas manuales de CloudWatch) | Recrear el log group con el nombre exacto del error: `aws logs create-log-group --log-group-name "<nombre>"` y reintentar `pnpm run deploy`. Ver los que CFN registra: `aws cloudformation describe-stack-resources --stack-name DocumentValidationStack --query "StackResources[?ResourceType=='AWS::Logs::LogGroup'].PhysicalResourceId"` |
| Extracción pobre en un campo                                           | Falta pista                                                                                          | Agregar/mejorar su `extraction_hints` en el catálogo y hot-reload                                                                                                                                                                                                                                                                           |

## 10. Mapa de archivos (qué editar y qué no)

```
catalogs/<TYPE_ID>.json              ← EDITAR: un archivo por tipo (fuente de verdad)
catalogs/_shared.json                ← EDITAR: field_sets + reglas cross-documento
catalogs/_TEMPLATE.json              ← plantilla para tipos nuevos
config/catalogs.json                 ← NO EDITAR: copia generada por pnpm build
validators/engine.py                 ← motor de reglas (agregar rule_types nuevos aquí)
pipeline/                            ← pipeline de expedientes (OCR, segmentación, extracción)
tests/integration/expected_pedimentos.json ← ground truth de los tests de pedimentos
.env.production                      ← modelo Bedrock y cuenta/región de deploy
```
