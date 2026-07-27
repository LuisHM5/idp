#!/usr/bin/env bash
# Envía un documento al API de validación y te devuelve el documentId para
# consultar el resultado. Sin límite de tamaño real: los archivos grandes
# usan una URL prefirmada de S3 (ver abajo), igual que haría cualquier
# cliente HTTP externo sin credenciales AWS.
#
# Uso:
#   scripts/enviar-documento.sh archivo.pdf [TIPO_DOCUMENTO] [--env dev|prod] [--stack NOMBRE]
#
# Ejemplos:
#   scripts/enviar-documento.sh 2607-0823-6001630_P.pdf PEDIMENTO_ADUANAL
#   scripts/enviar-documento.sh expediente.pdf            # sin tipo → Bedrock clasifica
#   scripts/enviar-documento.sh expediente.pdf --env prod # manda al stack de prod
#
# Opciones:
#   --env dev|prod    Ambiente destino (default: dev, igual que el deploy)
#   --stack <NOMBRE>  Stack explícito (gana sobre --env)
#
# Requisitos:
#   - AWS CLI autenticado (aws sts get-caller-identity debe funcionar) — solo
#     para LEER el endpoint del stack, no para subir el archivo.
#   - curl, base64
#
# El API es público (sin auth). Archivos chicos (<=~4MB) van inline como
# base64 en el POST /upload. Archivos más grandes: POST /upload SIN
# `contentBase64` devuelve una URL prefirmada de S3 (`uploadUrl`), y el
# script hace un PUT directo a esa URL — el archivo nunca pasa por API
# Gateway ni por el Lambda (esos tienen topes duros de AWS de ~10MB/6MB no
# configurables), así que no hay límite práctico de tamaño. Cualquier
# cliente HTTP sin credenciales AWS puede hacer lo mismo: solo necesita la
# `uploadUrl` que regresa el POST inicial.

set -euo pipefail

# Un stack por ambiente (ver bin/app.ts), igual que en scripts/track-execution.mjs.
DEV_STACK="DocumentValidationStack-Dev"
PROD_STACK="DocumentValidationStack"

STACK_NAME="${STACK_NAME:-}"
STACK_EXPLICIT=false
if [[ -n "$STACK_NAME" ]]; then
  STACK_EXPLICIT=true
fi
ENV="dev"
REGION="${AWS_REGION:-us-east-1}"
API_MAX_BYTES=4000000   # ~4 MB reales; por encima el API responde HTTP 413

FILE=""
DOC_TYPE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      if [[ $# -lt 2 ]]; then
        echo "Falta el valor de --env (dev o prod)." >&2
        exit 2
      fi
      ENV="$2"; shift 2 ;;
    --stack)
      if [[ $# -lt 2 ]]; then
        echo "Falta el valor de --stack." >&2
        exit 2
      fi
      STACK_NAME="$2"; STACK_EXPLICIT=true; shift 2 ;;
    --*)
      echo "Opción desconocida: $1" >&2
      exit 2 ;;
    *)
      if [[ -z "$FILE" ]]; then
        FILE="$1"
      elif [[ -z "$DOC_TYPE" ]]; then
        DOC_TYPE="$1"
      else
        echo "Argumento inesperado: $1" >&2
        exit 2
      fi
      shift ;;
  esac
done

if [[ "$ENV" != "dev" && "$ENV" != "prod" ]]; then
  echo "Ambiente inválido: $ENV (usa dev o prod)." >&2
  exit 2
fi

if [[ -z "$STACK_NAME" ]]; then
  if [[ "$ENV" == "prod" ]]; then
    STACK_NAME="$PROD_STACK"
  else
    STACK_NAME="$DEV_STACK"
  fi
fi

if [[ -z "$FILE" ]]; then
  echo "Uso: $0 archivo.pdf [TIPO_DOCUMENTO] [--env dev|prod] [--stack NOMBRE]" >&2
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

echo "Ambiente: $ENV (stack: $STACK_NAME)"

RESULT_TARGET=(--env "$ENV")
if [[ "$STACK_EXPLICIT" == true ]]; then
  RESULT_TARGET=(--stack "$STACK_NAME")
fi
RESULT_COMMAND='pnpm resultado -- --id %s --wait'
for arg in "${RESULT_TARGET[@]}"; do
  printf -v RESULT_COMMAND '%s %q' "$RESULT_COMMAND" "$arg"
done

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

# En prod el API exige `x-api-key`. Exporta API_KEY con el valor de tu key
# (aws apigateway get-api-key --api-key <id> --include-value). En dev no hace falta.
API_KEY_HEADER=()
if [[ -n "${API_KEY:-}" ]]; then
  API_KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

if (( SIZE > API_MAX_BYTES )); then
  # --- 2a. Archivo grande → POST /upload sin contentBase64 pide una URL
  # prefirmada de S3, y el PUT real del archivo va directo a S3 (nunca pasa
  # por API Gateway/Lambda) — así no hay tope de tamaño real. Cualquier
  # cliente HTTP puede hacer esto mismo sin credenciales AWS, con solo la
  # `uploadUrl` que regresa este POST.
  echo "Archivo de $SIZE bytes (> ~4 MB): pidiendo URL prefirmada de S3..."

  REQ_BODY_FILE=$(mktemp)
  trap 'rm -f "$REQ_BODY_FILE"' EXIT
  if [[ -n "$DOC_TYPE" ]]; then
    printf '{"fileName":"%s","documentType":"%s"}' "$FILE_NAME" "$DOC_TYPE" > "$REQ_BODY_FILE"
  else
    printf '{"fileName":"%s"}' "$FILE_NAME" > "$REQ_BODY_FILE"
  fi

  PRESIGN_RESPONSE=$(curl -sS -X POST "$API_ENDPOINT/upload" \
    -H "Content-Type: application/json" \
    "${API_KEY_HEADER[@]}" \
    --data-binary @"$REQ_BODY_FILE")

  UPLOAD_URL=$(echo "$PRESIGN_RESPONSE" | grep -o '"uploadUrl"[^,}]*' | head -1 | sed 's/.*: *"\([^"]*\)".*/\1/')
  DOCID=$(echo "$PRESIGN_RESPONSE" | grep -o '"documentId"[^,}]*' | head -1 | sed 's/.*: *"\([^"]*\)".*/\1/')
  if [[ -z "$UPLOAD_URL" || -z "$DOCID" ]]; then
    echo "No obtuve uploadUrl/documentId del API. Respuesta:" >&2
    echo "$PRESIGN_RESPONSE" >&2
    exit 1
  fi

  echo "Subiendo directo a S3 vía URL prefirmada..."
  curl -sS --fail -X PUT --upload-file "$FILE" "$UPLOAD_URL"

  echo "documentId: $DOCID"
  printf 'Consulta el resultado con: '"$RESULT_COMMAND"'\n' "$DOCID"
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
  "${API_KEY_HEADER[@]}" \
  --data-binary @"$BODY_FILE")

echo "Respuesta del API:"
echo "$RESPONSE"

# Extraer el documentId (sin depender de jq)
DOCID=$(echo "$RESPONSE" | grep -o '"documentId"[^,}]*' | head -1 | sed 's/.*: *"\([^"]*\)".*/\1/')
if [[ -n "$DOCID" ]]; then
  echo
  echo "documentId: $DOCID"
  printf 'Consulta el resultado con: '"$RESULT_COMMAND"'\n' "$DOCID"
fi
