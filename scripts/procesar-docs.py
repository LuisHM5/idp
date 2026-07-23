#!/usr/bin/env python3
"""Procesa documentos con el pipeline completo y deja los resultados listos
para revisión humana. Sin asserts: tú validas el resultado final.

Usa el pipeline localmente (misma lógica que la Lambda): necesita credenciales
AWS con Textract + Bedrock. NO necesita el stack desplegado.

Uso:
    pnpm procesar                                   # toda la carpeta default de pedimentos
    pnpm procesar -- --file "mi expediente.pdf"     # un archivo (ruta o nombre dentro de --dir)
    pnpm procesar -- --dir /ruta/a/mis/docs         # otra carpeta completa
    pnpm procesar -- --out resultados/              # dónde dejar los JSON (default: ./resultados)
    pnpm procesar -- --importer OFU910626UQ0        # RFC del importador esperado (checks cruzados)

Salida por documento:
  - resumen legible en consola (tipo, estado, campos clave, warnings)
  - <out>/<archivo>.json  → resultado completo (todos los campos, checks, confianzas)
  - <out>/_resumen.md     → tabla consolidada de la corrida para revisar de un vistazo
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))

DEFAULT_DIR = REPO / "test-docs" / "pedimentos docs" / "docs"

GREEN, RED, YELLOW, CYAN, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"

STATUS_COLOR = {
    "APPROVED": GREEN,
    "REJECTED": RED,
    "PARTIALLY_APPROVED": YELLOW,
    "REVIEW_REQUIRED": YELLOW,
    "PROCESSING_ERROR": RED,
}

# Campos que vale la pena mostrar en el resumen por tipo de documento
KEY_FIELDS = [
    "NUMERO_PEDIMENTO_NORMALIZADO", "NUMERO_PEDIMENTO", "PEDIMENTO_REFERENCE",
    "UUID", "EMISOR_RFC", "RECEPTOR_RFC", "RFC_IMPORTADOR_EXPORTADOR",
    "SUBTOTAL", "TOTAL", "TOTAL_PEDIMENTO", "IMPORTE_TOTAL",
    "FECHA_EMISION", "FECHA_PAGO", "INVOICE_NUMBER", "NUMERO_PERMISO_CERTIFICADO",
    "NUMERO_E_DOCUMENT",
]


def summarize_doc(doc: dict) -> list[str]:
    lines = []
    entities = doc.get("extractedEntities", {})
    for name in KEY_FIELDS:
        slot = (doc.get("fields") or {}).get(name) or {}
        value = slot.get("normalizedValue") or slot.get("extractedValue") or entities.get(name)
        if value not in (None, "", []):
            status = slot.get("status", "")
            conf = slot.get("confidence")
            conf_s = f" ({conf:.0%})" if isinstance(conf, (int, float)) else ""
            lines.append(f"      {name} = {value}  {DIM}{status}{conf_s}{RESET}")
    arrays = {k: len(v) for k, v in entities.items() if isinstance(v, list) and v}
    if arrays:
        lines.append(f"      {DIM}arrays: " + ", ".join(f"{k}×{n}" for k, n in arrays.items()) + RESET)
    bad = [c for c in doc.get("checks", []) if not c.get("valid") and not c.get("skipped")]
    for c in bad[:5]:
        sev = RED if c.get("severity") == "error" else YELLOW
        lines.append(f"      {sev}✗ {c.get('field')}: {c.get('message')}{RESET}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="un archivo (ruta completa o nombre dentro de --dir)")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="carpeta con PDFs")
    parser.add_argument("--out", default="resultados", help="carpeta de salida para los JSON")
    parser.add_argument("--importer", help="RFC del importador esperado (activa checks cruzados de RFC)")
    parser.add_argument("--model", help="override del modelo Bedrock")
    # pnpm forwards a literal "--" separator; argparse must not choke on it
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    from pipeline.expediente import process_pdf

    if args.file:
        p = Path(args.file)
        pdfs = [p] if p.exists() else sorted(
            f for f in Path(args.dir).glob("*.pdf") if args.file.lower() in f.name.lower()
        )
    else:
        pdfs = sorted(Path(args.dir).glob("*.pdf"))
    if not pdfs:
        print(f"No encontré PDFs ({args.file or args.dir})")
        return 2

    expected_values = {"IMPORTER_RFC": args.importer} if args.importer else {}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    resumen_md = [
        f"# Resultados de procesamiento — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "| Archivo | Estado | Documentos detectados | Warnings |",
        "|---|---|---|---|",
    ]

    for pdf in pdfs:
        print(f"\n{CYAN}{'=' * 72}\n{pdf.name}{RESET}")
        started = time.time()
        try:
            result = process_pdf(
                pdf.read_bytes(),
                expected_values=expected_values,
                model_id=args.model,
                source_name=pdf.name,
            )
        except Exception as e:
            print(f"  {RED}ERROR: {e}{RESET}")
            resumen_md.append(f"| {pdf.name} | ERROR | — | {e} |")
            continue

        status = result.get("status", "?")
        color = STATUS_COLOR.get(status, "")
        pages = result.get("pages", {})
        print(
            f"  Estado: {color}{status}{RESET}  |  {pages.get('totalPages')} páginas "
            f"({pages.get('nativePages')} digitales, {pages.get('ocrPages')} OCR)  |  "
            f"{time.time() - started:.0f}s"
        )

        detected = []
        for doc in result.get("documents", []):
            dt = doc.get("documentType")
            ds = doc.get("status", "-")
            dcolor = STATUS_COLOR.get(ds, "")
            print(f"\n   📄 p{doc.get('pageStart')}-{doc.get('pageEnd')}  {dt}  →  {dcolor}{ds}{RESET}")
            for line in summarize_doc(doc):
                print(line)
            if dt != "OTRO":
                detected.append(f"{dt} ({ds})")

        warnings = result.get("warnings", [])
        for w in warnings:
            print(f"\n  {YELLOW}⚠ {w}{RESET}")
        for c in result.get("crossDocumentChecks", []):
            if c.get("valid") and not c.get("skipped"):
                print(f"  {GREEN}✓ {c.get('rule')}: {c.get('message')}{RESET}")

        out_file = out_dir / f"{pdf.stem}.json"
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n  {DIM}JSON completo → {out_file}{RESET}")

        resumen_md.append(
            f"| {pdf.name} | {status} | {'<br>'.join(detected) or '—'} | {'<br>'.join(warnings) or '—'} |"
        )

    resumen_path = out_dir / "_resumen.md"
    resumen_path.write_text("\n".join(resumen_md) + "\n", encoding="utf-8")
    print(f"\n{'=' * 72}\nResumen consolidado → {resumen_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
