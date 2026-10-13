<!--
  MANTENIMIENTO: Este documento es la referencia de uso del API REST.
  Debe mantenerse sincronizado con las rutas definidas en
  `src/document-validation-stack.ts` (RestApi "DocumentUploadApi") y con los
  handlers en `src/document-processing/resources/{upload,query}-lambda/` y
  `src/document-processing/resources/enrichment-lambda/references_api.py`
  (rutas `/references`).
  Si agregas, quitas o cambias un endpoint (ruta, método, params, body o
  respuesta), ACTUALIZA este archivo Y `openapi.yaml` (mismo directorio) en
  el mismo cambio. Ver AGENTS.md.
-->

# API REST — Guía de uso

Referencia de todos los endpoints del API de la app de validación documental.

> **Documentación interactiva local (Scalar/Swagger):** `pnpm docs:api` sirve
> el contrato formal [`openapi.yaml`](openapi.yaml) en
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

- **Autorización por app:** además de autenticar, la `x-api-key` decide qué documentos puede leer, reprocesar o editar esa app (por default, solo los que ella subió) — ver "Seguridad — acceso por app" en la sección 6 y `AMBIENTES_Y_SEGURIDAD.md`. Las rutas `/references` (§8) son aparte: **todas**, también los `GET`, exigen el permiso `AllowReferenceWrite`, apagado por default.
- **Formato:** JSON. Errores devuelven `{ "error": "CODIGO", "message": "..." }`.
  Programa contra `error` (código estable), no contra `message`.

> **Sobre los ejemplos de este documento:** por brevedad, la mayoría omite
> `-H "x-api-key: $KEY"`. **Agrégalo en todos** — la única petición que NO lo
> lleva es el `PUT` a S3 del paso 2 de la subida, que va firmado en la URL.

## Límites y `429`

Cada API key (una por app) tiene cuatro límites. Los `GET` (consultar estado,
descargar, buscar, catálogo) tienen un throttle **más holgado** que las
escrituras y **no gastan** tu presupuesto de documentos:

| Límite | Qué cuenta | Respuesta al pasarse | Qué hacer |
| ------ | ---------- | -------------------- | --------- |
| Throttle de escritura (req/s) | `POST /documents`, `POST /documents/{id}/reprocess`, `PATCH /documents/{id}/metadata`, y de `/references` (§8) el `POST` de reserva, el `POST …/confirm` y el `DELETE` | `429` `{"message":"Too Many Requests"}` | Reintentar con backoff (1 s, 2 s, 4 s…) |
| Throttle de lectura (req/s) | Todos los `GET` | `429` `{"message":"Too Many Requests"}` | Reintentar con backoff; en el polling, esperar como un `202` |
| Documentos por día | Solo `POST /documents` y reprocess que pasaron validación | `429` `{"error":"DAILY_DOCUMENT_LIMIT","limit":N,"resetsAt":"…","retryAfterSeconds":N}` + `Retry-After` | No reintentar antes de `resetsAt` (medianoche UTC). Los `GET` siguen funcionando |
| Techo de requests por día | **Todo**, incluido el polling | `429` `{"message":"Limit Exceeded"}` | No se arregla reintentando: se reinicia a medianoche UTC. Si pasa seguido, pide que se suba el techo |

- Los dos `429` de API Gateway (`Too Many Requests`, `Limit Exceeded`) **no
  traen** el formato `{error, message}` ni cabecera `Retry-After`: distínguelos
  por `message`.
- En **prod** el WAF además limita por IP (2000 escrituras y 10000 `GET` cada
  5 min) y bloquea con **`403`** `{"message":"Forbidden"}` — el mismo cuerpo que
  una key inválida. Si una key que funcionaba empieza a recibir `403` en ráfaga,
  baja el ritmo antes de sospechar de la key.
- Ninguna request rechazada con `429`/`403` llega a la Lambda ni cuenta como
  documento.
- **Registrar referencias (`/references`, §8) no gasta documentos del día:** el
  tope de documentos por día no aplica a esas rutas. Sí aplican el throttle de
  escritura (reserva, confirmación y baja), el de lectura (sus dos `GET`) y el
  techo de requests por día.
- Tus valores concretos los da quien administra la API (`pnpm apps:usage`, ver
  `AMBIENTES_Y_SEGURIDAD.md` → "Límites por app").

## Resumen de endpoints

| Método  | Ruta                                     | Para qué                                                                                  |
| ------- | ---------------------------------------- | ----------------------------------------------------------------------------------------- |
| `POST`  | `/documents`                             | **Paso 1 de 2:** registrar el documento y obtener la URL de subida (no recibe el archivo) |
| `GET`   | `/documents/{id}`                        | Obtener el resultado completo de un documento por su id                                   |
| `POST`  | `/documents/{id}/reprocess`              | Crear un proceso nuevo reutilizando el archivo original ya almacenado en S3               |
| `PATCH` | `/documents/{id}/metadata`               | Crear, cambiar o borrar estados/marcas propios de la app sin alterar la validación        |
| `GET`   | `/documents/{id}/file`                   | Obtener una URL prefirmada de S3 para descargar el **archivo original** subido            |
| `GET`   | `/documents?docType=...`                 | Listar/filtrar documentos por tipo (indexado, sin Scan)                                   |
| `GET`   | `/documents?system=...&meta.<clave>=...` | Filtrar por qué **sistema** lo subió y por la **metadata** con la que se subió            |
| `GET`   | `/documents?field=...&value=...`         | Buscar documentos por cualquier campo extraído                                            |
| `GET`   | `/document-types`                        | Catálogo de tipos soportados, paginado (5 por página)                                     |
| `GET`   | `/document-types/{type}`                 | Un tipo de documento por su TYPE_ID exacto, con su schema opcional                         |
| `POST`  | `/references/{scope}/{key}`              | **Paso 1 de 3:** reservar una referencia (firma, sello o logo válido de una entidad) y obtener la URL de carga |
| `POST`  | `/references/{scope}/{key}/{kind}/{refId}/confirm` | **Paso 3 de 3:** revisar el recorte subido y activar la referencia              |
| `GET`   | `/references/{scope}/{key}`              | Listar las referencias de una entidad                                                     |
| `GET`   | `/references/{scope}/{key}/{kind}/{refId}` | Una referencia, con URL temporal de su imagen                                           |
| `DELETE`| `/references/{scope}/{key}/{kind}/{refId}` | Dar de baja (desactivar) una referencia                                                 |

---

## Flujo completo, de principio a fin

Un pedimento real, de la subida hasta tener los campos extraídos. Todo con
`PEDIMENTO_ERP`; cambia el tipo y aplica igual.

| #   | Petición                               | Auth                            | Cuerpo                       | Respuesta                                            |
| --- | -------------------------------------- | ------------------------------- | ---------------------------- | ---------------------------------------------------- |
| 1   | `POST $API/upload`                     | `x-api-key`                     | JSON o form con la metadata  | `200` · `documentId`, `uploadUrl`, `uploadExpiresAt` |
| 2   | `PUT {uploadUrl}`                      | **ninguna** (firmada en la URL) | los bytes crudos del archivo | `200` con **cuerpo vacío** — S3 no devuelve JSON     |
| 3   | `GET $API/documents/{documentId}`      | `x-api-key`                     | —                            | `202` mientras procesa; `200` al terminar            |
| 4   | `GET $API/documents/{documentId}/file` | `x-api-key`                     | —                            | `200` · URL prefirmada para bajar el original        |

El **paso 2 es el único sin `x-api-key`**, y el único que no pasa por API
Gateway: va firmado en la URL, directo a S3. Entre el 2 y el 3 el pipeline
corre solo (`S3 → SQS → Step Functions → Textract/Bedrock → DynamoDB`), sin que
haya que avisarle a nada.

Cada paso abajo trae el mismo ejemplo en **curl, Node.js, Python y C#** —
despliega el que uses. Los ejemplos encadenan: el paso 1 deja las variables
que usan el 2, el 3 y el 4.

> En Scalar (`pnpm docs:api`) los cuatro pasos aparecen como operaciones
> numeradas, cada una con su selector de lenguaje.

Registra el documento y devuelve la URL de subida. **No lleva el archivo.**

### Paso 1 — `POST /documents`

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
if (!res.ok)
  throw new Error(`POST /documents → ${res.status}: ${await res.text()}`);

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

El estado y, al terminar, el resultado. `waitSeconds=0..20` activa long polling
en esta misma ruta: espera hasta ese límite y retorna en cuanto el trabajo sea
terminal; no existe un segundo canal `/fast`. Sin el parámetro el GET sigue
siendo inmediato. Mientras el pipeline corre responde `202 Accepted`, trae
`processingStatus` y recomienda la siguiente consulta con `Retry-After: 1`.
`404` vuelve a significar solamente que el id no existe o no es visible para la
app.

<details open>
<summary><b>curl</b></summary>

```bash
# 202 = trabajo conocido pero todavía no terminal.
delay=1
while :; do
  code=$(curl -sS -o result.json -w '%{http_code}' \
    "$API/documents/$DOC_ID?waitSeconds=20" -H "x-api-key: $KEY")
  [ "$code" = 200 ] && break
  # 429 "Too Many Requests" = throttle: se espera igual que un 202.
  # 429 "Limit Exceeded" = cuota diaria agotada: reintentar no sirve.
  if [ "$code" = 429 ] && grep -q "Limit Exceeded" result.json; then cat result.json; exit 1; fi
  [ "$code" = 202 ] || [ "$code" = 429 ] || { cat result.json; exit 1; }
  sleep $((delay + RANDOM % 3))
  delay=$((delay < 15 ? delay * 2 : 30))
done

jq '.result.validation.status' result.json
jq '.result.documents[] | {index, documentType, pageStart, pageEnd, fields}' result.json
```

</details>

<details>
<summary><b>Node.js 18+</b> · `fetch` nativo, sin dependencias</summary>

```javascript
// 202 = trabajo conocido pero todavía no terminal.
let doc;
let delayMs = 1000;
for (let i = 0; i < 60; i++) {
  const r = await fetch(`${API}/documents/${documentId}?waitSeconds=20`, {
    headers: { "x-api-key": KEY },
  });
  const body = await r.json();
  if (r.status === 200) {
    doc = body;
    break;
  }
  // 429 "Too Many Requests" = throttle: se espera igual que un 202.
  // 429 "Limit Exceeded" = cuota diaria agotada: reintentar no sirve.
  const throttled = r.status === 429 && body.message !== "Limit Exceeded";
  if (r.status !== 202 && !throttled)
    throw new Error(`GET → ${r.status}: ${JSON.stringify(body)}`);
  const minimum =
    Number(r.headers.get("retry-after") ?? body.retryAfterSeconds ?? 1) * 1000;
  await new Promise((s) =>
    setTimeout(s, Math.max(delayMs, minimum) + Math.random() * 1000),
  );
  delayMs = Math.min(delayMs * 2, 30000);
}

if (doc.processingStatus === "FAILED")
  throw new Error("El procesamiento técnico falló");
const result = doc.result;
console.log(result.validation.status); // APROBADO | REQUIERE_REVISION | RECHAZADO
// `documents` siempre está, traiga el PDF uno o veinte.
for (const d of result.documents) {
  console.log(
    `#${d.index}`,
    d.documentType,
    `págs ${d.pageStart}-${d.pageEnd}`,
  );
  for (const [campo, v] of Object.entries(d.fields ?? {})) {
    if (v.items) console.log("  ", campo, `= ${v.items.length} renglón(es)`);
    else
      console.log(
        "  ",
        campo,
        "=",
        v.value,
        `(conf ${v.confidence}, valid ${v.valid})`,
      );
  }
}
```

</details>

<details>
<summary><b>Python 3</b> · requiere `requests`</summary>

```python
import random
import time

# 202 = trabajo conocido pero todavía no terminal.
doc = None
delay = 1
for _ in range(60):
    r = requests.get(f"{API}/documents/{document_id}?waitSeconds=20",
                     headers={"x-api-key": KEY}, timeout=30)
    body = r.json()
    if r.status_code == 200:
        doc = body
        break
    # 429 "Too Many Requests" = throttle: se espera igual que un 202.
    # 429 "Limit Exceeded" = cuota diaria agotada: reintentar no sirve.
    throttled = r.status_code == 429 and body.get("message") != "Limit Exceeded"
    if r.status_code != 202 and not throttled:
        r.raise_for_status()
    minimum = int(r.headers.get("Retry-After", body.get("retryAfterSeconds", 1)))
    time.sleep(max(delay, minimum) + random.random())
    delay = min(delay * 2, 30)

if doc["processingStatus"] == "FAILED":
    raise RuntimeError("El procesamiento técnico falló")
result = doc["result"]
print(result["validation"]["status"]) # APROBADO | REQUIERE_REVISION | RECHAZADO
# `documents` siempre está, traiga el PDF uno o veinte.
for d in result["documents"]:
    print(f"#{d['index']}", d["documentType"], f"págs {d['pageStart']}-{d['pageEnd']}")
    for campo, v in (d.get("fields") or {}).items():
        if "items" in v:
            print("  ", campo, f"= {len(v['items'])} renglón(es)")
        else:
            print("  ", campo, "=", v["value"], f"(conf {v.get('confidence')}, valid {v['valid']})")
```

</details>

<details>
<summary><b>C# (.NET 6+)</b> · top-level statements + implicit usings</summary>

```csharp
using System.Net;

// 202 = trabajo conocido pero todavía no terminal.
JsonElement doc = default;
var delay = TimeSpan.FromSeconds(1);
for (var i = 0; i < 60; i++)
{
    var r = await http.GetAsync(
        $"{api}/documents/{documentId}?waitSeconds=20");
    var body = await r.Content.ReadFromJsonAsync<JsonElement>();
    if (r.StatusCode == HttpStatusCode.OK)
    {
        doc = body;
        break;
    }
    // 429 "Too Many Requests" = throttle: se espera igual que un 202.
    // 429 "Limit Exceeded" = cuota diaria agotada: reintentar no sirve.
    var throttled = r.StatusCode == HttpStatusCode.TooManyRequests
        && !(body.TryGetProperty("message", out var m) && m.GetString() == "Limit Exceeded");
    if (r.StatusCode != HttpStatusCode.Accepted && !throttled) r.EnsureSuccessStatusCode();
    await Task.Delay(delay + TimeSpan.FromMilliseconds(Random.Shared.Next(0, 1000)));
    delay = TimeSpan.FromSeconds(Math.Min(delay.TotalSeconds * 2, 30));
}

if (doc.GetProperty("processingStatus").GetString() == "FAILED")
    throw new Exception("El procesamiento técnico falló");
var result = doc.GetProperty("result");
// APROBADO | REQUIERE_REVISION | RECHAZADO
Console.WriteLine(result.GetProperty("validation").GetProperty("status"));
// `documents` siempre está, traiga el PDF uno o veinte.
foreach (var d in result.GetProperty("documents").EnumerateArray())
{
    var tipo  = d.GetProperty("documentType");
    var desde = d.GetProperty("pageStart");
    var hasta = d.GetProperty("pageEnd");
    Console.WriteLine($"{tipo} págs {desde}-{hasta}");
    foreach (var campo in d.GetProperty("fields").EnumerateObject())
        if (campo.Value.TryGetProperty("value", out var v))
            Console.WriteLine($"   {campo.Name} = {v}");
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
const meta = await (
  await fetch(`${API}/documents/${documentId}/file`, {
    headers: { "x-api-key": KEY },
  })
).json();

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

**Qué esperar en el paso 3.** El ciclo técnico es
`AWAITING_UPLOAD → QUEUED → PROCESSING → FINISHED`; un error de infraestructura
termina en `FAILED`. Los tres primeros devuelven `202`, los dos terminales
devuelven `200`. Respeta `Retry-After`, duplica gradualmente la espera hasta un
máximo de 30 s y agrega un pequeño _jitter_ cuando tengas muchos clientes.
`404` ya no se usa como estado transitorio.

Texto recomendado para una interfaz: **“Documento recibido y en
procesamiento. Puedes cerrar esta pantalla; el resultado se actualizará
automáticamente.”** No muestres una duración prometida: las páginas, OCR y
modelo cambian mucho el tiempo real. Si la app puede consumir EventBridge,
úsalo como señal _push_ y conserva este polling espaciado solo como respaldo.

**Los tres estados que te van a importar** (`validation.status`):

| Estado              | Qué significa                                                                       | Qué hacer                                                        |
| ------------------- | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `APROBADO`          | Todas las reglas del catálogo pasaron                                               | Consumir los campos                                              |
| `REQUIERE_REVISION` | Algo quedó dudoso: confianza baja, un campo no encontrado, o el documento se truncó | Revisión humana; mira `warnings` y los campos con `valid: false` |
| `RECHAZADO`         | Una regla dura falló (p. ej. RFC del importador que no cuadra entre documentos)     | No consumir como bueno; revisar `checks`                         |

**Dónde están los campos.** En `documents[]`, un elemento por documento
detectado dentro del PDF. Para un pedimento declarado será uno solo — pero
**sigue siendo un arreglo**, para que el mismo código lea un PDF de una factura
y uno de tres:

```jsonc
{
  "documentId": "e18d014a-...",
  "documentCount": 1,
  "validation": { "status": "APROBADO" },
  "documents": [
    {
      "index": 1,
      "documentType": "PEDIMENTO_ERP",
      "pageStart": 3,
      "pageEnd": 18,
      "status": "APPROVED",
      "fields": {
        "No_Pedimento": {
          "value": "260734356041248",
          "confidence": 0.98,
          "valid": true,
        },
        "FACTURAS": {
          "items": [
            {
              "fields": {
                "PROVEEDOR_ID_FISCAL": {
                  "value": "334274279",
                  "confidence": 0.95,
                  "valid": true,
                },
                "PROVEEDOR_NOMBRE": {
                  "value": "MNG DISTRIBUTOR LLC",
                  "confidence": 0.95,
                  "valid": true,
                },
                "Factura": {
                  "value": "4099",
                  "confidence": 0.95,
                  "valid": true,
                },
                "Fecha_Factura": {
                  "value": "2026-07-03",
                  "confidence": 0.95,
                  "valid": true,
                },
                "Total_factura": {
                  "value": 155.0,
                  "confidence": 0.95,
                  "valid": true,
                },
              },
            },
          ],
        },
      },
    },
  ],
}
```

### `confidence`: siempre un número, nunca `null`

Cada campo escalar trae un `confidence` entre 0 y 1 — **incluidos los
calculados** (`LIQUIDACION_NORMALIZADA.*`, campos `derived`) y los que el
documento no traía. Cómo se decide:

| Caso                                                                  | `confidence`                                                                     |
| --------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Campo leído del documento                                             | Lo que reportó el modelo × qué tan bien se leyó la página.                       |
| Campo calculado de varias celdas (un total del cuadro de liquidación) | El **promedio** de las celdas que sumó.                                          |
| Fila de un array                                                      | La de **esa** fila, no la del array completo.                                    |
| Campo que el documento no traía                                       | `0.0` — "no lo sé" (con `value: null`; si era obligatorio, el `reason` lo dice). |

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

> **Nota de migración.** El modo _inline_ (`contentBase64` en el body) fue
> **eliminado**. Un request que lo mande recibe `400 INLINE_UPLOAD_REMOVED`
> explicando el flujo nuevo, en vez de fallar en silencio: base64 infla el
> archivo ~33 %, así que reventaba alrededor de los ~4 MB reales y hacía que el
> **tamaño** decidiera qué camino corría.

### Paso 1 — `POST /documents` (no lleva el archivo)

El body va como `application/json` **o** `application/x-www-form-urlencoded`.

```jsonc
{
  "fileName": "pedimento.pdf", // requerido; pdf|jpg|jpeg|png|xml|xlsx|csv
  "documentType": "PEDIMENTO_ERP", // opcional (obligatorio en .xml/.xlsx/.csv).
  // Varios separados por coma para que
  // los busque y clasifique: ver §Notas
  "occurrenceMode": "single", // opcional: pisa defaults de catálogo.
  // Omitido: catálogo; si no define,
  // single con un tipo/multiple con varios
  "metadata": { "lote": "A123" }, // opcional; se conserva con el documento
  // y luego se puede filtrar con
  // GET /documents?meta.lote=A123 (§6)
  "modelId": "amazon.nova-pro-v1:0", // opcional; SOLO DEV — ver notas
  "extractionMode": "deterministic", // opcional; SOLO DEV: catalog|model|hybrid|deterministic
}
```

**Respuesta `200`:**

```json
{
  "documentId": "e18d014a-...",
  "bucket": "documentvalidationstack-...",
  "key": "raw/PEDIMENTO_ERP/e18d014a-.../pedimento.pdf",
  "documentType": "PEDIMENTO_ERP",
  "occurrenceMode": "single",
  "documentTypeCounts": { "PEDIMENTO_ERP": 1 },
  "classificationMode": "manual",
  "uploadUrl": "https://...s3.amazonaws.com/...",
  "uploadMethod": "PUT",
  "uploadExpiresInSeconds": 900,
  "uploadExpiresAt": "2026-07-27T13:33:19.000Z",
  "uploadedAt": "2026-07-27T13:18:19.000Z",
  "registeredAt": "2026-07-27T13:18:19.000Z",
  "processingStatus": "AWAITING_UPLOAD",
  "statusUrl": "/documents/e18d014a-...",
  "retryAfterSeconds": 1
}
```

Guarda el `documentId`: es con lo que consultas el resultado después.

### Paso 2 — `PUT` del archivo a S3

Esta petición **no va a esta API**: va a S3, con la `uploadUrl` del paso 1.

```bash
curl -X PUT --upload-file pedimento.pdf "$UPLOAD_URL"
```

|                  |                                                                                                                                        |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| **Método**       | `PUT` (viene en `uploadMethod`). No `POST`, no multipart, no form-data                                                                 |
| **Cuerpo**       | Los bytes crudos del archivo, sin envolver ni codificar                                                                                |
| **Cabeceras**    | **Ninguna.** La firma y la metadata ya van en la URL (ver aviso abajo)                                                                 |
| **La URL**       | Úsala **completa y tal cual**. No separes parámetros. Al pegarla en shell, comillas **simples**: trae ~12 `&` en unos 1,900 caracteres |
| **Auth**         | Ninguna. La firma va en la URL; **no** mandes `x-api-key` aquí                                                                         |
| **Respuesta OK** | `200` con cuerpo vacío — S3 no devuelve JSON                                                                                           |
| **Vencimiento**  | 15 min (`uploadExpiresAt`). Si expira, repite el paso 1: no se renuevan                                                                |
| **Tamaño**       | Hasta 5 GB                                                                                                                             |

> ⚠️ **No mandes ninguna cabecera `x-amz-*` en el `PUT`.** Al prefirmar, SigV4
> _hoistea_ esas cabeceras al **query string**, así que la metadata del
> documento (`model-id`, `app-origin`) ya viaja dentro de `uploadUrl` y está
> cubierta por la firma — fíjate en que la URL trae `X-Amz-SignedHeaders=host`,
> o sea que la única cabecera firmada es `host`.
>
> Si además las mandas como header, S3 rechaza la subida con
> **`403 AccessDenied: There were headers present in the request which were not
signed`**, y lista las culpables en `<HeadersNotSigned>`. Un 403 que parece de
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

- **`metadata.proveedor` en tipos con verificación de autenticidad** (hoy `CARTA_NOM35`). Esos tipos comparan la firma, el sello y el logotipo del documento contra los que el emisor tiene **registrados con anticipación**, y saben de qué emisor se trata por esta llave: `"metadata": {"proveedor": "10040"}`. Es opcional y nunca rechaza por sí sola: sin ella — o si ese proveedor aún no tiene referencias — el documento se procesa igual, verificando que firma, sello y logo **estén** y **no sean imágenes insertadas**, y `authenticity.referencesFound` sale `false`. El resultado llega en `documents[i].authenticity` (§2). Las referencias se registran por API en `/references/PROVEEDOR/{key}` (§8), y la `key` de ahí es exactamente el valor que mandas aquí.

- **`documentType` decide entre tres modos**, según cuántos tipos mandes. Es **un solo campo**: varios tipos van separados por coma, igual que en `GET /documents?docType=A,B`.

  | Mandas                                              | `classificationMode` | Qué hace                                                                                                                                                                                                                                                                  |
  | --------------------------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | **Un** tipo (`PEDIMENTO_ERP`)                       | `manual`             | Se salta la clasificación automática **y se HONRA tu tipo**: el archivo entero se trata como ese tipo único y **no se re-clasifica ni se segmenta** (un PDF de varias páginas declarado `PEDIMENTO_ERP` es un solo pedimento, no un `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`). |
  | **Varios** (`PEDIMENTO_ADUANAL,FACTURA_HONORARIOS`) | `guided`             | El archivo **sí se segmenta y se clasifica**, pero la lista es **CERRADA**: `documents[]` sólo contiene los tipos declarados. Cada documento encontrado lleva su tipo, `pageStart`/`pageEnd` y su propio esquema; los demás segmentos se omiten.                          |
  | **Nada** (omites el campo)                          | `auto`               | El clasificador elige contra todo el catálogo.                                                                                                                                                                                                                            |

- **Cuándo usar la lista.** Cuando sabes qué _debería_ traer el archivo pero no cuántos ni en qué orden — un expediente con un pedimento y sus facturas de honorarios. Sale **más barato y más preciso** que omitir el tipo: el prompt del clasificador sólo lleva los tipos que pediste (en un caso real, 2,552 caracteres contra 4,649), y no hay tipos parecidos del resto del catálogo con los que confundirse. Para forzar la segmentación multi-documento sin acotar nada, sigue estando `documentType=EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`.

- **Cuántos documentos de cada tipo buscar: `TIPO:N`.** Cada tipo admite un sufijo opcional que dice cuántos documentos de ese tipo debe buscar el pipeline:

  ```
  documentType=PEDIMENTO_ERP                                  # occurrenceMode single (default)
  documentType=PEDIMENTO_ERP:2                                # hasta 2
  documentType=FACTURA_FLETE_EXTRANJERO:all                   # todas las que haya
  documentType=PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO         # ambos usan occurrenceMode del POST
  documentType=PEDIMENTO_ERP:1,FACTURA_FLETE_EXTRANJERO:2     # cantidades explícitas
  ```

  **El POST puede sobrescribir el catálogo:** `occurrenceMode: "single"` conserva la primera ocurrencia de cada tipo sin sufijo y `occurrenceMode: "multiple"` conserva todas. Si se omite, cada tipo usa su `occurrence_mode` de catálogo; sólo cuando esa propiedad falta aplica el default global (`single` con un tipo declarado, `multiple` con varios). Un `:N` o `:all` explícito siempre prevalece para ese tipo. También se acepta `occurrence_mode` como alias de compatibilidad, aunque el contrato usa camelCase.

  En una lista guiada no necesitas anticipar cuántas facturas contiene el expediente. Si quieres acotar un tipo por costo o por regla de negocio, usa `:N`.

  En una solicitud cerrada, los tipos ajenos, las copias y las ocurrencias que exceden un `:N` se omiten de `documents[]` y no se extraen. Si falta una ocurrencia solicitada, aparece un marcador del tipo pedido con `pageStart: null`, `pageEnd: null`, `status: "REVIEW_REQUIRED"`, `confidence: null` y `fields: {}`. Para `TIPO:N` se agrega uno por cada faltante; para `TIPO:all`, uno únicamente si el tipo no apareció. Esos marcadores activan revisión, pero no cuentan en `documentCount` ni en `validation.counts`.

  La respuesta del `POST` devuelve el `occurrenceMode` explícito (`null` si se omitió) y las decisiones materializadas en `documentTypeCounts`. Los defaults de catálogo se resuelven después, en enrichment. Por ejemplo, con `occurrenceMode: "multiple"` y un override `PEDIMENTO_ERP:1`:

  ```jsonc
  "documentType": "PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO",
  "occurrenceMode": "multiple",
  "documentTypeCounts": {
    "PEDIMENTO_ERP": 1,
    "FACTURA_FLETE_EXTRANJERO": "all"
  },
  "classificationMode": "guided"
  ```

  Una cuenta inválida (`:0`, `:-1`, `:1.5`, `:abc`) es `400 INVALID_DOCUMENT_TYPE`; un `occurrenceMode` distinto de `single`/`multiple` es `400 INVALID_OCCURRENCE_MODE`. Un tipo repetido con cuentas distintas conserva la MAYOR.

- **Topes de la lista:** máximo **8 tipos** y **200 caracteres** en total, porque viajan como un solo segmento de la llave de S3 (`raw/{A,B}/{uuid}/archivo.pdf`) y una llave de S3 tiene 1024 bytes contando uuid y nombre de archivo. Pasarse es `400 INVALID_DOCUMENT_TYPE`, igual que un tipo que no sea UPPER_SNAKE_CASE. Los tipos repetidos se descartan; el orden que mandes es el que ve el clasificador.

- **Archivos `.xml`/`.xlsx`/`.csv`:** para tipos con esquema XML (sufijo `_XML`) o tabular (sufijo `_XLSX`/`_CSV`) `documentType` es **obligatorio** — la API lo rechaza con `400 DOCUMENT_TYPE_REQUIRED` si falta, porque no existe clasificación automática por visión sobre datos ya estructurados. A cambio, el documento se extrae **determinísticamente** (XPath para XML, celda/columna para XLSX/CSV — sin llamar a Bedrock en ningún paso): más rápido y sin costo de modelo. Ver `catalogs/README.md` → "Tipos con fuente XML" / "Tipos con fuente tabular". Ahí además se acepta **exactamente un tipo**: no hay nada que elija entre varios esquemas, así que una lista se rechaza con `400 INVALID_DOCUMENT_TYPE` en vez de leer el archivo con el primero y hacerte creer que se buscaron todos.
- **`modelId` (solo dev):** procesa **ese documento** con el modelo de Bedrock que indiques, sin redeploy — útil para comparar modelos sobre el mismo archivo. Aplica a todas las fases y gana sobre las variables `BEDROCK_*_MODEL_ID`; el resultado reporta en `tracking.modelIds` qué modelo corrió en cada fase. **El stack de prod lo ignora a propósito** (no define `ALLOW_MODEL_OVERRIDE`): el valor viene de quien sube el archivo, y en prod dejaría que un tercero eligiera el modelo —y con él el costo y la calidad de extracción— sobre documentos ajenos. Con `pnpm enviar`: `--model amazon.nova-pro-v1:0`.
- **`extractionMode` (solo dev):** override de la extracción de campos para esta
  carga. **El default global recomendado es `catalog`**, porque conserva la
  política versionada de cada tipo: hoy `PEDIMENTO_ERP` elige código
  determinístico y los tipos PDF/imagen sin mapper conservan el modelo.

  | Valor | Nombre operativo | Efecto |
  |---|---|---|
  | `catalog` | **Mapper configurado; si no existe, IA completa** | Busca `deterministic_extraction`: si no existe manda todos los campos a Bedrock; si existe ejecuta código y el `fallback` decide entre IA para faltantes o revisión sin IA. Es el default y el comportamiento de producción. |
  | `model` | **IA para todos los campos** | Omite el mapper y manda el schema completo a Bedrock; reproduce el comportamiento anterior. |
  | `hybrid` | **Código primero; IA para faltantes** | Conserva lo resuelto por el mapper y sólo manda las ramas faltantes a Bedrock. |
  | `deterministic` | **Sólo código; faltantes a revisión** | No usa IA para extraer campos; los faltantes pueden mandar el documento a revisión. |

  Si se pide `hybrid`/`deterministic` para un tipo que no declara un mapper, el
  modo efectivo permanece `model` y reporta
  `reason: type_has_no_deterministic_mapping`; nunca devuelve vacío por un
  override incompatible. XML/XLSX/CSV se enrutan antes por su formato y ya son
  determinísticos (XPath o celda/columna), por lo que este override no cambia
  su extractor. `deterministic` significa **sin IA en la extracción de campos**,
  no necesariamente sin IA en toda la carga: clasificación, segmentación,
  localización de páginas o autenticidad todavía pueden ejecutar un modelo.
  La respuesta final es la autoridad mediante `aiExecuted`, `aiUsage.stages`,
  `documents[i].extractionMode` y `fallbackFields`. Con el CLI:
  `pnpm enviar doc.pdf PEDIMENTO_ERP --extraction-mode model|hybrid|deterministic`.
  Producción ignora overrides distintos de `catalog` con la misma compuerta de
  seguridad que `modelId`.

  Flujo de `catalog` para PDF/imagen: **sin `deterministic_extraction` → IA para
  todos los campos**; con mapper y `fallback: review` → sólo código y faltantes
  a revisión; con mapper y `fallback: missing_fields` → código primero y Bedrock
  únicamente si quedaron campos pendientes. `catalog` no inventa ni intenta
  generar un mapper. En `PEDIMENTO_ERP` el catálogo declara mapper + `review`,
  por lo que el default es código sin IA de extracción.

- **`aiResponseFormat`:** controla sólo el protocolo interno de la IA; no
  cambia `extractionMode` ni el JSON final. `compact` (default global) devuelve
  escalares como `[valor, confianza]` y tablas posicionales, y el backend arma
  el árbol completo. `legacy` reproduce la respuesta anidada anterior. La
  precedencia es request autorizado → catálogo → ambiente → `compact`. En
  producción el override requiere `AllowAiResponseFormatOverride` para la API
  key; sin permiso responde `403 AI_RESPONSE_FORMAT_OVERRIDE_FORBIDDEN`.
  También se acepta al reprocesar. Con el CLI:
  `pnpm enviar documento.pdf TIPO --ai-response-format compact|legacy`.

  Una respuesta compacta estructuralmente ilegible se reintenta una sola vez
  en legacy y ambos consumos son facturados. Un campo ausente o un valor que no
  pasa validación **no** provoca reintento. La auditoría aparece en
  `documents[i].aiResponseFormat`; `aiResponseSavings` muestra el contrafactual
  estimado, separado de `aiUsage`, que siempre contiene tokens reales.

  En la respuesta, `requested` dice **qué pidió el cliente**, `effective` dice
  **qué política pudo aplicar el tipo**, y `strategy` dice **qué ocurrió en ese
  documento**. Si no se configura nada en request ni schema: `requested` es
  `catalog`, `effective` es `model` y `strategy` es `model`.
- **Fallback automático de modelo (siempre activo, no es un flag de request):** si el modelo que le tocaba a una fase (`extractor`/`recovery`) falla porque no está disponible (no habilitado, no existe, error/timeout/cuota del proveedor — nunca por un request mal formado, eso propaga tal cual), el pipeline cambia solo al siguiente modelo de una lista ordenada y reintenta, hasta 4 veces por default. `tracking.modelIds` ya refleja el modelo que **realmente** corrió (no el configurado) cuando esto pasa; además, solo para las fases donde hubo cambio, `tracking.modelFallbacks` reporta el detalle: modelo esperado vs. el que finalmente corrió, cuántos reintentos tomó, y el error de cada intento fallido. Ver `documentacion/GUIA_DESPLIEGUE_Y_USO.md` §5.6.
- **Segmentación degradada (clasificación de un expediente con 2+ tipos declarados):** distinto del punto anterior, aquí Bedrock responde 200 OK pero con texto vacío o truncado (`stopReason: "max_tokens"`) — el pipeline lo detecta, sube el presupuesto de salida y reintenta una vez con el modelo más confiable antes de rendirse; para expedientes grandes, la clasificación se reparte en ventanas de páginas que luego se fusionan. Si pese a eso un rango de páginas queda sin clasificar (marcado `OTRO`), `tracking.segmentationDegraded` reporta cuáles, por qué y con qué intentos, `reviewRequired` se fuerza a `true` y se agrega un `warnings[]` legible — en vez del `documentCount: 0` mudo de antes. Ver `documentacion/GUIA_DESPLIEGUE_Y_USO.md` §5.6.1.
- **`tracking.appOrigin` (trazabilidad):** cuando el request trae un `x-api-key` de una app registrada con `pnpm apps:register` (ver `documentacion/AMBIENTES_Y_SEGURIDAD.md`), el resultado del documento incluye `tracking.appOrigin` con el nombre de esa app — buscable después vía `GET /documents?system=<app>` (§6). Sale de la **key**, no del body: un `metadata: {"app-origin": "..."}` se descarta, así que ninguna app puede declararse otra. Una key válida pero nunca registrada (p. ej. `idp-default` sin `pnpm apps:adopt`) reporta `appOrigin: "unregistered"`; sin key en absoluto (dev con `DEV_API_KEY_REQUIRED` apagado), el campo simplemente no aparece dentro de `tracking`.
- **`metadata` (contexto tuyo, editable y filtrable):** lo que mandes en `metadata` viaja con el documento hasta su resultado (queda en `metadata` del envelope y en la columna `Metadata` del item) y se puede filtrar con `GET /documents?meta.<clave>=<valor>` (§6). Después de procesarlo puede modificarse con `PATCH /documents/{id}/metadata` (§4), incluso para agregar metadata a documentos antiguos. Las claves se normalizan a minúsculas y deben cumplir `[a-z0-9][a-z0-9._-]*`; los valores son planos, US-ASCII y el mapa completo tiene 2 KB de tope. Lo que no cumpla se rechaza con `400 INVALID_METADATA`. `app-origin`, `model-id`, `extraction-mode`, `ai-response-format` y `document-counts` son claves reservadas del sistema.
- **Tope de páginas (pipeline de expedientes/pedimentos):** depende de si el tipo declarado **selecciona sus propias páginas** (`page_selection` en su catálogo). Si las selecciona, el documento **no se trunca**: el costo ya lo acota el selector, que solo manda al modelo las páginas con los campos del tipo, y solo aplica un tope muy superior (`max_source_pages`, 500 por defecto) como salvaguarda contra un archivo capaz de agotar la Lambda. Si **no** las selecciona —tipo sin `page_selection`, con `enabled: false`, `PAGE_SELECTION_ENABLED=false`, o una subida **sin tipo declarado** (no se sabe qué selector aplica hasta clasificar, y clasificar exige leer las páginas)— un PDF de más de 50 páginas (`MAX_PAGES`, configurable) se **trunca a las primeras 50** antes de correr Textract/Bedrock, para acotar el costo de un documento inesperadamente grande. El PDF original completo **no se toca** — sigue intacto en S3 — pero el resultado solo cubre las páginas procesadas, queda marcado `reviewRequired: true` con un warning explícito, y un campo `truncated: {originalPages, processedPages}`. Si necesitas las páginas restantes, hay que reprocesar manualmente.
- El resultado final se publica en **EventBridge** (broker `idp-broker`, source `document-validation-pipeline`) y se persiste (ver GET abajo).

**Ejemplo (atajo del repo, siempre mediante URL prefirmada):**

```bash
pnpm enviar pedimento.pdf PEDIMENTO_ERP              # dev por default
pnpm enviar pedimento.pdf PEDIMENTO_ERP --env prod   # API key desde .env.production
pnpm enviar facturas.pdf FACTURA_FLETE_EXTRANJERO --occurrence-mode multiple
```

---

## 2. `GET /documents/{id}` — Obtener un documento por id

Devuelve el resultado **completo** de un documento. `{id}` puede ser el id completo o un **prefijo** (gana el primer match).

**Respuestas del `GET`** — cuando termina, `result` es el **envelope completo**
(el mismo que se guarda en DynamoDB y se publica en EventBridge).

> **Un documento = un elemento de `documents[]`, siempre.** Ese arreglo está
> presente con uno o con veinte. No hay una forma "de un documento" y otra "de
> varios": quien consume no puede saber de antemano cuántos trae un PDF, y una
> respuesta que cambiara de forma según el resultado obligaría a escribir dos
> caminos y a equivocarse el día que llegan dos. `documentCount` y
> `documentClassification` en la raíz sirven para lo trivial sin abrir el arreglo.
>
> **`documentClassification` dice QUÉ TRAE el archivo.** Si todos los documentos
> son del mismo tipo, es ese tipo (aunque sean varios); si son de tipos
> distintos, son **los tipos separados por coma en orden de aparición**
> (`"PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO"`). En clasificación automática
> pueden existir filas `OTRO`, pero nunca entran en esa etiqueta ni en
> `documentCount`; en una solicitud cerrada ni siquiera se publican. Los
> marcadores de tipos solicitados no encontrados tampoco cuentan como contenido.
>
> No lo confundas con la **columna indexada** `documentType`, que en un archivo
> mixto sigue diciendo `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`: es la partition key
> de un GSI de DynamoDB y una Query busca por valor **exacto**, así que indexar
> bajo `"A,B"` haría el expediente invisible para `?docType=A` (y no hay forma de
> enumerar particiones sin un Scan). Buscar por los tipos de adentro sigue
> funcionando igual, vía `ContainedDocumentTypes` — ver §6.
>
> **`documentMatchesType` dice si el archivo ES lo que se declaró.** Tres
> llaves en la raíz, **siempre presentes**:
>
> | Llave | Valor | Significado |
> |---|---|---|
> | `documentMatchesType` | `true` / `false` / `null` | `false` = el contenido es de otra naturaleza (una constancia fiscal subida en la casilla del estado de cuenta). `null` = no se evaluó (XML, tabular, flujo estándar, verificación apagada). |
> | `rejectionConfidence` | `0.0`-`1.0` | Qué tan seguro está el sistema de que NO es el documento declarado. `0.0` cuando corresponde o no se evaluó. |
> | `detectedDocument` | texto / `null` | Qué parece ser en realidad ("Constancia de situación fiscal del SAT"). |
>
> Cuando es `false` el documento sale **`RECHAZADO`** por un check
> `document_type_match` bloqueante en `documents[].checks`, su `confidence`
> cae a **≤ 0.05** (los campos que el modelo haya leído se conservan con su
> propia confianza, pero el número del documento dice que no hay documento de
> ese tipo) y `documents[].typeCheck` repite el veredicto por documento — en un
> archivo con varios, la raíz es `false` si ALGUNO no corresponde y
> `rejectionConfidence` es la máxima. Una variante extranjera, de otro banco,
> país o formato, o un escaneo malo del MISMO documento es `true`: la
> pregunta es de naturaleza, no de calidad. Cuesta cero llamadas extra (viaja
> en la misma extracción). Cuando lo que el modelo dice choca con lo que se
> leyó (TODOS los campos obligatorios llenos y con buena confianza), el
> efecto baja a `REQUIERE_REVISION` con `typeCheck.reason =
> "SEÑAL_CONTRADICHA_POR_CAMPOS"` en vez de rechazar.
>
> **`aiExecuted` confirma si realmente corrió IA.** `true` no se infiere por
> el tiempo: se basa en las etapas que registraron un modelo/usage real.
> `aiUsage.stages` dice cuáles y `inputTokens`/`outputTokens` conserva los
> tokens facturados disponibles. Textract puede tardar varios segundos en un
> escaneo, pero ese OCR no se cuenta como IA generativa. Para tipos con mapeo
> por código, `deterministicSavings` es un contrafactual explícitamente
> `estimated`: compara el schema completo contra el schema parcial realmente
> enviado. Nunca se suma a `usage`. El detalle por documento expone
> `deterministicExtraction.strategy`, `fallbackFields` y, cuando el tipo usa
> varios layouts, `profile`: `matched` identifica el perfil que permitió usar
> código; `unknown`/`ambiguous` explican por qué se aplicó el fallback seguro.

**Mientras procesa — `202`:**

```json
{
  "documentId": "e18d014a-...",
  "processingStatus": "PROCESSING",
  "workflowStatus": "pending",
  "documentType": "FACTURA_FLETE_EXTRANJERO",
  "result": null,
  "retryAfterSeconds": 1
}
```

`processingStatus` también puede ser `AWAITING_UPLOAD` o `QUEUED`. El siguiente
`GET` no debe hacerse antes de `Retry-After`.

**Terminó correctamente — `200`:** este es el ejemplo corto recomendado para
entender la estructura. Los textos, domicilios y arreglos largos están
abreviados deliberadamente:

```jsonc
{
  "documentId": "e18d014a-...",
  "processingStatus": "FINISHED",
  "workflowStatus": "complete", // interno; no usar para polling
  "documentType": "FACTURA_FLETE_EXTRANJERO",
  "result": {
    "envelopeVersion": 2,
    "documentClassification": "FACTURA_FLETE_EXTRANJERO",
    "documentMatchesType": true, // false = el archivo no es lo declarado → RECHAZADO
    "rejectionConfidence": 0.0,  // 0-1, qué tan seguro de que NO lo es
    "detectedDocument": null,    // qué parece ser cuando no corresponde
    "aiExecuted": true,
    "aiUsage": {
      "executed": true,
      "stages": ["extraction"],
      "inputTokens": 4074,
      "outputTokens": 594,
      "images": 0,
      "tokenReportedStages": ["extraction"]
    },
    "documentCount": 1,
    "validation": {
      "status": "APROBADO", // o REQUIERE_REVISION / RECHAZADO
      "reviewRequired": false,
    },
    "documents": [
      {
        "index": 1,
        "documentType": "FACTURA_FLETE_EXTRANJERO",
        "pageStart": 9,
        "pageEnd": 9,
        "status": "APPROVED",
        "confidence": 0.94,
        "fields": {
          "NUMERO_FACTURA": {
            "value": "SJ191703",
            "confidence": 0.98,
            "valid": true,
          },
          "TOTAL": { "value": 14612.4, "confidence": 0.93, "valid": true },
          "CONCEPTOS": {
            "items": [
              {
                "fields": {
                  "DESCRIPCION": {
                    "value": "CA Valencia...",
                    "confidence": 0.93,
                    "valid": true,
                  },
                  "IMPORTE": {
                    "value": 14612.4,
                    "confidence": 0.93,
                    "valid": true,
                  },
                },
              },
            ],
          },
        },
      },
    ],
    "tracking": {
      "processingSeconds": 60.71,
      "uploadToResultSeconds": 64.02,
      "modelIds": { "extraction": "..." },
    },
    "sourceFile": "factura.pdf",
    "pages": { "totalPages": 1, "ocrPages": 1 },
  },
}
```

En un tipo que sí declaró extracción determinística, como `PEDIMENTO_ERP`, se
agregan los bloques de ahorro y decisión por documento:

```jsonc
{
  "deterministicSavings": {
    "estimated": true,
    "method": "partial_schema_counterfactual_v1",
    "estimatedInputTokensSaved": 2100,
    "estimatedOutputTokensSaved": 280
  },
  "documents": [{
    "extractionMode": { "requested": "hybrid", "effective": "hybrid" },
    "deterministicExtraction": {
      "strategy": "hybrid",
      "aiExecuted": true,
      "profile": {
        "id": "FL",
        "status": "matched",
        "scores": {"EMP": 0, "FL": 4, "GMM": 0, "SV": 0}
      },
      "resolvedFieldCount": 18,
      "fallbackFields": ["No_Pedimento"],
      "tokenSavings": { "estimated": true, "estimatedInputTokensSaved": 2100 }
    }
  }]
}
```

Para `CERTIFICADO_POLIZA`, los perfiles conocidos son `EMP` (Empresarial),
`FL` (Flotilla), `GMM` (Gastos medicos mayores) y `SV` (Seguro de Vida). Un
perfil reconocido usa código y manda a IA sólo sus campos esperados faltantes;
un formato desconocido o un empate usa IA completa. `aiExecuted` y
`fallbackFields` siguen siendo la autoridad para saber qué ocurrió realmente.

**Falla técnica terminal — `200`:** no confundir con una validación
`RECHAZADO`. Aquí el pipeline no pudo producir el resultado:

```json
{
  "documentId": "e18d014a-...",
  "processingStatus": "FAILED",
  "workflowStatus": "enrichment-failure",
  "documentType": "FACTURA_FLETE_EXTRANJERO",
  "result": null
}
```

<details>
<summary><b>Ver ejemplo extendido con tracking, objetos y arreglos</b></summary>

El siguiente JSON sigue abreviando valores de texto, pero enumera las secciones
opcionales importantes del envelope:

```jsonc
{
  "documentId": "PEDIMENTO-ADUANAL-4ff224be-...",
  "processingStatus": "FINISHED", // contrato técnico público
  "workflowStatus": "complete", // detalle interno; no usar para polling
  "result": {
    "documentId": "...",
    "envelopeVersion": 2, // ausente = envelope viejo (ver abajo)
    // Cuando TODOS los documentos son del mismo tipo, ese es el tipo — aunque
    // sean varios. EXPEDIENTE_ADUANAL_MULTIDOCUMENTO queda solo para paquetes
    // de tipos mixtos.
    "documentClassification": "FACTURA_FLETE_EXTRANJERO",
    "documentCount": 2, // encontrados: excluye OTRO y marcadores sin rango

    // Procedencia/procesamiento — presente en TODO resultado (flujo estándar y
    // expediente por igual). createdAt siempre está; modelIds/appOrigin/
    // modelFallbacks/segmentationDegraded/timings solo si hay algo que reportar.
    "tracking": {
      "createdAt": "2026-07-27T13:19:04.231Z",
      // ObjectCreated de S3: el PUT ya terminó. No es el momento en que se
      // pidió la URL prefirmada.
      "uploadedAt": "2026-07-27T13:18:15.000Z",
      "processingStartedAt": "...",
      "processingCompletedAt": "...",
      "workflowCompletedAt": "2026-07-27T13:19:05.500Z",
      // Sólo el pipeline documental.
      "processingSeconds": 45.23,
      "processingMs": 45231.7,
      // PUT → JSON final construido (incluye cola y pasos upstream).
      "uploadToResultSeconds": 49.23,
      "uploadToResultMs": 49231.7,
      // PUT → workflow FINISHED/FAILED, incluidos movimiento de archivo y
      // evento terminal. Se completa al consultar el resultado final por API.
      "uploadToCompletionSeconds": 50.5,
      "uploadToCompletionMs": 50500.0,
      // Milisegundos por etapa: en qué se fue el tiempo, sin volver a correr
      // el documento. Solo en los pipelines que lo miden.
      "timings": {
        "pageLoad": 3120.4,
        "segmentation": 0.0,
        "extraction": 41200.1,
        "validation": 911.2,
      },
      // Modelo que REALMENTE corrió por fase — ya refleja cualquier fallback.
      "modelIds": { "extraction": "minimax.minimax-m2.5" },
      "appOrigin": "CEDIS", // app que subió el documento; ausente si no hay api key registrada
      "schema": {
        "pipelineVersion": "1.0.0",
        "typeVersions": { "FACTURA_FLETE_EXTRANJERO": "5.1.0" },
      },
      // Ausente salvo que ALGUNA fase haya cambiado de modelo.
      "modelFallbacks": {
        "extractor": {
          "expectedModel": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
          "actualModel": "minimax.minimax-m2.5",
          "retries": 1,
          "attempts": [
            {
              "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
              "error": "AccessDeniedException",
            },
          ],
        },
      },
      // Ausente salvo que la segmentación se haya rendido en algún rango de
      // páginas (respuesta vacía/truncada pese al reintento — ver
      // GUIA_DESPLIEGUE_Y_USO.md §5.6.1). Un "marked_otro" fuerza reviewRequired.
      "segmentationDegraded": [
        {
          "pageStart": 1,
          "pageEnd": 31,
          "reason": "empty_response",
          "resolution": "marked_otro",
          "attempts": [
            {
              "modelId": "minimax.minimax-m2.5",
              "maxTokens": 8192,
              "stopReason": "max_tokens",
              "outputTokens": 8192,
              "reason": "empty_response",
            },
            {
              "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
              "maxTokens": 8192,
              "stopReason": "max_tokens",
              "outputTokens": 8192,
              "reason": "empty_response",
            },
          ],
        },
      ],
    },
    "metadata": { "lote": "A123" }, // la que mandaste en el POST, verbatim

    "validation": {
      "status": "APROBADO", // APROBADO | REQUIERE_REVISION | RECHAZADO
      "reviewRequired": false,
      "summary": "2 documento(s) — 2 APPROVED",
      "counts": { "APPROVED": 2 }, // por status de documento
    },

    "documents": [
      {
        "index": 1,
        "documentType": "FACTURA_FLETE_EXTRANJERO",
        "documentSubtype": null, // presente solo si lo hay
        "businessDestination": "PAGO_FLETE_AMERICANO",
        "pageStart": 1,
        "pageEnd": 1,
        "status": "APPROVED", // APPROVED | PARTIALLY_APPROVED | REVIEW_REQUIRED | REJECTED | PROCESSING_ERROR
        "confidence": 0.9229, // qué tan bien se leyó ESTE documento (promedio)
        // Resumen compatible: confianza más repetida entre los campos con
        // valor. Cada campo conserva además su propia confidence; no se hereda.
        // Ausente si ningún valor se repite al menos tres veces.
        "fieldConfidence": 0.9826,
        // Si SOLO este documento falló (el resto del archivo sigue):
        // "status": "PROCESSING_ERROR", "error": "mensaje truncado a 300 caracteres",
        // "errorType": "ValueError" (nombre de la excepción Python)
        "fields": {
          "NUMERO_FACTURA": {
            "value": "SJ191703",
            "confidence": 0.9826,
            "valid": true,
          },
          "TOTAL": { "value": 14612.4, "confidence": 0.9335, "valid": true }, // excepción
          // Objeto anidado
          "DATOS_PAGO": {
            "fields": {
              "BANCO": {
                "value": "WELLS FARGO BANK, NA",
                "confidence": 0.93,
                "valid": true,
              },
            },
          },
          // Arreglo: TODOS los renglones, de TODOS los documentos
          "CONCEPTOS": {
            "items": [
              {
                "fields": {
                  "DESCRIPCION": {
                    "value": "CA Valencia...",
                    "confidence": 0.93,
                    "valid": true,
                  },
                  "IMPORTE": {
                    "value": 14612.4,
                    "confidence": 0.93,
                    "valid": true,
                  },
                },
              },
            ],
          },
        },
      },
      {
        "index": 2,
        "documentType": "FACTURA_FLETE_EXTRANJERO",
        "pageStart": 2,
        "pageEnd": 2,
        "...": "...",
      },
    ],

    // Reglas de nivel documento que REALMENTE corrieron: entre documentos en el
    // pipeline de expedientes, del documento único en el flujo estándar. Una
    // regla que no aplicaba a este archivo no aparece — la llave completa
    // (incluidas las saltadas) está en `fullResult.crossDocumentChecks`. Si no
    // corrió ninguna, `checks` no viene. Las reglas POR CAMPO viven en
    // `documents[i].fields[*].valid`/`reason`.
    "checks": [
      {
        "rule": "expected_importer_rfc",
        "valid": true,
        "severity": "warning",
        "message": "...",
      },
    ],
    "warnings": [],

    // Datos del ARCHIVO
    "processingId": "890c9bb0-...",
    "sourceFile": "factura.pdf", // el NOMBRE; los bytes con GET /documents/{id}/file
    "sourceFormat": "pdf", // "pdf" | "jpg"/"jpeg"/"png" | "xml" | "xlsx" | "csv" — para xml/xlsx/csv, `pages` solo trae totalPages
    "pages": {
      "totalPages": 2,
      "nativePages": 0,
      "ocrPages": 2,
      "emptyPages": 0,
    },
    "usage": {
      "segmentation": null,
      "extraction": {
        "modelId": "...",
        "usage": { "inputTokens": 18697, "outputTokens": 3952 },
      },
    },

    "resultLocation": { "bucket": "...", "key": "results/{id}.json" },
    "processedAt": "2026-07-27T13:19:04.231Z",
  },
}
```

</details>

### Un campo que no aparece

**Se omite un campo solo cuando el documento no lo traía Y no era obligatorio.**
Cualquier otra cosa viaja, con su explicación:

| Situación                                      | Cómo se ve                                                                                                                    |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| No estaba y era opcional                       | **el campo no aparece**                                                                                                       |
| No estaba y era obligatorio                    | `{ "value": null, "confidence": 0.0, "valid": false, "reason": "required field 'X' missing or empty" }`                       |
| Se leyó con confianza bajo el piso de descarte | `{ "value": null, "confidence": 0.31, "valid": false, "reason": "value discarded: confidence 0.31 below discard floor 0.5" }` |
| Se leyó pero no pasó una regla                 | `{ "value": "...", "confidence": 0.9, "valid": false, "reason": "..." }`                                                      |
| El pipeline lo derivó o lo recuperó del QR     | se agrega `"source": "NORMALIZED"` \| `"QR_RECOVERY"` \| ...                                                                  |

`reason` solo aparece cuando `valid` es falso, y `source` solo cuando **no** es
`MODEL_EXTRACTION`: repetir el default en cada uno de 66 campos era el 20% de la
respuesta.

`confidence` siempre viaja en cada escalar emitido, incluidas las celdas de
arrays: el nodo es autocontenido y no necesita heredar nada del documento. El
extractor suele reportar el mismo número para muchos campos de una página;
`documents[i].fieldConfidence` conserva ese valor más repetido únicamente como
resumen compatible. No sustituye ni modifica las confianzas individuales.

El flujo estándar legado no mide confianza por campo; para esos valores emite
`confidence: 0.0`, que significa explícitamente **no medido**, en vez de omitir
la llave o inventar certeza.

### `documents[i].authenticity`: ¿es una hoja real o un Word con la firma pegada?

Solo en tipos que lo verifican (hoy `CARTA_NOM35`; un tipo que no lo declara no
trae la llave). Resume si el archivo es la **captura de una hoja impresa, firmada
con tinta, sellada y escaneada**, o una composición digital:

```jsonc
"authenticity": {
  "verdict": "NO_AUTENTICO",        // AUTENTICO | SOSPECHOSO | NO_AUTENTICO | NO_EVALUADO
  "layers": { "structure": "FALLA", "pixels": "OK", "vision": "OK", "memory": "OK" },
  "reasons": [
    { "code": "TEXTO_NATIVO_CON_IMAGEN_PEGADA", "hard": true,
      "message": "El PDF tiene texto nativo y una imagen insertada: no es la captura de un papel" },
    { "code": "FIRMA_ES_IMAGEN_INSERTADA", "hard": true,
      "message": "FIRMA: hay una imagen incrustada en el PDF justo donde está el elemento" }
  ],
  "referencesFound": false          // ¿el proveedor tenía firmas/sellos registrados?
}
```

Cómo leerlo:

- **Lo que decide el estatus son los campos**, no este bloque: `CAPTURA_FISICA`,
  `FIRMA_PRESENTE`, `SELLO_PRESENTE`, `LOGO_PRESENTE`, `FIRMA_SIN_INSERCION`,
  `SELLO_CORRESPONDE_EMISOR`, `FIRMA_COINCIDE_REFERENCIA`,
  `FIRMA_SELLO_NO_REUTILIZADOS`… viajan en `fields` como cualquier otro, con su
  `valid` y su `reason`. `authenticity` es el resumen legible para mostrarle a una
  persona o devolverle al proveedor.
- **`verdict` es el hallazgo, no el estatus.** `NO_AUTENTICO` = evidencia
  determinista (`hard: true`); `SOSPECHOSO` = solo señales de juicio. Qué hace cada
  hallazgo con el documento lo decide el catálogo del tipo, y puede estar en modo
  observación: un `NO_AUTENTICO` puede venir con `validation.status:
  "REQUIERE_REVISION"`. **Ramifica por `validation.status`**, y usa `reasons[].code`
  para el porqué.
- **Un campo de autenticidad ausente significa «no se pudo determinar», no «no».**
  `FIRMA_COINCIDE_REFERENCIA` no aparece cuando el proveedor no tiene firmas
  registradas; `CAPTURA_FISICA` no aparece en una foto JPG (no hay estructura PDF
  que leer). Eso nunca rechaza.
- **`NO_EVALUADO` siempre va a revisión.** Si la verificación no pudo correr, el
  documento no se aprueba sin que alguien lo vea.
- Lo que sí rechaza por sí solo: que la **misma** firma o sello aparezca en el
  documento de **otra** persona (`*_REUTILIZADO_EN_OTRA_CARTA`). Una firma autógrafa
  nunca se repite exacta.

Para que `referencesFound` salga `true` y existan los campos `*_COINCIDE_REFERENCIA`,
el proveedor de `metadata.proveedor` debe tener sus firmas, sellos y logos
registrados **antes** de subir el documento: ver
[§8 — `/references`](#8-references--registrar-las-firmas-sellos-y-logos-válidos-de-una-entidad).

Catálogo completo de códigos y guía de operación:
`documentacion/AUTENTICIDAD_Y_REFERENCIAS.md` (repo interno).

### Confianza y status: qué manda a revisión

Un campo por debajo de `min_confidence` **siempre** sale con `valid: false` y su
`reason`. Que además mande el documento a `REQUIERE_REVISION` depende de si el
campo era **obligatorio**: el status refleja si el dato que exigiste es confiable,
y para un opcional bien leído marcar el campo es suficiente. Un valor por debajo
del piso de descarte sí manda a revisión siempre — ahí no se marcó un dato dudoso,
se tiró.

### Si la respuesta no cabe

El envelope viaja por Step Functions (256 KB de payload) y se guarda en DynamoDB
(400 KB por item). Si el archivo genera más de lo que cabe, aparece `omitted` y
los arreglos más grandes se cambian por su conteo — **de mayor a menor**, para
que una factura con tres conceptos no pierda sus renglones por culpa de un
pedimento con cuatrocientas partidas:

```jsonc
"documents": [ { "...": "...", "fields": { "PARTIDAS": { "count": 412, "omitted": "size" } } } ],
"omitted": { "reason": "size", "limitBytes": 180000, "bytes": 178402,
             "fields": ["documents[3].fields.PARTIDAS"] }
```

Nunca se pierde nada: el resultado completo se guarda en S3 **antes** de este
recorte. Pídelo con `?full=1` o léelo desde `resultLocation`. En los expedientes
reales medidos (hasta 20 documentos con todos sus renglones) el envelope no pasa
de ~34 KB, así que esto es una red de seguridad, no el caso común.

### Envelopes guardados antes de v2

`envelopeVersion` **ausente** significa un resultado guardado con la forma
anterior: `extractedEntities` en la raíz (que era el árbol del PRIMER documento
solamente) y `expediente.documents[]` con escalares pelones. El API los sigue
devolviendo tal cual se guardaron — no se reescriben — así que un cliente que
consulte documentos históricos debe checar `envelopeVersion` antes de asumir
`documents[]`.

**Cuando falla el documento COMPLETO** (no se pudo ni leer el archivo — `documents: []`), el envelope trae además un bloque `error` estructurado en la raíz, para triage sin tener que parsear el mensaje de `warnings`:

```jsonc
{
  "validation": { "status": "REQUIERE_REVISION", "reviewRequired": true },
  "documentCount": 0,
  "documents": [],
  "sourceFormat": "jpeg",
  "warnings": ["unreadable jpeg image: ..."], // mensaje libre, siempre presente (compatibilidad hacia atrás)
  "error": {
    "message": "unreadable jpeg image: ...",
    "stage": "page_load", // "page_load" | "parse" | "declared_type" — dónde falló
    "type": "ClientError", // nombre de la clase de excepción Python
    "sourceFormat": "jpeg",
  },
}
```

Query params:

- `full=1` → agrega **`fullResult`**: el detalle más profundo desde S3 (`documents[].fields[]` con `extractedValue`/`normalizedValue`/`confidence`/`status`/`checks`, más `value`/`valid`/`reason` (vista simple: valor resuelto, booleano y motivo si `valid` es falso)). El `result` del envelope trae las entidades y `liquidacionNormalizada`, pero el detalle por-campo solo está en `fullResult`.

Un trabajo creado por `POST /documents/{id}/reprocess` incluye además
`reprocessedFrom` con el id completo usado como fuente, tanto mientras está
pendiente como cuando ya terminó.

**Errores:** `404 NOT_FOUND` si no hay documento con ese id/prefijo, **o si existe pero la app que llama no tiene permiso de verlo** (ver "Seguridad — acceso por app" más abajo) — misma respuesta en ambos casos, a propósito: no confirma que un documento fuera de tu alcance exista.

**Ejemplo:**

```bash
curl -sS "$API/documents/PEDIMENTO-ADUANAL-4ff224be" | python3 -m json.tool
curl -sS "$API/documents/PEDIMENTO-ADUANAL-4ff224be?full=1"   # + detalle por campo de S3
```

Atajo del repo (equivalente, formateado): `pnpm resultado -- --id 4ff224be`.

---

## 3. `POST /documents/{id}/reprocess` — Reprocesar el archivo original

Inicia otro procesamiento a partir de un documento que ya fue subido. Recibe
el id viejo en la ruta, localiza su archivo original en S3 y crea un
`documentId` nuevo. El resultado viejo queda intacto: no se sobrescribe ni se
borra.

No hay una segunda subida del cliente. La Query Lambda hace un `CopyObject`
interno de `processed/{id}/` o `failed/{id}/` a una key nueva bajo `raw/`; ese
objeto dispara el mismo flujo S3 → SQS → Step Functions que un upload normal.
Por eso el tamaño del PDF no atraviesa API Gateway/Lambda y la respuesta no
trae `uploadUrl`.

Body opcional, en JSON o `application/x-www-form-urlencoded`:

| Campo            | Comportamiento                                                                                                                                                                                                                                                          |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `documentType`   | Uno o varios TYPE_ID separados por coma, con el mismo contrato `:N`/`:all` de §1. Si se **omite**, hereda los tipos explícitos del proceso anterior; si el anterior era automático, vuelve a ser automático. Enviar `null` o `""` fuerza auto aunque antes hubiera tipo |
| `occurrenceMode` | `single` o `multiple`; también acepta `occurrence_mode`. Pisa catálogo/default para los tipos sin sufijo; `:N`/`:all` sigue ganando                                                                                                                                     |
| `modelId`        | Override sólo para la ejecución nueva y sólo efectivo en dev. Si se omite, se usa la configuración actual, no el override viejo                                                                                                                                         |

La metadata vigente del documento anterior se copia al trabajo nuevo. La app
que llama debe poder ver el documento fuente según `AccessScope` y
`AllowedDocTypes`; fuera de alcance responde `404`, igual que el GET. Si pide
explícitamente un tipo fuera de sus `AllowedDocTypes`, responde `403`. Una API
key válida pero todavía no registrada conserva las lecturas legacy, pero no
puede iniciar reprocesos; primero hay que adoptarla/registrarla.

**Reprocesar con tipos nuevos:**

```bash
RESP=$(curl -sS -X POST "$API/documents/$DOC_ID/reprocess" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "documentType": "PEDIMENTO_ERP:1,FACTURA_FLETE_EXTRANJERO:all",
    "occurrenceMode": "multiple"
  }')

NEW_DOC_ID=$(echo "$RESP" | jq -r .documentId)
curl -sS "$API/documents/$NEW_DOC_ID" -H "x-api-key: $KEY"
```

**Repetir con la declaración anterior:** body vacío u `{}`.

```bash
curl -sS -X POST "$API/documents/$DOC_ID/reprocess" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Respuesta `202`:**

```json
{
  "documentId": "8aa31cde-...",
  "reprocessedFrom": "ca22771e-72d8-403a-ad81-7f71617af908",
  "documentType": "PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO",
  "occurrenceMode": "multiple",
  "documentTypeCounts": {
    "PEDIMENTO_ERP": 1,
    "FACTURA_FLETE_EXTRANJERO": "all"
  },
  "classificationMode": "guided",
  "processingStatus": "QUEUED",
  "statusUrl": "/documents/8aa31cde-...",
  "retryAfterSeconds": 1
}
```

La cabecera `Location` repite `statusUrl` y `Retry-After` indica la espera
mínima antes de consultar. Cada POST crea un id nuevo; no es una actualización
del id anterior.

Errores:

- `400 INVALID_JSON|INVALID_BODY|INVALID_DOCUMENT_TYPE|INVALID_OCCURRENCE_MODE|INVALID_FORMAT|DOCUMENT_TYPE_REQUIRED` — body o combinación no válida;
- `403 FORBIDDEN|FORBIDDEN_DOCUMENT_TYPE` — key no registrada o tipo fuera del alcance;
- `404 NOT_FOUND` — no existe o la app no puede ver el documento viejo;
- `404 FILE_NOT_FOUND` — el registro existe pero el original ya no está en `processed/`/`failed/`;
- `409 SOURCE_NOT_READY` — todavía sigue bajo `raw/`; espera a que el GET termine;
- `429 DAILY_DOCUMENT_LIMIT` — la app ya usó su tope de documentos del día (un reproceso cuenta igual que una subida); no reintentes antes de `resetsAt` (ver "Límites y `429`");
- `500 JOB_REGISTRATION_FAILED|REPROCESS_COPY_FAILED` — no pudo reservarse el trabajo o copiarse el objeto. Si falla la copia, el `documentId` nuevo queda en `FAILED` para auditoría.

---

## 4. `PATCH /documents/{id}/metadata` — Estados y marcas propios de la app

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
- `app-origin`, `model-id`, `extraction-mode`, `ai-response-format` y `document-counts` son reservadas y no se pueden modificar.

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

## 5. `GET /documents/{id}/file` — Descargar el archivo original

Devuelve una **URL prefirmada de S3** (no los bytes) para descargar el archivo tal cual se subió (el PDF/imagen/XML/XLSX/CSV original, no el resultado de la extracción). `{id}` acepta id completo o prefijo, igual que `GET /documents/{id}`.

**Por qué una URL prefirmada y no los bytes directamente:** los expedientes de este sistema llegan a decenas de MB (un expediente digital real de referencia tiene 86 páginas) y **API Gateway topa cualquier respuesta a 10 MB** — límite duro de AWS, no configurable — sin contar el límite de invocación síncrona de Lambda (6 MB). Proxyear el archivo por la Lambda simplemente no es viable para el rango de tamaños real de este sistema. En vez de eso, el endpoint firma una URL de `GetObjectCommand` (mismo mecanismo `@aws-sdk/s3-request-presigner` que ya usa `POST /documents` en modo prefirmado — ver §1b) y el cliente descarga **directo de S3**, sin pasar por Lambda ni por API Gateway. Sin tope de tamaño real.

**Cómo se localiza el archivo:** el key de S3 del original **no es estático** — mientras se procesa vive en `raw/{TIPO?}/{documentId}/{archivo}`, pero al terminar la ejecución la Step Function propia lo mueve a `processed/{documentId}/{archivo}` (caso normal, incluida una validación RECHAZADA) o a `failed/{documentId}/{archivo}` (solo si la propia ejecución revienta). Como este endpoint solo responde para documentos que `GET /documents/{id}` ya puede resolver (es decir, ya salieron de `raw/`), el handler busca primero en `processed/{documentId}/` y si no hay nada, en `failed/{documentId}/` — un `ListObjectsV2` de una sola key por prefijo, no una reconstrucción a ciegas del nombre de archivo.

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

1. **La `x-api-key` SÍ filtra por dueño del documento, por default.** `query-lambda` resuelve la app que llama (por su `x-api-key`, contra `document-validation-apps[-dev]`) y, salvo que esa app tenga `AccessScope=all` configurado explícitamente, solo puede ver/descargar/reprocesar documentos cuyo `tracking.appOrigin` sea ella misma — cualquier otro id existe pero responde `404` (ver "Seguridad — acceso por app" en la sección 6, más abajo, para el detalle completo del modelo). Esto es el default de **cualquier app registrada desde ahora** (`pnpm apps:register`); dos casos quedan sin restringir a propósito — sin `x-api-key` (dev abierto) y una key válida nunca registrada aquí (`idp-default` antes de `apps:adopt`, el caso "sistema preexistente") — ver `AMBIENTES_Y_SEGURIDAD.md`. Si necesitas que una app vea todo (ej. un portal admin interno), es explícito: `pnpm apps:reconfigure -- --name X --access-scope all`.
2. **La URL prefirmada es en sí misma una credencial de acceso**, independiente de la `x-api-key` que la generó: cualquiera que la obtenga (log de proxy, historial de navegador, un `Referer` filtrado, copiar-pegar a un chat) puede descargar el archivo **sin volver a pasar por autenticación** hasta que expira. Por eso el default es corto (15 min, tope 1h) — no subir `expiresIn` más de lo necesario, y tratar la URL resultante como un secreto de vida corta, no como un link para reenviar. Esto es independiente del punto 1: la restricción por app decide **quién puede pedir** la URL; una vez emitida, cualquiera que la tenga puede usarla hasta que expire.

**Tamaño de archivo — resumen:** sin límite real. La única cota que aplica es la del **upload** original (§1): ~4 MB si se subió inline, sin tope práctico si se subió vía URL prefirmada (S3 acepta objetos hasta 5 TB, `PUT` de una sola parte hasta 5 GB). La descarga hereda ese mismo tamaño sin ningún límite adicional propio, porque nunca pasa por Lambda/API Gateway.

---

## 6. `GET /documents` — Buscar por tipo, sistema, metadata y campos extraídos

Cuatro filtros, **todos combinables entre sí y todos en AND**: un documento
tiene que cumplirlos todos para salir en `matches`. Basta con mandar uno.

| Filtro                | Qué acota                                  | De dónde sale el dato                                                                                                                                                                                         |
| --------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`docType`**         | El **tipo de documento**                   | Columna indexada `DocumentType`. Se resuelve **dentro de DynamoDB** contra el GSI `DocumentTypeIndex` (partición `DocumentType`, orden `ProcessedAt`): solo se leen los documentos de ese tipo, **sin Scan**. |
| **`system`**          | Qué **sistema lo subió**                   | `tracking.appOrigin`, que sale de la **API key** con la que se hizo el `POST /documents` — no de nada que mande el cliente en el body. Alias: `appOrigin`, `sistema`.                                         |
| **`meta.<clave>`**    | La **metadata actual** del documento       | El objeto del `POST /documents` (§1), incluyendo cambios posteriores hechos por `PATCH /documents/{id}/metadata` (§4), sin interpretarlo.                                                                     |
| **`field` + `value`** | Cualquier **campo extraído** del documento | El envelope del resultado, por dot-path o por búsqueda profunda. **Se puede repetir** para exigir varios campos a la vez.                                                                                     |

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
  const qs = new URLSearchParams(pares); // encodea espacios y acentos
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
  ["system", "ERP"], // quién lo subió, por su API key
  ["meta.lote", "A123"], // metadata del POST /documents
  ["meta.sucursal", "CHIH"],
  ["field", "PROVEEDOR_ID_FISCAL"],
  ["value", "OFU910626UQ0"],
  ["summary", "1"],
]);
for (const m of delLote.matches)
  console.log(m.documentId, m.appOrigin, m.metadata, m.matched);

// Camino rápido: campo declarado `unique_key` → una sola Query.
// El valor se normaliza igual al escribir y al buscar, así que da lo mismo
// mandarlo con espacios ("26 07 0823 6001630") o sin ellos.
const porNumero = await buscar([
  ["docType", "PEDIMENTO_ERP"],
  ["field", "No_Pedimento"],
  ["value", "26 07 0823 6001630"],
]);
console.log(porNumero.lookup, porNumero.count); // "uniqueKey" 1

// Dos campos extraídos a la vez: se emparejan POR POSICIÓN y son AND.
const porRfcYPatente = await buscar([
  ["docType", "PEDIMENTO_ERP"],
  ["field", "PROVEEDOR_ID_FISCAL"],
  ["value", "OFU910626UQ0"],
  ["field", "PATENTE"],
  ["value", "3456"],
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
| `field` | sí\* | — | **Basta el NOMBRE del campo** (`No_Pedimento`); también se acepta un dot-path. Si el path no resuelve en la raíz, hace **búsqueda profunda** por su última clave (encuentra el campo aunque esté anidado en cualquier documento, **incluido dentro de una fila de un array** — `field=Factura&value=4099` encuentra la factura sin importar en qué renglón de `FACTURAS` esté). **Se puede repetir** para exigir varios campos (AND). |
| `value` | con `field` | — | Valor a comparar (case-insensitive, se recortan espacios). Uno por cada `field`, **en el mismo orden**: se emparejan por posición. |
| `op` | no | `eq` | `eq` (igualdad exacta) o `contains` (subcadena) para los `field`/`value`. Una vez (aplica a todos) o una vez por filtro. |
| `status` | no | — | Filtra por estado de validación: `APROBADO`, `RECHAZADO`, `REQUIERE_REVISION`. Se aplica en DynamoDB (solo con `docType`). |
| `since` / `until` | no | — | Rango de `processedAt` (ISO-8601, p. ej. `2026-07-01`). Se aplica como condición de la clave de orden del índice (solo con `docType`). |
| `order` | no | `desc` | `desc` = más reciente primero, `asc` = más antiguo primero (solo con `docType`). |
| `expand` | no | `true` | `0`/`false` desactiva la búsqueda dentro de expedientes (ver abajo). |
| `limit` | no | `25` | Máximo de resultados (tope `100`). |
| `summary` | no | `false` | `1`/`true` devuelve solo un **resumen ligero** por match (id, clasificación, `documentCount`, status, y los campos y conteos del PRIMER documento) en vez del envelope completo. Útil para listar. |
| `full` | no | `false` | `1`/`true` agrega `fullResult` por match: el detalle **más profundo** desde S3 (`fields[]` con extractedValue/normalizedValue/confidence/status/checks, más value/valid/reason (vista simple), contenido completo de arrays). Cuesta un GET de S3 por match. |

\* Se requiere **al menos uno** de `docType`, `system`, `meta.<clave>` o `field`+`value`. Sin ninguno: `400 MISSING_PARAMS`.

**Errores de forma de los filtros:**

| Qué mandaste                                    | Respuesta                                                                                            |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Ningún filtro                                   | `400 MISSING_PARAMS`                                                                                 |
| `field` sin `value`                             | `400 MISSING_PARAMS` — `'field' requires 'value'`                                                    |
| Distinto número de `field` y de `value`         | `400 MISSING_PARAMS` — se emparejan por posición, una lista desbalanceada es ambigua y no se adivina |
| Varios `op`, pero ni uno solo ni uno por filtro | `400 INVALID_PARAMS`                                                                                 |
| Un `docType` fuera de tu `AllowedDocTypes`      | `403 FORBIDDEN`                                                                                      |
| `field`/`value` sin `AllowFieldSearch`          | `403 FORBIDDEN` (`system`/`meta.<clave>` **no** requieren ese permiso)                               |

**Expedientes multi-documento (`expand`):** un archivo con documentos de tipos **distintos** se guarda con el tipo de nivel superior `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO`, pero contiene pedimentos, facturas, etc. (si TODOS son del mismo tipo, el archivo se guarda con ese tipo y la consulta directa ya lo encuentra). Por eso `docType=FACTURA` devuelve **también** los expedientes que contienen una factura: además de la consulta por tipo se lanza otra sobre los expedientes filtrando por la columna `ContainedDocumentTypes` (también del lado de DynamoDB). Con `expand=0` solo se considera el tipo de nivel superior.

**Por defecto** cada match es el **envelope completo** (mismo shape que `GET /documents/{id}`): `documentId`, `documentClassification`, `documentCount`, `validation`, `documents[...]` (todos los documentos con sus campos, sus renglones y su `liquidacionNormalizada`), `tracking`, `metadata` (la del upload, si la hubo), más `matched[]` cuando hubo filtros por campo.

**Respuesta `200`** — cada match es el **envelope completo** + dónde coincidió:

```jsonc
{
  "docTypes": ["PEDIMENTO_ERP"], // tipos ya resueltos a TYPE_ID
  "resolvedTypes": [
    // cómo se resolvió lo que mandaste
    {
      "typeId": "PEDIMENTO_ERP",
      "input": "Pedimento (mapeo ERP)",
      "resolvedBy": "exact",
    },
  ],
  "expandedContainers": true, // se buscó también dentro de expedientes

  // --- eco de los filtros que se aplicaron ---
  "filters": [
    // todos los field/value, en orden
    { "field": "No_Pedimento", "value": "260708236001187", "op": "eq" },
  ],
  "field": "No_Pedimento", // = filters[0],
  "value": "260708236001187", // por compatibilidad con cuando solo
  "op": "eq", // podía haber un filtro
  "system": ["ERP"], // solo si mandaste system=
  "metaFilters": [
    // solo si mandaste meta.<clave>=
    { "key": "lote", "values": ["A123"] },
  ],
  "metaOp": "eq",

  "order": "desc",
  "summary": false,
  "full": false,
  "indexed": true, // true = Query indexada; false = Scan
  "lookup": "uniqueKey", // qué camino contestó: "uniqueKey" (una
  // Query a TIPODOC#valor), "typeIndex"
  // (Query al GSI + filtro en la Lambda)
  // o "scan" (sin docType)
  "restrictedToApp": "CEDIS", // solo si la app que llama tiene AccessScope=own (ver abajo)
  "restrictedToDocTypes": ["FACTURA_HONORARIOS_XML"], // solo si tiene AllowedDocTypes configurado
  "count": 1,
  "scanned": 2, // cuántos documentos se revisaron
  "matches": [
    {
      "documentId": "PEDIMENTO-ADUANAL-4ff224be-...",
      "documentClassification": "PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO", // qué trae
      "documentType": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO", // columna indexada (ver nota)
      "documentTypeName": "Expediente Aduanal Multidocumento",
      "documentName": "expediente-2026.pdf",
      "validationStatus": "REQUIERE_REVISION",
      "documentCount": 23,
      "validation": { "status": "REQUIERE_REVISION", "...": "..." },
      "documents": [
        /* TODOS los documentos, con sus campos y sus renglones */
      ],
      "tracking": { "appOrigin": "ERP", "...": "..." }, // qué sistema lo subió
      "metadata": { "lote": "A123", "sucursal": "chih" }, // la del POST /documents
      "matched": [
        // uno por cada filtro field/value
        {
          "field": "No_Pedimento",
          "matchedValue": "260708236001187",
          "matchedAt": "*.No_Pedimento",
        }, // "*." = búsqueda profunda
      ],
      "matchedValue": "260708236001187", // = matched[0], por
      "matchedAt": "*.No_Pedimento", // compatibilidad
      "fullResult": {
        /* SOLO con full=1: detalle por-campo de S3 */
      },
    },
  ],
}
```

> **Completitud:** por defecto cada match trae el envelope completo (con `documents[...]`). Con **`summary=1`** trae solo un resumen ligero — `documentCount`, los campos del PRIMER documento y sus conteos de renglones — que **incluye `appOrigin` y `metadata`**, para no tener que volver a pedir el documento solo para ver por qué entró. Con **`full=1`** agrega `fullResult` (el detalle por-campo `fields[]` desde S3).

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

## 7. `GET /document-types` — Catálogo de tipos de documento

Qué tipos existen, para armar un selector o generar un cliente sin tener que
leer los JSON activos de `catalogs/base/` y `catalogs/custom/` del repo. Dos formas:

| Petición | Para qué |
| --- | --- |
| `GET /document-types` | Lista paginada (**5 por página, fijo**), ordenada por `type`. |
| `GET /document-types/{type}` | Un solo tipo, por su TYPE_ID exacto (case-insensitive). |

Cada tipo trae `type` (el mismo valor que usas en `documentType`/`docType`),
`displayName` y `description`. El **schema** — el árbol completo de campos
que la extracción llena, con su `type`, si es `required` y su `hint` (dónde
vive el dato en el documento) — **se omite por default** y solo aparece con
`includeSchema=1`: un tipo como `PEDIMENTO_ADUANAL` tiene decenas de campos
anidados, así que una página sin él es liviana a propósito.

Refleja el catálogo **hot-reload**: un tipo que acabas de sincronizar con
`pnpm sync:one` aparece aquí de inmediato, sin esperar un redeploy — misma
fuente que usa el pipeline para clasificar y extraer.

<details open>
<summary><b>curl — listar</b></summary>

```bash
# Página 1, sin schema (liviano — solo type/displayName/description).
curl -sS "$API/document-types" -H "x-api-key: $KEY" | jq .

# Página 2, con el schema completo de cada tipo de esa página.
curl -sS -G "$API/document-types" -H "x-api-key: $KEY" \
  --data-urlencode "page=2" --data-urlencode "includeSchema=1" | jq .
```

```jsonc
{
  "page": 1,
  "pageSize": 5,
  "total": 9,
  "totalPages": 2,
  "types": [
    {
      "type": "PEDIMENTO_ERP",
      "displayName": "Pedimento (mapeo ERP)",
      "description": "Pedimento aduanal extraído con los nombres de campo de la tabla del ERP, listo para insertar sin traducción intermedia."
    }
  ]
}
```

</details>

<details>
<summary><b>curl — un tipo por su key, con schema</b></summary>

```bash
curl -sS -G "$API/document-types/PEDIMENTO_ERP" -H "x-api-key: $KEY" \
  --data-urlencode "includeSchema=1" | jq .
```

```jsonc
{
  "type": "PEDIMENTO_ERP",
  "displayName": "Pedimento (mapeo ERP)",
  "description": "Pedimento aduanal extraído con los nombres de campo de la tabla del ERP...",
  "schema": {
    "No_Pedimento": {
      "type": "string",
      "required": true,
      "hint": "Número de pedimento de 15 dígitos, en el encabezado."
    }
    // ... el resto de los campos del tipo, mismo shape que el JSON activo → "fields"
  }
}
```

`404 DOCUMENT_TYPE_NOT_FOUND` si el TYPE_ID no existe, o existe pero está
fuera del `AllowedDocTypes` de tu app — sin distinción entre ambos casos,
mismo principio que `GET /documents/{id}` con un id fuera de alcance (§2).

</details>

Sin `AllowedDocTypes` configurado en tu app ves el catálogo completo; con él,
tanto la lista como la búsqueda por key se acotan a esos tipos — igual que
`GET /documents` sin `docType` (§6).

---

## 8. `/references` — Registrar las firmas, sellos y logos válidos de una entidad

Los tipos con verificación de autenticidad (hoy `CARTA_NOM35`) comparan la firma,
el sello y el logotipo de cada documento contra los que el emisor tiene
**registrados con anticipación**. Estas rutas son ese registro: dar de alta,
consultar y dar de baja las **referencias** de una entidad.

| Petición | Para qué |
| --- | --- |
| `POST /references/{scope}/{key}` | Paso 1: reservar la referencia y obtener la URL de carga. |
| `PUT {uploadUrl}` | Paso 2: subir el recorte a S3 (no pasa por el API). |
| `POST /references/{scope}/{key}/{kind}/{refId}/confirm` | Paso 3: revisar el recorte y activarlo. Responde en el momento. |
| `GET /references/{scope}/{key}` | Listar las referencias de la entidad. |
| `GET /references/{scope}/{key}/{kind}/{refId}` | Una referencia, con URL temporal de su imagen. |
| `DELETE /references/{scope}/{key}/{kind}/{refId}` | Darla de baja (desactivar). |

Qué es cada parte de la ruta:

- **Una referencia es de la ENTIDAD, no de un tipo de documento.** La entidad es
  `scope` + `key` — p. ej. `PROVEEDOR` + el número de proveedor (`10040`). El
  logo de un proveedor se registra una vez y lo usa todo tipo de documento que
  declare ese `scope`.
- **`scope`** — un ámbito declarado por algún tipo activo del catálogo (en su
  `authenticity.references.scope`). Hoy solo existe **`PROVEEDOR`**
  (`CARTA_NOM35`). Otro valor → `400 INVALID_SCOPE`, con la lista `valid`.
- **`key`** — el identificador de la entidad dentro del `scope`. Solo letras,
  dígitos, `.`, `-` y `_`. **Una clave que cambiaría al normalizarse se
  rechaza**, no se reescribe en silencio: `400 INVALID_REFERENCE_KEY` trae en
  `suggested` la forma que sí funcionaría. Es el mismo valor que después mandas
  en `metadata.proveedor` al subir el documento (§1).
- **`kind`** — el elemento: `FIRMA`, `SELLO` o `LOGO` (los que declaran los
  tipos de ese `scope`). Otro valor → `400 INVALID_KIND`, con la lista `valid`.
- **`refId`** — lo genera la reserva a partir de `label` (`"Lic. Julio Lemus"` →
  `lic-julio-lemus`). Si ya existe una referencia con ese id, sale con sufijo
  (`-2`, `-3`…).

### Permiso: `AllowReferenceWrite`

Registrar una referencia es declarar "este sello es válido", y listarlas expone
firmas de personas. Por eso **todas** las rutas `/references` — también los dos
`GET` — exigen que la app tenga el permiso `AllowReferenceWrite`:

- **Apagado por default.** Sin él, cualquier ruta responde `403 FORBIDDEN`.
- **Falla cerrado:** una key válida pero no registrada como app, o un fallo al
  consultar los permisos, también es `403` — nunca pasa "por si acaso".
- En **dev sin API keys obligatorias** (`DEV_API_KEY_REQUIRED` apagado) queda
  abierto, como el resto de ese stack.
- Lo concede quien administra la API:
  `pnpm apps:grant -- --name <app> --env dev --reference-write` (ver
  `AMBIENTES_Y_SEGURIDAD.md` → "Acceso por app").

```jsonc
// 403 — la app no tiene AllowReferenceWrite
{
  "error": "FORBIDDEN",
  "message": "This app is not allowed to manage authenticity references. Ask for --reference-write if it's needed."
}
```

### Alta en tres pasos

Igual que `POST /documents`: la imagen va directo a S3 con una URL prefirmada,
nunca por el API.

```bash
API="https://xxxx.execute-api.us-east-1.amazonaws.com/prod"
KEY="tu-api-key"

# --- Paso 1: reservar. `kind` y `label` son obligatorios; `fileName` es
#     opcional (solo queda anotado como procedencia).
RESP=$(curl -sS -X POST "$API/references/PROVEEDOR/10040" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $KEY" \
  -d '{"kind":"FIRMA","label":"Lic. Julio Lemus","fileName":"firma-julio.png"}')

UPLOAD_URL=$(echo "$RESP"  | jq -r .uploadUrl)
CONFIRM_URL=$(echo "$RESP" | jq -r .confirmUrl)   # ruta relativa, empieza con /references/

# --- Paso 2: subir el recorte. SIN x-api-key y SIN ninguna cabecera extra,
#     igual que el PUT de un documento.
curl -sS --fail -X PUT --upload-file firma-julio.png "$UPLOAD_URL"

# --- Paso 3: confirmar. Revisa el recorte y responde en el momento.
curl -sS -X POST "$API$CONFIRM_URL" -H "x-api-key: $KEY" | jq .
```

Respuesta del **paso 1** (`200`):

```jsonc
{
  "scope": "PROVEEDOR",
  "key": "10040",
  "kind": "FIRMA",
  "refId": "lic-julio-lemus",
  "label": "Lic. Julio Lemus",
  "status": "PENDING",
  "uploadUrl": "https://...s3.us-east-1.amazonaws.com/references-pending/PROVEEDOR/10040/FIRMA/lic-julio-lemus?X-Amz-Algorithm=...&X-Amz-Signature=...",
  "uploadMethod": "PUT",
  "uploadExpiresInSeconds": 900,
  "uploadExpiresAt": "2026-10-06T18:15:00.000000+00:00",
  "confirmUrl": "/references/PROVEEDOR/10040/FIRMA/lic-julio-lemus/confirm"
}
```

Respuesta del **paso 3** (`200`) — la **ficha** de la referencia, ya activa, más
`warnings`:

```jsonc
{
  "scope": "PROVEEDOR",
  "key": "10040",
  "kind": "FIRMA",
  "refId": "lic-julio-lemus",
  "label": "Lic. Julio Lemus",
  "status": "ACTIVE",                // ACTIVE | INACTIVE | PENDING
  "format": "png",                   // png | jpeg — sale de los bytes, no de la extensión
  "width": 300,
  "height": 150,
  "sha256": "9f2c…",
  "hasInkFingerprint": true,         // false en elementos sin huella de tinta (p. ej. un logo)
  "indexed": true,                   // ya está en el índice de búsqueda por similitud
  "createdAt": "2026-10-06T18:00:00.000000+00:00",
  "createdBy": "ERP",                // la app que la reservó
  "activatedAt": "2026-10-06T18:00:07.000000+00:00",
  "warnings": []                     // [{code, message}] — avisos que no impiden el alta
}
```

Notas del alta:

- **La ficha nunca trae la huella de tinta ni el embedding** — son justo lo que
  permite detectar la reutilización de una firma.
- **Desde cuándo cuenta:** una vez confirmada, la referencia se usa en los
  documentos que se procesen **después** con `metadata.proveedor = key`. Los ya
  procesados no cambian (para eso, `POST /documents/{id}/reprocess`, §3).
- **La reserva vence en 24 h** y la `uploadUrl` en 15 min (`900` s). Una reserva
  sin confirmar no aparece en el listado ni se usa para comparar. Repetir el
  paso 1 con el mismo `kind` y `label` mientras sigue pendiente **reutiliza** el
  mismo `refId` y entrega una URL nueva: reintentar no deja basura.
- **Confirmar es idempotente:** confirmar una referencia ya activa responde
  `200` con la misma ficha.
- **`indexed: false` no es un error:** la referencia quedó activa pero su
  entrada en el índice de similitud no se pudo escribir; volver a llamar a
  `confirm` lo reintenta.
- **Tope:** 20 referencias activas por entidad y elemento
  (`409 REFERENCE_LIMIT_REACHED` en el paso 1; da de baja alguna antes).

### Qué debe cumplir el recorte

Sube **solo el elemento** (la firma, el sello o el logo), recortado — no la hoja
completa. `confirm` lo revisa con el mismo código que usa el pipeline:

| Regla | Límite | Código en `errors[]` |
| --- | --- | --- |
| Formato | PNG o JPEG legible (por contenido, no por extensión) | `NOT_AN_IMAGE` |
| Peso | hasta 3.5 MB | `TOO_LARGE_BYTES` |
| Tamaño mínimo | lado menor ≥ 120 px (≥ 80 px en elementos sin huella de tinta, como un logo) | `TOO_SMALL` |
| Tamaño máximo | lado menor ≤ 2200 px y no más de 12 megapíxeles | `TOO_LARGE_DIMENSIONS` |
| Tinta | debe distinguirse tinta sobre el fondo (solo elementos con huella) | `NO_INK` |

Avisos (`warnings[]`, no impiden el alta): `TOO_MUCH_INK` — más del 45 % de la
imagen es "tinta"; probablemente el recorte trae texto o fondo de más.

Si no pasa, `confirm` responde `422` y **descarta la imagen subida**. La reserva
sigue pendiente: sube un recorte corregido (a la misma `uploadUrl` si no ha
expirado, o repite el paso 1 para obtener otra) y confirma de nuevo.

```jsonc
// 422 — el recorte no pasó la revisión
{
  "error": "REFERENCE_REJECTED",
  "message": "El recorte no pasó la revisión de calidad.",
  "errors": [
    { "code": "TOO_SMALL", "message": "muy chica (90x60); escanea a 200-300 dpi" }
  ],
  "warnings": []
}
```

### Duplicados

```jsonc
// 409 — exactamente el mismo archivo ya está registrado para esta entidad.
// `force` NO lo salta.
{
  "error": "DUPLICATE_REFERENCE",
  "message": "Ese mismo archivo ya está registrado como FIRMA#julio.",
  "existing": { "kind": "FIRMA", "refId": "julio" }
}
```

```jsonc
// 409 — otra imagen del mismo trazo (p. ej. la misma firma re-codificada a JPG).
{
  "error": "SIMILAR_REFERENCE",
  "message": "Misma tinta (0.99) que FIRMA#julio: una muestra basta. Reenvía con `force: true` para registrarla igual.",
  "existing": { "kind": "FIRMA", "refId": "julio" }
}
```

Para registrarla de todos modos, confirma con `force`:

```bash
curl -sS -X POST "$API$CONFIRM_URL" \
  -H "Content-Type: application/json" -H "x-api-key: $KEY" \
  -d '{"force": true}'
```

### Consultar

```bash
# Las referencias activas del proveedor 10040.
curl -sS "$API/references/PROVEEDOR/10040" -H "x-api-key: $KEY" | jq .

# Solo sellos, incluyendo los dados de baja.
curl -sS -G "$API/references/PROVEEDOR/10040" -H "x-api-key: $KEY" \
  --data-urlencode "kind=SELLO" --data-urlencode "includeInactive=1" | jq .

# Una sola, con URL temporal de la imagen.
curl -sS "$API/references/PROVEEDOR/10040/FIRMA/lic-julio-lemus" -H "x-api-key: $KEY" | jq .
```

```jsonc
// GET /references/PROVEEDOR/10040
{
  "scope": "PROVEEDOR",
  "key": "10040",
  "count": 1,
  "references": [ { /* ficha, igual que la del paso 3 pero sin `warnings` */ } ]
}
```

- **Listado:** `kind` filtra por elemento; `includeInactive=1` (`1`/`true`/`yes`)
  agrega las dadas de baja. Las reservas pendientes **nunca** se listan. Una
  entidad sin referencias responde `200` con `count: 0`, no `404`.
- **Detalle:** la ficha más `imageUrl` (URL prefirmada de S3, se descarga sin
  `x-api-key`) e `imageUrlExpiresInSeconds` (`300`). `imageUrl` **solo viene en
  referencias activas**; una dada de baja o pendiente devuelve la ficha sin ella.
  `404 NOT_FOUND` si no existe o era una reserva ya vencida.

### Dar de baja

```bash
curl -sS -X DELETE "$API/references/PROVEEDOR/10040/FIRMA/lic-julio-lemus" \
  -H "x-api-key: $KEY" | jq .
# → la ficha con "status": "INACTIVE", "deactivatedAt" y "deactivatedBy"
```

`DELETE` **solo desactiva**: la referencia deja de compararse y sale del índice
de similitud, pero su registro y su imagen se conservan para auditoría (siguen
visibles con `includeInactive=1`). El borrado definitivo no está en el API: es
una operación de operador (`pnpm refs:remove --delete`). Una referencia dada de
baja no se reactiva; se registra de nuevo (`confirm` sobre ella →
`409 NOT_PENDING`).

### Errores

Mismo formato que el resto del API: `{ "error": "CODIGO", "message": "…" }`, con
campos extra según el código.

| HTTP | `error` | Cuándo | Extra |
| --- | --- | --- | --- |
| `400` | `INVALID_REFERENCE_KEY` | `key` vacía o con caracteres fuera de letras, dígitos, `.`, `-`, `_` | `suggested` (la forma válida, cuando existe) |
| `400` | `INVALID_SCOPE` | `scope` no declarado por ningún tipo activo | `valid[]` |
| `400` | `INVALID_KIND` | `kind` no es un elemento de ese `scope` | `valid[]` |
| `400` | `MISSING_FIELD` | Falta `kind` o `label` en la reserva | |
| `400` | `INVALID_JSON` | El body no es un objeto JSON válido | |
| `403` | `FORBIDDEN` | La app no tiene `AllowReferenceWrite` | |
| `404` | `NOT_FOUND` | La referencia no existe, o su reserva expiró | |
| `409` | `REFERENCE_LIMIT_REACHED` | Ya hay 20 activas de ese elemento para la entidad | |
| `409` | `REFERENCE_ID_EXHAUSTED` | Demasiadas referencias con la misma etiqueta; usa otro `label` | |
| `409` | `UPLOAD_NOT_FOUND` | `confirm` antes de subir la imagen (o la `uploadUrl` expiró) | |
| `409` | `DUPLICATE_REFERENCE` | El mismo archivo ya está registrado | `existing{kind,refId}` |
| `409` | `SIMILAR_REFERENCE` | Misma tinta que una ya registrada; se acepta con `force: true` | `existing{kind,refId}` |
| `409` | `NOT_PENDING` | `confirm` sobre una referencia dada de baja | |
| `422` | `REFERENCE_REJECTED` | El recorte no pasó la revisión | `errors[]`, `warnings[]` |
| `500` | `INTERNAL_ERROR` | Fallo interno | |

> **`{"message":"Missing Authentication Token"}` en `/references/...`** no es un
> problema de tu key: es la respuesta de API Gateway a una ruta que el stage aún
> no publica (despliegue incompleto del lado del servicio). Repórtalo a quien
> administra la API.

---

## Apéndice: dónde queda el resultado (y por qué a veces parece incompleto)

El resultado se guarda en **dos lugares con distinto nivel de detalle** — esto explica por qué una respuesta puede verse "incompleta":

- **DynamoDB** tabla `DocumentValidationStack-BedrockDocumentProcessingDocumentProcessingTable...` (PK `DocumentId`): el **envelope** en la columna `EnrichmentResult` (string JSON). Trae, por documento, sus campos y **sus renglones completos**. Lo que **no** trae es el detalle por campo (`fields[]` con extractedValue/normalizedValue/status/checks). Está acotado a propósito por presupuesto de bytes: el payload de Step Functions topa en 256 KB y un item de DynamoDB en 400 KB — si un archivo se pasara, los arreglos más grandes se cambian por su conteo y se anuncia en `omitted` (ver §2).
- **S3** `results/{documentId}.json`: el resultado **COMPLETO** — todos los campos con su detalle, el contenido de todos los arrays (PARTIDAS, CONTRIBUCIONES...), checks y confianzas.

### Columnas indexadas (para filtrar sin Scan)

Además del `EnrichmentResult`, cada item lleva columnas de primer nivel que escribe el enrichment Lambda (`enrichment-lambda/indexing.py`) al terminar de validar:

| Columna                                                     | Ejemplo                                                                                                                                                       | Para qué                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DocumentType`                                              | `PEDIMENTO_ERP`                                                                                                                                               | **Partición del GSI `DocumentTypeIndex`.** Es la clasificación FINAL (la del segmentador), no la de la clasificación previa de Bedrock.                                                                                                                                                                                                                                                                                                                                 |
| `ProcessedAt`                                               | `2026-07-24T11:30:00+00:00`                                                                                                                                   | **Orden del GSI**: resultados por fecha y rangos `since`/`until`.                                                                                                                                                                                                                                                                                                                                                                                                       |
| `DocumentTypeName`                                          | `Pedimento Aduanal`                                                                                                                                           | Nombre legible del tipo (del catálogo o derivado del id).                                                                                                                                                                                                                                                                                                                                                                                                               |
| `ValidationStatus`                                          | `REQUIERE_REVISION`                                                                                                                                           | Filtro `status=` evaluado en DynamoDB.                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `ContainedDocumentTypes`                                    | `["EXPEDIENTE_...", "PEDIMENTO_ERP", "FACTURA"]`                                                                                                              | Tipos **dentro** de un expediente: permite que `docType=FACTURA` encuentre la factura de un expediente.                                                                                                                                                                                                                                                                                                                                                                 |
| `DocumentName`                                              | `expediente-2026.pdf`                                                                                                                                         | Nombre del archivo original.                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `Tracking`                                                  | `{CreatedAt, UpdatedAt, UploadedAt, ProcessingStartedAt, ProcessingCompletedAt, ProcessingSeconds, ProcessingTime, UploadToResultSeconds, UploadToResultMs, Timings, Schema, AppOrigin, ModelIds, ModelFallbacks}` | **Map**, no filtrable por GSI (no es escalar) — promovido de `EnrichmentResult.tracking` para verlo en el item mismo (Scan/consola) sin parsear el JSON. `UploadToCompletion*` no se persiste aquí: el API lo calcula al leer combinando `UploadedAt` con el evento terminal `ProcessingCompletedAt`. `AppOrigin` es lo que filtra `?system=`. |
| `Metadata`                                                  | `{lote: "A123", estado_externo: "EN_REVISION"}`                                                                                                               | **Map mutable**: nace del `metadata` del `POST /documents` (§1), se hereda al reprocesar (§3) y `PATCH /documents/{id}/metadata` (§4) la puede crear o cambiar. Es la fuente actual que la API superpone al snapshot de `EnrichmentResult`/S3 y lo que filtra `?meta.<clave>=`.                                                                                                                                                                                         |
| `MetadataVersion`, `MetadataUpdatedAt`, `MetadataUpdatedBy` | `3`, fecha ISO, `ERP`                                                                                                                                         | Auditoría/concurrencia de la última edición de metadata; no forman parte de la metadata del usuario ni de sus filtros.                                                                                                                                                                                                                                                                                                                                                  |

Documentos procesados antes de esto no tienen las columnas: `pnpm backfill:index` las reconstruye desde `EnrichmentResult` (idempotente, `--dry-run` disponible).

Cómo lo devuelve cada endpoint:

| Llamada                                                  | De dónde lee                                                   | Nivel                                                             |
| -------------------------------------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------- |
| `GET /documents/{id}`                                    | DynamoDB (`result`)                                            | Envelope completo (con `documents[...]`)                          |
| `GET /documents/{id}?full=1`                             | DynamoDB + S3 (`fullResult`)                                   | Envelope + detalle por-campo `fields[]`                           |
| `POST /documents/{id}/reprocess`                         | DynamoDB + S3 (`HeadObject` + `CopyObject`)                    | Trabajo nuevo desde el original; no modifica el id viejo — ver §3 |
| `PATCH /documents/{id}/metadata`                         | DynamoDB (`UpdateItem` condicional)                            | Mapa completo actualizado; no toca S3 ni la validación — ver §4   |
| `GET /documents/{id}/file`                               | DynamoDB (resolver id) + S3 (`ListObjectsV2` + URL prefirmada) | El **archivo original**, no el resultado — ver §5                 |
| `GET /documents?docType=...`                             | DynamoDB (Query al GSI + BatchGet)                             | Envelope completo por match                                       |
| `GET /documents?docType=...&system=...&meta.<clave>=...` | DynamoDB (Query al GSI + BatchGet) + filtro en la Lambda       | Envelope completo por match; los dos filtros no añaden lecturas   |
| `GET /documents?field=...`                               | DynamoDB (Scan)                                                | Envelope completo por match                                       |
| `GET /documents?docType=...&field=<unique key>`          | DynamoDB (1 Query a `TIPODOC#valor` + BatchGet)                | Envelope completo por match                                       |
| `GET /documents?...&summary=1`                           | DynamoDB (Query o Scan)                                        | Resumen ligero por match                                          |
| `GET /documents?...&full=1`                              | DynamoDB + S3 por match                                        | Envelope + `fullResult` por match                                 |

> El envelope de DynamoDB ya trae, POR DOCUMENTO, sus campos, sus renglones y su `liquidacionNormalizada`. Lo único que **solo** está en S3 es el detalle por-campo (`documents[].fields[]` con `extractedValue`/`normalizedValue`/`confidence`/`status`/`checks`, más `value`/`valid`/`reason` (vista simple: valor resuelto, booleano y motivo si `valid` es falso)), que se obtiene con `full=1`.
