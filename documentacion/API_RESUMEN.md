# API — resumen

Una página. Solo verbos, parámetros y respuestas.

Base: `https://idp.alsuper.com` · Auth: `x-api-key` en todo salvo el paso 2.

```
1. POST /documents          → documentId, uploadUrl
2. PUT  {uploadUrl}         → (S3, arranca el pipeline)
3. GET  /documents/{id}     → resultado
4. GET  /documents/{id}/file→ archivo original   (opcional)
```

---

## 1. `POST /documents`

Auth: `x-api-key` · Body: `application/json` o `application/x-www-form-urlencoded`

| Body | | |
|---|---|---|
| `fileName` | **requerido** | `pdf` `jpg` `jpeg` `png` `xml` `xlsx` `csv` |
| `documentType` | opcional | TYPE_ID, nombre o alias. **Requerido** en `xml` `xlsx` `csv` |
| `metadata` | opcional | objeto libre. En form, JSON en string |
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

## 4. `GET /documents/{id}/file`

Auth: `x-api-key`

| Param | | |
|---|---|---|
| `id` | **requerido** | path |
| `expiresIn` | opcional | segundos. default 900, min 60, max 3600 |

`200` → `downloadUrl` (prefirmada; la descarga va a S3, sin `x-api-key`) · `404`

---

## 5. `GET /documents` — búsqueda

Auth: `x-api-key` · Requiere al menos `docType` **o** `field`+`value`

| Param | Default | |
|---|---|---|
| `docType` | | TYPE_ID, nombre o alias. Coma para varios. **Mándalo siempre**: sin él la consulta hace Scan |
| `field` | | Nombre del campo. No hace falta el dot-path completo |
| `value` | | Valor. Case-insensitive |
| `op` | `eq` | `eq` · `contains` |
| `status` | | `APROBADO` `RECHAZADO` `REQUIERE_REVISION` |
| `since` · `until` | | ISO-8601 |
| `order` | `desc` | `desc` · `asc` |
| `expand` | `true` | `0` no busca dentro de expedientes |
| `limit` | `25` | tope `100` |
| `summary` | `false` | `1` resumen ligero |
| `full` | `false` | `1` agrega detalle desde S3 |

| `200` | |
|---|---|
| `count` · `scanned` · `matches[]` | |
| `lookup` | `uniqueKey` (1 Query) · `typeIndex` · `scan` |
| `matches[].matchedValue` · `matchedAt` | dónde coincidió |

`400` `MISSING_PARAMS` · `403` sin permiso de `docType` o sin `AllowFieldSearch`

---

Detalle completo: `API_USO.md` · Runbook curl: `SUBIR_DOC_CURL.md` · Contrato: `openapi/openapi.yaml`
