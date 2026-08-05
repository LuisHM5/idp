#!/usr/bin/env python3
"""E2E real: sube un PDF por el API desplegado (S3 raw/ → SQS → Step
Functions → Lambdas → DynamoDB/S3) y compara el resultado final contra el
ground truth de tests/integration/expected_pedimentos.json.

Distinto de scripts/test-pedimentos.py: ese script llama process_pdf()
directo en Python (rápido, barato, pero NUNCA toca S3/SQS/Step
Functions/DynamoDB). Este script sí pasa por la infraestructura real, así
que además de precisión de campos valida cosas que solo pueden romperse ahí:
- que el documentId se resuelva bien desde la key de S3 (el bug de
  clasificación manual que se arregló antes en esta misma sesión),
- que la ejecución de Step Functions termine en SUCCEEDED y no en
  ExecutionTimedOut (el workflowTimeout que se agregó como red de
  seguridad),
- que el archivo se mueva de raw/ a processed/ (o failed/) al terminar.

Costo: igual que procesar el documento normalmente (Textract + Bedrock).
Tiempo: minutos, no segundos — corre Step Functions real.

Uso:
    pnpm test:e2e:pedimento -- --file normal_pend.pdf
    .venv/bin/python scripts/test-e2e-pedimento.py --file normal_pend.pdf --env dev
    .venv/bin/python scripts/test-e2e-pedimento.py --file normal_pend.pdf --wait 900
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO / "test-docs" / "pedimentos docs" / "docs"
EXPECTED_FILE = REPO / "tests" / "integration" / "expected_pedimentos.json"
DEV_STACK = "DocumentValidationStack-Dev"
PROD_STACK = "DocumentValidationStack"

GREEN, RED, YELLOW, CYAN, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Reutiliza la lógica ya probada de ver-resultado.py (localizar la state
# machine/ejecución, leer el paso de enrichment) y de test-pedimentos.py
# (comparar contra el ground truth) en vez de duplicarla.
vr = _load_module("ver_resultado_e2e", REPO / "scripts" / "ver-resultado.py")
tp = _load_module("test_pedimentos_e2e", REPO / "scripts" / "test-pedimentos.py")


def api_endpoint(stack: str, region: str) -> str:
    out = subprocess.run(
        ["aws", "cloudformation", "describe-stacks", "--stack-name", stack,
         "--region", region, "--query",
         "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue", "--output", "text"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0 or not out.stdout.strip() or out.stdout.strip() == "None":
        raise RuntimeError(f"No pude obtener ApiEndpoint de {stack}: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip().rstrip("/")


def bucket_name(stack: str, region: str) -> str:
    out = subprocess.run(
        ["aws", "cloudformation", "describe-stack-resources", "--stack-name", stack,
         "--region", region, "--query",
         "StackResources[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId | [0]",
         "--output", "text"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0 or not out.stdout.strip() or out.stdout.strip() == "None":
        raise RuntimeError(f"No pude obtener el bucket de {stack}")
    return out.stdout.strip()


def s3_key_exists(bucket: str, key: str, region: str) -> bool:
    out = subprocess.run(
        ["aws", "s3api", "head-object", "--bucket", bucket, "--key", key, "--region", region],
        capture_output=True, text=True, timeout=20,
    )
    return out.returncode == 0


def _read_dotenv_var(path: Path, key: str) -> str | None:
    """Lee UNA variable de un archivo estilo dotenv sin cargarlo completo
    (evita pisar variables ya presentes en el proceso). None si el archivo o
    la variable no existen."""
    if not path.exists():
        return None
    value = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            value = v.strip().strip('"').strip("'")
    return value


def resolve_api_key(env: str) -> str | None:
    """prod SIEMPRE requiere API_KEY explícito (nunca se autodetecta ahí, a
    propósito). En dev, si no hay API_KEY ya exportado y .env.dev tiene
    DEV_API_KEY_REQUIRED=true, usa automáticamente API_KEY_APP (de .env.dev
    o ya exportado) — mismo criterio que scripts/enviar-documento.sh. Ver
    documentacion/AMBIENTES_Y_SEGURIDAD.md."""
    if os.environ.get("API_KEY"):
        return os.environ["API_KEY"]
    if env != "dev":
        return None
    dotenv = REPO / ".env.dev"
    required = os.environ.get("DEV_API_KEY_REQUIRED") or _read_dotenv_var(dotenv, "DEV_API_KEY_REQUIRED") or ""
    if required.strip().lower() != "true":
        return None
    key = os.environ.get("API_KEY_APP") or _read_dotenv_var(dotenv, "API_KEY_APP")
    if key:
        print(f"{DIM}DEV_API_KEY_REQUIRED=true detectado — usando API_KEY_APP automáticamente.{RESET}")
    else:
        print(f"{YELLOW}AVISO: DEV_API_KEY_REQUIRED=true pero no hay API_KEY_APP en .env.dev ni exportado — el upload probablemente reciba 403.{RESET}")
    return key


def upload(endpoint: str, pdf_path: Path, doc_type: str, api_key: str | None = None) -> dict:
    body = json.dumps({
        "fileName": pdf_path.name,
        "contentBase64": base64.b64encode(pdf_path.read_bytes()).decode(),
        "documentType": doc_type,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(
        f"{endpoint}/upload", data=body,
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"POST /upload → HTTP {e.code}: {e.read().decode()[:500]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, help="nombre del PDF en test-docs/pedimentos docs/docs")
    parser.add_argument("--docs", default=str(DOCS_DIR))
    parser.add_argument("--doc-type", default="PEDIMENTO_ADUANAL")
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument("--stack", help="stack explícito (gana sobre --env)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--wait", type=int, default=600, help="segundos máximo a esperar Step Functions (default 600)")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    stack = args.stack or (PROD_STACK if args.env == "prod" else DEV_STACK)
    pdf_path = Path(args.docs) / args.file
    if not pdf_path.exists():
        print(f"{RED}No existe {pdf_path}{RESET}")
        return 2

    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    spec = expected.get("files", {}).get(args.file)
    if not spec:
        print(f"{RED}No hay ground truth para '{args.file}' en {EXPECTED_FILE.relative_to(REPO)}{RESET}")
        return 2

    print(f"{CYAN}=== E2E real: {args.file} contra '{stack}' ({args.env}) ==={RESET}")
    print(f"{DIM}Flujo: API /upload → S3 raw/ → SQS → Step Functions → Lambdas → DynamoDB/S3{RESET}\n")

    endpoint = api_endpoint(stack, args.region)
    bucket = bucket_name(stack, args.region)
    print(f"API:    {endpoint}")
    print(f"Bucket: {bucket}")

    t0 = time.time()
    api_key = resolve_api_key(args.env)
    upload_result = upload(endpoint, pdf_path, args.doc_type, api_key=api_key)
    doc_id = upload_result.get("documentId")
    if not doc_id:
        print(f"{RED}El API no devolvió documentId: {upload_result}{RESET}")
        return 1
    print(f"documentId: {doc_id}  {DIM}(subida en {time.time()-t0:.1f}s){RESET}")

    sm = vr.find_state_machine(args.region, args.env, args.stack)
    deadline = t0 + args.wait
    ex = None
    while time.time() < deadline:
        try:
            ex = vr.find_execution(sm, doc_id, args.region)
            break
        except RuntimeError:
            print(f"{DIM}   la ejecución aún no aparece en Step Functions; reintento en 10s...{RESET}")
            time.sleep(10)
    if ex is None:
        print(f"{RED}La ejecución nunca apareció en Step Functions tras {args.wait}s — "
              f"revisa la SQS DLQ o los logs del consumer Lambda.{RESET}")
        return 1

    status = ex["status"]
    sfn_started = time.time()
    while status == "RUNNING" and time.time() < deadline:
        time.sleep(15)
        status = vr.aws(
            ["stepfunctions", "describe-execution", "--execution-arn", ex["executionArn"]],
            args.region,
        )["status"]
    elapsed_sfn = time.time() - sfn_started

    print(f"\nStep Functions: {(GREEN if status == 'SUCCEEDED' else RED)}{status}{RESET}  {DIM}({elapsed_sfn:.0f}s){RESET}")

    infra_ok = True
    if status != "SUCCEEDED":
        infra_ok = False
        print(f"{RED}La ejecución no terminó en SUCCEEDED. Esto es exactamente el tipo de falla de\n"
              f"infraestructura (timeout, addCatch, SQS) que test-pedimentos.py NO puede detectar\n"
              f"porque nunca pasa por Step Functions.{RESET}")
        print(f"Detalle: pnpm track --execution-arn {ex['executionArn']}")

    out = vr.enrichment_output(ex["executionArn"], args.region) if infra_ok else None
    documents: list = []
    if out is not None:
        loc = vr.deep_find(out, "resultLocation")
        if isinstance(loc, dict) and loc.get("bucket") and loc.get("key"):
            body = subprocess.run(
                ["aws", "s3", "cp", f"s3://{loc['bucket']}/{loc['key']}", "-", "--region", args.region],
                capture_output=True, text=True, timeout=60,
            )
            if body.returncode == 0 and body.stdout.strip():
                out = {**out, "expediente": json.loads(body.stdout)}
        exp = vr.deep_find(out, "expediente") or {}
        documents = exp.get("documents", [])
        if not documents:
            infra_ok = False
            print(f"{RED}El resultado no trae 'documents' — ¿process_pdf() cambió de forma, o falló antes de extraer?{RESET}")

    print(f"\n{DIM}--- Infraestructura ---{RESET}")
    raw_key_guess = f"raw/{args.doc_type}/{doc_id}"
    # El documentId real puede llevar sufijo/prefijo distinto al de la key original;
    # nos basta verificar que YA NO haya nada bajo processed/{documentId}/ ausente
    # y que raw/ para este id específico ya no exista (se movió o falló).
    still_in_raw = subprocess.run(
        ["aws", "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", f"raw/{args.doc_type}/",
         "--region", args.region, "--query", f"Contents[?contains(Key, '{doc_id}')].Key", "--output", "text"],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    if still_in_raw and still_in_raw != "None":
        infra_ok = False
        print(f"  {RED}✗ el archivo sigue en raw/: {still_in_raw}{RESET} (nunca se movió a processed/ ni failed/)")
    else:
        print(f"  {GREEN}✓{RESET} el archivo ya no está en raw/ (se movió tras procesar)")

    print(f"\n{DIM}--- Comparación contra ground truth ({args.file}) ---{RESET}")
    file_ok = True
    if documents:
        for exp_doc in spec.get("expect_documents", []):
            ok, notes, matched_doc = tp._check_doc_expectation(exp_doc, documents)
            color = GREEN if ok else RED
            print(f"  {color}{'PASS' if ok else 'FAIL'}{RESET} expectation {exp_doc.get('type')}")
            for n in notes:
                print(f"      {n}")
            if matched_doc:
                tp._print_engine_checks(matched_doc)
            file_ok = file_ok and ok
    else:
        file_ok = False
        print(f"  {YELLOW}(sin documents que comparar — ver fallas de infraestructura arriba){RESET}")

    overall_ok = infra_ok and file_ok
    total_elapsed = time.time() - t0
    print(f"\n{GREEN if overall_ok else RED}{'E2E PASS' if overall_ok else 'E2E FAIL'}{RESET}  "
          f"{DIM}(infra={'ok' if infra_ok else 'FALLÓ'}, ground-truth={'ok' if file_ok else 'FALLÓ'}, "
          f"total {total_elapsed:.0f}s){RESET}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
