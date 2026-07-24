<!--
  MANTENIMIENTO: Este documento es la referencia de uso del API REST.
  Debe mantenerse sincronizado con las rutas definidas en
  `src/document-validation-stack.ts` (RestApi "DocumentUploadApi") y con los
  handlers en `src/document-processing/resources/{upload,query}-lambda/`.
  Si agregas, quitas o cambias un endpoint (ruta, método, params, body o
  respuesta), ACTUALIZA este archivo en el mismo cambio. Ver AGENTS.md.
-->

# API REST — Guía de uso

Referencia de todos los endpoints del API de la app de validación documental.

## Base URL y autenticación

- **Base URL:** sale del output `ApiEndpoint` del stack. Obtenlo con:
  ```bash
  API=$(aws cloudformation describe-stacks --stack-name DocumentValidationStack \
    --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
    --output text --region us-east-1)
  API=${API%/}   # quita el "/" final
  ```
  Actual: `https://4svzjzyix3.execute-api.us-east-1.amazonaws.com/prod`
- **Autenticación:** ninguna. Todos los endpoints son **públicos** (`AuthorizationType.NONE`, sin API key). Van por HTTPS pero sin token ni firma AWS. Las credenciales AWS solo se usan del lado del cliente para leer el endpoint del stack o subir archivos grandes a S3.
- **Formato:** JSON. Errores devuelven `{ "error": "CODIGO", "message": "..." }`.

## Resumen de endpoints

| Método | Ruta | Para qué |
|---|---|---|
| `POST` | `/upload` | Subir un documento a procesar |
| `GET`  | `/documents/{id}` | Obtener el resultado completo de un documento por su id |
| `GET`  | `/documents?field=...&value=...` | Buscar documentos por cualquier campo extraído |

---

## 1. `POST /upload` — Subir un documento

Sube un archivo (base64 en JSON) que dispara el pipeline de clasificación → extracción → validación.

**Body:**
```jsonc
{
  "fileName": "pedimento.pdf",          // requerido; extensión: pdf|jpg|jpeg|png
  "contentBase64": "JVBERi0xLjc...",    // requerido; el archivo en base64
  "documentType": "PEDIMENTO_ADUANAL",  // opcional; si se omite, Bedrock clasifica
  "metadata": { "lote": "A123" }        // opcional; se conserva con el documento
}
```

**Respuesta `200`:**
```json
{ "documentId": "PEDIMENTO-ADUANAL-e18d014a-...", "status": "queued" }
```

**Límites y notas:**
- Tope de payload ~4 MB reales (base64 infla ~33% → 6 MB de Lambda). Por encima: `HTTP 413`. Para archivos grandes, sube directo a S3 `raw/{id}/archivo.pdf` (dispara el mismo pipeline).
- El base64 **no cabe como argumento de curl** → usa `--data-binary @archivo`.
- Si mandas `documentType`, se salta la clasificación automática **y se HONRA tu tipo**: el documento se trata como ese tipo único y **no se re-clasifica ni se segmenta** en otro (p. ej. un PDF de varias páginas declarado `PEDIMENTO_ADUANAL` se procesa como un solo pedimento, no como `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`). Si quieres que el sistema segmente/clasifique solo, **omite** `documentType`. Para forzar la segmentación multi-documento explícitamente, declara `documentType=EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`.
- El resultado final se publica en **EventBridge** (broker `idp-broker`, source `document-validation-pipeline`) y se persiste (ver GET abajo).

**Ejemplo:**
```bash
{
  printf '{"fileName":"pedimento.pdf","contentBase64":"'
  base64 -w0 pedimento.pdf
  printf '","documentType":"PEDIMENTO_ADUANAL"}'
} > /tmp/body.json
curl -sS -X POST "$API/upload" -H "Content-Type: application/json" --data-binary @/tmp/body.json
```
Atajo del repo: `pnpm enviar pedimento.pdf PEDIMENTO_ADUANAL`.

---

## 2. `GET /documents/{id}` — Obtener un documento por id

Devuelve el resultado **completo** de un documento. `{id}` puede ser el id completo o un **prefijo** (gana el primer match).

**Respuesta `200`** — `result` es el **envelope completo** (mismo que se guarda en DynamoDB y se publica en EventBridge):
```jsonc
{
  "documentId": "PEDIMENTO-ADUANAL-4ff224be-...",
  "workflowStatus": "complete",           // estado del pipeline en DynamoDB
  "result": {
    "documentId": "...",
    "documentClassification": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
    "extractedEntities": { "...": "..." },  // escalares del documento principal
    "validation": { "status": "REQUIERE_REVISION", "summary": "expediente: REVIEW_REQUIRED (17 documents)" },
    "expediente": {
      "status": "REVIEW_REQUIRED",
      "pages": { "totalPages": 27, "...": 0 },
      "documents": [                        // TODOS los documentos del expediente
        {
          "documentType": "PEDIMENTO_ADUANAL",
          "status": "PARTIALLY_APPROVED",
          "extractedEntities": { "NUMERO_PEDIMENTO_NORMALIZADO": "260708236001187", "...": "..." },
          "arrayCounts": { "PARTIDAS": 1, "CONTRIBUCIONES": 2 },
          "liquidacionNormalizada": { "IVA": 53, "PRV": 330, "TOTAL": 383, "...": 0 }
        }
        // ... el resto de documentos
      ],
      "crossDocumentChecks": [ ... ],
      "tracking": { "schema": { "pipelineVersion": "1.0.0", "typeVersions": { "PEDIMENTO_ADUANAL": "2.0" } } }
    },
    "resultLocation": { "bucket": "...", "key": "results/{id}.json" }
  }
}
```

Query params:
- `full=1` → agrega **`fullResult`**: el detalle más profundo desde S3 (`documents[].fields[]` con `extractedValue`/`normalizedValue`/`status`/`checks`). El `result` del envelope trae las entidades y `liquidacionNormalizada`, pero el detalle por-campo solo está en `fullResult`.

**Errores:** `404 NOT_FOUND` si no hay documento con ese id/prefijo (acepta id completo o prefijo).

**Ejemplo:**
```bash
curl -sS "$API/documents/PEDIMENTO-ADUANAL-4ff224be" | python3 -m json.tool
curl -sS "$API/documents/PEDIMENTO-ADUANAL-4ff224be?full=1"   # + detalle por campo de S3
```
Atajo del repo (equivalente, formateado): `pnpm resultado -- --id 4ff224be`.

---

## 3. `GET /documents` — Buscar por cualquier campo extraído

Busca documentos cuyo campo `field` (dot-path) coincida con `value`. Pensado para consultas como "el pedimento con NUMERO_PEDIMENTO_NORMALIZADO = X".

**Query params:**
| Param | Requerido | Default | Descripción |
|---|---|---|---|
| `field` | sí | — | Dot-path del campo, p. ej. `extractedEntities.NUMERO_PEDIMENTO_NORMALIZADO`. Si no resuelve en la raíz, hace **búsqueda profunda** por la última clave del path (encuentra el campo aunque esté anidado en cualquier documento). |
| `value` | sí | — | Valor a comparar (case-insensitive, se recortan espacios). |
| `op` | no | `eq` | `eq` (igualdad exacta) o `contains` (subcadena). |
| `limit` | no | `25` | Máximo de resultados (tope `100`). |
| `summary` | no | `false` | `1`/`true` devuelve solo un **resumen ligero** por match (id, clasificación, status, escalares, arrayCounts) en vez del envelope completo. Útil para listar. |
| `full` | no | `false` | `1`/`true` agrega `fullResult` por match: el detalle **más profundo** desde S3 (`fields[]` con extractedValue/normalizedValue/status/checks, contenido completo de arrays). Cuesta un GET de S3 por match. |

**Por defecto** cada match es el **envelope completo** (mismo shape que `GET /documents/{id}`): `documentId`, `documentClassification`, `validation`, `expediente.documents[...]` (todos los documentos del expediente con sus entidades, arrayCounts y `liquidacionNormalizada`), `tracking`, más `matchedValue`/`matchedAt`.

**Respuesta `200`** — cada match es el **envelope completo** + dónde coincidió:
```jsonc
{
  "field": "extractedEntities.NUMERO_PEDIMENTO_NORMALIZADO",
  "value": "260708236001187",
  "op": "eq",
  "summary": false,
  "full": false,
  "count": 1,
  "scanned": 2,                       // cuántos documentos se revisaron
  "matches": [
    {
      "documentId": "PEDIMENTO-ADUANAL-4ff224be-...",
      "documentClassification": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
      "extractedEntities": { "...": "..." },
      "validation": { "status": "REQUIERE_REVISION", "...": "..." },
      "expediente": { "documents": [ /* TODOS los documentos, con liquidacionNormalizada */ ], "...": "..." },
      "matchedValue": "260708236001187",
      "matchedAt": "*.NUMERO_PEDIMENTO_NORMALIZADO",  // "*." = encontrado por búsqueda profunda
      "fullResult": { /* SOLO con full=1: detalle por-campo de S3 */ }
    }
  ]
}
```

> **Completitud:** por defecto cada match trae el envelope completo (con `expediente.documents[...]`). Con **`summary=1`** trae solo un resumen ligero. Con **`full=1`** agrega `fullResult` (el detalle por-campo `fields[]` desde S3).

**Ejemplos:**
```bash
# Por número de pedimento normalizado
curl -sS "$API/documents?field=extractedEntities.NUMERO_PEDIMENTO_NORMALIZADO&value=260708236001630"

# Por RFC del importador (búsqueda profunda: encuentra el campo esté donde esté)
curl -sS "$API/documents?field=RFC_IMPORTADOR_EXPORTADOR&value=OFU910626UQ0"

# Por coincidencia parcial en el nombre del importador
curl -sS "$API/documents?field=NOMBRE_IMPORTADOR_EXPORTADOR&value=futurama&op=contains&limit=10"
```

**Nota de rendimiento:** este endpoint hace un **Scan** de la tabla y filtra en la Lambda (los campos buscables viven dentro del JSON de `EnrichmentResult`, no como columnas indexadas). Es adecuado para volúmenes moderados. Si la tabla crece mucho y hay un campo "caliente" (p. ej. `NUMERO_PEDIMENTO_NORMALIZADO`), conviene promoverlo a atributo de nivel superior + un GSI para consultas O(1).

---

## Apéndice: dónde queda el resultado (y por qué a veces parece incompleto)

El resultado se guarda en **dos lugares con distinto nivel de detalle** — esto explica por qué una respuesta puede verse "incompleta":

- **DynamoDB** tabla `DocumentValidationStack-BedrockDocumentProcessingDocumentProcessingTable...` (PK `DocumentId`): un **envelope COMPACTO** en la columna `EnrichmentResult` (string JSON). Trae los campos **escalares**, `arrayCounts` (solo conteos), `liquidacionNormalizada`, `status` y `tracking`. **NO** trae el contenido de los arrays ni el detalle por campo (`fields[]` con extractedValue/normalizedValue/status/checks). Es compacto a propósito: el payload de Step Functions tiene límite de 256 KB.
- **S3** `results/{documentId}.json`: el resultado **COMPLETO** — todos los campos con su detalle, el contenido de todos los arrays (PARTIDAS, CONTRIBUCIONES...), checks y confianzas.

Cómo lo devuelve cada endpoint:

| Llamada | De dónde lee | Nivel |
|---|---|---|
| `GET /documents/{id}` | DynamoDB (`result`) | Envelope completo (con `expediente.documents[...]`) |
| `GET /documents/{id}?full=1` | DynamoDB + S3 (`fullResult`) | Envelope + detalle por-campo `fields[]` |
| `GET /documents?...` | DynamoDB (Scan) | Envelope completo por match |
| `GET /documents?...&summary=1` | DynamoDB (Scan) | Resumen ligero por match |
| `GET /documents?...&full=1` | DynamoDB + S3 por match | Envelope + `fullResult` por match |

> El envelope de DynamoDB ya trae los escalares, `arrayCounts`, `liquidacionNormalizada` y `expediente.documents[...]`. Lo único que **solo** está en S3 es el detalle por-campo (`documents[].fields[]` con `extractedValue`/`normalizedValue`/`status`/`checks`), que se obtiene con `full=1`.
