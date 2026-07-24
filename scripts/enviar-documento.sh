#!/usr/bin/env bash
# Envía un documento al API de validación (o directo a S3 si es grande) y te
# devuelve el documentId para consultar el resultado.
#
# Uso:
#   scripts/enviar-documento.sh archivo.pdf [TIPO_DOCUMENTO]
#
# Ejemplos:
#   scripts/enviar-documento.sh 2607-0823-6001630_P.pdf PEDIMENTO_ADUANAL
#   scripts/enviar-documento.sh expediente.pdf            # sin tipo → Bedrock clasifica
#
# Requisitos:
#   - AWS CLI autenticado (aws sts get-caller-identity debe funcionar)
#   - curl, base64
#
# El API es público (sin auth): las credenciales AWS solo se usan para LEER el
# endpoint del stack y, si el archivo es grande, para subirlo a S3.

set -euo pipefail

STACK_NAME="${STACK_NAME:-DocumentValidationStack}"
REGION="${AWS_REGION:-us-east-1}"
API_MAX_BYTES=4000000   # ~4 MB reales; por encima el API responde HTTP 413

FILE="${1:-}"
DOC_TYPE="${2:-}"

if [[ -z "$FILE" ]]; then
  echo "Uso: $0 archivo.pdf [TIPO_DOCUMENTO]" >&2
  exit 2
fi

# pnpm ejecuta scripts desde la raíz del paquete, aunque el usuario los invoque
# desde un subdirectorio. INIT_CWD conserva ese directorio original.
if [[ "$FILE" != /* && ! -f "$FILE" && -n "${INIT_CWD:-}" && -f "${INIT_CWD}/${FILE}" ]]; then
  FILE="${INIT_CWD}/${FILE}"
fi

if [[ ! -f "$FILE" ]]; then
  echo "No existe el archivo: $FILE" >&2
  exit 2
fi

# --- 0. ¿Estoy autenticado en AWS? -----------------------------------------
if ! aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1; then
  echo "No estás autenticado en AWS. Corre 'aws sso login' (o 'aws configure') y reintenta." >&2
  exit 1
fi

# --- 1. Obtener el endpoint del stack --------------------------------------
API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
  --output text \
  --region "$REGION")
API_ENDPOINT="${API_ENDPOINT%/}"   # api.url trae "/" final → quítalo

if [[ -z "$API_ENDPOINT" || "$API_ENDPOINT" == "None" ]]; then
  echo "No encontré el output ApiEndpoint en el stack '$STACK_NAME'." >&2
  exit 1
fi
echo "API_ENDPOINT = $API_ENDPOINT"

FILE_NAME=$(basename "$FILE")
SIZE=$(wc -c < "$FILE")

# --- 2a. Archivo grande → subida directa a S3 (mismo pipeline) --------------
if (( SIZE > API_MAX_BYTES )); then
  echo "Archivo de $SIZE bytes (> ~4 MB): subiendo directo a S3 (el API daría HTTP 413)."
  BUCKET=$(aws cloudformation describe-stack-resources \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "StackResources[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId" \
    --output text | tr '\t' '\n' | grep -i bedrockdocumentprocessing | head -1)
  if [[ -z "$BUCKET" ]]; then
    echo "No pude localizar el bucket de documentos en el stack." >&2
    exit 1
  fi
  DOCID=$(cat /proc/sys/kernel/random/uuid)
  # Con tipo declarado: raw/{TIPO}/{uuid}/... salta la clasificación Bedrock.
  if [[ -n "$DOC_TYPE" ]]; then
    KEY="raw/${DOC_TYPE}/${DOCID}/${FILE_NAME}"
  else
    KEY="raw/${DOCID}/${FILE_NAME}"
  fi
  aws s3 cp "$FILE" "s3://${BUCKET}/${KEY}" --region "$REGION"
  echo "documentId: $DOCID"
  echo "Consulta el resultado con: pnpm resultado -- --id $DOCID --wait"
  exit 0
fi

# --- 2b. Archivo chico → POST al API (base64 en JSON) -----------------------
echo "Enviando $FILE_NAME ($SIZE bytes) por el API..."

# El body (base64) puede pesar cientos de KB → NO cabe como argumento de curl
# ("Argument list too long"). Se escribe a un archivo temporal y se envía con
# --data-binary @archivo.
BODY_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE"' EXIT

{
  printf '{"fileName":"%s","contentBase64":"' "$FILE_NAME"
  base64 -w0 "$FILE"
  if [[ -n "$DOC_TYPE" ]]; then
    printf '","documentType":"%s"}' "$DOC_TYPE"
  else
    printf '"}'
  fi
} > "$BODY_FILE"

RESPONSE=$(curl -sS -X POST "$API_ENDPOINT/upload" \
  -H "Content-Type: application/json" \
  --data-binary @"$BODY_FILE")

echo "Respuesta del API:"
echo "$RESPONSE"

# Extraer el documentId (sin depender de jq)
DOCID=$(echo "$RESPONSE" | grep -o '"documentId"[^,}]*' | head -1 | sed 's/.*: *"\([^"]*\)".*/\1/')
if [[ -n "$DOCID" ]]; then
  echo
  echo "documentId: $DOCID"
  echo "Consulta el resultado con: pnpm resultado -- --id $DOCID --wait"
fi
