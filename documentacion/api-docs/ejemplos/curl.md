# Subir un documento por curl — paso a paso

Guía práctica para copiar y pegar. Ejemplo con `./test-docs/pages.pdf` como
`PEDIMENTO_ERP`.

> Referencia completa de la API (todos los endpoints, params y respuestas):
> [`API_USO.md`](../API_USO.md). Este archivo es solo el camino corto para subir un documento.

**Subir son SIEMPRE dos peticiones**, sin importar el tamaño del archivo. La
segunda no va a la API: va directo a S3.

```
1. POST $API/documents   → documentId + uploadUrl     (no lleva el archivo)
2. PUT  $UPLOAD_URL      → el archivo, directo a S3   (arranca el pipeline)
3. GET  $API/documents/$DOC_ID  → el resultado
```

---

## 0. Variables

Elige **un** bloque según el ambiente. La diferencia importante es que el
dominio propio de prod **no lleva `/prod` en la ruta** y la URL cruda de dev
**sí**.

```bash
# --- prod (default) — dominio propio: el stage NO va en la ruta
API="https://idp.alsuper.com"
KEY=$(grep -E '^API_KEY_APP=' .env.production | cut -d= -f2- | tr -d '"'"'"' ')
```

```bash
# --- dev — URL execute-api cruda: el stage SÍ va en la ruta
API="https://ls9rcym4d3.execute-api.us-east-1.amazonaws.com/prod"
KEY=$(grep -E '^API_KEY_APP=' .env.dev | cut -d= -f2- | tr -d '"'"'"' ')
```

```bash
# --- comunes
FILE="./test-docs/pages.pdf"
TYPE="PEDIMENTO_ERP"
```

Comprueba que quedaron bien antes de seguir:

```bash
echo "API=$API"
echo "KEY tiene ${#KEY} caracteres"    # 0 = no la leyó; revisa el .env
ls -la "$FILE"
```

---

## 1. Registrar el documento

Este POST **no lleva el archivo**: solo la metadata. Devuelve el `documentId`
con el que consultarás después, y la URL a la que subirás el archivo.

```bash
RESP=$(curl -sS -X POST "$API/documents" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $KEY" \
  -d "{\"fileName\":\"$(basename "$FILE")\",\"documentType\":\"$TYPE\",\"occurrenceMode\":\"multiple\"}")

echo "$RESP" | jq .
```

Guarda los dos valores que necesitas:

```bash
DOC_ID=$(echo "$RESP"     | jq -r .documentId)
UPLOAD_URL=$(echo "$RESP" | jq -r .uploadUrl)

echo "DOC_ID=$DOC_ID"
```

`documentType` es opcional. Si lo omites, el modelo clasifica y segmenta solo
(más lento y más caro). Si mandas **un** tipo, se honra sin re-clasificar. Si
mandas **varios separados por coma** (`-d "documentType=PEDIMENTO_ADUANAL,FACTURA_HONORARIOS"`),
el archivo sí se segmenta pero cada documento se clasifica sólo entre esos
tipos; `documents[]` omite lo demás. El campo `occurrenceMode` del POST controla
los tipos sin sufijo (`single` = primera, `multiple` = todas), y `:N`/`:all` lo
sobrescribe por tipo. Si se omite, usa `occurrence_mode` del catálogo y después
el default global (`single` con un tipo, `multiple` con varios). Un tipo pedido
que no aparezca se reporta con rango `null` y revisión.

---

## 2. Subir el archivo a S3

### Opción A — encadenado desde el paso 1

Si vienes del paso 1, `$UPLOAD_URL` ya está puesta:

```bash
curl -sS --fail -X PUT --upload-file "$FILE" "$UPLOAD_URL"
```

### Opción B — pegar la URL completa

Si ya tienes la URL (de otra terminal, de un log, de Postman), **pégala entera
en una sola variable y no toques nada más**. No hay que separar parámetros ni
reconstruir la firma: la URL ya lo trae todo.

```bash
UPLOAD_URL='https://documentvalidationstack-....s3.us-east-1.amazonaws.com/raw/PEDIMENTO_ERP/10c7f624-.../pedimento.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=...&X-Amz-Date=20260831T202053Z&X-Amz-Expires=900&X-Amz-Security-Token=...&X-Amz-Signature=...&X-Amz-SignedHeaders=host&x-amz-meta-app-origin=TEST&x-amz-meta-lote=A123&x-id=PutObject'

curl -sS --fail -X PUT --upload-file "$FILE" "$UPLOAD_URL"
```

> ⚠️ **Comillas SIMPLES (`'...'`) al pegar la URL.** Una URL prefirmada real
> lleva ~12 `&` y ronda los 1,900 caracteres. Sin comillas, el shell corta en el
> primer `&` y manda el resto a segundo plano — el `PUT` va a una URL truncada y
> S3 responde `403`. Las comillas simples también evitan que `$` o backticks
> dentro del token se expandan.
>
> Con `$UPLOAD_URL` en el `curl` van comillas **dobles**, para que la variable sí
> se expanda. Es la única diferencia entre las dos.

**Todo va dentro de la URL.** Fíjate en los últimos parámetros del ejemplo:
`x-amz-meta-app-origin=TEST` y `x-amz-meta-lote=A123` — la metadata del
documento (incluida la que mandaste en `metadata` en el paso 1) viaja en el
query string, ya firmada. Por eso el `PUT` no lleva ninguna cabecera.

Esa misma metadata es con la que después puedes reencontrar el documento sin
haber guardado su `documentId`: `GET /documents?docType=...&meta.lote=A123`
(ver [`API_USO.md`](../API_USO.md) §4). `app-origin` no lo mandaste tú — sale de tu API key, y
se filtra con `?system=`.

Qué NO lleva esta petición:

| | |
|---|---|
| `x-api-key` | **No.** Va a S3, no a la API. La autorización va firmada en la URL |
| Cabeceras `x-amz-*` | **No.** La metadata ya viaja dentro de la URL (ver "Errores" abajo) |
| `Content-Type` | Opcional. No es obligatorio ni se valida |
| Cuerpo | Los **bytes crudos**. No multipart, no form-data, no base64 |

**Éxito = `200` con cuerpo vacío.** S3 no devuelve JSON: si tu cliente intenta
parsear la respuesta, falla sobre algo que en realidad salió bien. Con
`--fail`, `curl` no imprime nada y sale con código 0.

La URL **caduca a los 15 minutos**. Si expira, repite el paso 1 — no se renueva.

En cuanto S3 recibe el archivo, el pipeline arranca solo
(`S3 → SQS → Step Functions → Textract/Bedrock → DynamoDB`).

---

## 3. Consultar el resultado

El procesamiento **no es instantáneo**: tarda segundos, y un expediente grande
más. Mientras corre, la consulta responde `202` con `processingStatus` y
`Retry-After`. El mismo endpoint acepta `waitSeconds=0..20`: mantiene abierta
la consulta hasta que haya resultado o venza ese plazo, sin crear otro canal.
`404` significa que el id no existe o la app no puede verlo.
Un `429 {"message":"Too Many Requests"}` es throttle y se espera igual que un
`202`; `429 {"message":"Limit Exceeded"}` es el techo diario agotado y no se
arregla reintentando (ver `API_USO.md` → "Límites y `429`").

```bash
delay=1
while :; do
  code=$(curl -sS -o result.json -w '%{http_code}' \
    "$API/documents/$DOC_ID?waitSeconds=20" -H "x-api-key: $KEY")
  [ "$code" = 200 ] && break
  if [ "$code" = 429 ] && grep -q "Limit Exceeded" result.json; then cat result.json; exit 1; fi
  [ "$code" = 202 ] || [ "$code" = 429 ] || { cat result.json; exit 1; }
  sleep $((delay + RANDOM % 3))
  delay=$((delay < 15 ? delay * 2 : 30))
done

jq '.result.validation.status' result.json
# `documents` siempre está, traiga el PDF uno o varios documentos.
jq '.result.documents[] | {index, documentType, pageStart, pageEnd}' result.json
```

Respuesta final abreviada (los documentos reales pueden traer decenas de
campos y renglones adicionales dentro de `fields`):

```jsonc
{
  "documentId": "e18d014a-...",
  "processingStatus": "FINISHED",
  "workflowStatus": "complete",
  "documentType": "FACTURA_FLETE_EXTRANJERO",
  "result": {
    "envelopeVersion": 2,
    "documentClassification": "FACTURA_FLETE_EXTRANJERO",
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

Los campos extraídos, por documento:

```bash
jq '.result.documents[].fields' result.json

# Solo el valor de cada campo, sin confianza ni validez:
jq '.result.documents[] | .fields | map_values(.value // (.items | length))' result.json
```

Un campo que **no aparece** significa que el documento no lo traía y no era
obligatorio. Todo lo demás viaja con su `valid: false` y su `reason`.

Qué hacer con cada estado:

| `validation.status` | Significa | Qué hacer |
|---|---|---|
| `APROBADO` | Todas las reglas pasaron | Consumir los campos |
| `REQUIERE_REVISION` | Confianza baja, campo no encontrado o documento truncado | Revisión humana: mira `warnings` y los campos con `valid: false` |
| `RECHAZADO` | Falló una regla dura | No consumir; revisar `checks` |

---

## 4. (Opcional) Recuperar el archivo original

Devuelve una **URL prefirmada**, no los bytes:

```bash
DL=$(curl -sS "$API/documents/$DOC_ID/file" -H "x-api-key: $KEY" | jq -r .downloadUrl)
curl -sS -o original.pdf "$DL"     # la descarga va a S3: sin x-api-key
```

---

## 5. (Aparte) Registrar una referencia de autenticidad

No es parte de subir un documento: se hace **antes**, una vez por firma, sello o
logo válido de un proveedor. Los tipos con verificación de autenticidad (hoy
`CARTA_NOM35`) comparan contra estas referencias los documentos que lleguen
después con `metadata.proveedor` igual a la clave registrada. Detalle completo:
[`API_USO.md`](../API_USO.md) §8.

Tu app necesita el permiso `AllowReferenceWrite` — **también para los `GET`**.
Sin él todo responde `403 FORBIDDEN`. Lo concede quien administra la API:

```bash
pnpm apps:grant -- --name <app> --env dev --reference-write
```

Son tres peticiones, con la misma mecánica que un documento:

```
1. POST $API/references/PROVEEDOR/$PROV         → refId + uploadUrl + confirmUrl
2. PUT  $REF_UPLOAD_URL                         → el recorte, directo a S3
3. POST $API$CONFIRM_URL                        → lo revisa y lo activa, en el momento
```

```bash
PROV="10040"                 # el mismo valor que mandarás en metadata.proveedor
CROP="./firma-julio.png"     # SOLO la firma recortada, no la hoja completa

# 1. Reservar. `kind` = FIRMA | SELLO | LOGO; `label` = quién firma o qué sucursal.
RESP=$(curl -sS -X POST "$API/references/PROVEEDOR/$PROV" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $KEY" \
  -d "{\"kind\":\"FIRMA\",\"label\":\"Lic. Julio Lemus\",\"fileName\":\"$(basename "$CROP")\"}")
echo "$RESP" | jq .

REF_UPLOAD_URL=$(echo "$RESP" | jq -r .uploadUrl)
CONFIRM_URL=$(echo "$RESP"    | jq -r .confirmUrl)   # /references/PROVEEDOR/10040/FIRMA/lic-julio-lemus/confirm
REF_ID=$(echo "$RESP"         | jq -r .refId)

# 2. Subir el recorte. Sin x-api-key y sin NINGUNA cabecera, igual que el paso 2
#    de un documento. La URL caduca a los 15 minutos.
curl -sS --fail -X PUT --upload-file "$CROP" "$REF_UPLOAD_URL"

# 3. Confirmar. 200 = quedó activa ("status": "ACTIVE").
curl -sS -X POST "$API$CONFIRM_URL" -H "x-api-key: $KEY" | jq .
```

Si el paso 3 responde `409 SIMILAR_REFERENCE` (la misma firma ya estaba
registrada con otra imagen) y aun así la quieres:

```bash
curl -sS -X POST "$API$CONFIRM_URL" \
  -H "Content-Type: application/json" -H "x-api-key: $KEY" \
  -d '{"force": true}' | jq .
```

Consultar y dar de baja:

```bash
# Lista (solo activas; agrega includeInactive=1 para ver las dadas de baja)
curl -sS "$API/references/PROVEEDOR/$PROV" -H "x-api-key: $KEY" | jq .

# Una, con URL temporal (5 min) para ver la imagen
curl -sS "$API/references/PROVEEDOR/$PROV/FIRMA/$REF_ID" -H "x-api-key: $KEY" | jq .

# Baja: solo la DESACTIVA (deja de compararse; el registro se conserva)
curl -sS -X DELETE "$API/references/PROVEEDOR/$PROV/FIRMA/$REF_ID" -H "x-api-key: $KEY" | jq .
```

Qué debe cumplir el recorte: PNG o JPEG, hasta 3.5 MB, lado menor de al menos
120 px (80 px en un logo) y no mayor a 2200 px, máximo 12 megapíxeles. Registrar
referencias **no gasta** documentos del día.

---

## Errores y qué significan de verdad

Los mensajes de API Gateway engañan. Esta tabla los traduce:

| Respuesta | Qué significa REALMENTE | Arreglo |
|---|---|---|
| `{"message":"Missing Authentication Token"}` | **No hay ninguna ruta con ese método + ese path.** Nunca es la API key | Revisa la URL: ¿pusiste `/prod` con el dominio propio? ¿método correcto? ¿typo en `/documents`? |
| `{"message":"Forbidden"}` | La ruta existe, pero falta la API key o es inválida | Manda `x-api-key`. En prod es obligatoria siempre |
| `403 AccessDenied` + `<HeadersNotSigned>` (XML) | Mandaste cabeceras `x-amz-*` en el `PUT` | Quítalas: la metadata ya va dentro de la URL |
| `403 SignatureDoesNotMatch` (XML) | La URL se alteró, o ya expiró | Repite el paso 1 |
| `400 INLINE_UPLOAD_REMOVED` | Mandaste `contentBase64` | El modo inline ya no existe: usa los dos pasos |
| `400 DOCUMENT_TYPE_REQUIRED` | Subiste `.xml`/`.xlsx`/`.csv` sin `documentType` | Esos formatos lo exigen: no hay clasificación automática |
| `400 INVALID_DOCUMENT_TYPE` | Un tipo no es UPPER_SNAKE_CASE, la lista pasa de 8 tipos / 200 chars, o mandaste varios en un `.xml`/`.xlsx`/`.csv` | Revisa la lista; en fuentes estructuradas sólo se acepta uno |
| `202` en el paso 3 | Todavía procesando | Respeta `Retry-After`; usa backoff con jitter |
| `404` en el paso 3 | No existe o no es visible | No lo reintentes como estado pendiente |
| `{"message":"Missing Authentication Token"}` en `/references/...` con la URL bien escrita | El stage de API Gateway todavía no publica esas rutas (despliegue incompleto del servicio) | No es tu key ni tu código: avisa a quien administra la API |
| `403 FORBIDDEN` en `/references/...` | Tu app no tiene `AllowReferenceWrite` (lo piden todas esas rutas, también los `GET`) | Pide el permiso: `pnpm apps:grant -- --name <app> --reference-write` |
| `400 INVALID_REFERENCE_KEY` | La clave trae espacios, acentos u otros caracteres fuera de letras, dígitos, `.`, `-`, `_` | Usa la que viene en `suggested` — y manda esa misma en `metadata.proveedor` |
| `400 INVALID_SCOPE` / `INVALID_KIND` | Ese ámbito o elemento no lo declara ningún tipo de documento | Usa uno de `valid` (hoy: `PROVEEDOR`; `FIRMA`, `SELLO`, `LOGO`) |
| `409 UPLOAD_NOT_FOUND` | Confirmaste sin haber hecho el `PUT`, o la `uploadUrl` expiró | Sube el recorte y confirma; si expiró, repite la reserva |
| `422 REFERENCE_REJECTED` | El recorte no pasó la revisión: mira `errors[].code` (`NOT_AN_IMAGE`, `TOO_LARGE_BYTES`, `TOO_SMALL`, `TOO_LARGE_DIMENSIONS`, `NO_INK`) | Corrige el recorte, súbelo de nuevo y confirma |
| `409 DUPLICATE_REFERENCE` | Ese mismo archivo ya está registrado (`existing` dice cuál) | Nada que hacer: ya existe |
| `409 SIMILAR_REFERENCE` | Misma tinta que una ya registrada (`existing` dice cuál) | Una muestra basta; si la quieres igual, confirma con `{"force": true}` |
| `409 REFERENCE_LIMIT_REACHED` | Ya hay 20 referencias activas de ese elemento para esa entidad | Da de baja alguna con `DELETE` |
| `409 NOT_PENDING` | Confirmaste una referencia dada de baja | Regístrala de nuevo desde la reserva |

**El `/prod` de más es el error más común.** Con el dominio propio la ruta es
`https://idp.alsuper.com/documents`; el `/prod` solo va en la URL cruda
`execute-api`.

---

## El atajo

Si no necesitas el detalle, el repo ya trae un script que hace los tres pasos:

```bash
pnpm enviar ./test-docs/pages.pdf PEDIMENTO_ERP              # dev
pnpm enviar ./test-docs/pages.pdf PEDIMENTO_ERP --env prod   # lee .env.production
```

En dev toma `API_KEY_APP` de `.env.dev` automáticamente cuando la protección
está activada. En prod toma `API_KEY` o `API_KEY_APP` de `.env.production`;
una `API_KEY` exportada siempre tiene prioridad.
