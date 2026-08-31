<!--
  MANTENIMIENTO: Inventario de APIs para integración con otros sistemas —
  internas (expuestas por este proyecto) y externas (servicios de AWS que
  este proyecto consume). Si agregas/quitas una ruta en
  `src/document-validation-stack.ts`, cambias el shape de una respuesta en
  `upload-lambda`/`query-lambda`, o agregas una llamada nueva a un servicio
  de AWS (nuevo cliente boto3/@aws-sdk en `enrichment-lambda`, `upload-lambda`
  o `query-lambda`), actualiza este documento en el mismo cambio. El detalle
  técnico exhaustivo del API REST interno vive en `API_USO.md` — este
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

Son APIs **propias**: este equipo las diseña, versiona y opera. Un sistema externo (ERP, portal, otro backend) las consume para integrarse con el pipeline de validación. Documentación técnica completa (bodies, ejemplos curl, códigos de error): **[`API_USO.md`](API_USO.md)**.

### 1.1 Resumen de endpoints

| # | Método | Ruta | Para qué (negocio) | Quién la implementa |
|---|---|---|---|---|
| 1 | `POST` | `/documents` | **Iniciar el proceso**: sube un documento (o pide una URL para subir uno grande) y arranca la validación | `upload-lambda/index.mjs` |
| 2 | `GET` | `/documents/{id}` | **Consultar** el resultado completo de un documento ya procesado, por su id | `query-lambda/index.mjs` |
| 3 | `GET` | `/documents/{id}/file` | **Descargar** el archivo original (el PDF/imagen/XML/XLSX/CSV que se subió, no el resultado) vía URL prefirmada de S3 | `query-lambda/index.mjs` |
| 4 | `GET` | `/documents?docType=&field=&value=` | **Buscar/listar** documentos por tipo o por cualquier campo extraído | `query-lambda/index.mjs` |

**Descarga del archivo original (endpoint 3):** el `documentId` no es un key S3 estático — mientras el documento está `PROCESSING` vive en `raw/{TIPO?}/{uuid}/archivo`, y el blueprint (`QueuedS3Adapter.createSuccessChain`/`createFailedChain`, en `@cdklabs/cdk-appmod-catalog-blueprints`) mueve el objeto (copyObject + deleteObject) a `processed/{documentId}/{filename}` cuando la ejecución de Step Functions termina sin excepción — el caso normal, incluida una validación RECHAZADA: eso es un resultado de negocio, no una falla de infraestructura — o a `failed/{documentId}/{filename}` únicamente cuando la propia ejecución revienta (excepción no capturada, timeout). `filename` es el nombre de archivo tal cual (sin el uuid ni el tipo delante — ver `patches/default-sqs-consumer-index.py`). El endpoint solo responde para documentos que `GET /documents/{id}` ya resuelve (o sea, ya salieron de `raw/`), así que resuelve el key real con un `ListObjectsV2` de una sola key contra `processed/{documentId}/` y, si no hay nada, `failed/{documentId}/` — nunca reconstruye el nombre de archivo a ciegas ni lee de `raw/`. Devuelve una URL prefirmada de `GetObjectCommand` (mismo patrón que el modo prefirmado de subida, `getSignedUrl`), no los bytes — ver el porqué (límite de 10 MB de API Gateway) y las implicaciones de seguridad de una key compartida en la sección 1.5 y en `API_USO.md` §3.

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

**Body** (`fileName` requerido; `documentType`, `metadata`, `modelId` opcionales). Acepta `application/json` o `application/x-www-form-urlencoded`:
```jsonc
{ "fileName": "pedimento.pdf", "documentType": "PEDIMENTO_ADUANAL", "metadata": {} }
```

**Respuesta 200**: `{ "documentId", "bucket", "key", "documentType", "classificationMode", "uploadUrl"? }`. El `documentId` es lo que el sistema externo debe guardar para consultar el resultado después (endpoint 1.3).

`documentType` es opcional: si se omite, Bedrock clasifica automáticamente; si se envía, se salta la clasificación (más rápido/barato) **y se fuerza** ese tipo — útil cuando el sistema que integra ya sabe qué tipo de documento está mandando.

### 1.3 `GET /documents/{id}` — Consultar un resultado

`{id}` acepta el id completo o un **prefijo** (gana el primer match). Devuelve el envelope completo: clasificación, `validation.status` (`APROBADO`/`RECHAZADO`/`REQUIERE_REVISION`), y para expedientes aduanales el detalle de cada documento detectado. Con `?full=1` agrega el detalle campo-por-campo (`extractedValue`/`normalizedValue`/`confidence`/`status`/`checks`) que solo vive en S3.

`404 NOT_FOUND` si no existe.

### 1.4 `GET /documents/{id}/file` — Descargar el archivo original

Devuelve, en JSON, una **URL prefirmada de S3** (`downloadUrl`, no los bytes) para el archivo tal cual se subió — el PDF/imagen/XML/XLSX/CSV original, no el resultado de la extracción. `{id}` acepta id completo o prefijo, igual que 1.3.

No proxya los bytes por Lambda/API Gateway a propósito: **API Gateway topa cualquier respuesta a 10 MB** (límite duro de AWS), y los expedientes reales de este sistema llegan a decenas de MB — proxyear no era viable. El cliente descarga directo de S3 con la URL prefirmada, igual que `POST /documents` (1.2) pero en sentido inverso (`GetObjectCommand` en vez de `PutObjectCommand`).

`404 NOT_FOUND` si el id no existe; `404 FILE_NOT_FOUND` si el documento existe pero no se encontró el objeto original (aún `PROCESSING`, o borrado de S3 por fuera del sistema). Detalle completo, incluidas las **implicaciones de seguridad de exponer archivos originales bajo el mismo modelo de API key** (ver 1.5): [`API_USO.md`](API_USO.md) §3.

### 1.5 `GET /documents` — Buscar/listar

Dos filtros combinables (al menos uno es obligatorio): `docType` (indexado vía GSI, acepta `TYPE_ID` o nombre humano) y/o `field`+`value` (búsqueda por cualquier campo extraído, con `op=eq|contains`). Soporta `status`, `since`/`until`, `order`, `limit`, `summary`, `full`. Sin `docType`, hace `Scan` completo de la tabla (más lento, adecuado para volumen moderado).

### 1.6 Autenticación, límites y alcance de acceso (por ambiente)

| | dev | prod |
|---|---|---|
| Auth | Ninguna (abierto) | **`x-api-key`** obligatorio en los 4 endpoints — sin key válida, `403` nativo de API Gateway sin invocar Lambda |
| Rate limit | No | WAFv2 (rate-based + reglas gestionadas) + usage plan (throttle/cuota por key) |
| Payload (upload) | Igual en ambos | Inline tope real ~4 MB (base64 + límite de invocación síncrona de Lambda, 6 MB); sin tope real vía modo prefirmado |
| Payload (descarga, 1.4) | Igual en ambos | Sin tope propio — nunca pasa por Lambda/API Gateway, hereda el tamaño que tenga el archivo en S3 |

Detalle completo (ciclo de vida de API keys, WAF, dominio propio): [`AMBIENTES_Y_SEGURIDAD.md`](AMBIENTES_Y_SEGURIDAD.md).

**Importante para el equipo de integración — la `x-api-key` NO es una ACL por documento.** Sea la key de una app registrada (`pnpm apps:register`) o una key única compartida "por sistema", **cualquier key válida puede leer, listar y descargar el archivo original de CUALQUIER documento procesado en ese ambiente**, no solo los que subió la misma app — `tracking.appOrigin` es trazabilidad (para auditoría posterior), nunca un control de acceso; no hay ningún filtro server-side que lo compare contra la key que hace el request. Esto ya era cierto para `GET /documents/{id}` (JSON), pero pesa más en 1.4 porque expone bytes de archivo (posible PII: identificaciones, RFC/CURP, montos) en vez de solo campos extraídos. Si varios sistemas externos van a compartir el mismo ambiente/key y sus datos deben estar aislados entre sí (no solo etiquetados), la opción real hoy es una key + stack por sistema, o agregar una validación de `appOrigin` en `query-lambda` antes de responder (no implementada). Ver `API_USO.md` §3 para el detalle completo, incluida la implicación adicional de que la URL prefirmada en sí misma es una credencial de acceso de vida corta una vez emitida.

---

## 2. APIs externas consumidas (de terceros — AWS)

Son APIs que este proyecto **no controla**: dependencias de AWS. Cambios de disponibilidad, pricing, límites o deprecación de estos servicios son riesgo externo, no interno. Se listan por servicio, con la operación exacta y qué la invoca — para distinguir qué corre en **código propio de este repo** vs. qué corre en el **blueprint** (`@cdklabs/cdk-appmod-catalog-blueprints`, infraestructura de terceros ya integrada).

### 2.1 Amazon Bedrock — clasificación y extracción de campos (IA generativa)

| Operación | Quién la llama | Nota |
|---|---|---|
| `Converse` | Código propio — `enrichment-lambda/pipeline/bedrock.py` | Segmentación, extracción por documento y recuperación de referencias del pipeline de expediente |
| `Converse` | Código propio — `document-processing/resources/model-invoke-lambda/index.py` | Los pasos `ClassificationStep`/`ProcessingStep` de Step Functions (flujo estándar), con visión |

Toda llamada al modelo usa **Converse**, la API de Bedrock que es agnóstica del proveedor: el mismo request sirve para cualquier modelo que Bedrock aloje, así que cambiar de modelo es configuración, no código.

Modelo base: `us.anthropic.claude-haiku-4-5-20251001-v1:0` (perfil de inferencia cross-region), vía `BEDROCK_MODEL_ID`. Cada paso puede apuntar a un modelo distinto — para bajar costo o subir precisión — con estos overrides, que caen al modelo base si no se definen:

| Variable | Paso | ¿Necesita visión? |
|---|---|---|
| `BEDROCK_CLASSIFICATION_MODEL_ID` | Clasificación (flujo estándar) | **Sí** |
| `BEDROCK_PROCESSING_MODEL_ID` | Extracción (flujo estándar) | **Sí** |
| `BEDROCK_SEGMENTER_MODEL_ID` | Segmentación del expediente | No |
| `BEDROCK_EXTRACTOR_MODEL_ID` | Extracción de campos (la mayor parte de los tokens) | No |
| `BEDROCK_RECOVERY_MODEL_ID` | Re-extracción Textract + modelo | No |

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
| `PutObject` (directo) | `upload-lambda/index.mjs` | Modo inline: escribe el archivo subido a `raw/` |
| `PutObject` (vía URL presignada, `getSignedUrl`) | `upload-lambda/index.mjs` | Modo prefirmado: el cliente hace el `PUT`, no la Lambda |
| `GetObject` | `enrichment-lambda/handler.py` | Lee el archivo de `raw/` para procesarlo |
| `PutObject` | `enrichment-lambda/handler.py` | Escribe el resultado completo a `results/{id}.json` |
| `GetObject` | `query-lambda/index.mjs` | Lee `results/{id}.json` para responder `?full=1` |
| `ListObjectsV2` | `query-lambda/index.mjs` | `GET /documents/{id}/file`: localiza el objeto original bajo `processed/{id}/` (o `failed/{id}/`) sin asumir el nombre de archivo |
| `GetObject` (vía URL presignada, `getSignedUrl`) | `query-lambda/index.mjs` | `GET /documents/{id}/file`: firma la URL de descarga, el cliente hace el `GET`, no la Lambda — simétrico al `PutObject` presignado de la subida |
| `DownloadFile` (transfer manager) | Código propio — `model-invoke-lambda/index.py` | Descarga el archivo para la llamada de visión del flujo estándar |
| `CopyObject` + `DeleteObject` | Blueprint — `QueuedS3Adapter.createSuccessChain` (`@cdklabs/cdk-appmod-catalog-blueprints`) | Al terminar la ejecución SIN excepción (incluida una validación RECHAZADA — es un resultado de negocio, no una falla): mueve `raw/{TIPO?}/{uuid}/archivo` → `processed/{documentId}/{filename}`. Código propio no lo invoca directamente. |
| `CopyObject` + `DeleteObject` | Blueprint — `QueuedS3Adapter.createFailedChain` (`@cdklabs/cdk-appmod-catalog-blueprints`) | Mismo movimiento pero a `failed/{documentId}/{filename}`, solo cuando la propia ejecución de Step Functions revienta (excepción no capturada, timeout) — no cuando el documento simplemente sale RECHAZADO. |

### 2.4 Amazon DynamoDB — catálogo, tracking y auditoría de costos

Tres tablas distintas, mismo servicio:

| Operación | Quién la llama | Tabla | Nota |
|---|---|---|---|
| `GetItem` | `query-lambda/index.mjs` | tracking | Lookup exacto por id |
| `Scan` | `query-lambda/index.mjs` | tracking | Fallback por prefijo de id, y búsqueda sin `docType` |
| `Query` | `query-lambda/index.mjs` | tracking | Vía GSI `DocumentTypeIndex` — la ruta indexada, sin `Scan` |
| `BatchGetItem` | `query-lambda/index.mjs` | tracking | Trae el envelope completo de los ids que matchearon el índice |
| `UpdateItem` | `enrichment-lambda/indexing.py` | tracking | Promueve columnas indexadas al terminar de validar |
| `Scan` | `enrichment-lambda/document_catalog.py` | catálogo | Carga tipos/reglas hot-reload al inicio de cada invocación |
| `Scan` | `query-lambda/index.mjs` | catálogo | Resuelve nombre humano → `TYPE_ID` |
| `PutItem` | `enrichment-lambda/cost_audit.py` | costos (`document-validation-costs[-dev]`) | Un item por documento procesado: $ desglosado por servicio (Bedrock segmentación/extracción/upstream, Textract, Lambda). Best-effort, tabla `RETAIN` separada de tracking — ver `COSTOS.md` §7 |
| `Scan` / `GetItem` | `scripts/ver-costos.py` (boto3 directo, no `aws` CLI) | costos | `pnpm costos` / `pnpm costos -- --id <id>` — herramienta de auditoría, no runtime |

### 2.5 AWS Step Functions — orquestación

| Operación | Quién la llama | Nota |
|---|---|---|
| `StartExecution` | Blueprint — `patches/default-sqs-consumer-index.py` | Arranca una ejecución por cada objeto nuevo en `S3 raw/` (vía SQS) |
| `DescribeExecution` / `GetExecutionHistory` | Scripts CLI (`scripts/track-execution.mjs`, `scripts/ver-resultado.py`) | Solo herramientas de desarrollo/operación, no runtime del sistema |

### 2.6 Amazon EventBridge — notificación del resultado final

**No hay ninguna llamada directa a `PutEvents` en el código de este repo.** La publicación del evento final (`document-processed`, broker `idp-broker`) la resuelve el blueprint dentro de la definición de Step Functions — este repo solo configura el broker (`EventbridgeBroker` en `document-validation-stack.ts`) y diseña el *shape* del resultado para que sea consumible. Un sistema externo que quiera reaccionar al resultado final se suscribe a este bus/evento (dependencia externa AWS, no HTTP).

### 2.7 Amazon SQS — desacople S3 → Step Functions

Solo se **consume** (trigger por event-source-mapping en la Lambda del blueprint); no hay ninguna llamada directa `SendMessage`/`ReceiveMessage`/`DeleteMessage` en código propio.

### 2.8 AWS Certificate Manager (ACM) — solo operación (prod)

`aws acm import-certificate` (CLI, `scripts/import-cert.mjs`) — importa/renueva el certificado TLS del dominio propio. No es parte del runtime del pipeline, es una tarea operativa manual.

---

## 3. Matriz resumen (para lectura rápida de negocio)

| API | Tipo | Dirección | Dependencia de proveedor | Documentación técnica |
|---|---|---|---|---|
| `POST /documents` | Interna | Entrante (terceros → nosotros) | — | `API_USO.md` §1 |
| `GET /documents/{id}` | Interna | Entrante | — | `API_USO.md` §2 |
| `GET /documents/{id}/file` | Interna | Entrante | — | `API_USO.md` §3 |
| `GET /documents` | Interna | Entrante | — | `API_USO.md` §4 |
| Amazon Bedrock (Converse/InvokeModel) | Externa | Saliente (nosotros → AWS) | Alta — es el motor de IA del producto | `ARQUITECTURA.md`, `COSTOS.md` |
| Amazon Textract | Externa | Saliente | Media — solo OCR de páginas escaneadas | `ARQUITECTURA.md` |
| Amazon S3 | Externa | Saliente | Alta — almacenamiento de todo el pipeline | `ARQUITECTURA.md` |
| Amazon DynamoDB | Externa | Saliente | Alta — catálogo y tracking | `ARQUITECTURA.md`, `catalogs/README.md` |
| AWS Step Functions | Externa | Saliente (vía blueprint) | Media — orquestación, reemplazable | `FLUJO_EJECUCION.md` |
| Amazon EventBridge | Externa | Saliente (vía blueprint) | Media — notificación, un consumidor externo depende de esto | `API_USO.md` §1 |
| Amazon SQS | Externa | Entrante (consumo, vía blueprint) | Baja — desacople interno | `FLUJO_EJECUCION.md` |
| AWS ACM | Externa | Operación manual | Baja — solo prod, renovación de cert | `AMBIENTES_Y_SEGURIDAD.md` |

---

## 4. Para el equipo de integración (checklist temprano)

- **Autenticación**: en prod, todo consumidor externo del API interna necesita una `x-api-key` provisionada — pedirla con anticipación (no es self-service hoy).
- **Tamaño de archivo (subida)**: si el sistema que integra manda documentos >4 MB, debe implementar el flujo de **modo prefirmado** (dos pasos: pedir `uploadUrl`, luego `PUT` a S3), no el modo inline.
- **Consumo del resultado**: dos formas — *pull* (`GET /documents/{id}` por polling) o *push* (suscribirse a EventBridge, requiere estar dentro de la cuenta/organización AWS o una regla de reenvío cross-account — a definir con el equipo de plataforma).
- **Descarga del archivo original**: soportada vía `GET /documents/{id}/file` (URL prefirmada de S3, sin tope de tamaño real — ver 1.4). **Antes de dar esta URL a un consumidor externo**, confirmar que entiende que **una sola `x-api-key` no aísla los documentos por sistema** (1.6): si varios consumidores comparten ambiente/key, cualquiera puede descargar los archivos de cualquier otro. Si eso es inaceptable para el requisito de integración (p. ej. datos de clientes distintos), resolverlo ANTES de comprometerse — no es un cambio trivial una vez que el consumidor ya integró.
- **Dependencia de proveedor único**: todo el pipeline depende de AWS (Bedrock/Textract/S3/DynamoDB) — no hay abstracción multi-proveedor; cualquier cambio de proveedor de IA es un rediseño, no una config.
