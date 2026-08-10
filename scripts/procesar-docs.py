#!/usr/bin/env python3
"""Procesa documentos con el pipeline completo y deja los resultados listos
para revisión humana. Sin asserts: tú validas el resultado final.

Usa el pipeline localmente (misma lógica que la Lambda): necesita credenciales
AWS con Textract + Bedrock. NO necesita el stack desplegado.

Uso:
    pnpm procesar                                   # toda la carpeta default de pedimentos
    pnpm procesar -- "mi expediente.pdf"            # un archivo (ruta completa o nombre suelto)
    pnpm procesar -- /ruta/a/mis/docs               # una carpeta — se auto-detecta, sin --file/--dir
    pnpm procesar -- /ruta/a/mis/docs PEDIMENTO_ADUANAL                    # + tipo declarado (posicional)
    pnpm procesar -- /ruta/a/mis/docs PEDIMENTO_ADUANAL moonshotai.kimi-k2.5  # + modelo (posicional)
    pnpm procesar -- /ruta/a/mis/docs PEDIMENTO_ADUANAL --model moonshotai.kimi-k2.5  # mezcla posicional/flag
    pnpm procesar -- --out resultados/              # dónde dejar los JSON (default: ./resultados)
    pnpm procesar -- --importer OFU910626UQ0        # RFC del importador esperado (checks cruzados)
    pnpm procesar -- factura.xml --type FACTURA_HONORARIOS_XML
        # .xml usa pipeline/xml_document.py (extracción por XPath, sin Bedrock/Textract,
        # no necesita credenciales AWS) — --type es OBLIGATORIO para .xml, no hay
        # clasificación automática por visión sobre XML crudo.
    pnpm procesar -- reporte.xlsx --type MI_TIPO_XLSX
        # .xlsx/.csv usan pipeline/tabular_document.py (extracción por celda/columna,
        # mismas condiciones que .xml: --type obligatorio, sin Bedrock/Textract).
    pnpm procesar -- ine.jpeg --type INE
        # .jpg/.jpeg/.png (una foto/escaneo, no un PDF) usan pipeline/expediente.py
        # con pdf_pages.load_image_page (Textract sobre los bytes de la imagen
        # directo, sin pypdf) — SÍ llama Bedrock/Textract, igual que un PDF.

    Tres posicionales, todos opcionales y en este orden: PATH [TYPE] [MODEL].
    PATH es archivo O carpeta (se detecta solo con Path.is_dir()/is_file() —
    ya no hace falta decir --file o --dir). TYPE/MODEL posicionales son
    exactamente lo mismo que --type/--model — con ambos, el --flag gana (se
    avisa en consola si no coinciden). --file/--dir/--type/--model se
    mantienen por compatibilidad con scripts/docs viejos.

Nada se sobrescribe nunca — cada documento procesado queda numerado como
historial, y los tres archivos de ESE intento viven juntos en la misma
carpeta:

  <out>/[<modelo>/]<TIPO>/<nombre-archivo>/NNN/
      - <nombre-archivo>.json → resultado completo (todos los campos, checks,
        confianzas)
      - _log.txt   → copia exacta de la consola de ESE documento (sin
        colores), para no perder el detalle si se cierra la terminal
      - _resumen.md → costo real, $/página, duración, campos válidos,
        confianza promedio y tabla campo-por-campo de ESE documento

  NNN sube (001, 002, ...) cada vez que reprocesas el mismo archivo — con el
  mismo modelo o con otro — así que las corridas anteriores nunca se pierden,
  quedan listas para comparar. <TIPO> es el declarado (--type / 2do
  posicional) o el detectado si no se declaró ("EXPEDIENTE" si el
  segmentador encontró varios tipos en un mismo PDF).

  Consola: resumen legible por documento (tipo, estado, campos clave, costo
  real, warnings) según se van procesando, más un benchmark agregado al final
  si procesaste una carpeta con varios archivos (solo en consola, no se
  guarda — cada documento ya tiene su propio detalle guardado arriba).

Costo y calidad de campos son reales, no estimados: el costo sale de los tokens
Bedrock/páginas Textract que esta corrida realmente facturó (mismas tarifas
verificadas que `pipeline/pricing.py` usa en producción); "campos válidos" es el
veredicto del motor de reglas contra el catálogo (no hay ground truth manual para
un archivo tuyo arbitrario, así que no es "accuracy" contra un valor esperado —
es "¿pasó las reglas del catálogo con la confianza que reportó el modelo?").
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))

DEFAULT_DIR = REPO / "test-docs" / "pedimentos docs" / "docs"

GREEN, RED, YELLOW, CYAN, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"
ANSI_RE = re.compile(r"\033\[[0-9;]*m")

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


class _Tee:
    """Duplica cada write() a la terminal real y a un log en disco (sin ANSI).

    Sin esto, todo lo que se imprime solo vive en el scrollback de la
    terminal: si se cierra, se pierde el output completo de la corrida.
    """

    def __init__(self, real_stream, log_file):
        self._real = real_stream
        self._log = log_file

    def write(self, data):
        self._real.write(data)
        self._log.write(ANSI_RE.sub("", data))
        self._log.flush()
        return len(data)

    def flush(self):
        self._real.flush()
        self._log.flush()

    def isatty(self):
        return self._real.isatty()


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


def _walk_field_tree(node, path: str):
    """Yields (qualified_name, valid, confidence) for every scalar leaf under
    a resolved field-tree node (see validators.engine.resolve_field_tree:
    scalar leaf -> {value, confidence, valid, reason, source}; object ->
    {valid, reason, fields: {...}}; array -> {items: [...]})."""
    if not isinstance(node, dict):
        return
    if isinstance(node.get("items"), list):
        for item in node["items"]:
            yield from _walk_field_tree(item, path)
        return
    if isinstance(node.get("fields"), dict):
        for name, child in node["fields"].items():
            yield from _walk_field_tree(child, f"{path}.{name}" if path else name)
        return
    if "valid" in node:
        yield path, node.get("valid"), node.get("confidence")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-") or "x"


def _next_numbered_dir(base: Path) -> Path:
    """Crea y devuelve <base>/NNN, el siguiente número libre — nunca pisa uno
    ya usado, así <base>/ queda como historial completo de todas las corridas
    anteriores. mkdir() sin exist_ok fuerza a reintentar con el siguiente
    número si dos corridas arrancan al mismo tiempo (evita una carrera
    silenciosa donde ambas terminan escribiendo el mismo NNN)."""
    base.mkdir(parents=True, exist_ok=True)
    run_num = max((int(f.name) for f in base.iterdir() if f.is_dir() and f.name.isdigit()), default=0)
    while True:
        run_num += 1
        run_dir = base / f"{run_num:03d}"
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            continue


def _doc_type_slug(declared_type: str | None, result: dict) -> str:
    """Carpeta de tipo para el árbol de salida: el tipo declarado si se dio
    (--type o el 2do posicional); si no, el tipo que detectó el segmentador
    cuando resultó ser uno solo; "EXPEDIENTE" si detectó varios tipos
    distintos dentro del mismo archivo; "SIN-TIPO" si no detectó ninguno."""
    if declared_type:
        return declared_type
    types = {
        d.get("documentType") for d in result.get("documents", [])
        if d.get("documentType") and d.get("documentType") != "OTRO"
    }
    if len(types) == 1:
        return next(iter(types))
    if len(types) > 1:
        return "EXPEDIENTE"
    return "SIN-TIPO"


def field_quality(doc: dict) -> list[tuple[str, bool | None, float | None]]:
    """Per-field (name, valid, confidence) for every leaf the rule engine
    actually evaluated. This is validation-derived quality — does the value
    satisfy the catalog's rules, at the confidence the model reported — NOT
    ground-truth accuracy: there's no labeled expected value to diff against
    for an arbitrary user-supplied file (that comparison exists separately,
    against the fixed test-docs set, in scripts/test-pedimentos.py)."""
    if doc.get("fieldTreeShape", False):
        entities = doc.get("extractedEntities", {})
        out = []
        for name, node in entities.items():
            out.extend(_walk_field_tree(node, name))
        return out
    out = []
    for name, slot in (doc.get("fields") or {}).items():
        status = slot.get("status")
        if not status:
            continue
        out.append((name, status == "valid", slot.get("confidence")))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?",
        help="archivo o carpeta a procesar — se auto-detecta cuál es. "
        "Si no es una ruta que exista, se busca como nombre suelto dentro de --dir.",
    )
    parser.add_argument(
        "type_pos", nargs="?", metavar="TYPE",
        help="TYPE_ID declarado (2do posicional, ej. PEDIMENTO_ADUANAL) — alias corto de --type.",
    )
    parser.add_argument(
        "model_pos", nargs="?", metavar="MODEL",
        help="modelo Bedrock (3er posicional, ej. moonshotai.kimi-k2.5) — alias corto de --model.",
    )
    parser.add_argument("--file", help="[legado, usa el posicional] un archivo (ruta o nombre dentro de --dir)")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="carpeta con PDFs/XMLs/XLSX/CSVs (default si no se da posicional ni --file)")
    parser.add_argument("--out", default="resultados", help="carpeta de salida para los JSON")
    parser.add_argument("--importer", help="RFC del importador esperado (activa checks cruzados de RFC)")
    parser.add_argument("--model", help="[igual que el 3er posicional] override del modelo Bedrock (solo aplica a PDFs)")
    parser.add_argument(
        "--type",
        help="[igual que el 2do posicional] TYPE_ID declarado. Obligatorio para .xml/.xlsx/.csv (no hay "
        "clasificación automática por visión sobre datos ya estructurados); opcional para .pdf (si se "
        "omite, el segmentador auto-clasifica).",
    )
    # pnpm forwards a literal "--" separator; argparse must not choke on it
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    # El flag explícito gana si alguien da los dos (posicional Y --flag) con
    # valores distintos — poco común, pero mejor avisar que fallar en
    # silencio con el que perdió.
    if args.type and args.type_pos and args.type != args.type_pos:
        print(f"{YELLOW}--type ({args.type}) y el TYPE posicional ({args.type_pos}) no coinciden — usando --type{RESET}")
    declared_type = args.type or args.type_pos
    if args.model and args.model_pos and args.model != args.model_pos:
        print(f"{YELLOW}--model ({args.model}) y el MODEL posicional ({args.model_pos}) no coinciden — usando --model{RESET}")
    model_arg = args.model or args.model_pos

    expected_values = {"IMPORTER_RFC": args.importer} if args.importer else {}
    # Todo lo de esta corrida cuelga de model_root: <out>/[<modelo-slug>/].
    # Un modelo pinneado agrupa antes que nada, para no mezclar el historial
    # de un modelo con el de otro.
    model_slug = model_arg or os.environ.get("BEDROCK_MODEL_ID")
    model_root = Path(args.out)
    if model_slug:
        model_root = model_root / _slug(model_slug)
    model_root.mkdir(parents=True, exist_ok=True)

    real_stdout, real_stderr = sys.stdout, sys.stderr
    # Agregados de TODA la corrida — solo para el benchmark que se imprime al
    # final en consola (no se guarda en archivo aparte: cada documento ya
    # tiene su propio json+log+resumen juntos en su carpeta numerada).
    counts: dict[str, int] = {}
    bench_rows: list[dict] = []
    field_stats: dict[str, dict] = {}

    def save_doc_result(
        tipo: str, doc_path: Path, buf: io.StringIO,
        result_json: dict | None, resumen_lines: list[str] | None,
    ) -> Path:
        # El json, el log de consola y el resumen de ESTE documento en ESTA
        # corrida van todos juntos en la misma carpeta numerada — nada en un
        # lugar aparte.
        doc_dir = _next_numbered_dir(model_root / _slug(tipo) / doc_path.stem)
        if result_json is not None:
            (doc_dir / f"{doc_path.stem}.json").write_text(
                json.dumps(result_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        (doc_dir / "_log.txt").write_text(ANSI_RE.sub("", buf.getvalue()), encoding="utf-8")
        if resumen_lines is not None:
            (doc_dir / "_resumen.md").write_text("\n".join(resumen_lines) + "\n", encoding="utf-8")
        return doc_dir

    try:
        print(f"salida por documento → {model_root}/<TIPO>/<nombre-doc>/NNN/ (json + log + resumen juntos)")

        from pipeline.expediente import process_pdf
        from pipeline.pricing import bedrock_cost, textract_cost
        from pipeline.tabular_document import process_tabular
        from pipeline.xml_document import process_xml

        _SUPPORTED_EXTS = (".pdf", ".xml", ".xlsx", ".csv", ".jpg", ".jpeg", ".png")

        # Un solo argumento sirve para archivo o carpeta: se detecta con el
        # filesystem (is_dir/exists), no hace falta decir cuál es. --file
        # queda como alias legado del mismo target.
        target = args.path or args.file
        if target:
            p = Path(target)
            if p.is_dir():
                docs = sorted(f for f in p.glob("*") if f.suffix.lower() in _SUPPORTED_EXTS)
            elif p.exists():
                docs = [p]
            else:
                # No es una ruta real — se busca como nombre suelto/parcial
                # dentro de --dir (ej. "bad-pedimento" sin ruta completa).
                docs = sorted(
                    f for f in Path(args.dir).glob("*")
                    if f.suffix.lower() in _SUPPORTED_EXTS and target.lower() in f.name.lower()
                )
        else:
            docs = sorted(
                f for f in Path(args.dir).glob("*") if f.suffix.lower() in _SUPPORTED_EXTS
            )
        if not docs:
            print(f"No encontré PDFs/XMLs/XLSX/CSVs/imágenes ({target or args.dir})")
            return 2

        for doc_path in docs:
            buf = io.StringIO()
            sys.stdout = _Tee(real_stdout, buf)
            sys.stderr = _Tee(real_stderr, buf)
            result = None
            resumen_lines = None
            tipo_for_save = "ERROR"
            try:
                print(f"\n{CYAN}{'=' * 72}\n{doc_path.name}{RESET}")
                started = time.time()
                ext = doc_path.suffix.lower().lstrip(".")
                try:
                    if ext == "xml":
                        result = process_xml(
                            doc_path.read_bytes(),
                            declared_type=declared_type,
                            expected_values=expected_values,
                            source_name=doc_path.name,
                        )
                    elif ext in ("xlsx", "csv"):
                        result = process_tabular(
                            doc_path.read_bytes(),
                            source_format=ext,
                            declared_type=declared_type,
                            expected_values=expected_values,
                            source_name=doc_path.name,
                        )
                    else:
                        # A field-centric type isn't necessarily backed by a PDF —
                        # a photographed/scanned ID (INE, CSF, ...) is routinely a
                        # raw JPG/PNG. process_pdf branches its page-loading
                        # internally on source_format; anything else (a real
                        # .pdf, or an unknown extension) keeps the existing pypdf
                        # path via the "pdf" default.
                        result = process_pdf(
                            doc_path.read_bytes(),
                            declared_type=declared_type,
                            expected_values=expected_values,
                            model_id=model_arg,
                            source_name=doc_path.name,
                            source_format=ext if ext in ("jpg", "jpeg", "png") else "pdf",
                        )
                except Exception as e:
                    print(f"  {RED}ERROR: {e}{RESET}")
                    traceback.print_exc()
                    counts["ERROR"] = counts.get("ERROR", 0) + 1
                    tipo_for_save = declared_type or "ERROR"
                    continue

                status = result.get("status", "?")
                counts[status] = counts.get(status, 0) + 1
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

                # Costo real de ESTA corrida: tokens Bedrock/páginas Textract que
                # el pipeline acaba de facturar de verdad (no estimado). No incluye
                # cómputo Lambda — aquí corre localmente, no dentro de una Lambda.
                usage = result.get("usage") or {}
                segmentation, extraction = usage.get("segmentation") or {}, usage.get("extraction") or {}
                seg_cost = bedrock_cost(segmentation.get("usage"), segmentation.get("modelId"))
                ext_cost = bedrock_cost(extraction.get("usage"), extraction.get("modelId"))
                ocr_cost = textract_cost(pages.get("ocrPages"))
                doc_cost = round(seg_cost["costUsd"] + ext_cost["costUsd"] + ocr_cost["costUsd"], 6)
                total_pages = pages.get("totalPages") or 0
                cost_per_page = (doc_cost / total_pages) if total_pages else None

                cost_note = f"${doc_cost:.4f}" + (f" (${cost_per_page:.4f}/pág)" if cost_per_page is not None else "")
                print(f"  Estado: {color}{status}{RESET}  |  {source_note}  |  {duration}  |  {cost_note}")

                detected = []
                field_ok = field_total = 0
                conf_sum, conf_n = 0.0, 0
                doc_field_stats: dict[str, dict] = {}
                for doc in result.get("documents", []):
                    dt = doc.get("documentType")
                    ds = doc.get("status", "-")
                    dcolor = STATUS_COLOR.get(ds, "")
                    print(f"\n   📄 p{doc.get('pageStart')}-{doc.get('pageEnd')}  {dt}  →  {dcolor}{ds}{RESET}")
                    for line in summarize_doc(doc):
                        print(line)
                    if dt != "OTRO":
                        detected.append(f"{dt} ({ds})")
                    for name, valid, conf in field_quality(doc):
                        field_total += 1
                        stat = field_stats.setdefault(name, {"ok": 0, "total": 0, "conf_sum": 0.0, "conf_n": 0})
                        dstat = doc_field_stats.setdefault(name, {"ok": 0, "total": 0, "conf_sum": 0.0, "conf_n": 0})
                        stat["total"] += 1
                        dstat["total"] += 1
                        if valid:
                            field_ok += 1
                            stat["ok"] += 1
                            dstat["ok"] += 1
                        if isinstance(conf, (int, float)):
                            conf_sum += conf
                            conf_n += 1
                            stat["conf_sum"] += conf
                            stat["conf_n"] += 1
                            dstat["conf_sum"] += conf
                            dstat["conf_n"] += 1
                field_rate = (field_ok / field_total) if field_total else None
                conf_avg = (conf_sum / conf_n) if conf_n else None
                if field_total:
                    quality_note = f"campos válidos {field_ok}/{field_total} ({field_rate:.0%})"
                    if conf_avg is not None:
                        quality_note += f"  |  confianza prom. {conf_avg:.0%}"
                    print(f"  {DIM}{quality_note}{RESET}")

                bench_rows.append({
                    "duration_s": elapsed_wall,
                    "pages": total_pages,
                    "cost": doc_cost,
                    "field_ok": field_ok,
                    "field_total": field_total,
                })

                warnings = result.get("warnings", [])
                for w in warnings:
                    print(f"\n  {YELLOW}⚠ {w}{RESET}")
                for c in result.get("crossDocumentChecks", []):
                    if c.get("valid") and not c.get("skipped"):
                        print(f"  {GREEN}✓ {c.get('rule')}: {c.get('message')}{RESET}")

                tipo_for_save = _doc_type_slug(declared_type, result)
                field_cell = f"{field_ok}/{field_total}" + (f" ({field_rate:.0%})" if field_rate is not None else "")
                cost_per_page_cell = f"${cost_per_page:.4f}" if cost_per_page is not None else "—"
                conf_cell = f"{conf_avg:.0%}" if conf_avg is not None else "—"
                resumen_lines = [
                    f"# {doc_path.name} — {status}",
                    "",
                    f"Comando: `{' '.join(sys.argv)}`",
                    f"Procesado: {datetime.now():%Y-%m-%d %H:%M:%S}",
                    "",
                    "| Métrica | Valor |",
                    "|---|---|",
                    f"| Estado | {status} |",
                    f"| Documentos detectados | {'<br>'.join(detected) or '—'} |",
                    f"| Costo | ${doc_cost:.4f} ({cost_per_page_cell}/página) |",
                    f"| Duración | {duration} |",
                    f"| Páginas | {total_pages} |",
                    f"| Campos válidos | {field_cell} |",
                    f"| Confianza promedio | {conf_cell} |",
                    f"| Warnings | {'<br>'.join(warnings) or '—'} |",
                ]
                if doc_field_stats:
                    resumen_lines += ["", "| Campo | Válido | Confianza |", "|---|---|---|"]
                    worst_first = sorted(
                        doc_field_stats.items(),
                        key=lambda kv: (kv[1]["ok"] / kv[1]["total"]) if kv[1]["total"] else 1.0,
                    )
                    for name, s in worst_first:
                        rate = s["ok"] / s["total"] if s["total"] else None
                        conf = s["conf_sum"] / s["conf_n"] if s["conf_n"] else None
                        resumen_lines.append(
                            f"| {name} | {s['ok']}/{s['total']}"
                            + (f" ({rate:.0%})" if rate is not None else "")
                            + f" | {f'{conf:.0%}' if conf is not None else '—'} |"
                        )
            finally:
                sys.stdout, sys.stderr = real_stdout, real_stderr
                doc_dir = save_doc_result(tipo_for_save, doc_path, buf, result, resumen_lines)
                print(f"  {DIM}Guardado en → {doc_dir}/{RESET}")

        return 0
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrumpido por el usuario — progreso parcial guardado.{RESET}")
        return 130
    except Exception:
        print(f"\n{RED}Error inesperado — progreso parcial guardado.{RESET}")
        traceback.print_exc()
        return 1
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
        summary_line = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "sin documentos procesados"
        print(f"\n{'=' * 72}\nResumen por estado: {summary_line}")

        # Benchmark del dataset completo, solo en consola — misma agregación
        # si es un solo --file o una carpeta entera (bench_rows tiene una
        # fila por documento que sí llegó a procesarse). No se guarda en
        # archivo: cada documento ya tiene su propio detalle junto a su json.
        if bench_rows:
            n = len(bench_rows)
            total_cost = sum(r["cost"] for r in bench_rows)
            total_pages = sum(r["pages"] for r in bench_rows)
            total_duration = sum(r["duration_s"] for r in bench_rows)
            durations = sorted(r["duration_s"] for r in bench_rows)
            total_field_ok = sum(r["field_ok"] for r in bench_rows)
            total_field_total = sum(r["field_total"] for r in bench_rows)
            cost_per_page = (total_cost / total_pages) if total_pages else None
            duration_per_page = (total_duration / total_pages) if total_pages else None
            field_rate = (total_field_ok / total_field_total) if total_field_total else None

            stats_lines = [
                f"Documentos: {n}  ({total_pages} páginas)",
                f"Costo total: ${total_cost:.4f}  |  ${total_cost / n:.4f}/doc"
                + (f"  |  ${cost_per_page:.4f}/página" if cost_per_page is not None else ""),
                f"Duración: {total_duration:.1f}s total  |  {total_duration / n:.1f}s/doc "
                f"(min {durations[0]:.1f}s, max {durations[-1]:.1f}s)"
                + (f"  |  {duration_per_page:.2f}s/página" if duration_per_page is not None else ""),
            ]
            if field_rate is not None:
                stats_lines.append(
                    "Campos válidos (según reglas del catálogo, no ground truth manual): "
                    f"{total_field_ok}/{total_field_total} ({field_rate:.0%})"
                )

            print(f"\n{CYAN}Benchmark del dataset{RESET}")
            for line in stats_lines:
                print(f"  {line}")

            worst_first = sorted(
                field_stats.items(),
                key=lambda kv: (kv[1]["ok"] / kv[1]["total"]) if kv[1]["total"] else 1.0,
            )
            bad_fields = [(name, s) for name, s in worst_first if s["total"] and s["ok"] < s["total"]]
            if bad_fields:
                print(f"  {DIM}Campos con más fallas:{RESET}")
                for name, s in bad_fields[:5]:
                    print(f"    {YELLOW}{name}: {s['ok']}/{s['total']} ({s['ok'] / s['total']:.0%}){RESET}")


if __name__ == "__main__":
    sys.exit(main())
