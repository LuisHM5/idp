# Subir un documento por curl — paso a paso

Guía práctica para copiar y pegar. Ejemplo con `./test-docs/pages.pdf` como
`PEDIMENTO_ERP`.

> Referencia completa de la API (todos los endpoints, params y respuestas):
> `API_USO.md`. Este archivo es solo el camino corto para subir un documento.

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
  -d "{\"fileName\":\"$(basename "$FILE")\",\"documentType\":\"$TYPE\"}")

echo "$RESP" | jq .
```

Guarda los dos valores que necesitas:

```bash
DOC_ID=$(echo "$RESP"     | jq -r .documentId)
UPLOAD_URL=$(echo "$RESP" | jq -r .uploadUrl)

echo "DOC_ID=$DOC_ID"
```

`documentType` es opcional. Si lo omites, el modelo clasifica y segmenta solo
(más lento y más caro). Si lo mandas, se honra sin re-clasificar.

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
(ver `API_USO.md` §4). `app-origin` no lo mandaste tú — sale de tu API key, y
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
`Retry-After`. `404` significa que el id no existe o la app no puede verlo.

```bash
delay=5
while :; do
  code=$(curl -sS -o result.json -w '%{http_code}' \
    "$API/documents/$DOC_ID" -H "x-api-key: $KEY")
  [ "$code" = 200 ] && break
  [ "$code" = 202 ] || { cat result.json; exit 1; }
  sleep $((delay + RANDOM % 3))
  delay=$((delay < 15 ? delay * 2 : 30))
done

jq '.validation.status' result.json
# `documents` siempre está, traiga el PDF uno o varios documentos.
jq '.documents[] | {index, documentType, pageStart, pageEnd}' result.json
```

Los campos extraídos, por documento:

```bash
jq '.documents[].fields' result.json

# Solo el valor de cada campo, sin confianza ni validez:
jq '.documents[] | .fields | map_values(.value // (.items | length))' result.json
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
| `202` en el paso 3 | Todavía procesando | Respeta `Retry-After`; usa backoff con jitter |
| `404` en el paso 3 | No existe o no es visible | No lo reintentes como estado pendiente |

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
