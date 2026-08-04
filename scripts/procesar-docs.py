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
    pnpm procesar -- --file factura.xml --type FACTURA_HONORARIOS_XML
        # .xml usa pipeline/xml_document.py (extracción por XPath, sin Bedrock/Textract,
        # no necesita credenciales AWS) — --type es OBLIGATORIO para .xml, no hay
        # clasificación automática por visión sobre XML crudo.
    pnpm procesar -- --file reporte.xlsx --type MI_TIPO_XLSX
        # .xlsx/.csv usan pipeline/tabular_document.py (extracción por celda/columna,
        # mismas condiciones que .xml: --type obligatorio, sin Bedrock/Textract).
    pnpm procesar -- --file ine.jpeg --type INE
        # .jpg/.jpeg/.png (una foto/escaneo, no un PDF) usan pipeline/expediente.py
        # con pdf_pages.load_image_page (Textract sobre los bytes de la imagen
        # directo, sin pypdf) — SÍ llama Bedrock/Textract, igual que un PDF.

Salida por documento:
  - resumen legible en consola (tipo, estado, campos clave, warnings)
  - <out>/<archivo>.json  → resultado completo (todos los campos, checks, confianzas)
  - <out>/_resumen.md     → tabla consolidada de la corrida para revisar de un vistazo
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

# "N páginas (M digitales, K OCR)" (pdf_pages.pages_summary) is PDF
# vocabulary that doesn't apply to a deterministically-parsed source — one
# label per non-PDF sourceFormat; absent from the dict = PDF page breakdown.
SOURCE_FORMAT_NOTE = {
    "xml": "XML (extracción por XPath, sin Bedrock/Textract)",
    "xlsx": "XLSX (extracción por celda/columna, sin Bedrock/Textract)",
    "csv": "CSV (extracción por columna, sin Bedrock/Textract)",
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
    is_field_tree = doc.get("fieldTreeShape", False)
    for name in KEY_FIELDS:
        if is_field_tree:
            # Field-centric DSL: extractedEntities IS the resolved tree
            # ({value, confidence, valid, reason, source} per scalar), no
            # separate `fields` view (see validators.engine.resolve_field_tree).
            node = entities.get(name) or {}
            value = node.get("value")
            status = "valid" if node.get("valid") else (node.get("reason") or "invalid")
            conf = node.get("confidence")
        else:
            slot = (doc.get("fields") or {}).get(name) or {}
            value = slot.get("normalizedValue") or slot.get("extractedValue") or entities.get(name)
            status = slot.get("status", "")
            conf = slot.get("confidence")
        if value not in (None, "", []):
            conf_s = f" ({conf:.0%})" if isinstance(conf, (int, float)) else ""
            lines.append(f"      {name} = {value}  {DIM}{status}{conf_s}{RESET}")
    if is_field_tree:
        arrays = {
            k: len(v["items"]) for k, v in entities.items()
            if isinstance(v, dict) and isinstance(v.get("items"), list) and v["items"]
        }
    else:
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
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="carpeta con PDFs/XMLs/XLSX/CSVs")
    parser.add_argument("--out", default="resultados", help="carpeta de salida para los JSON")
    parser.add_argument("--importer", help="RFC del importador esperado (activa checks cruzados de RFC)")
    parser.add_argument("--model", help="override del modelo Bedrock (solo aplica a PDFs)")
    parser.add_argument(
        "--type",
        help="TYPE_ID declarado. Obligatorio para .xml/.xlsx/.csv (no hay clasificación "
        "automática por visión sobre datos ya estructurados); opcional para .pdf (si se "
        "omite, el segmentador auto-clasifica).",
    )
    # pnpm forwards a literal "--" separator; argparse must not choke on it
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    from pipeline.expediente import process_pdf
    from pipeline.tabular_document import process_tabular
    from pipeline.xml_document import process_xml

    _SUPPORTED_EXTS = (".pdf", ".xml", ".xlsx", ".csv", ".jpg", ".jpeg", ".png")

    if args.file:
        p = Path(args.file)
        docs = [p] if p.exists() else sorted(
            f for f in Path(args.dir).glob("*")
            if f.suffix.lower() in _SUPPORTED_EXTS and args.file.lower() in f.name.lower()
        )
    else:
        docs = sorted(
            f for f in Path(args.dir).glob("*") if f.suffix.lower() in _SUPPORTED_EXTS
        )
    if not docs:
        print(f"No encontré PDFs/XMLs/XLSX/CSVs/imágenes ({args.file or args.dir})")
        return 2

    expected_values = {"IMPORTER_RFC": args.importer} if args.importer else {}
    out_dir = Path(args.out)
    # Comparing two models means two runs, and both would write
    # <out>/<archivo>.json — the second silently overwrites the first. When a
    # model is pinned for the run, give it its own subfolder so both survive.
    model_slug = args.model or os.environ.get("BEDROCK_MODEL_ID")
    if model_slug:
        out_dir = out_dir / re.sub(r"[^A-Za-z0-9]+", "-", model_slug).strip("-")
        print(f"salida de este modelo → {out_dir}/")
    out_dir.mkdir(parents=True, exist_ok=True)

    resumen_md = [
        f"# Resultados de procesamiento — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "| Archivo | Estado | Documentos detectados | Warnings |",
        "|---|---|---|---|",
    ]

    for doc_path in docs:
        print(f"\n{CYAN}{'=' * 72}\n{doc_path.name}{RESET}")
        started = time.time()
        ext = doc_path.suffix.lower().lstrip(".")
        try:
            if ext == "xml":
                result = process_xml(
                    doc_path.read_bytes(),
                    declared_type=args.type,
                    expected_values=expected_values,
                    source_name=doc_path.name,
                )
            elif ext in ("xlsx", "csv"):
                result = process_tabular(
                    doc_path.read_bytes(),
                    source_format=ext,
                    declared_type=args.type,
                    expected_values=expected_values,
                    source_name=doc_path.name,
                )
            else:
                # A field-centric type isn't necessarily backed by a PDF — a
                # photographed/scanned ID (INE, CSF, ...) is routinely a raw
                # JPG/PNG. process_pdf branches its page-loading internally on
                # source_format; anything else (a real .pdf, or an unknown
                # extension) keeps the existing pypdf path via the "pdf" default.
                result = process_pdf(
                    doc_path.read_bytes(),
                    declared_type=args.type,
                    expected_values=expected_values,
                    model_id=args.model,
                    source_name=doc_path.name,
                    source_format=ext if ext in ("jpg", "jpeg", "png") else "pdf",
                )
        except Exception as e:
            print(f"  {RED}ERROR: {e}{RESET}")
            resumen_md.append(f"| {doc_path.name} | ERROR | — | {e} |")
            continue

        status = result.get("status", "?")
        color = STATUS_COLOR.get(status, "")
        pages = result.get("pages", {})
        elapsed_wall = time.time() - started
        # elapsed_wall rounds to "0s" for the XML pipeline (no Bedrock/Textract
        # round-trip) — fall back to the pipeline's own millisecond figure,
        # which is accurate down to sub-second work instead of just wall time.
        duration = (
            f"{elapsed_wall * 1000:.0f}ms" if elapsed_wall < 1
            else f"{elapsed_wall:.0f}s"
        )
        source_note = SOURCE_FORMAT_NOTE.get(result.get("sourceFormat")) or (
            f"{pages.get('totalPages')} páginas "
            f"({pages.get('nativePages')} digitales, {pages.get('ocrPages')} OCR)"
        )
        print(f"  Estado: {color}{status}{RESET}  |  {source_note}  |  {duration}")

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

        out_file = out_dir / f"{doc_path.stem}.json"
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n  {DIM}JSON completo → {out_file}{RESET}")

        resumen_md.append(
            f"| {doc_path.name} | {status} | {'<br>'.join(detected) or '—'} | {'<br>'.join(warnings) or '—'} |"
        )

    resumen_path = out_dir / "_resumen.md"
    resumen_path.write_text("\n".join(resumen_md) + "\n", encoding="utf-8")
    print(f"\n{'=' * 72}\nResumen consolidado → {resumen_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
