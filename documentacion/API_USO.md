<!--
  MANTENIMIENTO: Este documento es la referencia de uso del API REST.
  Debe mantenerse sincronizado con las rutas definidas en
  `src/document-validation-stack.ts` (RestApi "DocumentUploadApi") y con los
  handlers en `src/document-processing/resources/{upload,query}-lambda/`.
  Si agregas, quitas o cambias un endpoint (ruta, método, params, body o
  respuesta), ACTUALIZA este archivo Y `openapi/openapi.yaml` en el mismo
  cambio. Ver AGENTS.md.
-->

# API REST — Guía de uso

Referencia de todos los endpoints del API de la app de validación documental.

> **Documentación interactiva local (Scalar/Swagger):** `pnpm docs:api` sirve
> el contrato formal [`openapi/openapi.yaml`](../openapi/openapi.yaml) en
> `http://localhost:4001` — Scalar por default (`/`), Swagger UI en
> `/swagger`. Esta guía sigue siendo la referencia narrativa con ejemplos
> curl; el YAML es el contrato tipado de request/response.

## Base URL y autenticación

- **Base URL:** sale del output `ApiEndpoint` del stack. Obtenlo con:
  ```bash
  API=$(aws cloudformation describe-stacks --stack-name DocumentValidationStack \
    --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
    --output text --region us-east-1)
  API=${API%/}   # quita el "/" final
  ```
  Actual: `https://4svzjzyix3.execute-api.us-east-1.amazonaws.com/prod`
  En **prod** con dominio propio: `https://idp.alsuper.com`.
- **Autenticación: manda siempre `x-api-key`.** Es el default del contrato, y
  el mismo código te sirve en los dos ambientes sin condicionales.
  ```bash
  curl -H "x-api-key: $KEY" ...
  ```
  Obtén el valor con:
  ```bash
  aws apigateway get-api-key --api-key <ApiKeyId> --include-value --query value --output text
  ```
  - **prod** (`DocumentValidationStack`): la exige **siempre**, en todos los endpoints.
  - **dev** (`DocumentValidationStack-Dev`): la exige si se desplegó con
    `DEV_API_KEY_REQUIRED=true`. Sin esa variable queda abierto — pero **también
    acepta requests que la manden**, así que no cambies tu cliente por ambiente.
  - Sin key válida cuando se exige → **403 nativo de API Gateway, sin invocar la
    Lambda** (no genera costo de backend). Ver `AMBIENTES_Y_SEGURIDAD.md`.
  - **No la mandes en el `PUT` a S3** (paso 2 de la subida): ese va firmado en la
    URL y no pasa por API Gateway.
- **Autorización por app:** además de autenticar, la `x-api-key` decide qué documentos puede leer o editar esa app (por default, solo los que ella subió) — ver "Seguridad — acceso por app" en la sección 5 y `AMBIENTES_Y_SEGURIDAD.md`.
- **Formato:** JSON. Errores devuelven `{ "error": "CODIGO", "message": "..." }`.
  Programa contra `error` (código estable), no contra `message`.

> **Sobre los ejemplos de este documento:** por brevedad, la mayoría omite
> `-H "x-api-key: $KEY"`. **Agrégalo en todos** — la única petición que NO lo
> lleva es el `PUT` a S3 del paso 2 de la subida, que va firmado en la URL.

## Resumen de endpoints

| Método | Ruta | Para qué |
|---|---|---|
| `POST` | `/documents` | **Paso 1 de 2:** registrar el documento y obtener la URL de subida (no recibe el archivo) |
| `GET`  | `/documents/{id}` | Obtener el resultado completo de un documento por su id |
| `PATCH` | `/documents/{id}/metadata` | Crear, cambiar o borrar estados/marcas propios de la app sin alterar la validación |
| `GET`  | `/documents/{id}/file` | Obtener una URL prefirmada de S3 para descargar el **archivo original** subido |
| `GET`  | `/documents?docType=...` | Listar/filtrar documentos por tipo (indexado, sin Scan) |
| `GET`  | `/documents?system=...&meta.<clave>=...` | Filtrar por qué **sistema** lo subió y por la **metadata** con la que se subió |
| `GET`  | `/documents?field=...&value=...` | Buscar documentos por cualquier campo extraído |

---

## Flujo completo, de principio a fin

Un pedimento real, de la subida hasta tener los campos extraídos. Todo con
`PEDIMENTO_ERP`; cambia el tipo y aplica igual.

| # | Petición | Auth | Cuerpo | Respuesta |
|---|---|---|---|---|
| 1 | `POST $API/upload` | `x-api-key` | JSON o form con la metadata | `200` · `documentId`, `uploadUrl`, `uploadExpiresAt` |
| 2 | `PUT {uploadUrl}` | **ninguna** (firmada en la URL) | los bytes crudos del archivo | `200` con **cuerpo vacío** — S3 no devuelve JSON |
| 3 | `GET $API/documents/{documentId}` | `x-api-key` | — | `200` con el resultado, o `404` mientras procesa |
| 4 | `GET $API/documents/{documentId}/file` | `x-api-key` | — | `200` · URL prefirmada para bajar el original |

El **paso 2 es el único sin `x-api-key`**, y el único que no pasa por API
Gateway: va firmado en la URL, directo a S3. Entre el 2 y el 3 el pipeline
corre solo (`S3 → SQS → Step Functions → Textract/Bedrock → DynamoDB`), sin que
haya que avisarle a nada.

Cada paso abajo trae el mismo ejemplo en **curl, Node.js, Python y C#** —
despliega el que uses. Los ejemplos encadenan: el paso 1 deja las variables
que usan el 2, el 3 y el 4.

> En Scalar (`pnpm docs:api`) los cuatro pasos aparecen como operaciones
> numeradas, cada una con su selector de lenguaje.

### Paso 1 — `POST /documents`

Registra el documento y devuelve la URL de subida. **No lleva el archivo.**

### Paso 1 — `POST /documents`

Registra el documento y devuelve la URL de subida. **No lleva el archivo.**

### Paso 1 — `POST /documents`

Registra el documento y devuelve la URL de subida. **No lleva el archivo.**

### Paso 1 — `POST /upload`

Registra el documento y devuelve la URL de subida. **No lleva el archivo.**

<details open>
<summary><b>curl</b></summary>

```bash
API="https://xxxx.execute-api.us-east-1.amazonaws.com/prod"
KEY="tu-api-key"

RESP=$(curl -sS -X POST "$API/documents" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $KEY" \
  -d '{"fileName":"pedimento.pdf","documentType":"PEDIMENTO_ERP"}')

# Guarda los dos: el uploadUrl para el paso 2, el documentId para el 3.
DOC_ID=$(echo "$RESP"     | jq -r .documentId)
UPLOAD_URL=$(echo "$RESP" | jq -r .uploadUrl)
```

</details>

<details>
<summary><b>Node.js 18+</b> · `fetch` nativo, sin dependencias</summary>

```javascript
const API = "https://xxxx.execute-api.us-east-1.amazonaws.com/prod";
const KEY = "tu-api-key";

const res = await fetch(`${API}/documents`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "x-api-key": KEY },
  body: JSON.stringify({
    fileName: "pedimento.pdf",
    documentType: "PEDIMENTO_ERP",
  }),
});
if (!res.ok) throw new Error(`POST /documents → ${res.status}: ${await res.text()}`);

// Guarda los dos: uploadUrl para el paso 2, documentId para el 3.
const { documentId, uploadUrl } = await res.json();
```

</details>

<details>
<summary><b>Python 3</b> · requiere `requests`</summary>

```python
import requests

API = "https://xxxx.execute-api.us-east-1.amazonaws.com/prod"
KEY = "tu-api-key"

res = requests.post(
    f"{API}/documents",
    headers={"x-api-key": KEY},
    json={"fileName": "pedimento.pdf", "documentType": "PEDIMENTO_ERP"},
    timeout=30,
)
res.raise_for_status()

# Guarda los dos: uploadUrl para el paso 2, documentId para el 3.
up = res.json()
document_id = up["documentId"]
upload_url = up["uploadUrl"]
```

</details>

<details>
<summary><b>C# (.NET 6+)</b> · top-level statements + implicit usings</summary>

```csharp
using System.Net.Http.Json;
using System.Text.Json;

const string api = "https://xxxx.execute-api.us-east-1.amazonaws.com/prod";
const string key = "tu-api-key";

// Cliente del API: siempre con x-api-key.
using var http = new HttpClient();
http.DefaultRequestHeaders.Add("x-api-key", key);

var res = await http.PostAsJsonAsync($"{api}/documents", new
{
    fileName = "pedimento.pdf",
    documentType = "PEDIMENTO_ERP",
});
res.EnsureSuccessStatusCode();

// Guarda los dos: uploadUrl para el paso 2, documentId para el 3.
var up = await res.Content.ReadFromJsonAsync<JsonElement>();
var documentId = up.GetProperty("documentId").GetString()!;
var uploadUrl  = up.GetProperty("uploadUrl").GetString()!;
```

</details>


### Paso 2 — `PUT` del archivo a S3

La única petición **sin `x-api-key`**: va firmada en la URL y no pasa por el API. **No lleva ninguna cabecera extra** — la firma y la metadata ya viajan dentro de la URL.

<details open>
<summary><b>curl</b></summary>

```bash
# UPLOAD_URL viene del paso 1. Si la pegas a mano, usa comillas SIMPLES: una
# URL prefirmada real lleva ~12 `&` y sin comillas el shell la parte en el
# primero, mandando el resto a segundo plano.
UPLOAD_URL='https://...s3.us-east-1.amazonaws.com/raw/PEDIMENTO_ERP/10c7f624-.../pedimento.pdf?X-Amz-Algorithm=...&X-Amz-Signature=...&x-amz-meta-lote=A123&x-id=PutObject'

# NINGUNA cabecera extra: la firma y la metadata ya viajan en la URL (mira los
# `x-amz-meta-*` de arriba). Sin x-api-key: esto no pasa por el API.
curl -sS --fail -X PUT --upload-file pedimento.pdf "$UPLOAD_URL"
# 200 con cuerpo vacío = subido. El pipeline arranca solo.
```

</details>

<details>
<summary><b>Node.js 18+</b> · `fetch` nativo, sin dependencias</summary>

```javascript
import { readFile } from "node:fs/promises";

// uploadUrl viene del paso 1. NINGUNA cabecera extra: la firma y la metadata
// ya viajan en el query string de la URL. Sin x-api-key: no pasa por el API.
const put = await fetch(uploadUrl, {
  method: "PUT",
  body: await readFile("pedimento.pdf"),
});
if (!put.ok) throw new Error(`PUT S3 → ${put.status}: ${await put.text()}`);
// 200 con cuerpo vacío = subido. El pipeline arranca solo.
```

</details>

<details>
<summary><b>Python 3</b> · requiere `requests`</summary>

```python
# upload_url viene del paso 1. NINGUNA cabecera extra: la firma y la metadata
# ya viajan en el query string de la URL. Sin x-api-key: no pasa por el API.
with open("pedimento.pdf", "rb") as fh:
    put = requests.put(upload_url, data=fh, timeout=300)  # data=fh → streaming
put.raise_for_status()
# 200 con cuerpo vacío = subido. El pipeline arranca solo.
```

</details>

<details>
<summary><b>C# (.NET 6+)</b> · top-level statements + implicit usings</summary>

```csharp
// uploadUrl viene del paso 1. Cliente SEPARADO: el del API lleva x-api-key en
// DefaultRequestHeaders y esta petición no debe llevarla.
// NINGUNA cabecera extra: la firma y la metadata ya viajan en la URL.
using var s3 = new HttpClient();
using var content = new StreamContent(File.OpenRead("pedimento.pdf"));

var put = await s3.PutAsync(uploadUrl, content);
put.EnsureSuccessStatusCode();
// 200 con cuerpo vacío = subido. El pipeline arranca solo.
```

</details>


### Paso 3 — `GET /documents/{id}`

El resultado. Mientras el pipeline corre responde `404`: es *todavía no*, no *no existe*.

<details open>
<summary><b>curl</b></summary>

```bash
# DOC_ID viene del paso 1. 404 = todavía procesando, no "no existe".
until curl -sS -f "$API/documents/$DOC_ID" -H "x-api-key: $KEY" -o result.json; do
  echo "aún procesando..."; sleep 5
done

jq '.validation.status' result.json
jq '.expediente.documents[] | {documentType, pageStart, pageEnd, extractedEntities}' result.json
```

</details>

<details>
<summary><b>Node.js 18+</b> · `fetch` nativo, sin dependencias</summary>

```javascript
// documentId viene del paso 1. 404 = todavía procesando, no "no existe".
let doc;
for (let i = 0; i < 60; i++) {
  const r = await fetch(`${API}/documents/${documentId}`, {
    headers: { "x-api-key": KEY },
  });
  if (r.ok) { doc = await r.json(); break; }
  if (r.status !== 404) throw new Error(`GET → ${r.status}: ${await r.text()}`);
  await new Promise((s) => setTimeout(s, 5000));
}

console.log(doc.validation.status);            // APROBADO | REQUIERE_REVISION | RECHAZADO
for (const d of doc.expediente.documents) {
  console.log(d.documentType, `págs ${d.pageStart}-${d.pageEnd}`);
  for (const [campo, v] of Object.entries(d.extractedEntities ?? {}))
    console.log("  ", campo, "=", v.value, `(conf ${v.confidence}, valid ${v.valid})`);
}
```

</details>

<details>
<summary><b>Python 3</b> · requiere `requests`</summary>

```python
import time

# document_id viene del paso 1. 404 = todavía procesando, no "no existe".
doc = None
for _ in range(60):
    r = requests.get(f"{API}/documents/{document_id}",
                     headers={"x-api-key": KEY}, timeout=30)
    if r.ok:
        doc = r.json()
        break
    if r.status_code != 404:
        r.raise_for_status()
    time.sleep(5)

print(doc["validation"]["status"])   # APROBADO | REQUIERE_REVISION | RECHAZADO
for d in doc["expediente"]["documents"]:
    print(d["documentType"], f"págs {d['pageStart']}-{d['pageEnd']}")
    for campo, v in (d.get("extractedEntities") or {}).items():
        print("  ", campo, "=", v["value"], f"(conf {v['confidence']}, valid {v['valid']})")
```

</details>

<details>
<summary><b>C# (.NET 6+)</b> · top-level statements + implicit usings</summary>

```csharp
using System.Net;

// documentId viene del paso 1. 404 = todavía procesando, no "no existe".
JsonElement doc = default;
for (var i = 0; i < 60; i++)
{
    var r = await http.GetAsync($"{api}/documents/{documentId}");
    if (r.IsSuccessStatusCode)
    {
        doc = await r.Content.ReadFromJsonAsync<JsonElement>();
        break;
    }
    if (r.StatusCode != HttpStatusCode.NotFound) r.EnsureSuccessStatusCode();
    await Task.Delay(5000);
}

// APROBADO | REQUIERE_REVISION | RECHAZADO
Console.WriteLine(doc.GetProperty("validation").GetProperty("status"));
foreach (var d in doc.GetProperty("expediente").GetProperty("documents").EnumerateArray())
{
    var tipo  = d.GetProperty("documentType");
    var desde = d.GetProperty("pageStart");
    var hasta = d.GetProperty("pageEnd");
    Console.WriteLine($"{tipo} págs {desde}-{hasta}");
}
```

</details>


### Paso 4 — `GET /documents/{id}/file` (opcional)

Devuelve una **URL prefirmada**, no los bytes: la descarga es una segunda petición, a S3.

<details open>
<summary><b>curl</b></summary>

```bash
# Devuelve una URL prefirmada, NO los bytes. Dos peticiones:
URL=$(curl -sS "$API/documents/$DOC_ID/file" -H "x-api-key: $KEY" | jq -r .downloadUrl)

# La descarga va a S3: sin x-api-key.
curl -sS -o pedimento-original.pdf "$URL"
```

</details>

<details>
<summary><b>Node.js 18+</b> · `fetch` nativo, sin dependencias</summary>

```javascript
import { writeFile } from "node:fs/promises";

// Devuelve una URL prefirmada, NO los bytes.
const meta = await (await fetch(`${API}/documents/${documentId}/file`, {
  headers: { "x-api-key": KEY },
})).json();

// La descarga va a S3: sin x-api-key.
const bytes = Buffer.from(await (await fetch(meta.downloadUrl)).arrayBuffer());
await writeFile("pedimento-original.pdf", bytes);
```

</details>

<details>
<summary><b>Python 3</b> · requiere `requests`</summary>

```python
# Devuelve una URL prefirmada, NO los bytes.
meta = requests.get(f"{API}/documents/{document_id}/file",
                    headers={"x-api-key": KEY}, timeout=30).json()

# La descarga va a S3: sin x-api-key, y en streaming por si pesa cientos de MB.
with requests.get(meta["downloadUrl"], stream=True, timeout=300) as resp:
    resp.raise_for_status()
    with open("pedimento-original.pdf", "wb") as fh:
        for chunk in resp.iter_content(1 << 20):
            fh.write(chunk)
```

</details>

<details>
<summary><b>C# (.NET 6+)</b> · top-level statements + implicit usings</summary>

```csharp
// Devuelve una URL prefirmada, NO los bytes.
var meta = await http.GetFromJsonAsync<JsonElement>(
    $"{api}/documents/{documentId}/file");

// La descarga va a S3: cliente sin x-api-key.
using var s3 = new HttpClient();
var url = meta.GetProperty("downloadUrl").GetString();
await File.WriteAllBytesAsync("pedimento-original.pdf",
    await s3.GetByteArrayAsync(url));
```

</details>


**Qué esperar en el paso 3.** El procesamiento **no es instantáneo** — un
pedimento de pocas páginas tarda segundos; un expediente grande, más. Mientras
corre, `GET /documents/{id}` puede responder `404` (el resultado aún no se
persiste). Trátalo como "todavía no", no como "no existe": haz *polling* cada
pocos segundos, no un solo intento.

**Los tres estados que te van a importar** (`validation.status`):

| Estado | Qué significa | Qué hacer |
|---|---|---|
| `APROBADO` | Todas las reglas del catálogo pasaron | Consumir los campos |
| `REQUIERE_REVISION` | Algo quedó dudoso: confianza baja, un campo no encontrado, o el documento se truncó | Revisión humana; mira `warnings` y los campos con `valid: false` |
| `RECHAZADO` | Una regla dura falló (p. ej. RFC del importador que no cuadra entre documentos) | No consumir como bueno; revisar `crossDocumentChecks` |

**Dónde están los campos.** En `expediente.documents[]`, un elemento por
documento detectado dentro del PDF. Para un pedimento declarado será uno solo:

```jsonc
{
  "documentId": "e18d014a-...",
  "validation": { "status": "APROBADO" },
  "expediente": {
    "documents": [
      {
        "documentType": "PEDIMENTO_ERP",
        "pageStart": 3, "pageEnd": 18,
        "status": "APPROVED",
        "extractedEntities": {
          "No_Pedimento": { "value": "260734356041248", "confidence": 0.98, "valid": true },
          "FACTURAS": { "items": [
            { "valid": true, "reason": null, "fields": {
                "PROVEEDOR_ID_FISCAL": { "value": "334274279", "confidence": 0.95, "valid": true },
                "PROVEEDOR_NOMBRE": { "value": "MNG DISTRIBUTOR LLC", "confidence": 0.95, "valid": true },
                "Factura": { "value": "4099", "confidence": 0.95, "valid": true },
                "Fecha_Factura": { "value": "2026-07-03", "confidence": 0.95, "valid": true },
                "Total_factura": { "value": 155.00, "confidence": 0.95, "valid": true }
            } }
          ] }
        }
      }
    ]
  }
}
```

### `confidence`: siempre un número, nunca `null`

Cada campo escalar trae un `confidence` entre 0 y 1 — **incluidos los
calculados** (`LIQUIDACION_NORMALIZADA.*`, campos `derived`) y los que el
documento no traía. Cómo se decide:

| Caso | `confidence` |
|---|---|
| Campo leído del documento | Lo que reportó el modelo × qué tan bien se leyó la página. |
| Campo calculado de varias celdas (un total del cuadro de liquidación) | El **promedio** de las celdas que sumó. |
| Fila de un array | La de **esa** fila, no la del array completo. |
| Campo que el documento no traía | `0.0` — "no lo sé" (con `value: null`; si era obligatorio, el `reason` lo dice). |

Y dos umbrales distintos, ambos por tipo en el catálogo:

- **`min_confidence`** (0.70 por default, 0.80 en `PEDIMENTO_ERP`) → el campo
  sale `valid: false` con `reason`, **pero conserva el valor** para que
  alguien lo revise.
- **`discard_below_confidence`** (0.50 por default) → el valor **no se
  entrega**: `value: null`, `valid: false` y
  `reason: "value discarded: confidence 0.31 below discard floor 0.5"`. La
  confianza baja se conserva: es la que explica por qué no hay valor. El
  documento cae a `REQUIERE_REVISION` (no a rechazo) por el check
  `discard_low_confidence`.

---

## 1. `POST /documents` — Registrar el documento y obtener la URL de subida

Sube un documento y dispara el pipeline de clasificación → extracción → validación.

**Son dos pasos, siempre, sin importar el tamaño del archivo:**

```
1. POST /documents          (solo metadata)   →  documentId + uploadUrl
2. PUT  {uploadUrl}      (el archivo)      →  directo a S3, arranca el pipeline
```

El archivo **nunca** pasa por API Gateway ni por el Lambda. Eso no es un detalle
de implementación: API Gateway topa un request en 10 MB y Lambda topa una
invocación síncrona en 6 MB, ambos límites duros de AWS y no configurables. Un
`PUT` a S3 acepta hasta 5 GB, así que un CFDI de 200 KB y un expediente de 300 MB
hacen exactamente lo mismo. No hay rama por tamaño que se pueda equivocar.

> **Nota de migración.** El modo *inline* (`contentBase64` en el body) fue
> **eliminado**. Un request que lo mande recibe `400 INLINE_UPLOAD_REMOVED`
> explicando el flujo nuevo, en vez de fallar en silencio: base64 infla el
> archivo ~33 %, así que reventaba alrededor de los ~4 MB reales y hacía que el
> **tamaño** decidiera qué camino corría.

### Paso 1 — `POST /documents` (no lleva el archivo)

El body va como `application/json` **o** `application/x-www-form-urlencoded`.

```jsonc
{
  "fileName": "pedimento.pdf",           // requerido; pdf|jpg|jpeg|png|xml|xlsx|csv
  "documentType": "PEDIMENTO_ERP",   // opcional (obligatorio en .xml/.xlsx/.csv)
  "metadata": { "lote": "A123" },        // opcional; se conserva con el documento
                                         // y luego se puede filtrar con
                                         // GET /documents?meta.lote=A123 (§5)
  "modelId": "amazon.nova-pro-v1:0"      // opcional; SOLO DEV — ver notas
}
```

**Respuesta `200`:**
```json
{
  "documentId": "e18d014a-...",
  "bucket": "documentvalidationstack-...",
  "key": "raw/PEDIMENTO_ERP/e18d014a-.../pedimento.pdf",
  "documentType": "PEDIMENTO_ERP",
  "classificationMode": "manual",
  "uploadUrl": "https://...s3.amazonaws.com/...",
  "uploadMethod": "PUT",
  "uploadExpiresInSeconds": 900,
  "uploadExpiresAt": "2026-07-27T13:33:19.000Z",
  "uploadedAt": "2026-07-27T13:18:19.000Z"
}
```

Guarda el `documentId`: es con lo que consultas el resultado después.

### Paso 2 — `PUT` del archivo a S3

Esta petición **no va a esta API**: va a S3, con la `uploadUrl` del paso 1.

```bash
curl -X PUT --upload-file pedimento.pdf \
  -H "x-amz-meta-app-origin: CEDIS" \
  "$UPLOAD_URL"
```

| | |
|---|---|
| **Método** | `PUT` (viene en `uploadMethod`). No `POST`, no multipart, no form-data |
| **Cuerpo** | Los bytes crudos del archivo, sin envolver ni codificar |
| **Cabeceras** | **Ninguna.** La firma y la metadata ya van en la URL (ver aviso abajo) |
| **La URL** | Úsala **completa y tal cual**. No separes parámetros. Al pegarla en shell, comillas **simples**: trae ~12 `&` en unos 1,900 caracteres |
| **Auth** | Ninguna. La firma va en la URL; **no** mandes `x-api-key` aquí |
| **Respuesta OK** | `200` con cuerpo vacío — S3 no devuelve JSON |
| **Vencimiento** | 15 min (`uploadExpiresAt`). Si expira, repite el paso 1: no se renuevan |
| **Tamaño** | Hasta 5 GB |

> ⚠️ **No mandes ninguna cabecera `x-amz-*` en el `PUT`.** Al prefirmar, SigV4
> *hoistea* esas cabeceras al **query string**, así que la metadata del
> documento (`model-id`, `app-origin`) ya viaja dentro de `uploadUrl` y está
> cubierta por la firma — fíjate en que la URL trae `X-Amz-SignedHeaders=host`,
> o sea que la única cabecera firmada es `host`.
>
> Si además las mandas como header, S3 rechaza la subida con
> **`403 AccessDenied: There were headers present in the request which were not
> signed`**, y lista las culpables en `<HeadersNotSigned>`. Un 403 que parece de
> permisos y en realidad es de firma.

No hace falta ninguna credencial de AWS: la URL prefirmada concede permiso
temporal para subir **ese** objeto y nada más. Expira en 15 minutos
(`uploadExpiresAt` trae el instante exacto). Sirve desde cualquier cliente HTTP
— Java, .NET, Python, SAP, Postman, curl.

En cuanto S3 recibe el objeto, el pipeline arranca solo (S3 → SQS → Step
Functions → Textract/Bedrock → DynamoDB). No hay que avisarle a nada más.

**`Content-Type` (opcional).** No está atado a la firma: puedes mandar el
header que quieras en el `PUT`, o ninguno. S3 lo guarda tal cual (no es una
cabecera `x-amz-*`, así que no exige que esté firmada). El pipeline no lo usa:
deduce el formato real de la extensión del archivo y nunca lee el Content-Type
guardado en S3.

### Body en `x-www-form-urlencoded`

Mismos campos, para clientes (SAP, stacks viejos de .NET/Java, un `curl -d` a
secas) que mandan formularios más fácil que JSON:

```bash
curl -X POST "$API/documents" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "x-api-key: $KEY" \
  -d "fileName=pedimento.pdf" \
  -d "documentType=PEDIMENTO_ERP"
```

Como todo valor de un formulario es texto, `metadata` va ahí como un objeto JSON
codificado en string; si no es JSON válido, la respuesta es `400 INVALID_METADATA`:

```bash
  -d 'metadata={"lote":"A123"}'
```

### Notas del upload

- Si mandas `documentType`, se salta la clasificación automática **y se HONRA tu tipo**: el documento se trata como ese tipo único y **no se re-clasifica ni se segmenta** en otro (p. ej. un PDF de varias páginas declarado `PEDIMENTO_ERP` se procesa como un solo pedimento, no como `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`). Si quieres que el sistema segmente/clasifique solo, **omite** `documentType`. Para forzar la segmentación multi-documento explícitamente, declara `documentType=EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`.
- **Archivos `.xml`/`.xlsx`/`.csv`:** para tipos con esquema XML (sufijo `_XML`) o tabular (sufijo `_XLSX`/`_CSV`) `documentType` es **obligatorio** — la API lo rechaza con `400 DOCUMENT_TYPE_REQUIRED` si falta, porque no existe clasificación automática por visión sobre datos ya estructurados. A cambio, el documento se extrae **determinísticamente** (XPath para XML, celda/columna para XLSX/CSV — sin llamar a Bedrock en ningún paso): más rápido y sin costo de modelo. Ver `catalogs/README.md` → "Tipos con fuente XML" / "Tipos con fuente tabular".
- **`modelId` (solo dev):** procesa **ese documento** con el modelo de Bedrock que indiques, sin redeploy — útil para comparar modelos sobre el mismo archivo. Aplica a todas las fases y gana sobre las variables `BEDROCK_*_MODEL_ID`; el resultado reporta en `tracking.modelIds` qué modelo corrió en cada fase. **El stack de prod lo ignora a propósito** (no define `ALLOW_MODEL_OVERRIDE`): el valor viene de quien sube el archivo, y en prod dejaría que un tercero eligiera el modelo —y con él el costo y la calidad de extracción— sobre documentos ajenos. Con `pnpm enviar`: `--model amazon.nova-pro-v1:0`.
- **Fallback automático de modelo (siempre activo, no es un flag de request):** si el modelo que le tocaba a una fase (`extractor`/`recovery`) falla porque no está disponible (no habilitado, no existe, error/timeout/cuota del proveedor — nunca por un request mal formado, eso propaga tal cual), el pipeline cambia solo al siguiente modelo de una lista ordenada y reintenta, hasta 4 veces por default. `tracking.modelIds` ya refleja el modelo que **realmente** corrió (no el configurado) cuando esto pasa; además, solo para las fases donde hubo cambio, `tracking.modelFallbacks` reporta el detalle: modelo esperado vs. el que finalmente corrió, cuántos reintentos tomó, y el error de cada intento fallido. Ver `documentacion/GUIA_DESPLIEGUE_Y_USO.md` §5.6.
- **`tracking.appOrigin` (trazabilidad):** cuando el request trae un `x-api-key` de una app registrada con `pnpm apps:register` (ver `documentacion/AMBIENTES_Y_SEGURIDAD.md`), el resultado del documento incluye `tracking.appOrigin` con el nombre de esa app — buscable después vía `GET /documents?system=<app>` (§5). Sale de la **key**, no del body: un `metadata: {"app-origin": "..."}` se descarta, así que ninguna app puede declararse otra. Una key válida pero nunca registrada (p. ej. `idp-default` sin `pnpm apps:adopt`) reporta `appOrigin: "unregistered"`; sin key en absoluto (dev con `DEV_API_KEY_REQUIRED` apagado), el campo simplemente no aparece dentro de `tracking`.
- **`metadata` (contexto tuyo, editable y filtrable):** lo que mandes en `metadata` viaja con el documento hasta su resultado (queda en `metadata` del envelope y en la columna `Metadata` del item) y se puede filtrar con `GET /documents?meta.<clave>=<valor>` (§5). Después de procesarlo puede modificarse con `PATCH /documents/{id}/metadata` (§3), incluso para agregar metadata a documentos antiguos. Las claves se normalizan a minúsculas y deben cumplir `[a-z0-9][a-z0-9._-]*`; los valores son planos, US-ASCII y el mapa completo tiene 2 KB de tope. Lo que no cumpla se rechaza con `400 INVALID_METADATA`. `app-origin` y `model-id` son claves reservadas del sistema.
- **Tope de páginas (pipeline de expedientes/pedimentos):** depende de si el tipo declarado **selecciona sus propias páginas** (`page_selection` en su catálogo). Si las selecciona, el documento **no se trunca**: el costo ya lo acota el selector, que solo manda al modelo las páginas con los campos del tipo, y solo aplica un tope muy superior (`max_source_pages`, 500 por defecto) como salvaguarda contra un archivo capaz de agotar la Lambda. Si **no** las selecciona —tipo sin `page_selection`, con `enabled: false`, `PAGE_SELECTION_ENABLED=false`, o una subida **sin tipo declarado** (no se sabe qué selector aplica hasta clasificar, y clasificar exige leer las páginas)— un PDF de más de 50 páginas (`MAX_PAGES`, configurable) se **trunca a las primeras 50** antes de correr Textract/Bedrock, para acotar el costo de un documento inesperadamente grande. El PDF original completo **no se toca** — sigue intacto en S3 — pero el resultado solo cubre las páginas procesadas, queda marcado `reviewRequired: true` con un warning explícito, y un campo `truncated: {originalPages, processedPages}`. Si necesitas las páginas restantes, hay que reprocesar manualmente.
- El resultado final se publica en **EventBridge** (broker `idp-broker`, source `document-validation-pipeline`) y se persiste (ver GET abajo).

**Ejemplo (atajo del repo, siempre mediante URL prefirmada):**
```bash
pnpm enviar pedimento.pdf PEDIMENTO_ERP              # dev por default
pnpm enviar pedimento.pdf PEDIMENTO_ERP --env prod   # API key desde .env.production
```

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
    // Resumen de procedencia/procesamiento — presente en TODO resultado
    // (flujo estándar y expediente por igual). createdAt siempre está;
    // modelIds/appOrigin/modelFallbacks solo si hay algo que reportar.
    "tracking": {
      "createdAt": "2026-07-27T13:19:04.231Z",
      // Modelo que REALMENTE corrió por fase — ya refleja cualquier fallback
      // (aquí "extractor" terminó en minimax porque haiku, el configurado,
      // falló — ver modelFallbacks abajo para el detalle de ese cambio).
      "modelIds": { "extractor": "minimax.minimax-m2.5" },
      "appOrigin": "CEDIS",                 // opcional; app que subió el documento (ver nota abajo) — ausente si no hay api key o no está registrada
      // Ausente salvo que ALGUNA fase haya cambiado de modelo. Solo las
      // fases que cambiaron aparecen aquí (comparar con modelIds arriba).
      "modelFallbacks": {
        "extractor": {
          "expectedModel": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
          "actualModel": "minimax.minimax-m2.5",
          "retries": 1,
          "attempts": [
            { "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "error": "AccessDeniedException" }
          ]
        }
      }
    },
    "extractedEntities": { "...": "..." },  // escalares del documento principal
    "validation": { "status": "REQUIERE_REVISION", "summary": "expediente: REVIEW_REQUIRED (17 documents)" },
    "expediente": {
      "status": "REVIEW_REQUIRED",
      "sourceFormat": "pdf",                // "pdf" | "jpg"/"jpeg"/"png" | "xml" | "xlsx" | "csv" — para xml/xlsx/csv, "pages" solo trae totalPages (no hay OCR/páginas nativas que reportar); jpg/jpeg/png sí reportan pages normal (siempre 1 página, source "textract")
      "processingMs": 45231.7,              // igual que processingSeconds pero con precisión de milisegundo (útil para xml, que suele redondear a 0.0s)
      "pages": { "totalPages": 27, "...": 0 },
      "documents": [                        // TODOS los documentos del expediente
        {
          "documentType": "PEDIMENTO_ERP",
          "status": "PARTIALLY_APPROVED",
          "extractedEntities": { "No_Pedimento": "260708236001187", "...": "..." },
          "arrayCounts": { "PARTIDAS": 1, "CONTRIBUCIONES": 2 },
          "liquidacionNormalizada": { "IVA": 53, "PRV": 330, "TOTAL": 383, "...": 0 }
          // si SOLO este documento falló (el resto del expediente sigue): "status": "PROCESSING_ERROR",
          // "error": "mensaje truncado a 300 caracteres", "errorType": "ValueError" (nombre de la excepción Python)
        }
        // ... el resto de documentos
      ],
      "crossDocumentChecks": [ ... ],
      // Detalle de procedencia DE ESTE EXPEDIENTE específicamente (qué schema/
      // versiones de tipo lo procesaron, su propia ventana de tiempo) —
      // complementa, no reemplaza, el resumen del `tracking` de nivel superior.
      "tracking": { "createdAt": "...", "processingStartedAt": "...", "processingCompletedAt": "...",
                     "processingSeconds": 45.23, "processingMs": 45231.7,
                     "schema": { "pipelineVersion": "1.0.0", "typeVersions": { "PEDIMENTO_ERP": "1.0.0" } } }
    },
    "resultLocation": { "bucket": "...", "key": "results/{id}.json" }
  }
}
```

**Cuando falla el documento COMPLETO** (no se pudo ni leer el archivo — `documents: []`), el `expediente` trae además un bloque `error` estructurado para triage sin tener que parsear el mensaje de `warnings`:

```jsonc
{
  "status": "PROCESSING_ERROR",
  "reviewRequired": true,
  "documents": [],
  "sourceFormat": "jpeg",
  "warnings": ["unreadable jpeg image: ..."],       // mensaje libre, siempre presente (compatibilidad hacia atrás)
  "error": {
    "message": "unreadable jpeg image: ...",
    "stage": "page_load",                            // "page_load" | "parse" | "declared_type" — dónde falló
    "type": "ClientError",                            // nombre de la clase de excepción Python
    "sourceFormat": "jpeg"
  }
}
```

Query params:
- `full=1` → agrega **`fullResult`**: el detalle más profundo desde S3 (`documents[].fields[]` con `extractedValue`/`normalizedValue`/`confidence`/`status`/`checks`, más `value`/`valid`/`reason` (vista simple: valor resuelto, booleano y motivo si `valid` es falso)). El `result` del envelope trae las entidades y `liquidacionNormalizada`, pero el detalle por-campo solo está en `fullResult`.

**Errores:** `404 NOT_FOUND` si no hay documento con ese id/prefijo, **o si existe pero la app que llama no tiene permiso de verlo** (ver "Seguridad — acceso por app" más abajo) — misma respuesta en ambos casos, a propósito: no confirma que un documento fuera de tu alcance exista.

**Ejemplo:**
```bash
curl -sS "$API/documents/PEDIMENTO-ADUANAL-4ff224be" | python3 -m json.tool
curl -sS "$API/documents/PEDIMENTO-ADUANAL-4ff224be?full=1"   # + detalle por campo de S3
```
Atajo del repo (equivalente, formateado): `pnpm resultado -- --id 4ff224be`.

---

## 3. `PATCH /documents/{id}/metadata` — Estados y marcas propios de la app

Aplica un **JSON Merge Patch plano** sobre la metadata mutable del documento.
Está pensado para seguimiento externo: `estado_externo`, `folio_erp`,
`requiere_atencion`, `fecha_envio`, etc. Esos valores pertenecen a la app que
integra; el validador no les asigna significado ni limita su vocabulario.

Esto es deliberadamente distinto de los estados canónicos del sistema:
`validation.status` (`APROBADO`, `RECHAZADO`, `REQUIERE_REVISION`) y
`workflowStatus` no cambian con este endpoint. Tampoco vuelve a publicar el
evento de validación ni reescribe el resultado histórico guardado en S3.

Semántica:

- valor string/número/booleano → crea o reemplaza la clave;
- `null` o `""` → borra esa clave;
- clave ausente → conserva el valor anterior;
- el body debe ser un objeto plano; claves `[a-z0-9][a-z0-9._-]*`, valores
  US-ASCII, máximo 2 KB para **todo el mapa resultante**;
- `app-origin` y `model-id` son reservadas y no se pueden modificar.

```bash
curl -sS -X PATCH "$API/documents/$DOC_ID/metadata" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/merge-patch+json" \
  -d '{
    "estado_externo": "EN_REVISION",
    "folio_erp": "4711",
    "marca_anterior": null
  }' | jq
```

Respuesta `200` (devuelve el mapa completo, no solo las claves del patch):

```json
{
  "documentId": "PEDIMENTO-ADUANAL-4ff224be-...",
  "metadata": {
    "lote": "A123",
    "estado_externo": "EN_REVISION",
    "folio_erp": "4711"
  },
  "metadataVersion": 3,
  "updatedAt": "2026-09-01T18:21:04.231Z",
  "updatedBy": "ERP"
}
```

La nueva metadata se refleja inmediatamente en `GET /documents/{id}`, en
`GET /documents?...`, en `summary=1`, en `full=1` y en los filtros
`meta.<clave>`. El JSON histórico de S3 conserva lo que existía al terminar el
procesamiento; la API superpone la columna `Metadata`, que es la fuente actual.

Seguridad: una app registrada necesita `AllowMetadataWrite=true` y sigue
limitada por `AccessScope`/`AllowedDocTypes`. El permiso se otorga o revoca sin
cambiar la API key:

```bash
pnpm apps:grant -- --name ERP --env prod --metadata-write
pnpm apps:deny  -- --name ERP --env prod --metadata-write
```

Errores: `400 INVALID_JSON|INVALID_METADATA|EMPTY_PATCH`, `403 FORBIDDEN` si
falta el permiso, `404 NOT_FOUND` si no existe o queda fuera del alcance de la
app y `409 METADATA_CONFLICT` si hubo demasiadas escrituras simultáneas (se
puede reintentar exactamente el mismo `PATCH`). Las actualizaciones usan una
versión optimista y reintentos internos para no perder claves distintas cuando
dos integraciones escriben al mismo tiempo.

---

## 4. `GET /documents/{id}/file` — Descargar el archivo original

Devuelve una **URL prefirmada de S3** (no los bytes) para descargar el archivo tal cual se subió (el PDF/imagen/XML/XLSX/CSV original, no el resultado de la extracción). `{id}` acepta id completo o prefijo, igual que `GET /documents/{id}`.

**Por qué una URL prefirmada y no los bytes directamente:** los expedientes de este sistema llegan a decenas de MB (un expediente digital real de referencia tiene 86 páginas) y **API Gateway topa cualquier respuesta a 10 MB** — límite duro de AWS, no configurable — sin contar el límite de invocación síncrona de Lambda (6 MB). Proxyear el archivo por la Lambda simplemente no es viable para el rango de tamaños real de este sistema. En vez de eso, el endpoint firma una URL de `GetObjectCommand` (mismo mecanismo `@aws-sdk/s3-request-presigner` que ya usa `POST /documents` en modo prefirmado — ver §1b) y el cliente descarga **directo de S3**, sin pasar por Lambda ni por API Gateway. Sin tope de tamaño real.

**Cómo se localiza el archivo:** el key de S3 del original **no es estático** — mientras se procesa vive en `raw/{TIPO?}/{documentId}/{archivo}`, pero al terminar la ejecución el blueprint lo mueve a `processed/{documentId}/{archivo}` (caso normal, incluida una validación RECHAZADA) o a `failed/{documentId}/{archivo}` (solo si la propia ejecución de Step Functions revienta). Como este endpoint solo responde para documentos que `GET /documents/{id}` ya puede resolver (es decir, ya salieron de `raw/`), el handler busca primero en `processed/{documentId}/` y si no hay nada, en `failed/{documentId}/` — un `ListObjectsV2` de una sola key por prefijo, no una reconstrucción a ciegas del nombre de archivo.

**Respuesta `200`:**
```json
{
  "documentId": "PEDIMENTO-ADUANAL-4ff224be-...",
  "fileName": "expediente.pdf",
  "fileSizeBytes": 8345213,
  "location": "processed",
  "downloadUrl": "https://documentvalidationstack-....s3.amazonaws.com/processed/...",
  "downloadMethod": "GET",
  "expiresInSeconds": 900,
  "expiresAt": "2026-08-05T13:33:19.000Z"
}
```

**Query params:**
| Param | Default | Descripción |
|---|---|---|
| `expiresIn` | `900` (15 min) | Segundos de validez de la URL. Se acota entre `60` y `3600` (1h) — un valor fuera de rango se recorta, no da error. |

**Errores:**
- `404 NOT_FOUND` — no hay documento con ese id/prefijo, **o la app que llama no tiene permiso de verlo** (igual que `GET /documents/{id}` — ver "Seguridad — acceso por app" abajo).
- `404 FILE_NOT_FOUND` — el documento existe (y es visible para esta app) pero no se encontró el objeto original en `processed/`/`failed/` (documento aún en `PROCESSING`, o el objeto se borró de S3 por fuera del sistema).

**Ejemplo:**
```bash
curl -sS "$API/documents/PEDIMENTO-ADUANAL-4ff224be/file"
# → { "downloadUrl": "https://...", ... }
curl -sS -o expediente.pdf "$(curl -sS "$API/documents/PEDIMENTO-ADUANAL-4ff224be/file" | python3 -c 'import sys,json;print(json.load(sys.stdin)["downloadUrl"])')"
```

### ⚠️ Implicaciones de seguridad — leer antes de exponer este endpoint a un tercero

Este endpoint expone **bytes del archivo original** (posible PII: fotos de identificación, RFC/CURP, montos, direcciones), no solo el JSON de resultado — así que el modelo de autorización de este API pesa más aquí que en cualquier otro endpoint. Dos puntos concretos:

1. **La `x-api-key` SÍ filtra por dueño del documento, por default.** `query-lambda` resuelve la app que llama (por su `x-api-key`, contra `document-validation-apps[-dev]`) y, salvo que esa app tenga `AccessScope=all` configurado explícitamente, solo puede ver/descargar documentos cuyo `tracking.appOrigin` sea ella misma — cualquier otro id existe pero responde `404` (ver "Seguridad — acceso por app" en la sección 5, más abajo, para el detalle completo del modelo). Esto es el default de **cualquier app registrada desde ahora** (`pnpm apps:register`); dos casos quedan sin restringir a propósito — sin `x-api-key` (dev abierto) y una key válida nunca registrada aquí (`idp-default` antes de `apps:adopt`, el caso "sistema preexistente") — ver `AMBIENTES_Y_SEGURIDAD.md`. Si necesitas que una app vea todo (ej. un portal admin interno), es explícito: `pnpm apps:reconfigure -- --name X --access-scope all`.
2. **La URL prefirmada es en sí misma una credencial de acceso**, independiente de la `x-api-key` que la generó: cualquiera que la obtenga (log de proxy, historial de navegador, un `Referer` filtrado, copiar-pegar a un chat) puede descargar el archivo **sin volver a pasar por autenticación** hasta que expira. Por eso el default es corto (15 min, tope 1h) — no subir `expiresIn` más de lo necesario, y tratar la URL resultante como un secreto de vida corta, no como un link para reenviar. Esto es independiente del punto 1: la restricción por app decide **quién puede pedir** la URL; una vez emitida, cualquiera que la tenga puede usarla hasta que expire.

**Tamaño de archivo — resumen:** sin límite real. La única cota que aplica es la del **upload** original (§1): ~4 MB si se subió inline, sin tope práctico si se subió vía URL prefirmada (S3 acepta objetos hasta 5 TB, `PUT` de una sola parte hasta 5 GB). La descarga hereda ese mismo tamaño sin ningún límite adicional propio, porque nunca pasa por Lambda/API Gateway.

---

## 5. `GET /documents` — Buscar por tipo, sistema, metadata y campos extraídos

Cuatro filtros, **todos combinables entre sí y todos en AND**: un documento
tiene que cumplirlos todos para salir en `matches`. Basta con mandar uno.

| Filtro | Qué acota | De dónde sale el dato |
|---|---|---|
| **`docType`** | El **tipo de documento** | Columna indexada `DocumentType`. Se resuelve **dentro de DynamoDB** contra el GSI `DocumentTypeIndex` (partición `DocumentType`, orden `ProcessedAt`): solo se leen los documentos de ese tipo, **sin Scan**. |
| **`system`** | Qué **sistema lo subió** | `tracking.appOrigin`, que sale de la **API key** con la que se hizo el `POST /documents` — no de nada que mande el cliente en el body. Alias: `appOrigin`, `sistema`. |
| **`meta.<clave>`** | La **metadata actual** del documento | El objeto del `POST /documents` (§1), incluyendo cambios posteriores hechos por `PATCH /documents/{id}/metadata` (§3), sin interpretarlo. |
| **`field` + `value`** | Cualquier **campo extraído** del documento | El envelope del resultado, por dot-path o por búsqueda profunda. **Se puede repetir** para exigir varios campos a la vez. |

Solo `docType` se filtra dentro de DynamoDB; los otros tres se aplican en la
Lambda sobre los documentos que ese primer filtro dejó pasar. Por eso **manda
`docType` siempre que puedas**: es lo que evita el Scan.

### Los cuatro juntos

La consulta típica de una integración — "el pedimento que **mi ERP** subió para
el **lote A123** de la **sucursal CHIH**, con este RFC de importador":

```bash
curl -sS -G "$API/documents" -H "x-api-key: $KEY" \
  --data-urlencode "docType=PEDIMENTO_ERP" \
  --data-urlencode "system=ERP" \
  --data-urlencode "meta.lote=A123" \
  --data-urlencode "meta.sucursal=CHIH" \
  --data-urlencode "field=PROVEEDOR_ID_FISCAL" \
  --data-urlencode "value=OFU910626UQ0" \
  --data-urlencode "summary=1" \
  | jq '{lookup, count, ids: [.matches[].documentId]}'
```

> `-G` + `--data-urlencode` en vez de pegar el query string a mano: encodea los
> espacios y los acentos, y evita que el shell parta la URL en el primer `&`.

**Reglas de combinación**, para que no haya que adivinarlas:

- **Entre filtros distintos → AND.** `system=ERP&meta.lote=A123` = del ERP **y**
  del lote A123.
- **Repetir el mismo filtro → OR.** `system=ERP,PORTAL` (o `system=ERP&system=PORTAL`)
  = de cualquiera de los dos. Igual `meta.lote=A123,B456` y `docType=FACTURA,PEDIMENTO_ERP`.
- **`field`/`value` repetidos se emparejan por posición** en la URL, y son AND
  entre sí: `?field=A&value=1&field=B&value=2` = el campo A vale 1 **y** el
  campo B vale 2. Si el número de `field` y de `value` no coincide → `400`, no
  una pareja inventada.
- **Todo compara en texto**, sin distinguir mayúsculas y recortando espacios.
  `op=contains` cambia los `field`/`value` a subcadena; `metaOp=contains` hace
  lo mismo con **todos** los `meta.<clave>`.
- Un documento **sin** `metadata` (o sin `appOrigin`) nunca matchea un filtro
  `meta.*`/`system` — no hay comodín.

### Metadata: qué se puede buscar

`meta.<clave>` busca sobre lo que se mandó en `metadata` al registrar el
documento (§1) y que viaja con él hasta el resultado. Dos cosas que conviene
saber **antes** de diseñar las claves:

- **Las claves van en minúsculas.** S3 las guarda así, y la API las baja a
  minúsculas en los dos extremos: `metadata: {"Lote": "A123"}` se busca igual
  con `meta.lote=` que con `meta.Lote=`.
- **Los valores son texto plano ASCII, sin anidar.** `{"lote": "A123"}` sí;
  `{"lote": {"id": "A123"}}` y `{"sucursal": "Chihuahua ñ"}` los rechaza el
  `POST /documents` con `400 INVALID_METADATA` (§1), no fallan luego en
  silencio.
- **Solo los documentos subidos con esa metadata la tienen.** No es
  reconstruible hacia atrás: la metadata de un documento ya procesado no
  existe en ningún índice, así que `meta.*` nunca lo va a encontrar. Es un
  filtro para lo que sube de ahora en adelante.

### Sistema: quién lo subió, no quién lo dice

`system=` filtra por `tracking.appOrigin`, que la API resuelve **de la API key
del que hizo el `POST`**, no de un campo del body. Una app no puede declararse
otra: si mandas `metadata: {"app-origin": "OTRA_APP"}` esa clave se descarta.

Ojo con la interacción con los permisos por app (ver "Seguridad" abajo): una
app con `AccessScope: "own"` **ya** está limitada a sus propios documentos, así
que ahí `system=` solo puede acotar más, nunca ver de más. El filtro es útil
sobre todo para una app con `AccessScope: "all"` (una consola interna, un
reporte) que quiere mirar solo lo que entró por un canal.

### Camino rápido y campos extraídos

**Camino rápido (unique key).** Si el campo que buscas es el que su tipo declara como **unique key** en el catálogo (`unique_key`, ver `catalogs/README.md` → "Unique keys"), la búsqueda **no recorre el tipo**: se resuelve con **una sola Query** sobre la clave `TIPODOC#<valor normalizado>` en la tabla de lookup `document-validation-keys[-dev]`, sin importar cuántos documentos de ese tipo existan. Hoy `PEDIMENTO_ERP` lo declara en `No_Pedimento`:

```bash
# Una Query. Da igual si mandas el número con espacios o sin ellos:
# la normalización (`digits` en este tipo) es la misma al escribir y al buscar.
curl -sS "$API/documents?docType=PEDIMENTO_ERP&field=No_Pedimento&value=26%2007%200823%206001630"
```

Condiciones: hace falta `docType` (la clave siempre lleva el tipo como prefijo — es lo que la hace única) y `op=eq`. Si no se cumplen, o si el valor todavía no está indexado, la búsqueda **cae al camino normal y encuentra el documento igual** — más lenta, nunca equivocada. El campo `lookup` de la respuesta dice cuál contestó.

#### Ejemplos en cuatro lenguajes

Todos asumen `$API`/`$KEY` (y en C#/Node el cliente `http` del paso 1).

<details open>
<summary><b>curl</b></summary>

```bash
# --- Los cuatro filtros a la vez, todos en AND: tipo + sistema que lo subió
#     + metadata del upload + un campo extraído del documento.
#     -G/--data-urlencode encodea espacios y acentos, y evita que el shell
#     parta la URL en el primer `&`.
curl -sS -G "$API/documents" -H "x-api-key: $KEY" \
  --data-urlencode "docType=PEDIMENTO_ERP" \
  --data-urlencode "system=ERP" \
  --data-urlencode "meta.lote=A123" \
  --data-urlencode "meta.sucursal=CHIH" \
  --data-urlencode "field=PROVEEDOR_ID_FISCAL" \
  --data-urlencode "value=OFU910626UQ0" \
  --data-urlencode "summary=1" \
  | jq '{lookup, count, ids: [.matches[].documentId]}'

# --- Camino rápido: campo declarado `unique_key` (una sola Query).
#     Da igual con espacios o sin ellos: la normalización es la misma al
#     escribir y al buscar (`digits` en este tipo).
curl -sS -H "x-api-key: $KEY" \
  "$API/documents?docType=PEDIMENTO_ERP&field=No_Pedimento&value=26%2007%200823%206001630" \
  | jq '{lookup, count, ids: [.matches[].documentId]}'

# --- Cualquier otro campo: basta el NOMBRE, no el dot-path completo.
#     Si el path no resuelve en la raíz, busca en profundidad por esa clave.
curl -sS -H "x-api-key: $KEY" \
  "$API/documents?docType=PEDIMENTO_ERP&field=PROVEEDOR_ID_FISCAL&value=OFU910626UQ0&summary=1" \
  | jq '.matches[] | {documentId, matchedValue, matchedAt}'

# --- Dos campos extraídos a la vez: se emparejan POR POSICIÓN y son AND.
#     `matched` en cada resultado dice qué coincidió y dónde, por filtro.
curl -sS -G "$API/documents" -H "x-api-key: $KEY" \
  --data-urlencode "docType=PEDIMENTO_ERP" \
  --data-urlencode "field=PROVEEDOR_ID_FISCAL" --data-urlencode "value=OFU910626UQ0" \
  --data-urlencode "field=PATENTE"                   --data-urlencode "value=3456" \
  | jq '.matches[] | {documentId, matched}'

# --- Repetir un filtro es OR; filtros distintos siguen siendo AND.
#     Esto es "(ERP o PORTAL) y (sucursal CHIH o JRZ)".
curl -sS -G "$API/documents" -H "x-api-key: $KEY" \
  --data-urlencode "docType=PEDIMENTO_ERP" \
  --data-urlencode "system=ERP,PORTAL" \
  --data-urlencode "meta.sucursal=CHIH,JRZ" \
  --data-urlencode "summary=1"

# --- Subcadena, y acotando por fecha y estado. `op` afecta a field/value;
#     `metaOp` hace lo mismo con todos los meta.<clave>.
curl -sS -H "x-api-key: $KEY" \
  "$API/documents?docType=PEDIMENTO_ERP&field=PROVEEDOR_NOMBRE&value=GAVANTI&op=contains&status=APROBADO&since=2026-07-01&limit=50"
```

</details>

<details>
<summary><b>Node.js 18+</b> · `fetch` nativo</summary>

```javascript
// Manda SIEMPRE docType: acota la búsqueda dentro de DynamoDB. Sin él, la
// consulta cae a un Scan de toda la tabla.
// Una LISTA de pares, no un objeto: `field`/`value` se pueden repetir y
// URLSearchParams los manda en ese mismo orden, que es como se emparejan.
async function buscar(pares) {
  const qs = new URLSearchParams(pares);            // encodea espacios y acentos
  const r = await fetch(`${API}/documents?${qs}`, {
    headers: { "x-api-key": KEY },
  });
  if (!r.ok) throw new Error(`GET /documents → ${r.status}: ${await r.text()}`);
  return r.json();
}

// Los cuatro filtros a la vez, todos en AND: tipo + sistema que lo subió +
// metadata del upload + un campo extraído del documento.
const delLote = await buscar([
  ["docType", "PEDIMENTO_ERP"],
  ["system", "ERP"],                                // quién lo subió, por su API key
  ["meta.lote", "A123"],                            // metadata del POST /documents
  ["meta.sucursal", "CHIH"],
  ["field", "PROVEEDOR_ID_FISCAL"], ["value", "OFU910626UQ0"],
  ["summary", "1"],
]);
for (const m of delLote.matches)
  console.log(m.documentId, m.appOrigin, m.metadata, m.matched);

// Camino rápido: campo declarado `unique_key` → una sola Query.
// El valor se normaliza igual al escribir y al buscar, así que da lo mismo
// mandarlo con espacios ("26 07 0823 6001630") o sin ellos.
const porNumero = await buscar([
  ["docType", "PEDIMENTO_ERP"],
  ["field", "No_Pedimento"], ["value", "26 07 0823 6001630"],
]);
console.log(porNumero.lookup, porNumero.count);     // "uniqueKey" 1

// Dos campos extraídos a la vez: se emparejan POR POSICIÓN y son AND.
const porRfcYPatente = await buscar([
  ["docType", "PEDIMENTO_ERP"],
  ["field", "PROVEEDOR_ID_FISCAL"], ["value", "OFU910626UQ0"],
  ["field", "PATENTE"], ["value", "3456"],
]);
for (const m of porRfcYPatente.matches)
  for (const hit of m.matched)
    console.log(m.documentId, hit.field, hit.matchedValue, "←", hit.matchedAt);
```

</details>

<details>
<summary><b>Python 3</b> · `requests`</summary>

```python
# Manda SIEMPRE docType: acota la búsqueda dentro de DynamoDB. Sin él, la
# consulta cae a un Scan de toda la tabla.
# Una LISTA de pares, no un dict: `field`/`value` se pueden repetir y requests
# los manda en ese mismo orden, que es como se emparejan.
def buscar(pares):
    r = requests.get(f"{API}/documents", params=pares,   # requests encodea solo
                     headers={"x-api-key": KEY}, timeout=30)
    r.raise_for_status()
    return r.json()

# Los cuatro filtros a la vez, todos en AND: tipo + sistema que lo subió +
# metadata del upload + un campo extraído del documento.
del_lote = buscar([
    ("docType", "PEDIMENTO_ERP"),
    ("system", "ERP"),                               # quién lo subió, por su API key
    ("meta.lote", "A123"),                           # metadata del POST /documents
    ("meta.sucursal", "CHIH"),
    ("field", "PROVEEDOR_ID_FISCAL"), ("value", "OFU910626UQ0"),
    ("summary", "1"),
])
for m in del_lote["matches"]:
    print(m["documentId"], m.get("appOrigin"), m.get("metadata"), m["matched"])

# Camino rápido: campo declarado `unique_key` → una sola Query.
# El valor se normaliza igual al escribir y al buscar, así que da lo mismo
# mandarlo con espacios ("26 07 0823 6001630") o sin ellos.
por_numero = buscar([("docType", "PEDIMENTO_ERP"),
                     ("field", "No_Pedimento"),
                     ("value", "26 07 0823 6001630")])
print(por_numero["lookup"], por_numero["count"])      # "uniqueKey" 1

# Dos campos extraídos a la vez: se emparejan POR POSICIÓN y son AND.
por_rfc_y_patente = buscar([
    ("docType", "PEDIMENTO_ERP"),
    ("field", "PROVEEDOR_ID_FISCAL"), ("value", "OFU910626UQ0"),
    ("field", "PATENTE"), ("value", "3456"),
])
for m in por_rfc_y_patente["matches"]:
    for hit in m["matched"]:
        print(m["documentId"], hit["field"], hit["matchedValue"], "<-", hit["matchedAt"])
```

</details>

<details>
<summary><b>C# / .NET 6+</b> · `HttpClient`</summary>

```csharp
using System.Web;

// Manda SIEMPRE docType: acota la búsqueda dentro de DynamoDB. Sin él, la
// consulta cae a un Scan de toda la tabla.
// `Add` (no el indexador `qs[k] = v`): repite la clave en vez de pisarla, que
// es lo que hace falta para mandar varios field/value — se emparejan por
// posición — o varios valores del mismo filtro.
async Task<JsonElement> BuscarAsync(params (string k, string v)[] parametros)
{
    var qs = HttpUtility.ParseQueryString(string.Empty);
    foreach (var (k, v) in parametros) qs.Add(k, v);
    return await http.GetFromJsonAsync<JsonElement>($"{api}/documents?{qs}");
}

// Los cuatro filtros a la vez, todos en AND: tipo + sistema que lo subió +
// metadata del upload + un campo extraído del documento.
var delLote = await BuscarAsync(
    ("docType", "PEDIMENTO_ERP"),
    ("system", "ERP"),                                 // quién lo subió, por su API key
    ("meta.lote", "A123"),                             // metadata del POST /documents
    ("meta.sucursal", "CHIH"),
    ("field", "PROVEEDOR_ID_FISCAL"), ("value", "OFU910626UQ0"),
    ("summary", "1"));
foreach (var m in delLote.GetProperty("matches").EnumerateArray())
{
    Console.WriteLine(m.GetProperty("documentId"));
    foreach (var hit in m.GetProperty("matched").EnumerateArray())
        Console.WriteLine($"  {hit.GetProperty("field")} = {hit.GetProperty("matchedValue")}"
                          + $" <- {hit.GetProperty("matchedAt")}");
}

// Camino rápido: campo declarado `unique_key` → una sola Query.
// El valor se normaliza igual al escribir y al buscar, así que da lo mismo
// mandarlo con espacios ("26 07 0823 6001630") o sin ellos.
var porNumero = await BuscarAsync(
    ("docType", "PEDIMENTO_ERP"),
    ("field", "No_Pedimento"),
    ("value", "26 07 0823 6001630"));
Console.WriteLine($"{porNumero.GetProperty("lookup")} {porNumero.GetProperty("count")}");
```

</details>

**Query params:**
| Param | Requerido | Default | Descripción |
|---|---|---|---|
| `docType` | sí\* | — | Tipo de documento. Acepta el **TYPE_ID** (`PEDIMENTO_ERP`) o su nombre (`Pedimento (mapeo ERP)`): se normaliza sin distinguir acentos, mayúsculas ni `_` y se resuelve contra el catálogo. Varios tipos separados por coma. Alias del propio param: `documentType`, `tipo`. |
| `system` | sí\* | — | Qué **sistema** subió el documento: se compara contra `tracking.appOrigin`, que sale de la **API key** del `POST /documents`. Varios separados por coma o repitiendo el param (OR). Alias: `appOrigin`, `sistema`. |
| `meta.<clave>` | sí\* | — | **Familia de params**, no uno literal: `meta.lote=A123`, `meta.sucursal=CHIH`... Filtra por la `metadata` del `POST /documents` (§1). Claves en minúsculas. Claves distintas = AND; repetir una clave o separar por coma = OR. |
| `metaOp` | no | `eq` | Cómo comparan **todos** los `meta.<clave>`: `eq` o `contains`. Independiente de `op`. |
| `field` | sí\* | — | Dot-path del campo, p. ej. `extractedEntities.No_Pedimento`. Si no resuelve en la raíz, hace **búsqueda profunda** por la última clave del path (encuentra el campo aunque esté anidado en cualquier documento, **incluido dentro de una fila de un array** — `field=Factura&value=4099` encuentra la factura sin importar en qué renglón de `FACTURAS` esté). **Se puede repetir** para exigir varios campos (AND). |
| `value` | con `field` | — | Valor a comparar (case-insensitive, se recortan espacios). Uno por cada `field`, **en el mismo orden**: se emparejan por posición. |
| `op` | no | `eq` | `eq` (igualdad exacta) o `contains` (subcadena) para los `field`/`value`. Una vez (aplica a todos) o una vez por filtro. |
| `status` | no | — | Filtra por estado de validación: `APROBADO`, `RECHAZADO`, `REQUIERE_REVISION`. Se aplica en DynamoDB (solo con `docType`). |
| `since` / `until` | no | — | Rango de `processedAt` (ISO-8601, p. ej. `2026-07-01`). Se aplica como condición de la clave de orden del índice (solo con `docType`). |
| `order` | no | `desc` | `desc` = más reciente primero, `asc` = más antiguo primero (solo con `docType`). |
| `expand` | no | `true` | `0`/`false` desactiva la búsqueda dentro de expedientes (ver abajo). |
| `limit` | no | `25` | Máximo de resultados (tope `100`). |
| `summary` | no | `false` | `1`/`true` devuelve solo un **resumen ligero** por match (id, clasificación, status, escalares, arrayCounts) en vez del envelope completo. Útil para listar. |
| `full` | no | `false` | `1`/`true` agrega `fullResult` por match: el detalle **más profundo** desde S3 (`fields[]` con extractedValue/normalizedValue/confidence/status/checks, más value/valid/reason (vista simple), contenido completo de arrays). Cuesta un GET de S3 por match. |

\* Se requiere **al menos uno** de `docType`, `system`, `meta.<clave>` o `field`+`value`. Sin ninguno: `400 MISSING_PARAMS`.

**Errores de forma de los filtros:**

| Qué mandaste | Respuesta |
|---|---|
| Ningún filtro | `400 MISSING_PARAMS` |
| `field` sin `value` | `400 MISSING_PARAMS` — `'field' requires 'value'` |
| Distinto número de `field` y de `value` | `400 MISSING_PARAMS` — se emparejan por posición, una lista desbalanceada es ambigua y no se adivina |
| Varios `op`, pero ni uno solo ni uno por filtro | `400 INVALID_PARAMS` |
| Un `docType` fuera de tu `AllowedDocTypes` | `403 FORBIDDEN` |
| `field`/`value` sin `AllowFieldSearch` | `403 FORBIDDEN` (`system`/`meta.<clave>` **no** requieren ese permiso) |

**Expedientes multi-documento (`expand`):** un expediente se guarda con el tipo de nivel superior `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`, pero contiene pedimentos, facturas, etc. Por eso `docType=FACTURA` devuelve **también** los expedientes que contienen una factura: además de la consulta por tipo se lanza otra sobre los expedientes filtrando por la columna `ContainedDocumentTypes` (también del lado de DynamoDB). Con `expand=0` solo se considera el tipo de nivel superior.

**Por defecto** cada match es el **envelope completo** (mismo shape que `GET /documents/{id}`): `documentId`, `documentClassification`, `validation`, `expediente.documents[...]` (todos los documentos del expediente con sus entidades, arrayCounts y `liquidacionNormalizada`), `tracking`, `metadata` (la del upload, si la hubo), más `matched[]` cuando hubo filtros por campo.

**Respuesta `200`** — cada match es el **envelope completo** + dónde coincidió:
```jsonc
{
  "docTypes": ["PEDIMENTO_ERP"],  // tipos ya resueltos a TYPE_ID
  "resolvedTypes": [                  // cómo se resolvió lo que mandaste
    { "typeId": "PEDIMENTO_ERP", "input": "Pedimento (mapeo ERP)", "resolvedBy": "exact" }
  ],
  "expandedContainers": true,         // se buscó también dentro de expedientes

  // --- eco de los filtros que se aplicaron ---
  "filters": [                        // todos los field/value, en orden
    { "field": "extractedEntities.No_Pedimento",
      "value": "260708236001187", "op": "eq" }
  ],
  "field": "extractedEntities.No_Pedimento",  // = filters[0],
  "value": "260708236001187",         // por compatibilidad con cuando solo
  "op": "eq",                         // podía haber un filtro
  "system": ["ERP"],                  // solo si mandaste system=
  "metaFilters": [                    // solo si mandaste meta.<clave>=
    { "key": "lote", "values": ["A123"] }
  ],
  "metaOp": "eq",

  "order": "desc",
  "summary": false,
  "full": false,
  "indexed": true,                    // true = Query indexada; false = Scan
  "lookup": "uniqueKey",              // qué camino contestó: "uniqueKey" (una
                                      // Query a TIPODOC#valor), "typeIndex"
                                      // (Query al GSI + filtro en la Lambda)
                                      // o "scan" (sin docType)
  "restrictedToApp": "CEDIS",         // solo si la app que llama tiene AccessScope=own (ver abajo)
  "restrictedToDocTypes": ["FACTURA_HONORARIOS_XML"], // solo si tiene AllowedDocTypes configurado
  "count": 1,
  "scanned": 2,                       // cuántos documentos se revisaron
  "matches": [
    {
      "documentId": "PEDIMENTO-ADUANAL-4ff224be-...",
      "documentClassification": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
      "documentType": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",  // columna indexada
      "documentTypeName": "Expediente Aduanal Multidocumento",
      "documentName": "expediente-2026.pdf",
      "validationStatus": "REQUIERE_REVISION",
      "extractedEntities": { "...": "..." },
      "validation": { "status": "REQUIERE_REVISION", "...": "..." },
      "expediente": { "documents": [ /* TODOS los documentos, con liquidacionNormalizada */ ], "...": "..." },
      "tracking": { "appOrigin": "ERP", "...": "..." },   // qué sistema lo subió
      "metadata": { "lote": "A123", "sucursal": "chih" }, // la del POST /documents
      "matched": [                    // uno por cada filtro field/value
        { "field": "extractedEntities.No_Pedimento",
          "matchedValue": "260708236001187",
          "matchedAt": "*.No_Pedimento" }  // "*." = búsqueda profunda
      ],
      "matchedValue": "260708236001187",              // = matched[0], por
      "matchedAt": "*.No_Pedimento",  // compatibilidad
      "fullResult": { /* SOLO con full=1: detalle por-campo de S3 */ }
    }
  ]
}
```

> **Completitud:** por defecto cada match trae el envelope completo (con `expediente.documents[...]`). Con **`summary=1`** trae solo un resumen ligero — que **incluye `appOrigin` y `metadata`**, para no tener que volver a pedir el documento solo para ver por qué entró. Con **`full=1`** agrega `fullResult` (el detalle por-campo `fields[]` desde S3).

**Ejemplos:**
```bash
# Todos los pedimentos, del más reciente al más viejo (indexado, sin Scan)
curl -sS "$API/documents?docType=PEDIMENTO_ERP&summary=1"

# Igual, pero escribiendo el nombre del tipo en vez del id
curl -sS "$API/documents?docType=pedimento%20aduanal&summary=1"

# Tipo + campo: acota por tipo en DynamoDB y luego filtra por el campo
curl -sS "$API/documents?docType=PEDIMENTO_ERP&field=No_Pedimento&value=260708236001630"

# Varios tipos, solo rechazados, de julio en adelante
curl -sS "$API/documents?docType=FACTURA,PEDIMENTO_ERP&status=RECHAZADO&since=2026-07-01&summary=1"

# Por RFC del importador (sin tipo → Scan; búsqueda profunda)
curl -sS "$API/documents?field=PROVEEDOR_ID_FISCAL&value=OFU910626UQ0"

# Por coincidencia parcial en el nombre del importador
curl -sS "$API/documents?field=NOMBRE_IMPORTADOR_EXPORTADOR&value=futurama&op=contains&limit=10"

# Todo lo que subió el ERP este mes (sistema + tipo + fechas)
curl -sS "$API/documents?docType=PEDIMENTO_ERP&system=ERP&since=2026-09-01&summary=1"

# El documento de un lote concreto, sin saber su documentId
curl -sS "$API/documents?docType=PEDIMENTO_ERP&meta.lote=A123&summary=1"

# Dos claves de metadata: AND entre ellas
curl -sS "$API/documents?docType=PEDIMENTO_ERP&meta.lote=A123&meta.sucursal=CHIH"

# Metadata por subcadena: todos los folios del ERP que empiezan con ERP-47
curl -sS "$API/documents?docType=PEDIMENTO_ERP&meta.folio=ERP-47&metaOp=contains"

# Dos campos extraídos a la vez (se emparejan por posición, y son AND)
curl -sS "$API/documents?docType=PEDIMENTO_ERP&field=PROVEEDOR_ID_FISCAL&value=OFU910626UQ0&field=Patente&value=3456"

# Los cuatro ejes juntos, rechazados de julio en adelante
curl -sS -G "$API/documents" \
  --data-urlencode "docType=PEDIMENTO_ERP" \
  --data-urlencode "system=ERP" \
  --data-urlencode "meta.lote=A123" \
  --data-urlencode "field=PROVEEDOR_NOMBRE" --data-urlencode "value=GAVANTI" \
  --data-urlencode "op=contains" \
  --data-urlencode "status=RECHAZADO" --data-urlencode "since=2026-07-01"
```

**Nota de rendimiento:**
- **Dónde se aplica cada filtro:** `docType`, `status`, `since`/`until` y el filtro de expedientes se evalúan **en DynamoDB**; `system`, `meta.<clave>` y `field`/`value` se evalúan **en la Lambda**, sobre los envelopes que el primer grupo dejó pasar. Añadir un `system=` o un `meta.*` a una consulta que ya trae `docType` no cuesta lecturas extra: filtra lo que de todos modos se iba a traer. Lo que sí cuesta es **omitir `docType`**, que manda la consulta a un Scan.
- **`system`/`meta.<clave>` vs. `field`/`value`:** los dos primeros leen **una clave conocida** del envelope (`tracking.appOrigin`, `metadata.<clave>`); `field`/`value` puede acabar recorriéndolo entero (búsqueda profunda). Por eso los dos primeros no están detrás de `AllowFieldSearch` y son la forma barata de reencontrar un documento propio.
- **Por unique key** (`"lookup": "uniqueKey"`): **una** Query a `document-validation-keys[-dev]` sobre `TIPODOC#<valor normalizado>` + un BatchGet de los envelopes que devolvió. Costo constante: no depende de cuántos documentos tenga el tipo. Requiere `docType`, `op=eq` y que **alguno** de los `field` sea el declarado `unique_key`; los demás filtros se aplican después, solo sobre lo que esa Query devolvió.
- **Con `docType`** (`"indexed": true`, `"lookup": "typeIndex"`): Query al GSI `DocumentTypeIndex`, se leen **solo** los documentos de ese tipo, ya ordenados por fecha; `status`, `since`/`until` y el filtro de expedientes se evalúan en DynamoDB. El envelope completo se trae por lotes (BatchGet) solo de los documentos que pasaron el filtro.
- **Sin `docType`** (`"indexed": false`, `"lookup": "scan"`): **Scan** de la tabla y filtrado en la Lambda, porque los campos buscables viven dentro del JSON de `EnrichmentResult`. Adecuado para volúmenes moderados; manda `docType` siempre que puedas. Si un campo se vuelve "caliente", declararlo `unique_key` en el catálogo es exactamente el paso siguiente — no hace falta código, solo `pnpm build && pnpm sync:one` (y `pnpm backfill:keys` para el histórico).
- **Documentos anteriores a la indexación:** los procesados antes de que existieran estas columnas no aparecen con `docType` hasta reconstruirlas con `pnpm backfill:index` (idempotente; `-- --dry-run` para revisar antes). Lo mismo para las unique keys declaradas después de procesar: `pnpm backfill:keys` (mientras tanto la búsqueda sigue funcionando por el camino normal).

### Seguridad — acceso por app

Este endpoint respeta los mismos permisos por app que `GET /documents/{id}`
(ver `documentacion/AMBIENTES_Y_SEGURIDAD.md` → "Acceso por app"), con dos
diferencias respecto a un solo documento:

- **Filtrado silencioso, no error:** un documento fuera de tu `AccessScope`
  simplemente no aparece en `matches` (no cuenta como error, ni se distingue
  de "no matcheó el filtro") — `restrictedToApp`/`restrictedToDocTypes` en la
  respuesta (arriba) son la pista de que el resultado está acotado.
- **`docType`/`field` explícitamente prohibidos SÍ dan error**, a diferencia
  de un documento individual: pedir un `docType` fuera de tu `AllowedDocTypes`
  → `403 FORBIDDEN`; usar `field=`/`value=` sin tener `AllowFieldSearch`
  → `403 FORBIDDEN`. **`system=` y `meta.<clave>=` no piden ese permiso:**
  leen una sola clave conocida del envelope en vez de recorrerlo entero, y
  son justo lo que una app con acceso normal por `docType` necesita para
  reencontrar su propio documento. Tampoco amplían nada — una app con
  `AccessScope: "own"` sigue viendo solo lo suyo, `system=` ahí solo puede
  acotar más. Aquí sí tiene sentido decir "no puedes" en vez de "no
  hay nada" — a diferencia de un id concreto, pedir un tipo entero no es una
  forma útil de enumerar/inferir datos ajenos.
- Sin `docType` y con `AllowedDocTypes` configurado, la búsqueda se **acota
  automáticamente** a esos tipos en vez de intentar (y fallar) un Scan sobre
  toda la tabla — más barato para la app y sin necesidad de que lo pida.

Para dar de alta/quitar estos permisos: `pnpm apps:register`/`reconfigure`
(fijan el valor completo) o `pnpm apps:grant`/`apps:deny --doctype A,B`
(suman/restan tipos sin pisar los que ya tenía la app) — ver
`AMBIENTES_Y_SEGURIDAD.md` → "Acceso por app".

---

## Apéndice: dónde queda el resultado (y por qué a veces parece incompleto)

El resultado se guarda en **dos lugares con distinto nivel de detalle** — esto explica por qué una respuesta puede verse "incompleta":

- **DynamoDB** tabla `DocumentValidationStack-BedrockDocumentProcessingDocumentProcessingTable...` (PK `DocumentId`): un **envelope COMPACTO** en la columna `EnrichmentResult` (string JSON). Trae los campos **escalares**, `arrayCounts` (solo conteos), `liquidacionNormalizada`, `status` y `tracking`. **NO** trae el contenido de los arrays ni el detalle por campo (`fields[]` con extractedValue/normalizedValue/confidence/status/checks, más value/valid/reason (vista simple)). Es compacto a propósito: el payload de Step Functions tiene límite de 256 KB.
- **S3** `results/{documentId}.json`: el resultado **COMPLETO** — todos los campos con su detalle, el contenido de todos los arrays (PARTIDAS, CONTRIBUCIONES...), checks y confianzas.

### Columnas indexadas (para filtrar sin Scan)

Además del `EnrichmentResult`, cada item lleva columnas de primer nivel que escribe el enrichment Lambda (`enrichment-lambda/indexing.py`) al terminar de validar:

| Columna | Ejemplo | Para qué |
|---|---|---|
| `DocumentType` | `PEDIMENTO_ERP` | **Partición del GSI `DocumentTypeIndex`.** Es la clasificación FINAL (la del segmentador), no la de la clasificación previa de Bedrock. |
| `ProcessedAt` | `2026-07-24T11:30:00+00:00` | **Orden del GSI**: resultados por fecha y rangos `since`/`until`. |
| `DocumentTypeName` | `Pedimento Aduanal` | Nombre legible del tipo (del catálogo o derivado del id). |
| `ValidationStatus` | `REQUIERE_REVISION` | Filtro `status=` evaluado en DynamoDB. |
| `ContainedDocumentTypes` | `["EXPEDIENTE_...", "PEDIMENTO_ERP", "FACTURA"]` | Tipos **dentro** de un expediente: permite que `docType=FACTURA` encuentre la factura de un expediente. |
| `DocumentName` | `expediente-2026.pdf` | Nombre del archivo original. |
| `Tracking` | `{CreatedAt, UpdatedAt, ProcessingStartedAt, ProcessingCompletedAt, ProcessingSeconds, ProcessingTime, Schema, AppOrigin, ModelIds, ModelFallbacks}` | **Map**, no filtrable por GSI (no es escalar) — promovido de `EnrichmentResult.tracking` para verlo en el item mismo (Scan/consola) sin parsear el JSON. Misma paridad de campos que `result.tracking`/`expediente.tracking` (ver §2 y `indexing.py::_TRACKING_FIELD_MAP`), solo que en PascalCase para combinar con el resto de estas columnas — nunca un subconjunto recortado. `AppOrigin` es lo que filtra `?system=`, pero el filtro se evalúa sobre el envelope, no sobre esta columna. |
| `Metadata` | `{lote: "A123", estado_externo: "EN_REVISION"}` | **Map mutable**: nace del `metadata` del `POST /documents` (§1) y `PATCH /documents/{id}/metadata` (§3) la puede crear o cambiar. Es la fuente actual que la API superpone al snapshot de `EnrichmentResult`/S3 y lo que filtra `?meta.<clave>=`. |
| `MetadataVersion`, `MetadataUpdatedAt`, `MetadataUpdatedBy` | `3`, fecha ISO, `ERP` | Auditoría/concurrencia de la última edición de metadata; no forman parte de la metadata del usuario ni de sus filtros. |

Documentos procesados antes de esto no tienen las columnas: `pnpm backfill:index` las reconstruye desde `EnrichmentResult` (idempotente, `--dry-run` disponible).

Cómo lo devuelve cada endpoint:

| Llamada | De dónde lee | Nivel |
|---|---|---|
| `GET /documents/{id}` | DynamoDB (`result`) | Envelope completo (con `expediente.documents[...]`) |
| `GET /documents/{id}?full=1` | DynamoDB + S3 (`fullResult`) | Envelope + detalle por-campo `fields[]` |
| `PATCH /documents/{id}/metadata` | DynamoDB (`UpdateItem` condicional) | Mapa completo actualizado; no toca S3 ni la validación — ver §3 |
| `GET /documents/{id}/file` | DynamoDB (resolver id) + S3 (`ListObjectsV2` + URL prefirmada) | El **archivo original**, no el resultado — ver §4 |
| `GET /documents?docType=...` | DynamoDB (Query al GSI + BatchGet) | Envelope completo por match |
| `GET /documents?docType=...&system=...&meta.<clave>=...` | DynamoDB (Query al GSI + BatchGet) + filtro en la Lambda | Envelope completo por match; los dos filtros no añaden lecturas |
| `GET /documents?field=...` | DynamoDB (Scan) | Envelope completo por match |
| `GET /documents?docType=...&field=<unique key>` | DynamoDB (1 Query a `TIPODOC#valor` + BatchGet) | Envelope completo por match |
| `GET /documents?...&summary=1` | DynamoDB (Query o Scan) | Resumen ligero por match |
| `GET /documents?...&full=1` | DynamoDB + S3 por match | Envelope + `fullResult` por match |

> El envelope de DynamoDB ya trae los escalares, `arrayCounts`, `liquidacionNormalizada` y `expediente.documents[...]`. Lo único que **solo** está en S3 es el detalle por-campo (`documents[].fields[]` con `extractedValue`/`normalizedValue`/`confidence`/`status`/`checks`, más `value`/`valid`/`reason` (vista simple: valor resuelto, booleano y motivo si `valid` es falso)), que se obtiene con `full=1`.
