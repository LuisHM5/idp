# Enviar un documento al API

Guía para mandar un documento al pipeline de validación y leer el resultado.

## TL;DR (script)

```bash
pnpm enviar 2607-0823-6001630_P.pdf PEDIMENTO_ADUANAL  # dev (default)
pnpm enviar 2607-0823-6001630_P.pdf PEDIMENTO_ADUANAL --env prod
# o: bash scripts/enviar-documento.sh archivo.pdf [TIPO_DOCUMENTO] [--env dev|prod]
```

El script hace todo: verifica tu sesión de AWS, obtiene el endpoint del stack,
elige API (chico) o S3 directo (grande) y te devuelve el `documentId`. Luego:

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
  lleva ningún header de autorización.
- **Las credenciales AWS** solo se usan para: (1) *leer* el endpoint desde los
  outputs del stack y (2) subir a S3 los archivos grandes. Verifícalas con:

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

### 2. Hacer el POST (archivos < ~4 MB)

El base64 pesa cientos de KB y **no cabe como argumento** de `curl`
(`Argument list too long`). Escribe el body a un archivo y envíalo con
`--data-binary @archivo`:

```bash
FILE="2607-0823-6001630_P.pdf"
{
  printf '{"fileName":"%s","contentBase64":"' "$(basename "$FILE")"
  base64 -w0 "$FILE"
  printf '","documentType":"PEDIMENTO_ADUANAL"}'
} > /tmp/body.json

curl -sS -X POST "$API_ENDPOINT/upload" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/body.json
```

- `documentType` es **opcional**: si lo omites (quita esa línea del `printf`),
  Bedrock clasifica solo (más lento y caro); si lo mandas, se salta ese paso.
- Formatos: `pdf`, `jpg`, `jpeg`, `png`.
- Respuesta: `{ "documentId": "..." }` — **guárdalo**.

> **fish shell:** el bloque `{ ...; } > file` es de bash. En fish, envuélvelo en
> `bash -c '...'` o usa directamente `pnpm enviar` (que ya corre en bash).

### 3. Archivos grandes (≥ ~4 MB) → directo a S3

El API tope es ~4 MB reales (base64 infla ~33% → 6 MB de payload de Lambda);
por encima devuelve `HTTP 413`. Sube directo a `raw/` — dispara el mismo pipeline:

```bash
BUCKET=$(aws cloudformation describe-stack-resources \
  --stack-name DocumentValidationStack --region us-east-1 \
  --query "StackResources[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId" \
  --output text | tr '\t' '\n' | grep -i bedrockdocumentprocessing | head -1)

DOCID=$(uuidgen)
aws s3 cp "expediente.pdf" "s3://$BUCKET/raw/$DOCID/expediente.pdf"
echo "documentId: $DOCID"
```

> **Clasificación manual (opcional):** sube a `raw/{TIPO}/{uuid}/archivo.pdf`
> (p. ej. `raw/PEDIMENTO_ADUANAL/$DOCID/...`) y se salta la clasificación Bedrock.

### 4. Leer el resultado

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
pnpm procesar -- --file "archivo.pdf" --importer TU_RFC
```

Deja `resultados/<archivo>.json` + `_resumen.md` para revisión humana.
