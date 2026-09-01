# API — resumen

Una página. Solo verbos, parámetros y respuestas.

Base: `https://idp.alsuper.com` · Auth: `x-api-key` en todo salvo el paso 2.

```
1. POST /documents          → documentId, uploadUrl
2. PUT  {uploadUrl}         → (S3, arranca el pipeline)
3. GET  /documents/{id}     → resultado
4. PATCH /documents/{id}/metadata → estado/marcas externas (opcional)
5. GET  /documents/{id}/file→ archivo original   (opcional)
```

---

## 1. `POST /documents`

Auth: `x-api-key` · Body: `application/json` o `application/x-www-form-urlencoded`

| Body | | |
|---|---|---|
| `fileName` | **requerido** | `pdf` `jpg` `jpeg` `png` `xml` `xlsx` `csv` |
| `documentType` | opcional | TYPE_ID, nombre o alias. **Requerido** en `xml` `xlsx` `csv` |
| `metadata` | opcional | objeto plano: claves `[a-z0-9][a-z0-9._-]*`, valores US-ASCII sin anidar, 2 KB en total. Editable (§4) y filtrable con `meta.<clave>` (§6). En form, JSON en string |
| `modelId` | opcional | solo dev |

| `200` | |
|---|---|
| `documentId` | id para los pasos 3 y 4 |
| `uploadUrl` | destino del paso 2 |
| `uploadMethod` | `PUT` |
| `uploadExpiresInSeconds` · `uploadExpiresAt` | 900 s |
| `bucket` · `key` · `documentType` · `classificationMode` · `uploadedAt` | |

`400` `MISSING_FIELD` `INVALID_FORMAT` `DOCUMENT_TYPE_REQUIRED` `INVALID_DOCUMENT_TYPE` `INVALID_JSON` `INVALID_METADATA` `INLINE_UPLOAD_REMOVED` · `500` `PRESIGN_FAILED`

---

## 2. `PUT {uploadUrl}`

Auth: **ninguna** · Body: **bytes crudos** · Cabeceras: **ninguna**

Host S3, no la API. Usa la URL completa tal cual.

| `200` | cuerpo vacío |
|---|---|
| `403` | `AccessDenied` (mandaste cabeceras `x-amz-*`) · `SignatureDoesNotMatch` (URL alterada o expirada) |

---

## 3. `GET /documents/{id}`

Auth: `x-api-key`

| Param | | |
|---|---|---|
| `id` | **requerido** | path. Acepta id completo o prefijo |
| `full` | opcional | `1` agrega el detalle por campo desde S3 |

| `200` | |
|---|---|
| `documentId` · `validation.status` | `APROBADO` `REQUIERE_REVISION` `RECHAZADO` |
| `expediente.documents[]` | `documentType` `pageStart` `pageEnd` `status` `extractedEntities` |
| `tracking` · `warnings` · `crossDocumentChecks` | |

`404` — no existe **o todavía procesando**. Reintenta.

---

## 4. `PATCH /documents/{id}/metadata`

Auth: `x-api-key` · Requiere `AllowMetadataWrite=true` · Body: JSON Merge Patch plano

| Body | efecto |
|---|---|
| `{"estado_externo":"EN_REVISION"}` | crea/reemplaza; estado libre de la app, no cambia `validation.status` |
| `{"estado_externo":null}` | borra la clave |
| clave ausente | conserva el valor actual |

`200` → `documentId` · `metadata` completa · `metadataVersion` · `updatedAt` · `updatedBy`

`400` `INVALID_JSON` `INVALID_METADATA` `EMPTY_PATCH` · `403` `FORBIDDEN` · `404` `NOT_FOUND` · `409` `METADATA_CONFLICT`

---

## 5. `GET /documents/{id}/file`

Auth: `x-api-key`

| Param | | |
|---|---|---|
| `id` | **requerido** | path |
| `expiresIn` | opcional | segundos. default 900, min 60, max 3600 |

`200` → `downloadUrl` (prefirmada; la descarga va a S3, sin `x-api-key`) · `404`

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

```
GET /documents?docType=PEDIMENTO_ADUANAL&system=ERP&meta.lote=A123
              &field=RFC_IMPORTADOR_EXPORTADOR&value=OFU910626UQ0
```

| `200` | |
|---|---|
| `count` · `scanned` · `matches[]` | |
| `lookup` | `uniqueKey` (1 Query) · `typeIndex` · `scan` |
| `filters[]` · `system[]` · `metaFilters[]` | eco de lo que se aplicó |
| `matches[].matched[]` | `{field, matchedValue, matchedAt}` por filtro |
| `matches[].matchedValue` · `matchedAt` | el primero, por compatibilidad |

`400` `MISSING_PARAMS` `INVALID_PARAMS` · `403` sin permiso de `docType` o sin `AllowFieldSearch` (`system`/`meta.<clave>` no lo piden)

---

Detalle completo: `API_USO.md` · Runbook curl: `SUBIR_DOC_CURL.md` · Contrato: `openapi/openapi.yaml`
