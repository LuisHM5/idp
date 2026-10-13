<!--
  MANTENIMIENTO: Inventario de APIs para integración con otros sistemas —
  internas (expuestas por este proyecto) y externas (servicios de AWS que
  este proyecto consume). Si agregas/quitas una ruta en
  `src/document-validation-stack.ts`, cambias el shape de una respuesta en
  `upload-lambda`/`query-lambda`/`enrichment-lambda/references_api.py`, o
  agregas una llamada nueva a un servicio
  de AWS (nuevo cliente boto3/@aws-sdk en `enrichment-lambda`, `upload-lambda`
  o `query-lambda`), actualiza este documento en el mismo cambio. El detalle
  técnico exhaustivo del API REST interno vive en `api-docs/API_USO.md` — este
  documento no lo duplica, solo lo resume y lo pone en contexto junto con las
  dependencias externas.
-->

# APIs de Integración — Document Validator

Inventario de las interfaces de programación necesarias para integrar este sistema con otros sistemas: **qué expone** (APIs internas, propias) y **de qué depende** (APIs externas, de terceros — en este caso, servicios de AWS). Pensado para lectura dual:

- **Negocio**: qué puede hacer un sistema externo con esta plataforma (subir un documento, consultar un resultado) y de qué proveedores depende operativamente (para SLA, costos y riesgo de proveedor).
- **Tecnología**: contratos exactos (endpoints, verbos, parámetros, operaciones de SDK) para que un equipo de integración no tenga que leer el código fuente.

> **Nota sobre "Amazon Q"**: se revisó el código completo del repositorio (Lambdas en Python y Node.js, scripts, infraestructura CDK) y **no se encontró ningún uso de Amazon Q (Business o Developer)** — no es un servicio que este proyecto consuma. El servicio de IA generativa que sí usa el proyecto es **Amazon Bedrock** (modelos Claude, ver sección 2.1). Si el requerimiento original se refería a otro servicio, avisar para corregir esta sección.

---

## 1. APIs internas (expuestas por este proyecto)

Son APIs **propias**: este equipo las diseña, versiona y opera. Un sistema externo (ERP, portal, otro backend) las consume para integrarse con el pipeline de validación. Documentación técnica completa (bodies, ejemplos curl, códigos de error): **[`api-docs/API_USO.md`](api-docs/API_USO.md)**.

### 1.1 Resumen de endpoints

| # | Método | Ruta | Para qué (negocio) | Quién la implementa |
|---|---|---|---|---|
| 1 | `POST` | `/documents` | **Iniciar el proceso**: sube un documento (o pide una URL para subir uno grande) y arranca la validación | `upload-lambda/index.mjs` |
| 2 | `GET` | `/documents/{id}` | **Consultar** el resultado completo de un documento ya procesado, por su id | `query-lambda/index.mjs` |
| 3 | `PATCH` | `/documents/{id}/metadata` | **Actualizar seguimiento externo**: estados transitorios, folios y marcas propios de la app, sin alterar la validación | `query-lambda/index.mjs` |
| 4 | `GET` | `/documents/{id}/file` | **Descargar** el archivo original (el PDF/imagen/XML/XLSX/CSV que se subió, no el resultado) vía URL prefirmada de S3 | `query-lambda/index.mjs` |
| 5 | `POST` | `/documents/{id}/reprocess` | **Reprocesar** el archivo original con un id nuevo y, opcionalmente, otros tipos/cardinalidad/modelo | `query-lambda/index.mjs` |
| 6 | `GET` | `/documents?docType=&system=&meta.<clave>=&field=&value=` | **Buscar/listar** documentos por tipo, sistema que los subió, metadata actual y/o cualquier campo extraído | `query-lambda/index.mjs` |
| 7 | `GET` | `/document-types` | **Catálogo de tipos**: lista paginada (5/página) de los tipos soportados, con nombre y descripción | `query-lambda/index.mjs` |
| 8 | `GET` | `/document-types/{type}` | Un tipo del catálogo por su TYPE_ID exacto, con su `schema` de campos opcional (`?includeSchema=1`) | `query-lambda/index.mjs` |
| 9 | `POST` | `/references/{scope}/{key}` | **Reservar una referencia** (firma, sello o logo válido de una entidad, p. ej. un proveedor) y obtener la URL de carga | `enrichment-lambda/references_api.py` (References Lambda) |
| 10 | `POST` | `/references/{scope}/{key}/{kind}/{refId}/confirm` | **Revisar y activar** el recorte subido; responde en el momento | `enrichment-lambda/references_api.py` |
| 11 | `GET` | `/references/{scope}/{key}` | **Listar** las referencias de una entidad (`?kind=`, `?includeInactive=1`) | `enrichment-lambda/references_api.py` |
| 12 | `GET` | `/references/{scope}/{key}/{kind}/{refId}` | Una referencia, con URL prefirmada de su imagen (5 min) | `enrichment-lambda/references_api.py` |
| 13 | `DELETE` | `/references/{scope}/{key}/{kind}/{refId}` | **Dar de baja** (solo desactiva; el borrado definitivo es `pnpm refs:remove --delete`) | `enrichment-lambda/references_api.py` |

**Descarga del archivo original (endpoint 4):** el `documentId` no es un key S3 estático — mientras el documento está `PROCESSING` vive en `raw/{TIPO?}/{uuid}/archivo`, y la state machine propia (`multi-model-document-processing.ts`) mueve el objeto (copyObject + deleteObject) a `processed/{documentId}/{filename}` cuando termina sin excepción — incluida una validación RECHAZADA, que es resultado de negocio — o a `failed/{documentId}/{filename}` únicamente ante falla técnica. `filename` es el original (ver `resources/sqs-consumer/index.py`). El endpoint localiza el key bajo `processed/` o `failed/` y devuelve una URL prefirmada, no los bytes.

### 1.2 `POST /documents` — Iniciar el proceso de subida

Un solo flujo, para cualquier tamaño — la subida son **dos pasos** y el archivo
nunca pasa por API Gateway ni por la Lambda:

| Paso | Qué | Cómo |
|---|---|---|
| 1 | `POST /documents` con solo metadata | La Lambda valida, acuña `documentId`/key y firma una URL de S3 (`getSignedUrl` + `PutObjectCommand`, expira en 15 min). Devuelve `uploadUrl` (con la metadata ya dentro del query string, firmada). |
| 2 | `PUT {uploadUrl}` con el archivo | Va directo a S3, sin Lambda, sin credenciales AWS y **sin cabeceras extra**. El pipeline arranca al recibirse el objeto. |

SigV4 mueve las cabeceras `x-amz-*` al query string al prefirmar, así que la
metadata ya viaja dentro de la URL y firmada. Mandarla también como header da
`403 AccessDenied: There were headers present in the request which were not signed`.

El modo inline (`contentBase64`) fue eliminado — devuelve
`400 INLINE_UPLOAD_REMOVED`. Existía solo para archivos chicos y hacía que el
**tamaño** decidiera qué código corría: base64 infla ~33 %, y arriba de ~4 MB
reales chocaba con el tope de payload de Lambda (6 MB, límite duro de AWS).

**Body** (`fileName` requerido; `documentType`, `occurrenceMode`, `metadata`, `modelId` opcionales). `occurrenceMode` acepta `single` o `multiple` y sobrescribe el default de catálogo de cada tipo sin sufijo `:N`/`:all`. Si se omite, manda el catálogo; si éste no define `occurrence_mode`, aplica `single` con un tipo y `multiple` con varios. `metadata` es contexto libre del que sube que después se puede editar con `PATCH /documents/{id}/metadata` (1.6) y filtrar con `GET /documents?meta.<clave>=` (1.7); admite claves `[a-z0-9][a-z0-9._-]*`, valores planos US-ASCII y 2 KB de tope — lo demás es `400 INVALID_METADATA`. Acepta `application/json` o `application/x-www-form-urlencoded`:
```json
{
  "fileName": "pedimento.pdf",
  "documentType": "PEDIMENTO_ERP",
  "occurrenceMode": "single",
  "metadata": {
    "lote": "A123",
    "folio_erp": "4711",
    "estado_externo": "RECIBIDO",
    "requiere_atencion": false
  }
}
```

**Respuesta 200** (campos principales — shape completo y anotado en `api-docs/API_USO.md` §1): `{ "documentId", "bucket", "key", "documentType", "occurrenceMode", "documentTypeCounts", "classificationMode", "uploadUrl", "uploadMethod", "uploadExpiresAt", "processingStatus": "AWAITING_UPLOAD", "statusUrl", "retryAfterSeconds" }`. El `documentId` es lo que el sistema externo debe guardar para consultar el resultado después (endpoint 1.3). El registro ligero ya existe antes del PUT.

`documentType` es opcional y admite **uno o varios tipos separados por coma**:

- **uno** — se salta la clasificación (más rápido/barato) **y se fuerza** ese tipo, sin re-segmentar. Útil cuando el sistema que integra ya sabe exactamente qué está mandando.
- **varios** (`PEDIMENTO_ADUANAL,FACTURA_HONORARIOS`) — el archivo se segmenta y cada documento encontrado se clasifica **sólo** entre esos tipos; `documents[]` omite todo lo demás. Sin `occurrenceMode`, cada catálogo manda y los tipos que no definan `occurrence_mode` buscan todas por el default global de varios tipos. `single` permite limitar todos a la primera y `:N`/`:all` prevalece por tipo. Si un tipo pedido no aparece, se devuelve un marcador `REVIEW_REQUIRED` con rango `null`, que no cuenta como documento encontrado. `classificationMode` responde `guided`.
- **omitido** — Bedrock clasifica contra todo el catálogo.

En `.xml`/`.xlsx`/`.csv` es obligatorio y sólo se acepta un tipo.

### 1.3 `GET /documents/{id}` — Consultar un resultado

`{id}` acepta el id completo o un **prefijo** (gana el primer match). Devuelve el envelope completo: clasificación, `validation.status` (`APROBADO`/`RECHAZADO`/`REQUIERE_REVISION`), y para expedientes aduanales el detalle de cada documento detectado. Con `?full=1` agrega el detalle campo-por-campo (`extractedValue`/`normalizedValue`/`confidence`/`status`/`checks`) que solo vive en S3.

Mientras trabaja responde `202` con `processingStatus` (`AWAITING_UPLOAD`,
`QUEUED` o `PROCESSING`), `Retry-After` y `retryAfterSeconds`. Al terminar
responde `200` con `FINISHED` o `FAILED`; `validation.status` sigue siendo un
estado de negocio independiente. `404 NOT_FOUND` queda reservado para un id
inexistente o no visible para la app.

Formas abreviadas para implementar el cliente sin cargar ejemplos con todos
los campos extraídos:

```jsonc
// 202: seguir esperando según Retry-After
{ "documentId": "e18d014a-...", "processingStatus": "PROCESSING",
  "result": null, "retryAfterSeconds": 1 }
```

```jsonc
// 200: resultado disponible; documents siempre es arreglo
{
  "documentId": "e18d014a-...",
  "processingStatus": "FINISHED",
  "workflowStatus": "complete",
  "result": {
    "envelopeVersion": 2,
    "documentClassification": "FACTURA_FLETE_EXTRANJERO",
    "aiExecuted": true,
    "aiUsage": { "stages": ["extraction"], "inputTokens": 4074, "outputTokens": 594 },
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

`aiExecuted` responde con evidencia de ejecución, no por duración. En tipos
determinísticos, `documents[].deterministicExtraction.fallbackFields` explica
qué campos activaron el modelo. `deterministicSavings` siempre es un
contrafactual estimado y permanece separado de los tokens facturados de
`aiUsage`/`usage`.

```jsonc
// 200: el workflow falló técnicamente y no produjo result
{ "documentId": "e18d014a-...", "processingStatus": "FAILED",
  "workflowStatus": "enrichment-failure", "result": null }
```

### 1.4 `GET /documents/{id}/file` — Descargar el archivo original

Devuelve, en JSON, una **URL prefirmada de S3** (`downloadUrl`, no los bytes) para el archivo tal cual se subió — el PDF/imagen/XML/XLSX/CSV original, no el resultado de la extracción. `{id}` acepta id completo o prefijo, igual que 1.3.

No proxya los bytes por Lambda/API Gateway a propósito: **API Gateway topa cualquier respuesta a 10 MB** (límite duro de AWS), y los expedientes reales de este sistema llegan a decenas de MB — proxyear no era viable. El cliente descarga directo de S3 con la URL prefirmada, igual que `POST /documents` (1.2) pero en sentido inverso (`GetObjectCommand` en vez de `PutObjectCommand`).

`404 NOT_FOUND` si el id no existe o queda fuera del alcance de la app; `404 FILE_NOT_FOUND` si el documento existe pero no se encontró el objeto original (aún `PROCESSING`, o borrado de S3 por fuera del sistema). Detalle completo, incluidas las implicaciones de la URL temporal (ver 1.8): [`api-docs/API_USO.md`](api-docs/API_USO.md) §5.

### 1.5 `POST /documents/{id}/reprocess` — Crear otro proceso desde el original

Resuelve el id viejo (completo o prefijo), aplica la misma autorización de
lectura y localiza el objeto en `processed/{id}/` o `failed/{id}/`. Después
reserva una fila nueva en DynamoDB y ejecuta `CopyObject` hacia
`raw/{TIPOS?}/{nuevoId}/{filename}`. La creación de esa copia dispara el flujo
normal; no se transfieren bytes por HTTP y el documento/resultados anteriores
no cambian.

El body es opcional y acepta `documentType`, `occurrenceMode` (o
`occurrence_mode`) y `modelId`. La gramática de tipos y precedencia de
cardinalidad es idéntica a 1.2. Omitir `documentType` hereda la declaración
anterior y sus cuentas explícitas; `null`/vacío fuerza auto. La metadata actual
se conserva. Responde `202` con id nuevo, `reprocessedFrom`, `QUEUED`,
`statusUrl`, `Location` y `Retry-After`.

Una key válida pero no registrada conserva lectura por compatibilidad, pero es
read-only: no puede reprocesar ni hacer PATCH. Toda app registrada sí puede
reprocesar los documentos que su `AccessScope`/`AllowedDocTypes` le permita ver.

`409 SOURCE_NOT_READY` indica que el objeto aún sigue en `raw/`; `404
FILE_NOT_FOUND`, que ya no existe bajo `processed/`/`failed/`. Un fallo de copia
marca el trabajo nuevo `FAILED` y responde `500 REPROCESS_COPY_FAILED`.

### 1.6 `PATCH /documents/{id}/metadata` — Actualizar seguimiento externo

Aplica un JSON Merge Patch plano a la columna mutable `Metadata`: un valor
escalar crea/reemplaza, `null` o `""` borra y una clave ausente se conserva.
Permite a ERP/portal/CEDIS manejar estados transitorios libres como
`RECIBIDO`, `EN_REVISION` o `ENVIADO_A_ERP`; estos **no cambian**
`validation.status`, `WorkflowStatus`, EventBridge ni el resultado histórico
de S3. La API superpone la columna actual en todas sus lecturas y filtros.

Una app registrada necesita `AllowMetadataWrite=true` (`pnpm apps:grant --
--name APP --metadata-write`) y además queda limitada por `AccessScope` y
`AllowedDocTypes`. Usa `UpdateItem` con `MetadataVersion` como lock optimista:
si dos integraciones escriben a la vez, relee/reaplica para no perder claves
independientes. Devuelve la metadata completa, versión, fecha y app autora.

### 1.7 `GET /documents` — Buscar/listar

Cuatro filtros combinables, **todos en AND** (al menos uno es obligatorio):

| Filtro | Dónde se evalúa | Notas |
|---|---|---|
| `docType` | **DynamoDB** (Query al GSI `DocumentTypeIndex`) | Acepta `TYPE_ID` o nombre humano, coma para varios. Es el único que evita el `Scan` |
| `system` | Lambda | `tracking.appOrigin`, resuelto de la `x-api-key` del `POST /documents` — no de nada que mande el cliente. Alias `appOrigin`, `sistema` |
| `meta.<clave>` | Lambda | La metadata actual (`POST` + cambios por `PATCH`; `meta.lote=A123`). Claves en minúsculas; claves distintas AND, misma clave OR. `metaOp=contains` para subcadena |
| `field`+`value` | Lambda | Cualquier campo extraído, con `op=eq|contains`. Repetible: los pares se emparejan por posición y son AND |

Soporta además `status`, `since`/`until`, `order`, `limit`, `summary`, `full`. Sin `docType`, hace `Scan` completo de la tabla (más lento, adecuado para volumen moderado). `system` y `meta.<clave>` leen una sola clave conocida del envelope, así que —a diferencia de `field`/`value`— no están detrás del permiso `AllowFieldSearch`.

### 1.8 Autenticación, límites y alcance de acceso (por ambiente)

| | dev | prod |
|---|---|---|
| Auth | `x-api-key` solo si el stack se desplegó con `DEV_API_KEY_REQUIRED=true` (hoy sí, en `.env.dev`); si no, abierto | **`x-api-key`** obligatorio en todos los endpoints — sin key válida, `403` nativo de API Gateway sin invocar Lambda |
| Rate limit | Throttle de stage compartido por todos los clientes (10 req/s en escrituras, 40 req/s por cada ruta GET) + los límites por app si hay key | WAFv2 por IP (escrituras y GET por separado, + reglas gestionadas) + límites por app |
| Límites por app | Throttle de escritura, throttle de GET (más holgado), documentos/día (solo cuentan `POST /documents` y reprocess) y techo de requests/día — ver [`AMBIENTES_Y_SEGURIDAD.md`](AMBIENTES_Y_SEGURIDAD.md) "Límites por app" | igual |
| Al pasarse | `429` (`Too Many Requests` = reintentar con backoff; `Limit Exceeded` o `DAILY_DOCUMENT_LIMIT` = hasta la medianoche UTC) | igual, y el WAF responde `403` |
| Payload (upload) | Igual en ambos | El archivo nunca pasa por la API: `POST` devuelve URL y el cliente hace `PUT` directo a S3 |
| Payload (descarga, 1.4) | Igual en ambos | Sin tope propio — nunca pasa por Lambda/API Gateway, hereda el tamaño que tenga el archivo en S3 |

Detalle completo (ciclo de vida de API keys, WAF, dominio propio): [`AMBIENTES_Y_SEGURIDAD.md`](AMBIENTES_Y_SEGURIDAD.md).

Cada app registrada queda por default con `AccessScope=own`: solo lee, lista,
descarga o modifica documentos cuyo `tracking.appOrigin` coincide con su
`AppName`; un id fuera de alcance responde `404`. `AllowedDocTypes` puede
restringir más. `AccessScope=all` es un grant administrativo explícito. Dos
casos conservan lectura irrestricta por compatibilidad — dev sin key y una key
válida todavía no adoptada — pero una key no registrada **no puede escribir
metadata ni iniciar reprocesos**. Una URL de descarga ya emitida sigue siendo una credencial de vida
corta y debe tratarse como secreto. Ver `api-docs/API_USO.md` §3/§4/§5 y
`AMBIENTES_Y_SEGURIDAD.md`.

### 1.9 `/references` — Registro de referencias de autenticidad

Las firmas, sellos y logos **válidos** de una entidad, contra los que la etapa de
autenticidad compara cada documento. Una referencia pertenece a la **entidad**
(`scope` + `key`, p. ej. `PROVEEDOR` + número de proveedor), no a un tipo de
documento. `scope` y `kind` válidos salen del catálogo
(`authenticity.reference_scopes`: los `authenticity.references.scope` y
elementos de los tipos activos); hoy solo existe `PROVEEDOR` con `FIRMA`,
`SELLO` y `LOGO` (`CARTA_NOM35`).

| Paso | Qué | Cómo |
|---|---|---|
| 1 | `POST /references/{scope}/{key}` con `{kind, label, fileName?}` | `Registry.reserve`: fila `PENDING` en la tabla de huellas (con TTL de 24 h) y URL prefirmada de `PutObject` hacia `references-pending/…` (15 min). Devuelve `refId`, `uploadUrl`, `confirmUrl`. |
| 2 | `PUT {uploadUrl}` con el recorte | Directo a S3, sin Lambda y **sin cabeceras extra** — mismo contrato que 1.2. |
| 3 | `POST …/confirm` (body opcional `{force: true}`) | `Registry.confirm`: lee el recorte, lo revisa con el código del pipeline (formato, peso, dimensiones, tinta), busca duplicados, lo copia a `references/…`, calcula su embedding, lo indexa y activa la fila. Responde la ficha + `warnings`, o `422 REFERENCE_REJECTED` / `409`. |

Es **la misma implementación** que usa el CLI de operador (`pnpm refs:*` →
`pipeline/reference_registry.py`): un recorte que el CLI rechazaría no entra por
la API. Corre en una Lambda propia (**References Lambda**: Python 3.12, mismo
asset que la Enrichment Lambda con handler `references_api.handler`, 29 s,
1024 MB) con permisos mínimos: filas de referencias, los prefijos
`references/` y `references-pending/` del bucket y el índice vectorial — nunca
un documento.

- **Permiso:** todas las rutas, **también los `GET`**, exigen
  `AllowReferenceWrite=true` en la fila de la app (`pnpm apps:grant -- --name
  APP --reference-write`); apagado por default. **Falla cerrado**: key no
  registrada o AppsTable inalcanzable → `403 FORBIDDEN`. En dev sin API keys
  obligatorias queda abierto, como el resto de ese stack.
- **Límites:** no cuenta contra documentos/día. `POST` y `DELETE` están en
  `WRITE_METHODS` (throttle de escritura); los dos `GET`, en el de lectura.
- **Efecto:** la referencia confirmada se usa en los documentos que se procesen
  **después** con `metadata.proveedor = key`.
- **La ficha nunca expone** la huella de tinta ni el embedding.

Contrato completo, límites del recorte y códigos de error:
[`api-docs/API_USO.md`](api-docs/API_USO.md) §8.

---

## 2. APIs externas consumidas (de terceros — AWS)

Son APIs de AWS que este proyecto consume. La orquestación y el ingress son
código CDK propio; no existe ya una biblioteca de blueprint entre el repo y
estos servicios.

### 2.1 Amazon Bedrock — clasificación y extracción de campos (IA generativa)

| Operación | Quién la llama | Nota |
|---|---|---|
| `Converse` | Código propio — `enrichment-lambda/pipeline/bedrock.py` | Segmentación, extracción por documento y recuperación de referencias del pipeline de expediente |
| `Converse` | Código propio — `document-processing/resources/model-invoke-lambda/index.py` | Los pasos `ClassificationStep`/`ProcessingStep` de Step Functions (flujo estándar), con visión |
| `Converse` **con bloques de imagen** | Código propio — `enrichment-lambda/pipeline/vision_inspector.py` (vía `pipeline/bedrock.py`) | Etapa de autenticidad física, solo en tipos que declaran `authenticity` (hoy `CARTA_NOM35`): 1–2 llamadas por documento — la página, y recortes de firma/sello/logo + referencias registradas. Salida forzada por herramienta. Ver [`AUTENTICIDAD_Y_REFERENCIAS.md`](AUTENTICIDAD_Y_REFERENCIAS.md) |

| `InvokeModel` (`amazon.titan-embed-image-v1`, Amazon Titan Multimodal Embeddings G1, 1024 dimensiones) | Código propio — `enrichment-lambda/pipeline/reference_embeddings.py::TitanMultimodalProvider.embed_image` | Embedding de un recorte de firma/sello/logo. Lo llaman la References Lambda al confirmar una referencia (`reference_registry.Registry._store`, 1 imagen por alta), el pipeline (`authenticity._crop_vector`) solo cuando un proveedor tiene más referencias de un elemento que `max_references_per_kind` o en la búsqueda sin proveedor, y `pnpm refs:reindex`/`refs:search`/`refs:eval`. Se factura por imagen, en la línea `BedrockEmbeddings` |

Toda llamada **generativa** al modelo usa **Converse**, la API de Bedrock que es agnóstica del proveedor: el mismo request sirve para cualquier modelo que Bedrock aloje, así que cambiar de modelo es configuración, no código.

Modelo base: `us.anthropic.claude-haiku-4-5-20251001-v1:0` (perfil de inferencia cross-region), vía `BEDROCK_MODEL_ID`. Cada paso puede apuntar a un modelo distinto — para bajar costo o subir precisión — con estos overrides, que caen al modelo base si no se definen:

| Variable | Paso | ¿Necesita visión? |
|---|---|---|
| `BEDROCK_CLASSIFICATION_MODEL_ID` | Clasificación (flujo estándar) | **Sí** |
| `BEDROCK_PROCESSING_MODEL_ID` | Extracción (flujo estándar) | **Sí** |
| `BEDROCK_SEGMENTER_MODEL_ID` | Segmentación del expediente | No |
| `BEDROCK_EXTRACTOR_MODEL_ID` | Extracción de campos (la mayor parte de los tokens) | No |
| `BEDROCK_RECOVERY_MODEL_ID` | Re-extracción Textract + modelo | No |
| `BEDROCK_VISION_MODEL_ID` | Etapa de autenticidad (ve la página y los recortes) | **Sí** — y **no** cae al modelo base: si no se define usa un default con visión (Haiku 4.5), porque `BEDROCK_MODEL_ID` y la lista de fallback pueden ser de solo texto |

Antes de mover un paso a otro modelo, confirmar que el modelo soporte lo que ese paso usa: Converse; tool use forzado (el extractor lo requiere para catálogos de 100+ campos); visión (solo los dos primeros pasos); y su ventana de contexto (retunear `MAX_MODEL_DOC_BYTES`). Requiere además habilitar "Model access" en la consola de Bedrock — paso manual por cuenta/región.

Sobre **prompt caching**: Bedrock rechaza la petición completa si el modelo no lo soporta (no la ignora), y lo reporta como `AccessDeniedException` — el mismo código que un permiso faltante real. `pipeline/bedrock.py` distingue ambos por el mensaje, reintenta sin el `cachePoint` y recuerda el modelo, así que el pipeline funciona con cualquier modelo; lo único que se pierde es el descuento de caché. Capacidades verificadas por modelo: [`ARQUITECTURA.md`](ARQUITECTURA.md).

Detalle de costos reales por llamada: [`COSTOS.md`](COSTOS.md). Diagrama de dónde encaja cada llamada: [`ARQUITECTURA.md`](ARQUITECTURA.md).

### 2.2 Amazon Textract — OCR

| Operación | Quién la llama | Nota |
|---|---|---|
| `DetectDocumentText` | Código propio — `pipeline/pdf_pages.py`, `extractors/textract_bedrock.py` | Texto de páginas de PDF **sin** capa de texto (escaneadas). Solo se paga por estas páginas — el resto usa `pypdf`, gratis |
| `AnalyzeID` | Código propio — `extractors/textract_analyze_id.py` | Fallback estructurado para credenciales de identidad (flujo estándar) |

### 2.3 Amazon S3 — almacenamiento de documentos y resultados

| Operación | Quién la llama | Nota |
|---|---|---|
| `PutObject` (vía URL presignada, `getSignedUrl`) | `upload-lambda/index.mjs` | Modo prefirmado: el cliente hace el `PUT`, no la Lambda |
| `GetObject` | `enrichment-lambda/handler.py` | Lee el archivo de `raw/` para procesarlo |
| `PutObject` | `enrichment-lambda/handler.py` | Escribe el resultado completo a `results/{id}.json` |
| `GetObject` | `query-lambda/index.mjs` | Lee `results/{id}.json` para responder `?full=1` |
| `ListObjectsV2` | `query-lambda/index.mjs` | `GET /documents/{id}/file`: localiza el objeto original bajo `processed/{id}/` (o `failed/{id}/`) sin asumir el nombre de archivo |
| `GetObject` (vía URL presignada, `getSignedUrl`) | `query-lambda/index.mjs` | `GET /documents/{id}/file`: firma la URL de descarga, el cliente hace el `GET`, no la Lambda — simétrico al `PutObject` presignado de la subida |
| `HeadObject` + `CopyObject` | `query-lambda/index.mjs` | `POST /documents/{id}/reprocess`: recupera metadata y copia el original de `processed/`/`failed/` a una key nueva en `raw/`; no descarga bytes |
| `GetObject` | `enrichment-lambda/pipeline/fingerprint_store.py` | Lee las imágenes de referencia registradas de un proveedor bajo `references/<scope>/<clave>/<ELEMENTO>/…` (mismo bucket; solo `raw/` dispara el flujo). Por eso la Lambda recibe `DOCUMENT_BUCKET` |
| `PutObject` / `DeleteObject` | `scripts/manage-references.py` (`pnpm refs:*`, boto3) | Alta y baja de esas imágenes de referencia — herramienta de operación, no runtime. Usa el mismo `pipeline/reference_registry.py` que la API |
| `PutObject` (vía URL presignada, `generate_presigned_url`) | References Lambda — `enrichment-lambda/pipeline/reference_registry.py::Registry.upload_url` | `POST /references/{scope}/{key}`: firma la carga del recorte a `references-pending/<scope>/<clave>/<ELEMENTO>/<refId>` (15 min, sin cabeceras firmadas). Ese prefijo tiene lifecycle de 1 día: una carga sin confirmar desaparece sola |
| `HeadObject` + `GetObject` + `DeleteObject` | References Lambda — `reference_registry.py::Registry._read_upload` / `_discard` | `confirm`: mide el objeto antes de bajarlo (rechaza > 3.5 MB sin descargar), lo lee y borra el staging |
| `PutObject` | References Lambda — `reference_registry.py::Registry._store` | `confirm`: escribe el recorte aceptado en `references/<scope>/<clave>/<ELEMENTO>/<refId>.png\|jpg`, donde lo lee el pipeline |
| `GetObject` (vía URL presignada) | References Lambda — `reference_registry.py::Registry.image_url` | `GET /references/…/{refId}`: URL de 5 min para ver la imagen de una referencia activa |
| `DownloadFile` (transfer manager) | Código propio — `model-invoke-lambda/index.py` | Descarga el archivo para la llamada de visión del flujo estándar |
| `CopyObject` + `DeleteObject` | Construct propio — `MultiModelDocumentProcessing.moveChain` | Mueve `raw/` a `processed/` tras resultado de negocio o a `failed/` solo ante falla técnica. |

### 2.4 Amazon DynamoDB — catálogo, tracking y auditoría de costos

Varias tablas distintas, mismo servicio (tracking, catálogo, costos, y — abajo — llaves únicas y huellas de autenticidad):

| Operación | Quién la llama | Tabla | Nota |
|---|---|---|---|
| `PutItem` condicional | `upload-lambda/index.mjs` | tracking | Reserva el id con `ProcessingStatus=AWAITING_UPLOAD` antes de entregar la URL; si falla, el POST responde `JOB_REGISTRATION_FAILED` |
| `GetItem` | `query-lambda/index.mjs` | tracking | Lookup exacto por id |
| `Scan` | `query-lambda/index.mjs` | tracking | Fallback por prefijo de id, y búsqueda sin `docType` |
| `Query` | `query-lambda/index.mjs` | tracking | Vía GSI `DocumentTypeIndex` — la ruta indexada, sin `Scan` |
| `BatchGetItem` | `query-lambda/index.mjs` | tracking | Trae el envelope completo de los ids que matchearon el índice |
| `UpdateItem` condicional | `query-lambda/index.mjs` | tracking | `PATCH /documents/{id}/metadata`: actualiza `Metadata` y su versión/auditoría; no toca `EnrichmentResult` |
| `PutItem` + `UpdateItem` | `query-lambda/index.mjs` | tracking | reproceso: reserva el id nuevo antes de copiar; si `CopyObject` falla lo marca `FAILED` |
| `UpdateItem` | `enrichment-lambda/indexing.py` | tracking | Promueve columnas indexadas al terminar de validar |
| `UpdateItem` | SQS consumer (`resources/sqs-consumer/index.py`) | tracking | Marca `QUEUED` al recibir `ObjectCreated`, sin hacer retroceder entregas duplicadas |
| `UpdateItem` | `status-event-lambda/index.py` | tracking | Proyecta eventos terminales a `FINISHED` o `FAILED` |
| `Scan` | `enrichment-lambda/document_catalog.py` | catálogo | Refresca tipos/reglas al vencer el TTL (30 s por default), no por documento |
| `Scan` | `query-lambda/index.mjs` | catálogo | Resuelve nombre humano → `TYPE_ID` |
| `PutItem` | `enrichment-lambda/cost_audit.py` | costos (`document-validation-costs[-dev]`) | Un item por documento procesado: $ desglosado por servicio (Bedrock segmentación/extracción/upstream, Textract, Lambda). Best-effort, tabla `RETAIN` separada de tracking — ver `COSTOS.md` §7 |
| `PutItem` + `Query` | `enrichment-lambda/pipeline/fingerprint_store.py` | huellas (`document-validation-fingerprints[-dev]`) | Etapa de autenticidad: escribe la huella de tinta de firma/sello, el hash de cualquier imagen incrustada bajo ellos y la identidad de la carta, y consulta la partición del proveedor para detectar la MISMA firma en la carta de otro empleado o un reenvío. También lee (`Query`) las referencias registradas (`Pk = R#…`). Best-effort: sin tabla o con error, los campos quedan vacíos. Tabla `RETAIN`, una por ambiente |
| `PutItem` / `UpdateItem` / `DeleteItem` / `Query` / `Scan` | `scripts/manage-references.py` (`pnpm refs:*`, boto3) | huellas | Alta, consulta y baja de referencias por proveedor — herramienta de operación, no runtime |
| `PutItem` condicional / `GetItem` / `Query` / `UpdateItem` | References Lambda — `enrichment-lambda/pipeline/reference_registry.py::Registry` | huellas | API `/references`: reserva la fila `R#<scope>#<clave>` / `<ELEMENTO>#<refId>` en `PENDING` con `ExpiresAt` (TTL de la tabla, 24 h), la activa quitando `ExpiresAt` en la misma escritura, la lista y la desactiva. El TTL solo aplica a filas `PENDING` |
| `GetItem` | References Lambda — `enrichment-lambda/references_api.py::resolve_caller` | apps (`document-validation-apps[-dev]`) | Lee `AllowReferenceWrite` de la app que llama; sin fila o con error → `403` (falla cerrado) |
| `Scan` | References Lambda — `enrichment-lambda/document_catalog.py` (vía `references_api._catalog_scopes`) | catálogo | Qué `scope`/`kind` declaran los tipos activos |
| `Scan` / `GetItem` | `scripts/ver-costos.py` (boto3 directo, no `aws` CLI) | costos | `pnpm costos` / `pnpm costos -- --id <id>` — herramienta de auditoría, no runtime |

### 2.5 AWS Step Functions — orquestación

| Operación | Quién la llama | Nota |
|---|---|---|
| `StartExecution` | Código propio — `resources/sqs-consumer/index.py` | Arranca una ejecución idempotente por objeto nuevo en `S3 raw/` (vía SQS) |
| `DescribeExecution` / `GetExecutionHistory` | Scripts CLI (`scripts/track-execution.mjs`, `scripts/ver-resultado.py`) | Solo herramientas de desarrollo/operación, no runtime del sistema |

### 2.6 Amazon EventBridge — notificación del resultado final

`EventBridgePutEvents` se declara directamente en el construct propio. Publica
`document-processed-successful` / `document-processing-failed`; una regla
interna entrega ambos a `status-event-lambda/index.py`.

### 2.7 Amazon SQS — desacople S3 → Step Functions

Solo se **consume** mediante el event-source-mapping de la Lambda propia; no hay
llamadas manuales `ReceiveMessage`/`DeleteMessage`.

### 2.8 AWS Certificate Manager (ACM) — solo operación (prod)

`aws acm import-certificate` (CLI, `scripts/import-cert.mjs`) — importa/renueva el certificado TLS del dominio propio. No es parte del runtime del pipeline, es una tarea operativa manual.

### 2.9 Amazon S3 Vectors — índice de similitud de las referencias

Bucket vectorial `document-validation-references[-dev]`, índice
`references-titan-multimodal-1024` (float32, 1024 dimensiones, distancia
coseno). Es un dato **derivado**: cada fila de referencia guarda su propio
vector en DynamoDB y `pnpm refs:reindex` reconstruye el índice desde la tabla.

| Operación | Quién la llama | Nota |
|---|---|---|
| `PutVectors` | References Lambda — `enrichment-lambda/pipeline/reference_index.py::put` (desde `reference_registry.Registry._embedding_fields`) | Al confirmar una referencia. Si falla, la referencia queda activa con `indexed: false` y un nuevo `confirm` o `pnpm refs:reindex` lo reintenta |
| `DeleteVectors` | References Lambda — `reference_index.py::delete` (desde `Registry._unindex`) | Al dar de baja o borrar una referencia |
| `QueryVectors` (+ `GetVectors`, que el filtro por metadatos exige) | Enrichment Lambda — `reference_index.py::search` (desde `authenticity._search_references`) | Búsqueda sin proveedor: solo si el tipo declara `authenticity.references.search.enabled`. El pipeline **solo lee** el índice |
| `PutVectors` / `DeleteVectors` / `QueryVectors` | `scripts/manage-references.py` (`pnpm refs:register`, `refs:remove`, `refs:reindex`, `refs:search`) | Herramienta de operación, no runtime |

---

## 3. Matriz resumen (para lectura rápida de negocio)

| API | Tipo | Dirección | Dependencia de proveedor | Documentación técnica |
|---|---|---|---|---|
| `POST /documents` | Interna | Entrante (terceros → nosotros) | — | `api-docs/API_USO.md` §1 |
| `GET /documents/{id}` | Interna | Entrante | — | `api-docs/API_USO.md` §2 |
| `POST /documents/{id}/reprocess` | Interna | Entrante | — | `api-docs/API_USO.md` §3 |
| `PATCH /documents/{id}/metadata` | Interna | Entrante | — | `api-docs/API_USO.md` §4 |
| `GET /documents/{id}/file` | Interna | Entrante | — | `api-docs/API_USO.md` §5 |
| `GET /documents` | Interna | Entrante | — | `api-docs/API_USO.md` §6 |
| `/references/{scope}/{key}…` (5 operaciones) | Interna | Entrante | — | `api-docs/API_USO.md` §8 |
| Amazon Bedrock (Converse/InvokeModel) | Externa | Saliente (nosotros → AWS) | Alta — es el motor de IA del producto | `ARQUITECTURA.md`, `COSTOS.md` |
| Amazon Textract | Externa | Saliente | Media — solo OCR de páginas escaneadas | `ARQUITECTURA.md` |
| Amazon S3 | Externa | Saliente | Alta — almacenamiento de todo el pipeline | `ARQUITECTURA.md` |
| Amazon S3 Vectors | Externa | Saliente | Baja — índice derivado, reconstruible con `pnpm refs:reindex`; solo lo usa la búsqueda de referencias por similitud | `AUTENTICIDAD_Y_REFERENCIAS.md` |
| Amazon DynamoDB | Externa | Saliente | Alta — catálogo y tracking | `ARQUITECTURA.md`, `catalogs/README.md` |
| Amazon EventBridge | Externa | Saliente (construct propio) | Media — notificación, un consumidor externo depende de esto | `api-docs/API_USO.md` §1 |
| AWS ACM | Externa | Operación manual | Baja — solo prod, renovación de cert | `AMBIENTES_Y_SEGURIDAD.md` |

---

## 4. Para el equipo de integración (checklist temprano)

- **Autenticación**: en prod, todo consumidor externo del API interna necesita una `x-api-key` provisionada — pedirla con anticipación (no es self-service hoy).
- **Tamaño de archivo (subida)**: siempre se implementa el flujo de dos pasos (pedir `uploadUrl`, luego `PUT` directo a S3); el modo inline ya no existe.
- **Consumo del resultado**: dos formas — *pull* (`GET /documents/{id}`: `202` + `Retry-After`, backoff con jitter, máximo recomendado 30 s) o *push* (suscribirse a EventBridge, requiere estar dentro de la cuenta/organización AWS o una regla de reenvío cross-account). Un `404` no se reintenta como pendiente.
- **Metadata externa**: si la integración necesita estados transitorios/marcas, otorgar `AllowMetadataWrite` y usar `PATCH /documents/{id}/metadata`; no reutilizar `validation.status`.
- **Referencias de autenticidad**: si la integración va a registrar las firmas/sellos/logos de sus proveedores, pedir `AllowReferenceWrite` (lo exigen todas las rutas `/references`, también los `GET`) y registrar **antes** de subir los documentos; la `key` debe ser exactamente el valor que después se manda en `metadata.proveedor`.
- **Descarga del archivo original**: soportada vía `GET /documents/{id}/file` (URL prefirmada de S3, sin tope de tamaño real — ver 1.4). La API key aplica el alcance por app antes de emitirla; después, la URL misma es una credencial temporal y no debe compartirse.
- **Dependencia de proveedor único**: todo el pipeline depende de AWS (Bedrock/Textract/S3/DynamoDB) — no hay abstracción multi-proveedor; cualquier cambio de proveedor de IA es un rediseño, no una config.
