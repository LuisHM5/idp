#!/usr/bin/env python3
"""Consulta el resultado final de un documento procesado por el stack desplegado.

Lee la fila del documento en DynamoDB, toma el resultado del enrichment
(incluido el detalle del expediente en S3) y lo muestra en formato legible.
Opcionalmente guarda el JSON completo.

Uso:
    pnpm resultado                          # último documento
    pnpm resultado -- --id e07e3e90         # por documentId (prefijo basta)
    pnpm resultado -- --id e07e3e90 --wait  # espera si aún está corriendo
    pnpm resultado -- --env prod --id e07e3e90
    pnpm resultado -- --save resultados/    # además guarda el JSON completo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envelope_shape import array_counts, documents_of, field_confidence, field_value  # noqa: E402

GREEN, RED, YELLOW, CYAN, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"
DEV_STACK = "DocumentValidationStack-Dev"
PROD_STACK = "DocumentValidationStack"
STATUS_COLOR = {
    "APPROVED": GREEN, "APROBADO": GREEN,
    "REJECTED": RED, "RECHAZADO": RED,
    "PARTIALLY_APPROVED": YELLOW, "REVIEW_REQUIRED": YELLOW, "REQUIERE_REVISION": YELLOW,
    "PROCESSING_ERROR": RED,
}

# "N páginas (M digitales, K OCR)" is PDF vocabulary that doesn't apply to a
# deterministically-parsed source — one label per non-PDF sourceFormat;
# absent from the dict = PDF page breakdown.
SOURCE_FORMAT_NOTE = {
    "xml": "XML (extracción por XPath, sin Bedrock/Textract)",
    "xlsx": "XLSX (extracción por celda/columna, sin Bedrock/Textract)",
    "csv": "CSV (extracción por columna, sin Bedrock/Textract)",
}

KEY_FIELDS = [
    "NUMERO_PEDIMENTO_NORMALIZADO", "NUMERO_PEDIMENTO", "PEDIMENTO_REFERENCE",
    "UUID", "EMISOR_RFC", "RECEPTOR_RFC", "RFC_IMPORTADOR_EXPORTADOR",
    "SUBTOTAL", "TOTAL", "TOTAL_PEDIMENTO", "IMPORTE_TOTAL",
    "FECHA_EMISION", "FECHA_PAGO", "INVOICE_NUMBER", "NUMERO_E_DOCUMENT",
]


def aws(args: list[str], region: str) -> dict:
    out = subprocess.run(
        ["aws", *args, "--region", region, "--output", "json"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return json.loads(out.stdout) if out.stdout.strip() else {}


DOCUMENT_TABLE_LOGICAL_ID = "BedrockDocumentProcessingDocumentProcessingTable"
FAILURE_WORKFLOW_STATUSES = {
    "classification-failure", "processing-failure", "enrichment-failure", "post-processing-failure",
}


def find_document_table(region: str, environment: str = "dev", stack_name: str | None = None) -> str:
    """Resolve the documents table from its owning stack, avoiding dev/prod ambiguity."""
    target_stack = stack_name or (PROD_STACK if environment == "prod" else DEV_STACK)
    stack = aws(
        ["cloudformation", "describe-stack-resources", "--stack-name", target_stack],
        region,
    )
    matches = [
        resource["PhysicalResourceId"]
        for resource in stack.get("StackResources", [])
        if resource.get("ResourceType") == "AWS::DynamoDB::Table"
        and resource.get("LogicalResourceId", "").startswith(DOCUMENT_TABLE_LOGICAL_ID)
    ]
    if not matches:
        raise RuntimeError(
            f"El stack '{target_stack}' no existe o no contiene la tabla de documentos"
        )
    return matches[0]


def _plain(item: dict) -> dict:
    """Flatten the string/bool attributes of a DynamoDB JSON item."""
    return {
        name: value.get("S", value.get("BOOL"))
        for name, value in item.items()
        if "S" in value or "BOOL" in value
    }


def find_job(table: str, doc_id: str | None, region: str) -> dict:
    """The tracking row of a document: exact id, id prefix, or the most recent."""
    if doc_id:
        item = aws(
            ["dynamodb", "get-item", "--table-name", table, "--consistent-read",
             "--key", json.dumps({"DocumentId": {"S": doc_id}})],
            region,
        ).get("Item")
        if item:
            return _plain(item)
    rows = [
        _plain(item)
        for item in aws(
            ["dynamodb", "scan", "--table-name", table, "--projection-expression",
             "DocumentId, ProcessingStatusUpdatedAt, RegisteredAt"],
            region,
        ).get("Items", [])
    ]
    if doc_id:
        rows = [row for row in rows if row["DocumentId"].lower().startswith(doc_id.lower())]
        if not rows:
            raise RuntimeError(f"No encontré el documento '{doc_id}'")
    if not rows:
        raise RuntimeError("No hay documentos registrados")
    latest = max(rows, key=lambda r: r.get("ProcessingStatusUpdatedAt") or r.get("RegisteredAt") or "")
    return find_job(table, latest["DocumentId"], region)


def job_status(job: dict) -> str:
    """Public lifecycle of a row — same rule as query-lambda's processingStatusFor."""
    if job.get("EnrichmentResult") or job.get("WorkflowStatus") == "complete":
        return "FINISHED"
    if job.get("WorkflowStatus") in FAILURE_WORKFLOW_STATUSES:
        return "FAILED"
    return job.get("ProcessingStatus") or "AWAITING_UPLOAD"


def enrichment_output(job: dict) -> dict | None:
    """The envelope enrichment stored on the row, or None while there is none."""
    raw = job.get("EnrichmentResult")
    return json.loads(raw) if raw else None


def deep_find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = deep_find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_find(v, key)
            if r is not None:
                return r
    return None


def print_document(doc: dict) -> None:
    ds = doc.get("status", "-")
    print(f"\n   📄 p{doc.get('pageStart')}-{doc.get('pageEnd')}  "
          f"{doc.get('documentType')}  →  {STATUS_COLOR.get(ds, '')}{ds}{RESET}")
    for name in KEY_FIELDS:
        value = field_value(doc, name)
        if value not in (None, "", []):
            conf = field_confidence(doc, name)
            conf_s = f" ({conf:.0%})" if isinstance(conf, (int, float)) else ""
            status = ((doc.get("fields") or {}).get(name) or {}).get("status", "")
            print(f"      {name} = {value}  {DIM}{status}{conf_s}{RESET}")
    arrays = {k: n for k, n in array_counts(doc).items() if n}
    if arrays:
        print(f"      {DIM}arrays: " + ", ".join(f"{k}×{n}" for k, n in arrays.items()) + RESET)
    for c in [c for c in doc.get("checks", []) if not c.get("valid") and not c.get("skipped")][:5]:
        sev = RED if c.get("severity") == "error" else YELLOW
        print(f"      {sev}✗ {c.get('field')}: {c.get('message')}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="documentId (o prefijo) de la ejecución a consultar")
    parser.add_argument("--wait", action="store_true", help="esperar si la ejecución sigue corriendo")
    parser.add_argument("--save", help="carpeta donde guardar el JSON completo")
    parser.add_argument("--env", choices=("dev", "prod"), default="dev",
                        help="ambiente a consultar (default: dev)")
    parser.add_argument("--stack", help="stack explícito (gana sobre --env)")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    table = find_document_table(args.region, args.env, args.stack)
    job = find_job(table, args.id, args.region)
    print(f"{DIM}Documento: {job['DocumentId']}{RESET}")

    # With --wait, allow for the Lambda's 15-minute cap plus its two async
    # retries before giving up on a document that is still in flight.
    deadline = time.time() + (1200 if args.wait else 0)
    status = job_status(job)
    while status not in ("FINISHED", "FAILED"):
        if time.time() >= deadline:
            print(f"{YELLOW}⏳ Aún en {status}. Reintenta en un momento o usa --wait.{RESET}")
            return 1
        print(f"{DIM}   {status}... (reviso en 5s){RESET}")
        time.sleep(5)
        job = find_job(table, job["DocumentId"], args.region)
        status = job_status(job)

    if status == "FAILED":
        print(f"{RED}El procesamiento falló: {job.get('ProcessingError') or job.get('WorkflowStatus')}{RESET}")
        return 1

    out = enrichment_output(job)
    if out is None:
        print(f"{RED}La fila terminó sin resultado de enrichment.{RESET}")
        return 1

    # Large expedientes: the workflow payload only carries a compact summary;
    # the full per-field detail lives in S3 (resultLocation pointer).
    loc = deep_find(out, "resultLocation")
    if isinstance(loc, dict) and loc.get("bucket") and loc.get("key"):
        try:
            body = subprocess.run(
                ["aws", "s3", "cp", f"s3://{loc['bucket']}/{loc['key']}", "-",
                 "--region", args.region],
                capture_output=True, text=True, timeout=60,
            )
            if body.returncode == 0 and body.stdout.strip():
                full = json.loads(body.stdout)
                # El resultado completo de S3 tiene la MISMA raíz que el
                # envelope v2 (documents/crossDocumentChecks/pages), solo que
                # más profunda: se puede usar tal cual en lugar del envelope.
                out = {**out, **full}
                print(f"{DIM}Detalle completo desde s3://{loc['bucket']}/{loc['key']}{RESET}")
        except Exception as e:
            print(f"{YELLOW}⚠ No pude leer el detalle de S3 ({e}); muestro el resumen compacto.{RESET}")

    exp = out if documents_of(out) else deep_find(out, "expediente")
    val = deep_find(out, "validation") or {}
    vs = val.get("status", "?")
    print(f"\nResultado: {STATUS_COLOR.get(vs, '')}{vs}{RESET}  {DIM}| {val.get('summary', '')}{RESET}")

    if exp and documents_of(exp):
        pages = exp.get("pages", {})
        # processingSeconds rounds to 2 decimals and reads as a flat "0.0s"
        # for anything under ~5ms (routine for the XML pipeline, which never
        # calls Bedrock/Textract) — processingMs keeps that visible.
        # Duration now lives at the envelope's top-level `tracking` (see
        # handler.py::_run_expediente); exp.get(...) is only the fallback for
        # results persisted before that (old compact-envelope shape) or for
        # the raw S3 full-detail merge above (which still carries its own
        # copy at the pipeline-result level, untouched by that change).
        tracking = out.get("tracking") or {}
        ms = tracking.get("processingMs", exp.get("processingMs"))
        seconds = tracking.get("processingSeconds", exp.get("processingSeconds"))
        duration = f"{ms:.0f}ms" if isinstance(ms, (int, float)) and ms < 1000 else f"{seconds if seconds is not None else '?'}s"
        upload_to_result = tracking.get("uploadToResultSeconds")
        total_note = (
            f" | PUT→resultado {upload_to_result}s"
            if isinstance(upload_to_result, (int, float)) else ""
        )
        source_note = SOURCE_FORMAT_NOTE.get(exp.get("sourceFormat")) or (
            f"{pages.get('totalPages')} páginas "
            f"({pages.get('nativePages')} digitales, {pages.get('ocrPages')} OCR)"
        )
        print(f"Expediente: {STATUS_COLOR.get(exp.get('status'), '')}{exp.get('status')}{RESET}  "
              f"| {source_note} | pipeline {duration}{total_note}")
        for doc in documents_of(exp):
            print_document(doc)
        print()
        for c in exp.get("crossDocumentChecks") or exp.get("checks") or []:
            if c.get("skipped"):
                continue
            mark = f"{GREEN}✓" if c.get("valid") else (f"{YELLOW}!" if c.get("severity") == "warning" else f"{RED}✗")
            print(f"  {mark} {c.get('rule')}: {c.get('message')}{RESET}")
        for w in exp.get("warnings", []):
            print(f"  {YELLOW}⚠ {w}{RESET}")
    else:
        for doc in documents_of(out):
            print_document(doc)
        for c in (val.get("checks") or []):
            mark = f"{GREEN}✓" if c.get("valid") else f"{RED}✗"
            print(f"  {mark} {c.get('field')} [{c.get('rule')}]: {c.get('message')}{RESET}")

    if args.save:
        save_dir = Path(args.save)
        save_dir.mkdir(parents=True, exist_ok=True)
        dest = save_dir / f"{job['DocumentId']}.json"
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n{DIM}JSON completo → {dest}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
