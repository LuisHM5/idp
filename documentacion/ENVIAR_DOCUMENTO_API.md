# Enviar un documento al API

Guía para mandar un documento al pipeline de validación y leer el resultado.

## TL;DR (script)

```bash
pnpm enviar 2607-0823-6001630_P.pdf PEDIMENTO_ERP  # dev (default)
pnpm enviar 2607-0823-6001630_P.pdf PEDIMENTO_ERP --env prod
pnpm enviar invoices-flete.pdf FACTURA_FLETE_EXTRANJERO --occurrence-mode multiple
# o: bash scripts/enviar-documento.sh archivo.pdf [TIPO_DOCUMENTO] [--occurrence-mode single|multiple] [--env dev|prod]
```

Para probar de una vez la subida, la espera eficiente por `202`/`Retry-After`
y el `GET` final, usa el E2E genérico. Su `stdout` es exclusivamente el JSON
final:

```bash
pnpm test-e2e -- archivo.pdf TIPO_DOCUMENTO
pnpm --silent test-e2e -- archivo.pdf TIPO_DOCUMENTO > resultado.json
```

### Probar otro modelo sin redeploy (solo dev)

`--model` corre **ese documento** con el modelo que le indiques, sin cambiar
variables de entorno ni volver a desplegar:

```bash
pnpm enviar doc.pdf PEDIMENTO_ERP --model amazon.nova-pro-v1:0
```

`--occurrence-mode multiple` agrega `"occurrenceMode":"multiple"` al POST y
busca todas las ocurrencias de cada tipo sin `:N`/`:all`; si se omite, el API
usa primero `occurrence_mode` del catálogo y después el default global
(`single` con un tipo, `multiple` con varios). Un sufijo sigue ganando por
tipo, por ejemplo:

```bash
pnpm enviar invoices-flete.pdf \
  "PEDIMENTO_ERP:1,FACTURA_FLETE_EXTRANJERO" \
  --occurrence-mode multiple --env dev
```

Aplica a todas las fases del pipeline y gana sobre `BEDROCK_*_MODEL_ID`. El
resultado registra qué modelo corrió en cada fase (`tracking.modelIds`), así
que puedes mandar el mismo archivo con dos modelos y comparar.

**Solo funciona en dev.** El stack de prod no define `ALLOW_MODEL_OVERRIDE`, así
que ignora el valor a propósito: viene de quien sube el archivo, y en prod
dejaría que un tercero elija el modelo —y con él el costo y la calidad de
extracción— sobre documentos ajenos. Si lo usas contra prod, el script avisa y
el documento se procesa con el modelo configurado.

El script hace todo: verifica tu sesión de AWS, obtiene el endpoint del stack,
pide una URL prefirmada, sube el archivo directo a S3 y te devuelve el
`documentId`. Todos los tamaños usan el mismo flujo. Luego:

```bash
pnpm resultado -- --id <documentId> --wait             # dev
pnpm resultado -- --id <documentId> --wait --env prod  # prod
```

Usa el mismo ambiente en ambos comandos. También puedes seleccionar un stack
concreto con `--stack <NOMBRE>`; esta opción gana sobre `--env`.

---

## ¿Necesito autenticación?

- **Prod exige una API key** en `x-api-key`. `pnpm enviar --env prod` usa primero
  una `API_KEY` exportada y, si no existe, lee `API_KEY` o `API_KEY_APP` de
  `.env.production`. Si no encuentra ninguna, se detiene antes de llamar al
  API. Dev queda abierto por default; si despliegas dev con
  `DEV_API_KEY_REQUIRED=true`, el script toma `API_KEY_APP` de `.env.dev`.
- La URL prefirmada autoriza por sí sola el segundo paso, el `PUT` directo a
  S3; ese `PUT` no lleva `x-api-key` ni otras cabeceras adicionales.
- **Las credenciales AWS** en `scripts/enviar-documento.sh` solo se usan para
  *leer* el endpoint desde los outputs del stack (`aws cloudformation
  describe-stacks`) — no para subir el archivo. Verifícalas con:

  ```bash
  aws sts get-caller-identity        # debe devolver tu Account/Arn (886031931562)
  ```

  Si falla: `aws sso login` (o `aws configure`), según lo que uses.

---

## Paso a paso (manual, sin el script)

### 1. Obtener el endpoint correcto

No lo adivines: sale de los outputs de CloudFormation.

```bash
API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name DocumentValidationStack \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
  --output text \
  --region us-east-1)

API_ENDPOINT=${API_ENDPOINT%/}     # api.url trae "/" final → quítalo
echo "$API_ENDPOINT"
# → https://4svzjzyix3.execute-api.us-east-1.amazonaws.com/prod
```

> **fish shell:** `${VAR%/}` es sintaxis bash. En fish usa
> `set API_ENDPOINT (string trim --right --chars=/ $API_ENDPOINT)`, o corre los
> comandos dentro de `bash -c '...'`.

### 2. Subir el documento (dos pasos, cualquier tamaño)

El `POST` **no lleva el archivo**: solo metadata. Devuelve una URL prefirmada y
el archivo se sube con un `PUT` directo a S3.

```bash
FILE="2607-0823-6001630_P.pdf"

# --- Paso 1: pedir la URL
RESP=$(curl -sS -X POST "$API_ENDPOINT/documents" \
  -H "Content-Type: application/json" \
  -d "{\"fileName\":\"$(basename "$FILE")\",\"documentType\":\"PEDIMENTO_ERP\",\"occurrenceMode\":\"single\"}")

DOC_ID=$(echo "$RESP" | jq -r .documentId)      # guárdalo
UPLOAD_URL=$(echo "$RESP" | jq -r .uploadUrl)

# --- Paso 2: subir el archivo
curl -sS --fail -X PUT --upload-file "$FILE" "$UPLOAD_URL"
```

- **El `PUT` no lleva ninguna cabecera extra.** Al prefirmar, SigV4 mueve las
  cabeceras `x-amz-*` al query string, así que la metadata ya viaja dentro de la
  URL y firmada. Si además la mandas como header, S3 responde
  `403 AccessDenied: There were headers present in the request which were not
  signed` — un 403 que parece de permisos y es de firma.
- `documentType` es **opcional**: si lo omites, Bedrock clasifica solo (más
  lento y caro); si mandas un tipo, se salta ese paso. Con **varios separados por
  coma** (`"documentType":"PEDIMENTO_ADUANAL,FACTURA_HONORARIOS"`) el archivo se
  segmenta y cada documento encontrado se clasifica, pero sólo entre esos tipos;
  `documents[]` omite el resto. `occurrenceMode` en el POST decide si cada tipo
  sin sufijo conserva la primera (`single`) o todas (`multiple`); si se omite,
  usa el catálogo y luego `single` con un tipo/`multiple` con varios;
  `:N`/`:all` lo sobrescribe por tipo. Un tipo pedido que no aparezca sale con
  rango `null` y revisión.
- Formatos: `pdf`, `jpg`, `jpeg`, `png`, `xml`, `xlsx`, `csv`.
- **No hay tope de tamaño**: el archivo no pasa por API Gateway ni por la
  Lambda. El modo inline (`contentBase64`) fue eliminado — devuelve
  `400 INLINE_UPLOAD_REMOVED`.

> **fish shell:** estos bloques son de bash. En fish, envuélvelos en
> `bash -c '...'` o usa directamente `pnpm enviar` (que ya corre en bash y hace
> los dos pasos por ti).

### 3. Leer el resultado

```bash
pnpm resultado -- --id <documentId> --wait     # espera si sigue procesando
pnpm track --latest --follow                   # ver el avance en vivo
```

La respuesta terminal tiene esta forma abreviada; `fields` normalmente contiene
muchos más campos y puede incluir arreglos como `CONCEPTOS.items[]`:

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
      "status": "APPROVED",
      "fields": {
        "NUMERO_FACTURA": { "value": "SJ191703", "confidence": 0.98, "valid": true },
        "TOTAL": { "value": 14612.4, "confidence": 0.93, "valid": true }
      }
    }]
  }
}
```

El resultado final también se publica en **EventBridge** (broker `idp-broker`,
source `document-validation-pipeline`).

### 4. Reprocesar el mismo archivo con otros tipos

No lo descargues ni repitas el PUT. Usa el id anterior; el servidor copia el
original dentro de S3, crea otro id y deja intacto el resultado viejo:

```bash
RESP=$(curl -sS -X POST "$API_ENDPOINT/documents/$DOC_ID/reprocess" \
  -H "Content-Type: application/json" \
  -d '{
    "documentType": "PEDIMENTO_ERP:1,FACTURA_FLETE_EXTRANJERO:all",
    "occurrenceMode": "multiple"
  }')

NEW_DOC_ID=$(echo "$RESP" | jq -r .documentId)
pnpm resultado -- --id "$NEW_DOC_ID" --wait
```

En prod agrega `-H "x-api-key: $KEY"`. Un body `{}` hereda la declaración de
tipos anterior; `{"documentType":null}` vuelve a clasificación automática.
La referencia completa está en `api-docs/API_USO.md` §3.

---

## Alternativa: sin API, local

Mismo pipeline, con tus credenciales AWS, sin stack desplegado:

```bash
pnpm procesar -- "archivo.pdf" --importer TU_RFC
```

Deja `resultados/<TIPO>/<archivo>/NNN/` con el `.json`, `_log.txt` y
`_resumen.md` juntos para revisión humana — cada documento numera su propia
carpeta (001, 002, ...); nunca se pisa una corrida anterior.
