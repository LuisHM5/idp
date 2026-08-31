# Enviar un documento al API

Guía para mandar un documento al pipeline de validación y leer el resultado.

## TL;DR (script)

```bash
pnpm enviar 2607-0823-6001630_P.pdf PEDIMENTO_ADUANAL  # dev (default)
pnpm enviar 2607-0823-6001630_P.pdf PEDIMENTO_ADUANAL --env prod
# o: bash scripts/enviar-documento.sh archivo.pdf [TIPO_DOCUMENTO] [--env dev|prod]
```

### Probar otro modelo sin redeploy (solo dev)

`--model` corre **ese documento** con el modelo que le indiques, sin cambiar
variables de entorno ni volver a desplegar:

```bash
pnpm enviar doc.pdf PEDIMENTO_ADUANAL --model amazon.nova-pro-v1:0
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
elige inline (chico) o URL prefirmada de S3 (grande) y te devuelve el
`documentId`. Luego:

```bash
pnpm resultado -- --id <documentId> --wait             # dev
pnpm resultado -- --id <documentId> --wait --env prod  # prod
```

Usa el mismo ambiente en ambos comandos. También puedes seleccionar un stack
concreto con `--stack <NOMBRE>`; esta opción gana sobre `--env`.

---

## ¿Necesito autenticación?

- **El API es PÚBLICO** (sin API key ni auth): el método `POST /upload` se creó
  con `AuthorizationType.NONE` (`src/document-validation-stack.ts`). El `curl` no
  lleva ningún header de autorización — ni para archivos chicos (inline) ni
  para grandes (URL prefirmada): la URL prefirmada por sí sola autoriza el
  `PUT` a S3, sin necesitar credenciales AWS del lado del cliente.
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
RESP=$(curl -sS -X POST "$API_ENDPOINT/upload" \
  -H "Content-Type: application/json" \
  -d "{\"fileName\":\"$(basename "$FILE")\",\"documentType\":\"PEDIMENTO_ADUANAL\"}")

DOC_ID=$(echo "$RESP" | jq -r .documentId)      # guárdalo
UPLOAD_URL=$(echo "$RESP" | jq -r .uploadUrl)

# --- Paso 2: subir el archivo
curl -sS --fail -X PUT --upload-file "$FILE" "$UPLOAD_URL"
```

- **Si el `POST` llevó `x-api-key`**, la respuesta trae `uploadHeaders` y el
  `PUT` **debe reenviar esas cabeceras tal cual** — van dentro de la firma, y
  omitir una da `403 SignatureDoesNotMatch` (un 403 que parece de permisos y no
  lo es). Con `jq`: `jq -r '.uploadHeaders // {} | to_entries[]'`.
- `documentType` es **opcional**: si lo omites, Bedrock clasifica solo (más
  lento y caro); si lo mandas, se salta ese paso.
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

El resultado final también se publica en **EventBridge** (broker `idp-broker`,
source `document-validation-pipeline`).

---

## Alternativa: sin API, local

Mismo pipeline, con tus credenciales AWS, sin stack desplegado:

```bash
pnpm procesar -- "archivo.pdf" --importer TU_RFC
```

Deja `resultados/<TIPO>/<archivo>/NNN/` con el `.json`, `_log.txt` y
`_resumen.md` juntos para revisión humana — cada documento numera su propia
carpeta (001, 002, ...); nunca se pisa una corrida anterior.
