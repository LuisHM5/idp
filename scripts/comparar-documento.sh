#!/usr/bin/env bash
# Script simple: procesa UN PDF de prueba con el pipeline real y muestra, campo
# por campo, el valor esperado (tests/integration/expected_pedimentos.json)
# contra el valor que realmente extrajo. Es una envoltura de
# scripts/test-pedimentos.py pensada para correr rápido mientras se decide un
# fix, sin tener que recordar flags.
#
# Uso:
#   bash scripts/comparar-documento.sh normal_pend.pdf
#   pnpm comparar -- normal_pend.pdf
#
# Requiere AWS configurado (Bedrock + Textract). Tiene costo real (unos
# centavos de dólar por documento chico) porque llama al modelo de verdad.
set -euo pipefail

FILE="${1:-}"
if [[ -z "$FILE" ]]; then
  echo "Uso: bash scripts/comparar-documento.sh <archivo.pdf>"
  echo "Ejemplo: bash scripts/comparar-documento.sh normal_pend.pdf"
  echo
  echo "Archivos disponibles en 'test-docs/pedimentos docs/docs':"
  ls "test-docs/pedimentos docs/docs" 2>/dev/null | sed 's/^/  - /' || echo "  (no se encontró la carpeta)"
  exit 1
fi

echo "Esto va a llamar a AWS Bedrock/Textract de verdad (costo real, unos centavos)."
read -r -p "¿Continuar? [y/N] " REPLY
if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
  echo "Cancelado."
  exit 1
fi

.venv/bin/python scripts/test-pedimentos.py --file "$FILE"
