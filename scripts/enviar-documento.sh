#!/usr/bin/env bash
# Envía un documento al API de validación y te devuelve el documentId para
# consultar el resultado. Sin límite de tamaño real: los archivos grandes
# usan una URL prefirmada de S3 (ver abajo), igual que haría cualquier
# cliente HTTP externo sin credenciales AWS.
#
# Uso:
#   scripts/enviar-documento.sh archivo.pdf [TIPO_DOCUMENTO] [--occurrence-mode single|multiple] [--env dev|prod] [--stack NOMBRE] [--model MODEL_ID]
#
#   TIPO_DOCUMENTO admite varios tipos separados por coma
#   (PEDIMENTO_ADUANAL,FACTURA_HONORARIOS): el archivo se segmenta y cada
#   documento se clasifica solo entre esos tipos y se omite el resto. Sin
#   sufijo, `single` conserva la primera ocurrencia y `multiple` conserva todas;
#   :N/:all sobrescribe el modo para ese tipo.
#
# Ejemplos:
#   scripts/enviar-documento.sh 2607-0823-6001630_P.pdf PEDIMENTO_ADUANAL
#   scripts/enviar-documento.sh expediente.pdf            # sin tipo → Bedrock clasifica
#   scripts/enviar-documento.sh expediente.pdf --env prod # manda al stack de prod
#   scripts/enviar-documento.sh doc.pdf PEDIMENTO_ADUANAL --model amazon.nova-pro-v1:0
#   scripts/enviar-documento.sh facturas.pdf FACTURA_FLETE_EXTRANJERO --occurrence-mode multiple
#
# Opciones:
#   --env dev|prod    Ambiente destino (default: dev, igual que el deploy)
#   --stack <NOMBRE>  Stack explícito (gana sobre --env)
#   --meta clave=valor  Metadata de la carga (repetible): va en el campo `metadata`
#                     del POST /documents. Ej. `--meta proveedor=10021` para que la
#                     etapa de autenticidad compare contra las firmas/sellos
#                     registrados de ese proveedor. Claves [a-z0-9][a-z0-9._-]*,
#                     valores ASCII simples (el API rechaza lo demás).
#   --model <ID>      Modelo de Bedrock solo para ESTE documento, sin redeploy.
#                     Solo dev: el stack de prod ignora el valor a propósito
#                     (ALLOW_MODEL_OVERRIDE), porque viene de quien sube el
#                     archivo y decidiría costo y calidad de extracción.
#   --occurrence-mode single|multiple
#                     Cardinalidad de los tipos sin sufijo para ESTE POST.
#                     Si se omite: catálogo; después single con un tipo o
#                     multiple con varios.
#
# Requisitos:
#   - AWS CLI autenticado (aws sts get-caller-identity debe funcionar) — solo
#     para LEER el endpoint del stack, no para subir el archivo.
#   - curl
#
# x-api-key:
#   - prod SIEMPRE la exige: si API_KEY no está exportada, se lee
#     automáticamente de API_KEY_APP/API_KEY en .env.production.
#   - dev: si .env.dev tiene DEV_API_KEY_REQUIRED=true, se usa AUTOMÁTICAMENTE
#     API_KEY_APP (de .env.dev o ya exportado) sin que tengas que pasar nada —
#     ver documentacion/AMBIENTES_Y_SEGURIDAD.md. API_KEY explícito siempre gana.
#
# Dos pasos, siempre, sin importar el tamaño:
#   1. POST /documents (solo metadata) -> devuelve uploadUrl
#   2. PUT del archivo directo a S3 con esa URL
# El archivo nunca pasa por API Gateway ni por el Lambda, que tienen topes
# duros de AWS (~10MB/6MB) no configurables. Cualquier cliente HTTP sin
# credenciales AWS puede hacer exactamente lo mismo.

set -euo pipefail

# Directorio real del script (no depende de INIT_CWD): de aquí sale .env.dev.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Un stack por ambiente (ver bin/app.ts), igual que en scripts/track-execution.mjs.
DEV_STACK="DocumentValidationStack-Dev"
PROD_STACK="DocumentValidationStack"

# Lee UNA variable de un archivo estilo dotenv sin sourcearlo completo (evita
# pisar otras variables del shell por accidente). No falla si el archivo o la
# variable no existen — solo no imprime nada.
read_dotenv_var() {
  local file="$1" key="$2" line val
  [[ -f "$file" ]] || return 0
  line="$(grep -E "^${key}=" "$file" | tail -1 || true)"
  [[ -n "$line" ]] || return 0
  val="${line#*=}"
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}

STACK_NAME="${STACK_NAME:-}"
STACK_EXPLICIT=false
if [[ -n "$STACK_NAME" ]]; then
  STACK_EXPLICIT=true
fi
ENV="dev"
REGION="${AWS_REGION:-us-east-1}"

FILE=""
DOC_TYPE=""
MODEL_ID=""
OCCURRENCE_MODE=""
META_JSON=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --meta)
      if [[ $# -lt 2 || "$2" != *=* ]]; then
        echo "Falta el valor de --meta (clave=valor, ej. proveedor=10021)." >&2
        exit 2
      fi
      # Sin comillas ni backslashes: el body se arma con printf, no con un
      # serializador, y el API solo admite valores ASCII planos de todos modos.
      if [[ "$2" == *'"'* || "$2" == *'\'* ]]; then
        echo "--meta no admite comillas ni backslashes: $2" >&2
        exit 2
      fi
      META_JSON+="${META_JSON:+,}\"${2%%=*}\":\"${2#*=}\""; shift 2 ;;
    --model)
      if [[ $# -lt 2 ]]; then
        echo "Falta el valor de --model (ej. amazon.nova-pro-v1:0)." >&2
        exit 2
      fi
      MODEL_ID="$2"; shift 2 ;;
    --env)
      if [[ $# -lt 2 ]]; then
        echo "Falta el valor de --env (dev o prod)." >&2
        exit 2
      fi
      ENV="$2"; shift 2 ;;
    --occurrence-mode)
      if [[ $# -lt 2 ]]; then
        echo "Falta el valor de --occurrence-mode (single o multiple)." >&2
        exit 2
      fi
      OCCURRENCE_MODE="${2,,}"; shift 2 ;;
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

if [[ -n "$OCCURRENCE_MODE" && "$OCCURRENCE_MODE" != "single" && "$OCCURRENCE_MODE" != "multiple" ]]; then
  echo "Modo de ocurrencia inválido: $OCCURRENCE_MODE (usa single o multiple)." >&2
  exit 2
fi

# Un tipo, o varios separados por coma — mismo contrato que POST /documents.
# Solo se valida la FORMA: los topes (8 tipos / 200 caracteres) los aplica la
# API, para no tener dos fuentes de verdad de los límites. Fallar aquí evita
# un viaje de ida y vuelta por el error más común: meter un espacio tras la coma.
if [[ -n "$DOC_TYPE" && ! "$DOC_TYPE" =~ ^[A-Z][A-Z0-9_]*(:([1-9][0-9]*|[aA][lL][lL]))?(,[A-Z][A-Z0-9_]*(:([1-9][0-9]*|[aA][lL][lL]))?)*$ ]]; then
  echo "TIPO_DOCUMENTO inválido: '$DOC_TYPE'" >&2
  echo "Debe ser UPPER_SNAKE_CASE. Para varios tipos, sepáralos por coma SIN espacios:" >&2
  echo "  $0 archivo.pdf PEDIMENTO_ADUANAL,FACTURA_HONORARIOS" >&2
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
  echo "Uso: $0 archivo.pdf [TIPO_DOCUMENTO] [--occurrence-mode single|multiple] [--meta clave=valor]... [--env dev|prod] [--stack NOMBRE] [--model MODEL_ID]" >&2
  echo "     TIPO_DOCUMENTO acepta varios tipos y sufijos :N/:all, sin espacios." >&2
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
if [[ -n "$MODEL_ID" ]]; then
  if [[ "$ENV" == "prod" ]]; then
    echo "AVISO: --model solo aplica en dev; el stack de prod lo ignora." >&2
  else
    echo "Modelo para este documento: $MODEL_ID"
  fi
fi

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

# En prod el API siempre exige `x-api-key`. Una API_KEY exportada gana; si no
# existe, toma API_KEY_APP del shell o API_KEY/API_KEY_APP de .env.production,
# en ese orden. Así el comando normal funciona sin repetir secretos en la línea
# de comandos y todavía permite probar otra key explícitamente.
#
# En dev, si no diste API_KEY a mano y .env.dev tiene DEV_API_KEY_REQUIRED=true,
# se usa automáticamente API_KEY_APP (de .env.dev o ya exportado) — regístrala
# una vez con `pnpm apps:register -- --name local-scripts --env dev` y pega el
# valor en .env.dev. Un API_KEY ya exportado en el shell siempre gana.
if [[ "$ENV" == "prod" && -z "${API_KEY:-}" ]]; then
  ENV_PRODUCTION_FILE="$SCRIPT_DIR/../.env.production"
  API_KEY="${API_KEY_APP:-}"
  if [[ -z "$API_KEY" ]]; then
    API_KEY="$(read_dotenv_var "$ENV_PRODUCTION_FILE" API_KEY)"
  fi
  if [[ -z "$API_KEY" ]]; then
    API_KEY="$(read_dotenv_var "$ENV_PRODUCTION_FILE" API_KEY_APP)"
  fi
  if [[ -n "$API_KEY" ]]; then
    echo "Usando la API key configurada para producción."
  else
    echo "No hay API_KEY/API_KEY_APP exportada ni configurada en .env.production." >&2
    echo "Agrega API_KEY_APP=<valor> a .env.production o exporta API_KEY y reintenta." >&2
    exit 1
  fi
elif [[ "$ENV" == "dev" && -z "${API_KEY:-}" ]]; then
  ENV_DEV_FILE="$SCRIPT_DIR/../.env.dev"
  DEV_KEY_REQUIRED="${DEV_API_KEY_REQUIRED:-$(read_dotenv_var "$ENV_DEV_FILE" DEV_API_KEY_REQUIRED)}"
  if [[ "$(printf '%s' "$DEV_KEY_REQUIRED" | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
    API_KEY="${API_KEY_APP:-$(read_dotenv_var "$ENV_DEV_FILE" API_KEY_APP)}"
    if [[ -n "${API_KEY:-}" ]]; then
      echo "DEV_API_KEY_REQUIRED=true detectado — usando API_KEY_APP automáticamente."
    else
      echo "AVISO: DEV_API_KEY_REQUIRED=true pero no hay API_KEY_APP en .env.dev ni exportado — el request probablemente reciba 403." >&2
    fi
  fi
fi

API_KEY_HEADER=()
if [[ -n "${API_KEY:-}" ]]; then
  API_KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

# --- 2. Un solo flujo, para cualquier tamaño: POST /documents NO lleva el
# archivo; devuelve una URL prefirmada de S3 y el PUT va directo a S3 (nunca
# pasa por API Gateway/Lambda). Antes esto dependía del tamaño, y ese salto
# era justo donde aparecían los casos raros: base64 infla ~33%, así que un
# archivo de ~4.5 MB reventaba el límite de payload de Lambda. Ahora un CFDI
# de 200 KB y un expediente de 300 MB hacen exactamente los mismos dos pasos.
echo "Archivo de $SIZE bytes: pidiendo URL prefirmada de S3..."

REQ_BODY_FILE=$(mktemp)
trap 'rm -f "$REQ_BODY_FILE"' EXIT
{
  printf '{"fileName":"%s"' "$FILE_NAME"
  [[ -n "$DOC_TYPE" ]] && printf ',"documentType":"%s"' "$DOC_TYPE"
  [[ -n "$OCCURRENCE_MODE" ]] && printf ',"occurrenceMode":"%s"' "$OCCURRENCE_MODE"
  [[ -n "$MODEL_ID" ]] && printf ',"modelId":"%s"' "$MODEL_ID"
  [[ -n "$META_JSON" ]] && printf ',"metadata":{%s}' "$META_JSON"
  printf '}'
} > "$REQ_BODY_FILE"

PRESIGN_BODY=$(mktemp)
trap 'rm -f "$REQ_BODY_FILE" "$PRESIGN_BODY"' EXIT
PRESIGN_HTTP_CODE=$(curl -sS -X POST "$API_ENDPOINT/documents" \
  -H "Content-Type: application/json" \
  "${API_KEY_HEADER[@]}" \
  --data-binary @"$REQ_BODY_FILE" \
  -o "$PRESIGN_BODY" -w '%{http_code}')
PRESIGN_RESPONSE=$(<"$PRESIGN_BODY")

if [[ ! "$PRESIGN_HTTP_CODE" =~ ^2[0-9][0-9]$ ]]; then
  echo "El API rechazó la solicitud de URL prefirmada (HTTP $PRESIGN_HTTP_CODE):" >&2
  echo "$PRESIGN_RESPONSE" >&2
  exit 1
fi

UPLOAD_URL=$(echo "$PRESIGN_RESPONSE" | grep -o '"uploadUrl"[^,}]*' | head -1 | sed 's/.*: *"\([^"]*\)".*/\1/' || true)
DOCID=$(echo "$PRESIGN_RESPONSE" | grep -o '"documentId"[^,}]*' | head -1 | sed 's/.*: *"\([^"]*\)".*/\1/' || true)
if [[ -z "$UPLOAD_URL" || -z "$DOCID" ]]; then
  echo "No obtuve uploadUrl/documentId del API. Respuesta:" >&2
  echo "$PRESIGN_RESPONSE" >&2
  exit 1
fi

# El PUT no lleva NINGUNA cabecera extra. Al prefirmar, SigV4 **hoistea** los
# `x-amz-*` al query string, así que la metadata (model-id, app-origin) ya
# viaja dentro de la URL y queda cubierta por la firma
# (`X-Amz-SignedHeaders=host`). Mandarla ADEMÁS como header hace que S3
# conteste `403 AccessDenied: There were headers present in the request which
# were not signed`. Este script llegó a hacerlo, y ése era el bug.

echo "Subiendo directo a S3 vía URL prefirmada..."
# Sin --fail: S3 explica el motivo en el cuerpo del error. --fail lo descarta
# y deja solo "error: 403", que no dice nada de por qué.
PUT_BODY=$(mktemp); trap 'rm -f "$REQ_BODY_FILE" "$PRESIGN_BODY" "$PUT_BODY"' EXIT
HTTP_CODE=$(curl -sS -X PUT --upload-file "$FILE" \
  -o "$PUT_BODY" -w '%{http_code}' "$UPLOAD_URL")
if [[ "$HTTP_CODE" != "200" ]]; then
  echo "S3 rechazó el PUT (HTTP $HTTP_CODE):" >&2
  cat "$PUT_BODY" >&2; echo >&2
  exit 1
fi

echo "documentId: $DOCID"
printf 'Consulta el resultado con: '"$RESULT_COMMAND"'\n' "$DOCID"
