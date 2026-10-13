# API IDP — Resumen de integración

**IDP** (*Intelligent Document Processing*; en español, **Gestor Documental
Inteligente, GDI**) recibe documentos de sistemas externos, los almacena
de forma segura, extrae y valida sus datos y deja el resultado disponible para
consulta. Además, cada app puede conservar metadata propia —por ejemplo lote,
folio ERP o un estado transitorio—, actualizarla sin reprocesar el documento y
usarla después para buscarlo. Esta ficha resume verbos, parámetros y respuestas.

Base: `https://idp.alsuper.com` · Auth: `x-api-key` en todo salvo el paso 2.

Los ejemplos usan estas variables:

```bash
API="https://idp.alsuper.com"
KEY="<API_KEY_DE_LA_APP>"
DOC_ID="<DOCUMENT_ID_DEVUELTO_POR_EL_POST>"
UPLOAD_URL="<UPLOAD_URL_DEVUELTA_POR_EL_POST>"
```

```
1. POST /documents          → documentId, uploadUrl
2. PUT  {uploadUrl}         → (S3, arranca el pipeline)
3. GET  /documents/{id}     → resultado
4. POST /documents/{id}/reprocess → nuevo proceso desde el original (opcional)
5. PATCH /documents/{id}/metadata → estado/marcas externas (opcional)
6. GET  /documents/{id}/file→ archivo original   (opcional)
7. GET  /documents?...       → buscar/listar      (opcional)
```

Aparte del flujo de documentos: `/references/...` registra las firmas, sellos
y logos válidos de un proveedor (§9; requiere `AllowReferenceWrite`).

**Límites (por API key):** los `GET` tienen un throttle más holgado que las
escrituras y no cuentan como documentos. `429` en cualquier ruta:
`{"message":"Too Many Requests"}` = throttle, reintenta con backoff (en el
polling, espera como un `202`) · `{"message":"Limit Exceeded"}` = techo diario
de requests agotado · `{"error":"DAILY_DOCUMENT_LIMIT"}` = tope de documentos
del día (solo `POST /documents` y reprocess), con `Retry-After` hasta la
medianoche UTC. En prod el WAF por IP responde `403`. `/references` no cuenta
como documentos; su `POST`/`DELETE` usan el throttle de escritura. Detalle:
`API_USO.md` → "Límites y `429`".

---

## 1. `POST /documents`

Auth: `x-api-key` · Body: `application/json` o `application/x-www-form-urlencoded`

| Body | | |
|---|---|---|
| `fileName` | **requerido** | `pdf` `jpg` `jpeg` `png` `xml` `xlsx` `csv` |
| `documentType` | opcional | TYPE_ID, nombre o alias. **Varios separados por coma** = salida cerrada sólo a esos tipos. Admite overrides por tipo `:N`/`:all`. Los faltantes salen con rango `null` y revisión, sin contar como documentos. Máx. 8 tipos / 200 chars. **Requerido** en `xml` `xlsx` `csv`, y ahí sólo se acepta uno |
| `occurrenceMode` | opcional | Sobrescribe los defaults de catálogo para todos los tipos sin sufijo: `single` conserva la primera; `multiple`, todas. También se acepta el alias `occurrence_mode`. `:N`/`:all` prevalece por tipo |
| `metadata` | opcional | objeto plano: claves `[a-z0-9][a-z0-9._-]*`, valores US-ASCII sin anidar, 2 KB en total. `app-origin`, `model-id`, `extraction-mode`, `ai-response-format` y `document-counts` están reservadas. Editable (§5) y filtrable con `meta.<clave>` (§7). En form, JSON en string |
| `modelId` | opcional | solo dev |
| `extractionMode` | opcional | solo dev: `catalog` (**default**) = usa mapper si el tipo lo configuró; sin mapper, IA completa. `model` = IA completa; `hybrid` = código + IA para faltantes; `deterministic` = sólo código, faltantes a revisión |
| `aiResponseFormat` | opcional | `compact` = pares/filas mínimos y ensamblado por código; `legacy` = JSON anidado anterior. En prod requiere permiso de la API key |

### Cardinalidad: `single` o `multiple`

| `occurrenceMode` del POST | Sin sufijo en `documentType` |
|---|---|
| `single` | conserva la primera ocurrencia de cada tipo |
| `multiple` | conserva todas las ocurrencias físicas |

Si se omite el campo, cada tipo usa `occurrence_mode` de su catálogo. Si ese
tipo no lo define, aplica el default global: `single` cuando declaraste un tipo
y `multiple` cuando declaraste varios.

Un sufijo del request siempre prevalece: `TIPO:1`, `TIPO:2` o `TIPO:all`.
Precedencia: `:N/:all` → `occurrenceMode` del POST → `occurrence_mode` del
catálogo → default global.

```bash
# FACTURA_FLETE_EXTRANJERO tiene occurrence_mode=multiple en su catálogo
pnpm enviar ./test-docs/invoices-flete.pdf \
  FACTURA_FLETE_EXTRANJERO --env dev

# Un pedimento y todas las facturas: el sufijo :1 pisa multiple sólo para ERP
pnpm enviar ./test-docs/invoices-flete.pdf \
  "PEDIMENTO_ERP:1,FACTURA_FLETE_EXTRANJERO" \
  --occurrence-mode multiple --env dev

# Overrides por tipo
pnpm enviar archivo.pdf "FACTURA_FLETE_EXTRANJERO:1" --env dev
pnpm enviar archivo.pdf "PEDIMENTO_ERP:all" --env dev
```

`page_selection.multi_instance` define cómo separar determinísticamente las
ocurrencias de un tipo `multiple`; `start_markers`/`page_of_pattern` delimitan
cada documento. El detalle completo del schema está en `catalogs/README.md`.

Petición completa de ejemplo (la metadata siempre va dentro del body del
documento; no se manda como un JSON separado):

```bash
curl -sS -X POST "$API/documents" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "fileName": "pedimento.pdf",
    "documentType": "PEDIMENTO_ERP",
    "occurrenceMode": "single",
    "metadata": {
      "lote": "A123",
      "folio_erp": "4711",
      "estado_externo": "RECIBIDO",
      "requiere_atencion": false
    }
  }'
```

Números y booleanos se guardan como texto (`"4711"`, `"false"`). Los nombres
de estado son libres de la app; no sustituyen `validation.status`. `modelId`
se omite porque es únicamente un override de pruebas en dev; si se usa, va al
mismo nivel que `fileName`, no dentro de `metadata`.

| `200` | |
|---|---|
| `documentId` | id para los pasos 3, 4 y 5 |
| `uploadUrl` | destino del paso 2 |
| `uploadMethod` | `PUT` |
| `uploadExpiresInSeconds` · `uploadExpiresAt` | 900 s |
| `processingStatus` · `statusUrl` · `retryAfterSeconds` | `AWAITING_UPLOAD` · ruta de consulta · 1 s |
| `bucket` · `key` · `documentType` · `uploadedAt` · `registeredAt` | `documentType` devuelve los TYPE_ID normalizados y sin sufijos, unidos por comas si fueron varios |
| `occurrenceMode` | modo explícito del POST; `null` si se usará catálogo/default global |
| `documentTypeCounts` | decisiones explícitas del POST (`:N`/`:all`, o el modo materializado); los defaults de catálogo no aparecen aquí |
| `classificationMode` | `manual` (un tipo, honrado) · `guided` (varios, se clasifica sólo entre ellos) · `auto` (ninguno) |

`400` `MISSING_FIELD` `INVALID_FORMAT` `DOCUMENT_TYPE_REQUIRED` `INVALID_DOCUMENT_TYPE` `INVALID_OCCURRENCE_MODE` `INVALID_JSON` `INVALID_METADATA` `INLINE_UPLOAD_REMOVED` · `429` `DAILY_DOCUMENT_LIMIT` · `500` `PRESIGN_FAILED` `JOB_REGISTRATION_FAILED`

---

## 2. `PUT {uploadUrl}`

Auth: **ninguna** · Body: **bytes crudos** · Cabeceras: **ninguna**

Host S3, no la API. Usa la URL completa tal cual.

```bash
curl -sS -X PUT --upload-file pedimento.pdf "$UPLOAD_URL"
```

| `200` | cuerpo vacío |
|---|---|
| `403` | `AccessDenied` (mandaste cabeceras `x-amz-*`) · `SignatureDoesNotMatch` (URL alterada o expirada) |

---

## 3. `GET /documents/{id}`

Auth: `x-api-key`

```bash
curl -sS "$API/documents/$DOC_ID?full=1" -H "x-api-key: $KEY"
```

| Param | | |
|---|---|---|
| `id` | **requerido** | path. Acepta id completo o prefijo |
| `full` | opcional | `1` agrega el detalle por campo desde S3 |
| `waitSeconds` | opcional | `0..20`; espera en el mismo GET hasta resultado terminal (default `0`) |

| `200` | |
|---|---|
| `documentId` · `processingStatus` · `workflowStatus` · `documentType` · `documentTypeName` | `FINISHED`/`FAILED`; `workflowStatus` es interno |
| `reprocessedFrom` | sólo en trabajos creados por `/reprocess`; id completo anterior |
| `result.documentId` · `result.documentClassification` | envelope persistido |
| `result.validation.status` | `APROBADO` `REQUIERE_REVISION` `RECHAZADO` |
| `result.documents[]` | **siempre un arreglo**, con 1 o con N: `index` `documentType` `pageStart` `pageEnd` `status` `fields`. Un tipo solicitado ausente lleva rango `null` y `REVIEW_REQUIRED` |
| `result.documentCount` · `result.envelopeVersion` | cuántos documentos se encontraron (sin `OTRO` ni marcadores ausentes) · `2` (ausente = resultado guardado con la forma anterior) |
| `result.documentMatchesType` · `result.rejectionConfidence` · `result.detectedDocument` | siempre presentes: ¿el archivo ES lo que se declaró? `false` → `RECHAZADO`, `confidence` ≤ 0.05, con la confianza del rechazo (0-1) y qué parece ser en realidad; `null`/`0.0`/`null` = no se evaluó (XML, tabular). Detalle por documento en `result.documents[].typeCheck` |
| `result.aiExecuted` · `result.aiUsage` | siempre presentes: si realmente corrió IA, etapas y tokens/imágenes reportados. Textract OCR no cuenta como IA generativa |
| `result.deterministicSavings` | solo con extracción determinística: ahorro **estimado** de input/output contra el schema completo; no se suma al usage facturado |
| `result.documents[].deterministicExtraction` | estrategia `deterministic`/`hybrid`/`model`, si ese documento llamó IA, conteo resuelto, `fallbackFields` y `tokenSavings` |
| `result.pages` · `result.pages.ocrOptimization` | conteos reales native/OCR/empty; el bloque opcional audita OCR bajo demanda (`effectiveMode`, motivo, intentadas/omitidas y rollback global) |
| `result.documents[].authenticity` | solo en tipos que verifican autenticidad física (`CARTA_NOM35`): `verdict` (`AUTENTICO`·`SOSPECHOSO`·`NO_AUTENTICO`·`NO_EVALUADO`), `layers`, `reasons[{code,hard,message}]`, `referencesFound`. Resumen: el estatus lo deciden los campos (`CAPTURA_FISICA`, `FIRMA_PRESENTE`…). Mandar `metadata.proveedor` en el `POST` para comparar contra las firmas/sellos registrados de ese proveedor |
| `result.tracking` · `result.metadata` · `result.warnings` · `result.checks` | contexto y detalle. `processingSeconds` = pipeline; `uploadToResultSeconds` = PUT→resultado; `uploadToCompletionSeconds` = PUT→FINISHED/FAILED |
| `fullResult` | solo con `full=1`; resultado profundo desde S3 con la metadata vigente superpuesta |

`202` — `AWAITING_UPLOAD` · `QUEUED` · `PROCESSING`; respeta `Retry-After` y usa backoff con jitter.

`404` — no existe o la app no puede verlo. No es un estado transitorio.

Formas mínimas de la respuesta:

```jsonc
// 202: todavía no hay resultado
{
  "documentId": "e18d014a-...",
  "processingStatus": "PROCESSING",
  "result": null,
  "retryAfterSeconds": 1
}
```

```jsonc
// 200: terminó; fields puede contener muchos campos y aquí se abrevia
{
  "documentId": "e18d014a-...",
  "processingStatus": "FINISHED",
  "workflowStatus": "complete",
  "result": {
    "envelopeVersion": 2,
    "documentClassification": "FACTURA_FLETE_EXTRANJERO",
    "documentMatchesType": true,
    "rejectionConfidence": 0.0,
    "detectedDocument": null,
    "documentCount": 1,
    "validation": { "status": "APROBADO", "reviewRequired": false },
    "documents": [{
      "index": 1,
      "documentType": "FACTURA_FLETE_EXTRANJERO",
      "status": "APPROVED",
      "fields": {
        "NUMERO_FACTURA": { "value": "SJ191703", "confidence": 0.98, "valid": true },
        "TOTAL": { "value": 14612.4, "confidence": 0.93, "valid": true }
      }
    }]
  }
}
```

```jsonc
// 200: falla técnica; no equivale a validation.status = RECHAZADO
{
  "documentId": "e18d014a-...",
  "processingStatus": "FAILED",
  "workflowStatus": "enrichment-failure",
  "result": null
}
```

---

## 4. `POST /documents/{id}/reprocess`

Auth: `x-api-key` · Body opcional: `application/json` o
`application/x-www-form-urlencoded`.

Crea un `documentId` nuevo y copia internamente el archivo original desde
`processed/` o `failed/` hacia `raw/`. No descarga bytes al cliente, no exige
otro `PUT` y nunca modifica el resultado anterior.

| Body | | |
|---|---|---|
| `documentType` | opcional | Mismo contrato que POST `/documents`: uno o varios separados por coma, admite `:N`/`:all`. Si se **omite**, hereda la declaración anterior. `null` o `""` fuerza auto |
| `occurrenceMode` | opcional | `single` o `multiple`; alias `occurrence_mode`. Sobrescribe catálogo/default; `:N`/`:all` prevalece |
| `modelId` | opcional | sólo dev; aplica al proceso nuevo |

```bash
curl -sS -X POST "$API/documents/$DOC_ID/reprocess" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "documentType": "PEDIMENTO_ERP:1,FACTURA_FLETE_EXTRANJERO:all",
    "occurrenceMode": "multiple"
  }'
```

| `202` | |
|---|---|
| `documentId` | **id nuevo** para consultar con GET |
| `reprocessedFrom` | id completo anterior |
| `documentType` · `documentTypeCounts` · `occurrenceMode` · `classificationMode` | decisión efectiva del reproceso |
| `processingStatus` · `statusUrl` · `retryAfterSeconds` | `QUEUED` · ruta del id nuevo · 1 s |

`400` `INVALID_JSON` `INVALID_BODY` `INVALID_DOCUMENT_TYPE`
`INVALID_OCCURRENCE_MODE` `INVALID_FORMAT` `DOCUMENT_TYPE_REQUIRED` ·
`403` `FORBIDDEN` `FORBIDDEN_DOCUMENT_TYPE` · `404` `NOT_FOUND` `FILE_NOT_FOUND` ·
`409` `SOURCE_NOT_READY` · `429` `DAILY_DOCUMENT_LIMIT` · `500` `JOB_REGISTRATION_FAILED`
`REPROCESS_COPY_FAILED`

---

## 5. `PATCH /documents/{id}/metadata`

Auth: `x-api-key` · Requiere `AllowMetadataWrite=true` · Body: JSON Merge Patch plano.
Un valor crea/reemplaza; `null` o string vacío borra; una clave ausente se
conserva. Los estados externos son libres de la app y nunca cambian
`validation.status`.

Petición completa para crear/reemplazar claves:

```bash
curl -sS -X PATCH "$API/documents/$DOC_ID/metadata" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/merge-patch+json" \
  -d '{
    "estado_externo": "ENVIADO_A_ERP",
    "folio_erp": "4711",
    "requiere_atencion": true
  }'
```

Petición completa para borrar una clave:

```bash
curl -sS -X PATCH "$API/documents/$DOC_ID/metadata" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/merge-patch+json" \
  -d '{"requiere_atencion": null}'
```

El body mostrado es **todo el body HTTP del PATCH**, pero no tiene que repetir
todo el mapa de metadata: solo enumera las operaciones. Las claves ausentes se
conservan; por eso reenviar el mapa completo sería innecesario y propenso a
pisar cambios de otra integración.

| `200` | |
|---|---|
| `documentId` | documento modificado |
| `metadata` | mapa completo después de aplicar el parche, no solo las claves enviadas |
| `metadataVersion` | versión entera resultante del control de concurrencia |
| `updatedAt` | fecha ISO-8601 de la modificación |
| `updatedBy` | nombre registrado de la app que hizo el cambio |

`400` `INVALID_JSON` `INVALID_METADATA` `EMPTY_PATCH` · `403` `FORBIDDEN` · `404` `NOT_FOUND` · `409` `METADATA_CONFLICT`

---

## 6. `GET /documents/{id}/file`

Auth: `x-api-key`

```bash
curl -sS "$API/documents/$DOC_ID/file?expiresIn=900" -H "x-api-key: $KEY"
```

| Param | | |
|---|---|---|
| `id` | **requerido** | path |
| `expiresIn` | opcional | segundos. default 900, min 60, max 3600 |

| `200` | |
|---|---|
| `documentId` · `fileName` · `fileSizeBytes` · `location` | identidad y ubicación (`processed`/`failed`) |
| `downloadUrl` · `downloadMethod` | URL prefirmada + `GET`; la descarga va a S3 sin `x-api-key` |
| `expiresInSeconds` · `expiresAt` | vigencia efectiva |

`404` `NOT_FOUND` o `FILE_NOT_FOUND`

---

## 7. `GET /documents` — búsqueda

Auth: `x-api-key` · Requiere al menos uno de `docType`, `system`, `meta.<clave>` o `field`+`value`

Cuatro filtros, **todos en AND**. Repetir uno es **OR**.

| Param | Default | |
|---|---|---|
| `docType` | | TYPE_ID, nombre o alias. Coma para varios. **Mándalo siempre**: sin él la consulta hace Scan |
| `system` | | Qué sistema lo subió (`tracking.appOrigin`, sale de la API key del upload). Coma para varios. Alias `appOrigin` `sistema` |
| `meta.<clave>` | | Metadata actual (upload + PATCH): `meta.lote=A123`. Claves en minúsculas. Claves distintas = AND, misma clave = OR |
| `metaOp` | `eq` | `eq` · `contains`, para todos los `meta.<clave>` |
| `field` | | Nombre del campo. No hace falta el dot-path completo. **Repetible** (AND) |
| `value` | | Valor. Case-insensitive. Uno por `field`, **en el mismo orden** |
| `op` | `eq` | `eq` · `contains`. Una vez (a todos) o una por filtro |
| `status` | | `APROBADO` `RECHAZADO` `REQUIERE_REVISION` |
| `since` · `until` | | ISO-8601 |
| `order` | `desc` | `desc` · `asc` |
| `expand` | `true` | `0` no busca dentro de expedientes |
| `limit` | `25` | tope `100` |
| `summary` | `false` | `1` resumen ligero (incluye `appOrigin` y `metadata`) |
| `full` | `false` | `1` agrega detalle desde S3 |

Petición completa con todos los grupos de parámetros:

```bash
curl -sS -G "$API/documents" \
  -H "x-api-key: $KEY" \
  --data-urlencode "docType=PEDIMENTO_ERP" \
  --data-urlencode "system=ERP" \
  --data-urlencode "meta.lote=A123" \
  --data-urlencode "field=PROVEEDOR_ID_FISCAL" \
  --data-urlencode "value=OFU910626UQ0" \
  --data-urlencode "op=eq" \
  --data-urlencode "status=APROBADO" \
  --data-urlencode "since=2026-09-01T00:00:00Z" \
  --data-urlencode "until=2026-09-30T23:59:59Z" \
  --data-urlencode "order=desc" \
  --data-urlencode "expand=true" \
  --data-urlencode "limit=25" \
  --data-urlencode "summary=false" \
  --data-urlencode "full=false"
```

Ejemplos de metadata:

```bash
curl -sS -G "$API/documents" -H "x-api-key: $KEY" \
  --data-urlencode "docType=PEDIMENTO_ERP" \
  --data-urlencode "meta.sucursal=CHIH" \
  --data-urlencode "meta.estado_externo=ENVIADO_A_ERP"

curl -sS -G "$API/documents" -H "x-api-key: $KEY" \
  --data-urlencode "docType=PEDIMENTO_ERP" \
  --data-urlencode "meta.folio_erp=47" \
  --data-urlencode "metaOp=contains"
```

| `200` | |
|---|---|
| `count` · `scanned` · `matches[]` | |
| `docTypes[]` · `resolvedTypes[]` · `expandedContainers` | presentes cuando se pidió/acotó tipo |
| `field` · `value` · `op` · `filters[]` | eco de filtros extraídos; los tres escalares corresponden al primero |
| `system[]` · `metaFilters[]` · `metaOp` | eco de sistema/metadata cuando se enviaron |
| `status` · `since` · `until` · `order` | eco de estado/ventana/orden |
| `summary` · `full` · `indexed` · `lookup` | forma de respuesta y camino (`uniqueKey`, `typeIndex`, `scan`) |
| `restrictedToApp` · `restrictedToDocTypes[]` | solo si los permisos de la app acotaron la consulta |
| `matches[].matched[]` | `{field, matchedValue, matchedAt}` por filtro extraído |
| `matches[].matchedValue` · `matches[].matchedAt` | coincidencia del primer filtro, por compatibilidad |

`400` `MISSING_PARAMS` `INVALID_PARAMS` · `403` sin permiso de `docType` o sin `AllowFieldSearch` (`system`/`meta.<clave>` no lo piden)

---

## 8. `GET /document-types` [`/{type}`] — catálogo de tipos

Auth: `x-api-key` · Hot-reload (refleja `pnpm sync:one` sin redeploy)

| | `GET /document-types` | `GET /document-types/{type}` |
|---|---|---|
| Devuelve | Página, **5 fijo por página** | Un tipo |
| Param de paginación | `page` (default `1`, fuera de rango se recorta) | — |
| `includeSchema` | `1`/`true` agrega `schema` a cada tipo de la página | `1`/`true` agrega `schema` |
| `404` | — | `DOCUMENT_TYPE_NOT_FOUND` — no existe, o fuera de `AllowedDocTypes` |

```bash
curl -sS "$API/document-types" -H "x-api-key: $KEY" | jq .
curl -sS -G "$API/document-types/PEDIMENTO_ERP" -H "x-api-key: $KEY" \
  --data-urlencode "includeSchema=1" | jq .
```

| `200` | |
|---|---|
| `type` · `displayName` · `description` | siempre |
| `schema` | solo con `includeSchema=1` — el árbol `fields` del catálogo tal cual |
| `page` · `pageSize` · `total` · `totalPages` · `types[]` | solo en la lista |

Sin `AllowedDocTypes` en la app: catálogo completo. Con él: acotado a esos tipos, en ambos endpoints.

---

## 9. `/references/{scope}/{key}` — firmas, sellos y logos válidos de una entidad

Auth: `x-api-key` · Requiere `AllowReferenceWrite=true` en **todas** las rutas,
también los `GET` (`403` `FORBIDDEN` sin él) · No cuenta como documentos del día

```
1. POST   /references/{scope}/{key}                         → refId, uploadUrl, confirmUrl
2. PUT    {uploadUrl}                                       → (S3, sin x-api-key ni cabeceras)
3. POST   /references/{scope}/{key}/{kind}/{refId}/confirm  → ficha ACTIVE (revisa en el momento)
   GET    /references/{scope}/{key}                         → lista
   GET    /references/{scope}/{key}/{kind}/{refId}          → ficha + imageUrl
   DELETE /references/{scope}/{key}/{kind}/{refId}          → desactiva
```

| Ruta | | |
|---|---|---|
| `scope` | **requerido** | ámbito declarado por un tipo activo (`authenticity.references.scope`). Hoy solo `PROVEEDOR` |
| `key` | **requerido** | id de la entidad (número de proveedor) = el valor de `metadata.proveedor` del `POST /documents`. Solo letras, dígitos, `.` `-` `_`; si cambiaría al normalizarse se rechaza con `suggested` |
| `kind` | **requerido** | `FIRMA` `SELLO` `LOGO` |
| `refId` | **requerido** | lo devuelve el paso 1 (slug de `label`; `-2`, `-3`… si ya existe) |

| Body paso 1 | | |
|---|---|---|
| `kind` | **requerido** | `FIRMA` `SELLO` `LOGO` |
| `label` | **requerido** | quién firma / qué sucursal es el sello |
| `fileName` | opcional | solo procedencia |

| Body paso 3 | | |
|---|---|---|
| `force` | opcional | `true` acepta una referencia con la misma tinta que otra (`SIMILAR_REFERENCE`). No salta `DUPLICATE_REFERENCE` |

| Query `GET` lista | Default | |
|---|---|---|
| `kind` | | filtra por elemento |
| `includeInactive` | `false` | `1` incluye las dadas de baja. Las `PENDING` nunca se listan |

```bash
RESP=$(curl -sS -X POST "$API/references/PROVEEDOR/10040" \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"kind":"FIRMA","label":"Lic. Julio Lemus"}')
curl -sS --fail -X PUT --upload-file firma.png "$(echo "$RESP" | jq -r .uploadUrl)"
curl -sS -X POST "$API$(echo "$RESP" | jq -r .confirmUrl)" -H "x-api-key: $KEY"

curl -sS "$API/references/PROVEEDOR/10040?kind=FIRMA&includeInactive=1" -H "x-api-key: $KEY"
curl -sS "$API/references/PROVEEDOR/10040/FIRMA/lic-julio-lemus" -H "x-api-key: $KEY"
curl -sS -X DELETE "$API/references/PROVEEDOR/10040/FIRMA/lic-julio-lemus" -H "x-api-key: $KEY"
```

| `200` paso 1 | |
|---|---|
| `scope` · `key` · `kind` · `refId` · `label` | identidad |
| `status` | `PENDING` |
| `uploadUrl` · `uploadMethod` | destino del paso 2 · `PUT` |
| `uploadExpiresInSeconds` · `uploadExpiresAt` | 900 s. La reserva vence en 24 h |
| `confirmUrl` | ruta relativa del paso 3 |

| `200` ficha (confirm, detalle, `references[]`, `DELETE`) | |
|---|---|
| `scope` · `key` · `kind` · `refId` · `label` | identidad |
| `status` | `ACTIVE` `INACTIVE` `PENDING` |
| `format` · `width` · `height` · `sha256` | `png`/`jpeg`, px, hash del archivo |
| `hasInkFingerprint` · `indexed` | tiene huella de tinta · está en el índice de similitud |
| `createdAt` · `createdBy` | alta y app que la hizo |
| `activatedAt` · `deactivatedAt` · `deactivatedBy` | solo cuando aplican |
| `warnings[]` | solo en confirm: `{code, message}` (`TOO_MUCH_INK`) |
| `imageUrl` · `imageUrlExpiresInSeconds` | solo en detalle de una referencia `ACTIVE`; 300 s |
| `count` · `references[]` | solo en la lista |

Nunca devuelve la huella de tinta ni el embedding. `DELETE` solo desactiva (la
ficha vuelve con `status: INACTIVE`). Una referencia confirmada aplica a los
documentos procesados **después**.

Recorte: PNG/JPEG · ≤ 3.5 MB · lado menor ≥ 120 px (≥ 80 px sin huella, p. ej.
logo) y ≤ 2200 px · ≤ 12 MP · máx. 20 activas por entidad y elemento.

`400` `INVALID_REFERENCE_KEY` (+`suggested`) `INVALID_SCOPE` `INVALID_KIND` (+`valid[]`) `MISSING_FIELD` `INVALID_JSON` · `403` `FORBIDDEN` · `404` `NOT_FOUND` · `409` `REFERENCE_LIMIT_REACHED` `REFERENCE_ID_EXHAUSTED` `UPLOAD_NOT_FOUND` `DUPLICATE_REFERENCE` `SIMILAR_REFERENCE` (+`existing{kind,refId}`) `NOT_PENDING` · `422` `REFERENCE_REJECTED` (+`errors[]` `warnings[]`; códigos `NOT_AN_IMAGE` `TOO_LARGE_BYTES` `TOO_SMALL` `TOO_LARGE_DIMENSIONS` `NO_INK`) · `500` `INTERNAL_ERROR`

---

SDK Node.js/TypeScript: <https://code.alsuper.com/Infraestructura/idp-sdk-node> · Detalle completo: `API_USO.md` · C#/.NET: `ejemplos/csharp.md` · Runbook curl: `ejemplos/curl.md` · Contrato: `openapi.yaml`
