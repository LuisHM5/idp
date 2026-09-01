# API IDP — Resumen de integración

**IDP Documents Validator** recibe documentos de sistemas externos, los almacena
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
4. PATCH /documents/{id}/metadata → estado/marcas externas (opcional)
5. GET  /documents/{id}/file→ archivo original   (opcional)
6. GET  /documents?...       → buscar/listar      (opcional)
```

---

## 1. `POST /documents`

Auth: `x-api-key` · Body: `application/json` o `application/x-www-form-urlencoded`

| Body | | |
|---|---|---|
| `fileName` | **requerido** | `pdf` `jpg` `jpeg` `png` `xml` `xlsx` `csv` |
| `documentType` | opcional | TYPE_ID, nombre o alias. **Requerido** en `xml` `xlsx` `csv` |
| `metadata` | opcional | objeto plano: claves `[a-z0-9][a-z0-9._-]*`, valores US-ASCII sin anidar, 2 KB en total. `app-origin` y `model-id` están reservadas. Editable (§4) y filtrable con `meta.<clave>` (§6). En form, JSON en string |
| `modelId` | opcional | solo dev |

Petición completa de ejemplo (la metadata siempre va dentro del body del
documento; no se manda como un JSON separado):

```bash
curl -sS -X POST "$API/documents" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "fileName": "pedimento.pdf",
    "documentType": "PEDIMENTO_ADUANAL",
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
| `bucket` · `key` · `documentType` · `classificationMode` · `uploadedAt` | |

`400` `MISSING_FIELD` `INVALID_FORMAT` `DOCUMENT_TYPE_REQUIRED` `INVALID_DOCUMENT_TYPE` `INVALID_JSON` `INVALID_METADATA` `INLINE_UPLOAD_REMOVED` · `500` `PRESIGN_FAILED`

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

| `200` | |
|---|---|
| `documentId` · `workflowStatus` · `documentType` · `documentTypeName` | wrapper de la API |
| `result.documentId` · `result.documentClassification` | envelope persistido |
| `result.validation.status` | `APROBADO` `REQUIERE_REVISION` `RECHAZADO` |
| `result.expediente.documents[]` | `documentType` `pageStart` `pageEnd` `status` `extractedEntities` |
| `result.tracking` · `result.metadata` · `result.warnings` · `result.expediente.crossDocumentChecks` | contexto y detalle |
| `fullResult` | solo con `full=1`; resultado profundo desde S3 con la metadata vigente superpuesta |

`404` — no existe **o todavía procesando**. Reintenta.

---

## 4. `PATCH /documents/{id}/metadata`

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

## 5. `GET /documents/{id}/file`

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

## 6. `GET /documents` — búsqueda

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
  --data-urlencode "docType=PEDIMENTO_ADUANAL" \
  --data-urlencode "system=ERP" \
  --data-urlencode "meta.lote=A123" \
  --data-urlencode "field=RFC_IMPORTADOR_EXPORTADOR" \
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
  --data-urlencode "docType=PEDIMENTO_ADUANAL" \
  --data-urlencode "meta.sucursal=CHIH" \
  --data-urlencode "meta.estado_externo=ENVIADO_A_ERP"

curl -sS -G "$API/documents" -H "x-api-key: $KEY" \
  --data-urlencode "docType=PEDIMENTO_ADUANAL" \
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

Detalle completo: `API_USO.md` · Runbook curl: `SUBIR_DOC_CURL.md` · Contrato: `openapi/openapi.yaml`
