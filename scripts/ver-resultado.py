#!/usr/bin/env python3
"""Consulta el resultado final de un documento procesado por el stack desplegado.

Busca la ejecución en Step Functions, extrae el resultado del enrichment
(incluido el detalle del expediente) y lo muestra en formato legible.
Opcionalmente guarda el JSON completo.

Uso:
    pnpm resultado                          # última ejecución
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

GREEN, RED, YELLOW, CYAN, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"
DEV_STACK = "DocumentValidationStack-Dev"
PROD_STACK = "DocumentValidationStack"
STATUS_COLOR = {
    "APPROVED": GREEN, "APROBADO": GREEN,
    "REJECTED": RED, "RECHAZADO": RED,
    "PARTIALLY_APPROVED": YELLOW, "REVIEW_REQUIRED": YELLOW, "REQUIERE_REVISION": YELLOW,
    "PROCESSING_ERROR": RED,
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


def find_state_machine(region: str, environment: str = "dev", stack_name: str | None = None) -> str:
    """Resolve the workflow from its owning stack, avoiding dev/prod ambiguity."""
    target_stack = stack_name or (PROD_STACK if environment == "prod" else DEV_STACK)
    stack = aws(
        ["cloudformation", "describe-stack-resources", "--stack-name", target_stack],
        region,
    )
    matches = [
        resource["PhysicalResourceId"]
        for resource in stack.get("StackResources", [])
        if resource.get("ResourceType") == "AWS::StepFunctions::StateMachine"
    ]
    if not matches:
        raise RuntimeError(
            f"El stack '{target_stack}' no existe o no contiene una state machine"
        )
    return matches[0]


def find_execution(sm_arn: str, doc_id: str | None, region: str) -> dict:
    executions = aws(
        ["stepfunctions", "list-executions", "--state-machine-arn", sm_arn, "--max-results", "50"],
        region,
    )["executions"]
    if not executions:
        raise RuntimeError("No hay ejecuciones registradas")
    if doc_id:
        for ex in executions:
            if doc_id.lower() in ex["name"].lower():
                return ex
        raise RuntimeError(f"No encontré ejecución para documentId '{doc_id}' en las últimas 50")
    return executions[0]


def enrichment_output(ex_arn: str, region: str) -> dict | None:
    """Walk the execution history backwards for the enrichment step output."""
    kwargs = ["stepfunctions", "get-execution-history", "--execution-arn", ex_arn,
              "--reverse-order", "--max-results", "150"]
    history = aws(kwargs, region)
    for ev in history.get("events", []):
        details = ev.get("stateExitedEventDetails") or {}
        if "nrichment" in (details.get("name") or ""):
            return json.loads(details["output"])
    return None


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
    entities = doc.get("extractedEntities", {})
    fields = doc.get("fields") or {}
    for name in KEY_FIELDS:
        slot = fields.get(name) or {}
        value = slot.get("normalizedValue") or slot.get("extractedValue") or entities.get(name)
        if value not in (None, "", []):
            conf = slot.get("confidence")
            conf_s = f" ({conf:.0%})" if isinstance(conf, (int, float)) else ""
            print(f"      {name} = {value}  {DIM}{slot.get('status', '')}{conf_s}{RESET}")
    arrays = {k: len(v) for k, v in entities.items() if isinstance(v, list) and v}
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

    sm = find_state_machine(args.region, args.env, args.stack)
    # S3 → SQS → workflow start takes a few seconds; with --wait, retry until
    # the execution shows up instead of failing immediately.
    deadline = time.time() + (300 if args.wait else 0)
    while True:
        try:
            ex = find_execution(sm, args.id, args.region)
            break
        except RuntimeError:
            if time.time() >= deadline:
                raise
            print(f"{DIM}   la ejecución aún no aparece; reintento en 10s...{RESET}")
            time.sleep(10)
    print(f"{DIM}Ejecución: {ex['name']}{RESET}")

    status = ex["status"]
    while status == "RUNNING":
        if not args.wait:
            print(f"{YELLOW}⏳ Aún procesando. Reintenta en un momento o usa --wait.{RESET}")
            return 1
        print(f"{DIM}   procesando... (reviso en 20s){RESET}")
        time.sleep(20)
        status = aws(["stepfunctions", "describe-execution", "--execution-arn",
                      ex["executionArn"]], args.region)["status"]

    if status != "SUCCEEDED":
        print(f"{RED}La ejecución terminó en {status}. Revisa: pnpm track --execution-arn {ex['executionArn']}{RESET}")
        return 1

    out = enrichment_output(ex["executionArn"], args.region)
    if out is None:
        print(f"{RED}No encontré el paso de enrichment en el historial.{RESET}")
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
                out = {**out, "expediente": full}
                print(f"{DIM}Detalle completo desde s3://{loc['bucket']}/{loc['key']}{RESET}")
        except Exception as e:
            print(f"{YELLOW}⚠ No pude leer el detalle de S3 ({e}); muestro el resumen compacto.{RESET}")

    exp = deep_find(out, "expediente")
    val = deep_find(out, "validation") or {}
    vs = val.get("status", "?")
    print(f"\nResultado: {STATUS_COLOR.get(vs, '')}{vs}{RESET}  {DIM}| {val.get('summary', '')}{RESET}")

    if exp:
        pages = exp.get("pages", {})
        # processingSeconds rounds to 2 decimals and reads as a flat "0.0s"
        # for anything under ~5ms (routine for the XML pipeline, which never
        # calls Bedrock/Textract) — processingMs keeps that visible.
        ms = exp.get("processingMs")
        duration = f"{ms:.0f}ms" if isinstance(ms, (int, float)) and ms < 1000 else f"{exp.get('processingSeconds', '?')}s"
        if exp.get("sourceFormat") == "xml":
            # "N páginas (M digitales, K OCR)" is PDF vocabulary that doesn't
            # apply to a single parsed XML document.
            source_note = "XML (extracción por XPath, sin Bedrock/Textract)"
        else:
            source_note = (
                f"{pages.get('totalPages')} páginas "
                f"({pages.get('nativePages')} digitales, {pages.get('ocrPages')} OCR)"
            )
        print(f"Expediente: {STATUS_COLOR.get(exp.get('status'), '')}{exp.get('status')}{RESET}  "
              f"| {source_note} | {duration}")
        for doc in exp.get("documents", []):
            print_document(doc)
        print()
        for c in exp.get("crossDocumentChecks", []):
            if c.get("skipped"):
                continue
            mark = f"{GREEN}✓" if c.get("valid") else (f"{YELLOW}!" if c.get("severity") == "warning" else f"{RED}✗")
            print(f"  {mark} {c.get('rule')}: {c.get('message')}{RESET}")
        for w in exp.get("warnings", []):
            print(f"  {YELLOW}⚠ {w}{RESET}")
    else:
        entities = deep_find(out, "extractedEntities") or {}
        for k, v in entities.items():
            print(f"   {k} = {v}")
        for c in (val.get("checks") or []):
            mark = f"{GREEN}✓" if c.get("valid") else f"{RED}✗"
            print(f"  {mark} {c.get('field')} [{c.get('rule')}]: {c.get('message')}{RESET}")

    if args.save:
        save_dir = Path(args.save)
        save_dir.mkdir(parents=True, exist_ok=True)
        dest = save_dir / f"{ex['name']}.json"
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n{DIM}JSON completo → {dest}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
