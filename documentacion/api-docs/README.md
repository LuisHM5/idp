# API IDP — Documentación para integradores

> **IDP** = *Intelligent Document Processing*. En español: **Gestor Documental
> Inteligente (GDI)**. Es el mismo sistema con sus dos nombres.

Esta carpeta es **todo lo que necesitas para integrarte con el API**, sin
tener que conocer el resto del proyecto: qué hace, cómo autenticarte, cómo
mandar un documento y leer el resultado, y ejemplos de código listos para
copiar en varios lenguajes.

## ¿Qué hace esta API?

Recibe un documento (PDF, imagen, XML o XLSX), lo procesa de forma
automática — extrae sus datos y los valida contra reglas de negocio — y te
deja el resultado disponible para consultar por HTTP. Tú controlas todo el
ciclo con **una API REST**: registrar el documento, subirlo, y leer/buscar el
resultado cuando esté listo. Es HTTP + JSON, así que funciona desde cualquier
lenguaje que pueda hacer un request.

**¿Integras desde Node.js o TypeScript?** Hay un SDK que ya resuelve el flujo
de dos pasos, la espera del resultado, los reintentos y los errores:
[`idp-sdk-node`](https://code.alsuper.com/Infraestructura/idp-sdk-node). Se instala directo del repositorio:

```bash
pnpm add git+https://code.alsuper.com/Infraestructura/idp-sdk-node.git
```

Para cualquier otro lenguaje no hay nada que instalar: sigue con los archivos
de esta carpeta.

## ¿Qué hay en esta carpeta?

| Archivo | Para qué | Cuándo usarlo |
| --- | --- | --- |
| **[`API_USO.md`](API_USO.md)** | Referencia completa: los 8 endpoints de subida/consulta más el catálogo de tipos, cada uno con su request/response anotado y ejemplos en **curl, Node.js, Python y C#** (selector de lenguaje por paso). | Tu documento de cabecera. Empieza aquí. |
| **[`openapi.yaml`](openapi.yaml)** | El mismo API como contrato OpenAPI 3.1 — tipado, machine-readable. | Generar un cliente automático (Postman, `openapi-generator`, etc.) o cargarlo en tu editor de OpenAPI favorito. |
| **[`serve.mjs`](serve.mjs)** | Visor local del contrato: **Scalar** (UI moderna) y **Swagger UI**, sobre `openapi.yaml`. Sin dependencias que instalar. | Quieres explorar el API interactivamente en el navegador en vez de leer el YAML. |
| **[`API_RESUMEN.md`](API_RESUMEN.md)** | Chuleta de una página: solo verbos, parámetros y forma de la respuesta, sin prosa. | Ya conoces el API y solo quieres recordar un shape rápido. |
| **[`ejemplos/curl.md`](ejemplos/curl.md)** | Runbook curl copiar-y-pegar, paso a paso, con tabla de errores de API Gateway traducidos. | No te importa el lenguaje, o solo quieres probar el flujo desde la terminal primero. |
| **[`ejemplos/csharp.md`](ejemplos/csharp.md)** | Programa C#/.NET completo en un solo archivo: registrar, subir, esperar el resultado, actualizar metadata, buscar y descargar el original. | Integras desde .NET y quieres un ejemplo end-to-end, no solo fragmentos. |

> Node.js y Python **también tienen ejemplo para cada paso** — están dentro de
> `API_USO.md`, en el selector de lenguaje de cada sección (no en un archivo
> aparte). Si integras en esos lenguajes, ese es el lugar.

### Ver el contrato interactivo (Scalar / Swagger)

Necesitas **Node.js** — nada más, cero `npm install`: el servidor solo usa
Node core y carga Scalar/Swagger UI desde CDN en el navegador. Funciona
igual en Windows, macOS y Linux.

Parado en esta carpeta (`documentacion/api-docs/`), sola, sin el resto del repo:

```bash
npm start
```

(`npm run docs` y `npm run serve` hacen lo mismo — usa el que te resulte más
natural. `node serve.mjs` directo también funciona si prefieres no usar npm.)

Si en cambio tienes el repo completo clonado, desde la raíz:

```bash
pnpm docs:api
```

Cualquiera de las dos abre **Scalar** en `http://localhost:4001/` (default) y
deja **Swagger UI** en `/swagger`. Para otro puerto o sin abrir el navegador:

```bash
npm start -- --port 4500
npm start -- --no-open
```

## Antes de empezar

Necesitas dos cosas, que te da tu contacto en este proyecto (no las generas
tú):

- **URL base** — algo como `https://idp.alsuper.com` (prod) o una URL
  `execute-api.amazonaws.com` (dev/pruebas).
- **`x-api-key`** — una key propia de tu aplicación. Va en el header
  `x-api-key` de **todas** las peticiones excepto una (ver abajo). Además de
  autenticarte, determina qué documentos puedes ver, buscar o modificar.

## El flujo, en cuatro pasos

Subir y leer un documento son siempre **cuatro peticiones HTTP**, sin importar
el tamaño del archivo ni el lenguaje que uses:

| # | Petición | Lleva `x-api-key` | Qué hace |
| --- | --- | --- | --- |
| 1 | `POST /documents` | Sí | Registras el documento (nombre, tipo). Te devuelve un `documentId` y una `uploadUrl` — **no le mandas el archivo aquí**. |
| 2 | `PUT {uploadUrl}` | **No** — va firmada en la URL | Subes los bytes del archivo directo a S3. Responde `200` con cuerpo vacío; el procesamiento arranca solo. |
| 3 | `GET /documents/{documentId}` | Sí | Consultas el resultado. `202` mientras procesa, `200` cuando termina. |
| 4 | `GET /documents/{documentId}/file` | Sí | *(Opcional)* Recuperas el archivo original que subiste. |

Un documento puede tardar de unos segundos a un par de minutos según su
tamaño — el paso 3 se consulta con reintentos (polling), no es instantáneo.

El resultado (paso 3) trae `validation.status` (`APROBADO` / `RECHAZADO` /
`REQUIERE_REVISION`) y, dentro de `documents[]`, cada dato extraído con su
propio nivel de confianza y validez. El shape completo, campo por campo, está
en `API_USO.md` §2.

Hay además tres endpoints opcionales para casos más avanzados — reprocesar el
mismo archivo, guardar tu propia metadata (folio, lote, estado interno) y
buscar documentos por tipo/metadata/campo extraído — documentados igual de
completo en `API_USO.md`.

## ¿Qué tipos de documento existen?

Antes de subir nada, `GET /document-types` te da el catálogo completo —
paginado, 5 por página — con el `type` exacto que va en `documentType`, su
nombre y su descripción. Para ver el schema de campos de uno en particular
(qué extrae, con qué tipo y de dónde), pide ese tipo por su key con
`?includeSchema=1`:

```bash
curl -sS "$API/document-types" -H "x-api-key: $KEY" | jq .
curl -sS -G "$API/document-types/PEDIMENTO_ERP" -H "x-api-key: $KEY" \
  --data-urlencode "includeSchema=1" | jq .
```

Refleja el catálogo en caliente: un tipo nuevo aparece aquí apenas se
sincroniza, sin esperar un despliegue. Detalle completo: `API_USO.md` §7.

## Prueba rápida (curl)

```bash
API="https://idp.alsuper.com"      # tu URL base
KEY="tu-api-key"

# 1. Registrar
RESP=$(curl -sS -X POST "$API/documents" \
  -H "Content-Type: application/json" -H "x-api-key: $KEY" \
  -d '{"fileName":"mi_documento.pdf","documentType":"PEDIMENTO_ERP"}')
DOC_ID=$(echo "$RESP" | jq -r .documentId)
UPLOAD_URL=$(echo "$RESP" | jq -r .uploadUrl)

# 2. Subir (sin x-api-key: va firmada en la URL)
curl -sS --fail -X PUT --upload-file mi_documento.pdf "$UPLOAD_URL"

# 3. Consultar (repite hasta que salga de 202)
curl -sS "$API/documents/$DOC_ID" -H "x-api-key: $KEY" | jq .
```

Versión con reintentos, manejo de errores y explicación línea por línea:
[`ejemplos/curl.md`](ejemplos/curl.md).

## Errores

Todo error responde `{ "error": "CODIGO", "message": "..." }` — programa
contra `error` (estable), no contra `message` (texto libre para humanos). La
tabla completa de códigos, por endpoint, está en `API_USO.md`; los errores más
comunes al integrarse por primera vez (`Missing Authentication Token`, 403 sin
key, 404 de un id que no existe) están explicados en
[`ejemplos/curl.md`](ejemplos/curl.md).

## ¿Y si mi lenguaje no está aquí?

El contrato es HTTP + JSON puro — sin SDK propio. `openapi.yaml` describe cada
endpoint con su schema exacto y puede generar un cliente para prácticamente
cualquier lenguaje (`openapi-generator`, Postman, Insomnia, etc.). Los cuatro
ejemplos existentes (curl, Node.js, Python, C#) cubren el mismo flujo, así que
también sirven como referencia de traducción a otro lenguaje.
