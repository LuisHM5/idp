#!/usr/bin/env bash
# Convierte un Markdown de documentacion/ a PDF.
#
#   pnpm doc:pdf                                 # documentacion/API_RESUMEN.md
#   pnpm doc:pdf documentacion/API_USO.md        # otro archivo
#   pnpm doc:pdf documentacion/API_USO.md /tmp/api.pdf
#
# Dos pasos: pandoc arma un HTML autocontenido (respeta tablas y bloques de
# código del Markdown) y Chrome headless lo imprime a PDF. No usa LaTeX, que
# suele no estar instalado y arrastra cientos de MB.
#
# Requisitos: pandoc y google-chrome (o chromium). El script avisa si falta uno.

set -euo pipefail

IN="${1:-documentacion/API_RESUMEN.md}"
OUT="${2:-}"

[[ -f "$IN" ]] || { echo "No existe: $IN" >&2; exit 1; }
[[ -z "$OUT" ]] && OUT="${IN%.md}.pdf"

command -v pandoc >/dev/null || { echo "Falta pandoc. Instálalo: sudo apt install pandoc" >&2; exit 1; }

CHROME=""
for c in google-chrome google-chrome-stable chromium chromium-browser; do
  command -v "$c" >/dev/null && { CHROME="$c"; break; }
done
[[ -n "$CHROME" ]] || { echo "Falta Chrome/Chromium (para imprimir a PDF)." >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/estilo.css" <<'CSSEOF'
@page { size: A4; margin: 16mm 14mm; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 10pt; line-height: 1.45; color: #1a1a1a; max-width: none; margin: 0;
}
h1 { font-size: 19pt; margin: 0 0 4pt; border-bottom: 2px solid #1a1a1a; padding-bottom: 5pt; }
h2 { font-size: 13pt; margin: 16pt 0 6pt; color: #0b4f6c; break-after: avoid; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; break-after: avoid; }
p  { margin: 5pt 0; }
code {
  font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 8.6pt;
  background: #f2f4f6; padding: 1px 4px; border-radius: 3px; white-space: nowrap;
}
pre {
  background: #f7f8fa; border: 1px solid #e1e5ea; border-left: 3px solid #0b4f6c;
  padding: 8pt 10pt; border-radius: 4px; break-inside: avoid;
  /* En papel no hay scroll: `overflow-x: auto` recortaría lo que no cabe. */
  white-space: pre-wrap; word-break: break-word;
}
pre code { background: none; padding: 0; font-size: 8.4pt; white-space: inherit; }
/* Un `code` largo (una URL) tampoco puede desbordar la caja de la tabla. */
code { word-break: break-word; }
td code { white-space: normal; }
table {
  border-collapse: collapse; width: 100%; margin: 6pt 0 10pt;
  font-size: 9pt; break-inside: avoid;
}
th, td { border: 1px solid #dde2e8; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #eef2f6; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
hr { border: 0; border-top: 1px solid #e1e5ea; margin: 14pt 0; }
blockquote {
  margin: 8pt 0; padding: 6pt 10pt; background: #fff8e6;
  border-left: 3px solid #d9a441; break-inside: avoid;
}
blockquote p { margin: 2pt 0; }
a { color: #0b4f6c; text-decoration: none; }
CSSEOF

# Sin --metadata title: el H1 del propio Markdown ya es el título, y pandoc
# añadiría encima un bloque <h1 class="title"> duplicado.
pandoc "$IN" \
  --standalone --embed-resources \
  --css "$TMP/estilo.css" \
  -o "$TMP/doc.html"

mkdir -p "$(dirname "$OUT")"
"$CHROME" --headless=new --disable-gpu --no-sandbox \
  --no-pdf-header-footer \
  --print-to-pdf="$(realpath -m "$OUT")" \
  "file://$TMP/doc.html" >/dev/null 2>&1

[[ -s "$OUT" ]] || { echo "Chrome no generó el PDF." >&2; exit 1; }
echo "$OUT  ($(du -h "$OUT" | cut -f1))"
