#!/usr/bin/env python3
"""E2E real: sube documentos por el API desplegado (S3 raw/ → SQS → Step
Functions → Lambdas → DynamoDB/S3) y valida el resultado final.

Dos modos:

- **Archivo único** (`--file`): el E2E de siempre. Infraestructura + ground
  truth de tests/integration/expected_pedimentos.json cuando el archivo lo
  declara para `--doc-type`.
- **Dataset** (sin `--file`): procesa TODOS los archivos de `--docs` en
  paralelo y termina con un análisis agregado (scripts/_e2e_analysis.py):
  documentos aprobados/en revisión/rechazados, confianza y validez por
  parámetro, los parámetros con índice más bajo, reglas que fallan y reglas
  inertes, coherencia interna del número de pedimento y de los importes,
  costo/tiempo, y diferencias contra la corrida anterior. Antes de gastar
  muestra el costo estimado y pide confirmación.

Distinto de scripts/test-pedimentos.py: ese script llama process_pdf()
directo en Python (rápido, barato, pero NUNCA toca S3/SQS/Step
Functions/DynamoDB). Este script sí pasa por la infraestructura real, así
que además de precisión de campos valida cosas que solo pueden romperse ahí:
- que el documentId se resuelva bien desde la key de S3,
- que la ejecución de Step Functions termine en SUCCEEDED y no en
  ExecutionTimedOut,
- que el archivo se mueva de raw/ a processed/ (o failed/) al terminar,
- que el GET público, la descarga del original, la búsqueda por unique key y
  la auditoría de costo respondan.

Costo: igual que procesar cada documento normalmente (Textract + Bedrock).
Tiempo: minutos, no segundos — corre Step Functions real.

Uso:
    pnpm test:e2e:pedimento -- --file normal_pend.pdf
    pnpm test:e2e:pedimento -- --docs ./test-docs/full-dataset-pedimentos --doc-type PEDIMENTO_ERP --env prod
    pnpm test:e2e:pedimento -- --docs <carpeta> --doc-type PEDIMENTO_ERP --limit 2 --concurrency 2
    pnpm test:e2e:pedimento -- --docs <carpeta> --doc-type PEDIMENTO_ERP --min-approval-rate 0.8 --min-field-index 90
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO / "test-docs" / "pedimentos docs" / "docs"
EXPECTED_FILE = REPO / "tests" / "integration" / "expected_pedimentos.json"
DEV_STACK = "DocumentValidationStack-Dev"
PROD_STACK = "DocumentValidationStack"

# Mismas extensiones que acepta upload-lambda (ALLOWED_EXTENSIONS).
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".xml", ".xlsx", ".csv"}

GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # @dataclass resuelve anotaciones buscando su módulo en sys.modules
    spec.loader.exec_module(mod)
    return mod


# Reutiliza la lógica ya probada de ver-resultado.py (localizar la state
# machine/ejecución, leer el paso de enrichment), de test-pedimentos.py
# (comparar contra el ground truth) y de quality_history.py (carpeta privada
# de la corrida) en vez de duplicarla.
vr = _load_module("ver_resultado_e2e", REPO / "scripts" / "ver-resultado.py")
tp = _load_module("test_pedimentos_e2e", REPO / "scripts" / "test-pedimentos.py")
ea = _load_module("e2e_analysis", REPO / "scripts" / "_e2e_analysis.py")
qh = _load_module("quality_history_e2e", REPO / "scripts" / "quality_history.py")
ce = _load_module("cost_estimate_e2e", REPO / "scripts" / "_cost_estimate.py")


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


def upload(endpoint: str, pdf_path: Path, doc_type: str, api_key: str | None = None,
           metadata: dict | None = None) -> dict:
    """Sube el documento con el flujo real de la API: dos pasos, siempre.

    1. `POST /documents` con solo metadata → devuelve `uploadUrl`.
    2. `PUT` del archivo directo a S3, **sin ninguna cabecera extra**.

    Al prefirmar, SigV4 hoistea los `x-amz-*` al query string, así que la
    metadata del documento ya viaja dentro de la URL y cubierta por la firma
    (`X-Amz-SignedHeaders=host`). Mandarla ADEMÁS como header hace que S3
    responda `403 AccessDenied: There were headers present in the request
    which were not signed`.
    """
    body = json.dumps({
        "fileName": pdf_path.name, "documentType": doc_type,
        "metadata": metadata or {"source": "test-e2e-pedimento"},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(f"{endpoint}/documents", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            presign = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"POST /documents → HTTP {e.code}: {e.read().decode()[:500]}")

    put = urllib.request.Request(
        presign["uploadUrl"],
        data=pdf_path.read_bytes(),
        method="PUT",
    )
    try:
        urllib.request.urlopen(put, timeout=300)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"PUT a S3 → HTTP {e.code}: {e.read().decode()[:500]}")
    return presign


def api_json(url: str, api_key: str | None, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read() or b"{}")
        except ValueError:
            payload = {}
        return error.code, payload


# ---------------------------------------------------------------------------
# Un caso
# ---------------------------------------------------------------------------

@dataclass
class Target:
    """Stack desplegado contra el que corren los casos (se resuelve una vez)."""
    env: str
    stack: str
    region: str
    endpoint: str
    bucket: str
    state_machine: str
    api_key: str | None


def resolve_target(env: str, stack: str | None, region: str, api_key: str | None) -> Target:
    stack_name = stack or (PROD_STACK if env == "prod" else DEV_STACK)
    return Target(
        env=env, stack=stack_name, region=region,
        endpoint=api_endpoint(stack_name, region), bucket=bucket_name(stack_name, region),
        state_machine=vr.find_state_machine(region, env, stack), api_key=api_key,
    )


class CaseLog:
    """Salida de un caso. En archivo único imprime en vivo, como siempre; en
    dataset acumula las líneas para que los casos paralelos no se intercalen."""

    def __init__(self, live: bool):
        self.live = live
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)
        if self.live:
            print(text)

    def text(self) -> str:
        return "\n".join(self.lines)


def run_case(target: Target, pdf_path: Path, *, doc_type: str, wait: int, spec: dict | None = None,
             run_id: str | None = None, exercise_metadata: bool = False,
             unique_fields: list[str] | tuple[str, ...] = (),
             log: Callable[[str], None] = print) -> tuple[dict, dict | None]:
    """Sube un archivo, espera su ejecución real y valida infraestructura y
    ground truth. Devuelve (resumen del caso, resultado completo de S3 o None)."""
    t0 = time.time()
    metadata = {"source": "test-real-e2e", "test-suite": "real-e2e"}
    if run_id:
        metadata["test-run-id"] = run_id
    upload_result = upload(target.endpoint, pdf_path, doc_type, api_key=target.api_key, metadata=metadata)
    doc_id = upload_result.get("documentId")
    if not doc_id:
        raise RuntimeError(f"El API no devolvió documentId: {upload_result}")
    log(f"documentId: {doc_id}  {DIM}(subida en {time.time()-t0:.1f}s){RESET}")

    deadline = t0 + wait
    ex = None
    while time.time() < deadline:
        try:
            ex = vr.find_execution(target.state_machine, doc_id, target.region)
            break
        except RuntimeError:
            log(f"{DIM}   la ejecución aún no aparece en Step Functions; reintento en 10s...{RESET}")
            time.sleep(10)
    if ex is None:
        log(f"{RED}La ejecución nunca apareció en Step Functions tras {wait}s — "
            f"revisa la SQS DLQ o los logs del consumer Lambda.{RESET}")
        return {
            "schemaVersion": 1, "file": pdf_path.name, "documentId": doc_id, "docType": doc_type,
            "environment": target.env, "stack": target.stack, "executionArn": None,
            "executionStatus": "NOT_FOUND", "infraPassed": False, "groundTruthPassed": None, "passed": False,
            "durationSeconds": round(time.time() - t0, 3),
            "error": f"la ejecución no apareció en Step Functions tras {wait}s",
        }, None

    status = ex["status"]
    sfn_started = time.time()
    while status == "RUNNING" and time.time() < deadline:
        time.sleep(15)
        status = vr.aws(
            ["stepfunctions", "describe-execution", "--execution-arn", ex["executionArn"]],
            target.region,
        )["status"]
    elapsed_sfn = time.time() - sfn_started

    log(f"\nStep Functions: {(GREEN if status == 'SUCCEEDED' else RED)}{status}{RESET}  {DIM}({elapsed_sfn:.0f}s){RESET}")

    infra_ok = True
    if status != "SUCCEEDED":
        infra_ok = False
        log(f"{RED}La ejecución no terminó en SUCCEEDED. Esto es exactamente el tipo de falla de\n"
            f"infraestructura (timeout, addCatch, SQS) que test-pedimentos.py NO puede detectar\n"
            f"porque nunca pasa por Step Functions.{RESET}")
        log(f"Detalle: pnpm track --execution-arn {ex['executionArn']}")

    out = vr.enrichment_output(ex["executionArn"], target.region) if infra_ok else None
    documents: list = []
    exp: dict | None = None
    if out is not None:
        loc = vr.deep_find(out, "resultLocation")
        if isinstance(loc, dict) and loc.get("bucket") and loc.get("key"):
            body = subprocess.run(
                ["aws", "s3", "cp", f"s3://{loc['bucket']}/{loc['key']}", "-", "--region", target.region],
                capture_output=True, text=True, timeout=60,
            )
            if body.returncode == 0 and body.stdout.strip():
                out = {**out, "expediente": json.loads(body.stdout)}
        exp = vr.deep_find(out, "expediente") or {}
        documents = exp.get("documents", [])
        if not documents:
            infra_ok = False
            log(f"{RED}El resultado no trae 'documents' — ¿process_pdf() cambió de forma, o falló antes de extraer?{RESET}")

    log(f"\n{DIM}--- Infraestructura ---{RESET}")
    # El documentId real puede llevar sufijo/prefijo distinto al de la key original;
    # nos basta verificar que raw/ para este id específico ya no exista (se movió o falló).
    still_in_raw = subprocess.run(
        ["aws", "s3api", "list-objects-v2", "--bucket", target.bucket, "--prefix", f"raw/{doc_type}/",
         "--region", target.region, "--query", f"Contents[?contains(Key, '{doc_id}')].Key", "--output", "text"],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    if still_in_raw and still_in_raw != "None":
        infra_ok = False
        log(f"  {RED}✗ el archivo sigue en raw/: {still_in_raw}{RESET} (nunca se movió a processed/ ni failed/)")
    else:
        log(f"  {GREEN}✓{RESET} el archivo ya no está en raw/ (se movió tras procesar)")

    processed_keys = subprocess.run(
        ["aws", "s3api", "list-objects-v2", "--bucket", target.bucket, "--prefix", f"processed/{doc_id}/",
         "--region", target.region, "--query", "Contents[].Key", "--output", "text"],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    if not processed_keys or processed_keys == "None":
        infra_ok = False
        log(f"  {RED}✗ no apareció el original bajo processed/{doc_id}/{RESET}")
    else:
        log(f"  {GREEN}✓{RESET} original persistido en processed/")

    api_status, public = api_json(f"{target.endpoint}/documents/{doc_id}?full=1", target.api_key)
    if api_status != 200 or public.get("processingStatus") != "FINISHED":
        infra_ok = False
        log(f"  {RED}✗ GET público no terminó correctamente (HTTP {api_status}){RESET}")
    else:
        log(f"  {GREEN}✓{RESET} GET público devuelve FINISHED")
    public_result = public.get("result") if isinstance(public.get("result"), dict) else {}

    file_status, file_meta = api_json(f"{target.endpoint}/documents/{doc_id}/file", target.api_key)
    original_hash_ok = False
    if file_status == 200 and file_meta.get("downloadUrl"):
        try:
            with urllib.request.urlopen(file_meta["downloadUrl"], timeout=300) as response:
                original_hash_ok = hashlib.sha256(response.read()).hexdigest() == hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        except Exception:
            original_hash_ok = False
    if not original_hash_ok:
        infra_ok = False
        log(f"  {RED}✗ la descarga del original no conserva el SHA-256{RESET}")
    else:
        log(f"  {GREEN}✓{RESET} descarga del original con SHA-256 idéntico")

    metadata_ok = None
    if exercise_metadata:
        marker = (run_id or "manual")[-40:]
        patch_status, _ = api_json(f"{target.endpoint}/documents/{doc_id}/metadata", target.api_key, "PATCH",
                                   {"quality-canary": marker})
        _, refreshed = api_json(f"{target.endpoint}/documents/{doc_id}", target.api_key)
        refreshed_metadata = refreshed.get("metadata") or (refreshed.get("result") or {}).get("metadata") or {}
        metadata_ok = patch_status == 200 and refreshed_metadata.get("quality-canary") == marker
        if not metadata_ok:
            infra_ok = False
            log(f"  {RED}✗ PATCH/GET de metadata no conservó el valor canario{RESET}")
        else:
            log(f"  {GREEN}✓{RESET} PATCH/GET de metadata confirmado")

    applicable, declared = ea.applicable_expectations(spec, doc_type)
    ground_truth_note = None
    if not spec:
        ground_truth_note = "sin ground truth"
    elif not applicable:
        ground_truth_note = f"ground truth declarado para {', '.join(declared)}, no para {doc_type}"

    log(f"\n{DIM}--- Comparación contra ground truth ({pdf_path.name}) ---{RESET}")
    file_ok: bool | None = None
    if not applicable:
        log(f"  {DIM}(omitida: {ground_truth_note}){RESET}")
    elif documents:
        file_ok = True
        for exp_doc in applicable:
            ok, notes, matched_doc = tp._check_doc_expectation(exp_doc, documents)
            color = GREEN if ok else RED
            log(f"  {color}{'PASS' if ok else 'FAIL'}{RESET} expectation {exp_doc.get('type')}")
            for n in notes:
                log(f"      {n}")
            if matched_doc:
                tp._print_engine_checks(matched_doc, out=log)
            file_ok = file_ok and ok
    else:
        file_ok = False
        log(f"  {YELLOW}(sin documents que comparar — ver fallas de infraestructura arriba){RESET}")

    evaluations = [tp.evaluate_expectation(exp_doc, documents) for exp_doc in applicable]

    # Búsqueda por unique key: el campo que el catálogo declara `unique_key`,
    # con el valor del ground truth si existe o, si no, el extraído. Sin unique
    # key declarada queda el camino de siempre (NUMERO_PEDIMENTO_NORMALIZADO).
    search_ok = None
    search_field = search_value = None
    for name in unique_fields:
        truth = next(((e.get("fields") or {}).get(name) for e in applicable if (e.get("fields") or {}).get(name)), None)
        extracted = next((ea.top_value(d.get("extractedEntities") or {}, name) for d in documents
                          if d.get("documentType") == doc_type
                          and not ea.is_empty(ea.top_value(d.get("extractedEntities") or {}, name))), None)
        if truth or extracted:
            search_field, search_value = name, truth or extracted
            break
    if search_field is None and applicable:
        legacy = (applicable[0].get("fields") or {}).get("NUMERO_PEDIMENTO_NORMALIZADO")
        if legacy:
            search_field, search_value = "NUMERO_PEDIMENTO_NORMALIZADO", legacy
    if search_field:
        query = urllib.parse.urlencode({"docType": doc_type, "field": search_field, "value": str(search_value)})
        search_status, search_body = api_json(f"{target.endpoint}/documents?{query}", target.api_key)
        search_ok = search_status == 200 and doc_id in json.dumps(search_body)
        if not search_ok:
            infra_ok = False
            log(f"  {RED}✗ búsqueda por {search_field}={search_value} no devolvió este documento{RESET}")
        else:
            log(f"  {GREEN}✓{RESET} búsqueda por {search_field} confirmada")

    cost_table = "document-validation-costs" + ("-dev" if target.env == "dev" else "")
    cost_recorded = False
    cost_usd = None
    cost_breakdown = None
    for _ in range(6):
        cost_lookup = subprocess.run(
            ["aws", "dynamodb", "get-item", "--table-name", cost_table,
             "--key", json.dumps({"DocumentId": {"S": doc_id}}), "--region", target.region, "--output", "json"],
            capture_output=True, text=True, timeout=20,
        )
        try:
            item = json.loads(cost_lookup.stdout or "{}").get("Item") if cost_lookup.returncode == 0 else None
        except ValueError:
            item = None
        cost_recorded = bool(item) and (item.get("DocumentId") or {}).get("S") == doc_id
        if cost_recorded:
            cost_breakdown = cost_breakdown_from_item(item)
            cost_usd = cost_breakdown["totalUsd"]
            break
        time.sleep(3)
    if not cost_recorded:
        infra_ok = False
        log(f"  {RED}✗ no apareció el registro de auditoría de costo{RESET}")
    else:
        log(f"  {GREEN}✓{RESET} auditoría de costo persistida")

    overall_ok = infra_ok and file_ok is not False
    total_elapsed = time.time() - t0
    gt_label = {True: "ok", False: "FALLÓ", None: "n/a"}[file_ok]
    log(f"\n{GREEN if overall_ok else RED}{'E2E PASS' if overall_ok else 'E2E FAIL'}{RESET}  "
        f"{DIM}(infra={'ok' if infra_ok else 'FALLÓ'}, ground-truth={gt_label}, total {total_elapsed:.0f}s){RESET}")
    summary = {
        "schemaVersion": 1, "file": pdf_path.name, "documentId": doc_id, "docType": doc_type,
        "environment": target.env, "stack": target.stack, "executionArn": ex.get("executionArn"),
        "executionStatus": status, "infraPassed": infra_ok, "groundTruthPassed": file_ok,
        "groundTruthNote": ground_truth_note, "passed": overall_ok, "durationSeconds": round(total_elapsed, 3),
        "publicApiPassed": api_status == 200 and public.get("processingStatus") == "FINISHED",
        "businessStatus": (public_result.get("validation") or {}).get("status"),
        "documentClassification": public_result.get("documentClassification"),
        "originalHashPassed": original_hash_ok, "metadataRoundTripPassed": metadata_ok,
        "uniqueKeySearchPassed": search_ok, "uniqueKeySearchField": search_field,
        "costRecordPassed": cost_recorded, "costUsd": cost_usd, "costBreakdown": cost_breakdown,
        "evaluations": evaluations,
    }
    return summary, exp


def _ddb_plain(value: Any) -> Any:
    """Un atributo en JSON de DynamoDB (`{"N": "1.5"}`, `{"M": {...}}`) a Python."""
    if not isinstance(value, dict) or len(value) != 1:
        return value
    (kind, inner), = value.items()
    if kind == "N":
        return float(inner)
    if kind == "M":
        return {k: _ddb_plain(v) for k, v in inner.items()}
    if kind == "L":
        return [_ddb_plain(v) for v in inner]
    if kind == "NULL":
        return None
    return inner


def cost_breakdown_from_item(item: dict) -> dict:
    """Lo cobrado a ESTE documento según la auditoría, en las mismas piezas que el estimado."""
    plain = {k: _ddb_plain(v) for k, v in item.items()}
    services = plain.get("Services") or {}
    bedrock = {name: svc for name, svc in services.items() if name.startswith("Bedrock")}
    extraction = services.get("BedrockExtraction") or {}
    return {
        "totalUsd": float(plain.get("TotalCostUsd") or 0),
        "textractUsd": float((services.get("TextractOcr") or {}).get("costUsd") or 0),
        "bedrockUsd": round(sum(float(s.get("costUsd") or 0) for s in bedrock.values()), 6),
        "lambdaUsd": float((services.get("LambdaCompute") or {}).get("costUsd") or 0),
        "inputTokens": int(sum(float(s.get("inputTokens") or 0) for s in bedrock.values())),
        "outputTokens": int(sum(float(s.get("outputTokens") or 0) for s in bedrock.values())),
        "ocrPages": int((services.get("TextractOcr") or {}).get("pages") or 0),
        "model": extraction.get("modelId"), "pricingVersion": plain.get("PricingVersion"),
    }


def _write_private(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Estimación de costo (antes de gastar)
# ---------------------------------------------------------------------------

def list_dataset_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith("."))


def estimate_plan(groups: list[tuple[str, list[Path]]], env: str, stack: str | None, region: str) -> list[dict]:
    """Estimado de costo por tipo, calculado como lo cobra el pipeline
    (scripts/_cost_estimate.py): modelo del Lambda desplegado, páginas OCR,
    selección de páginas, llamadas y tokens reales del prompt."""
    deployment = ce.load_deployment(env, stack, region)
    calibration = ce.load_calibration()
    return [ce.estimate_files(files, doc_type, deployment, calibration) for doc_type, files in groups]


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:.2f}" if value >= 0.1 else f"${value:.4f}"


def print_plan(plans: list[dict], expected: dict, *, detail: bool) -> dict:
    """Tabla del plan de costo. Devuelve los totales de todos los tipos."""
    print(f"{DIM}  {'Tipo':<30}{'Modelo':<11}{'Arch':>5}{'Págs':>6}{'OCR':>5}{'Llam':>5}"
          f"{'Textract':>10}{'Bedrock':>10}{'Lambda':>9}{'Estimado':>10}{'Tope':>9}{RESET}")
    totals = {"files": 0, "pages": 0, "ocrPages": 0, "calls": 0, "textractUsd": 0.0, "bedrockUsd": 0.0,
              "lambdaUsd": 0.0, "estimateUsd": 0.0, "ceilingUsd": 0.0}
    for plan in plans:
        t = plan["total"]
        models = ",".join(ce.model_alias(m) for m in t["models"]) or "—"
        print(f"  {t['docType'][:29]:<30}{models[:10]:<11}{t['files']:>5}{t['pages']:>6}{t['ocrPages']:>5}{t['calls']:>5}"
              f"{_money(t['textractUsd']):>10}{_money(t['bedrockUsd']):>10}{_money(t['lambdaUsd']):>9}"
              f"{BOLD}{_money(t['estimateUsd']):>10}{RESET}{_money(t['ceilingUsd']):>9}")
        for key in totals:
            totals[key] += t.get(key, 0)
        if detail:
            for f in plan["files"]:
                if "error" in f:
                    print(f"    {RED}✗ {f['file']}: {f['error']}{RESET}")
                    continue
                gt = "GT " if ea.applicable_expectations((expected.get("files") or {}).get(f["file"]), t["docType"])[0] else "   "
                sel = f"{f['extractPages']}/{f['pages']} pág a IA" if f["extractPages"] != f["pages"] else f"{f['pages']} pág a IA"
                print(f"    {DIM}{gt}{f['file'][:38]:<39}{sel:<18}{f['calls']:>3} llam  in {f['inputTokens']:>7,} out {f['outputTokens']:>6,}"
                      f"  {_money(f['totalUsd']):>8} (tope {_money(f['totalHighUsd'])}){'' if f.get('exact') else ' ~'}{RESET}")
        if not detail:
            for f in t["errors"]:
                print(f"    {RED}✗ {f['file']}: {f['error']}{RESET}")
        if t["uncertain"]:
            print(f"    {YELLOW}~ {len(t['uncertain'])} archivo(s) con páginas escaneadas: el texto OCR y las páginas que "
                  f"elegirá la selección se estiman con la calibración (el tope cubre el peor caso){RESET}")
        if t["uncalibratedModels"]:
            print(f"    {YELLOW}! sin calibración para {', '.join(t['uncalibratedModels'])}: tokens estimados con valores por default{RESET}")
    return totals


def print_plan_footer(plans: list[dict]) -> None:
    """De dónde sale el estimado y qué tan bien ha acertado contra costos reales."""
    info = plans[0]["total"] if plans else {}
    deployment = info.get("deployment") or {}
    for warning in deployment.get("warnings") or []:
        print(f"  {YELLOW}! {warning}{RESET}")
    accuracy = (info.get("calibration") or {}).get("accuracy") or {}
    source = ("modelo y memoria leídos del Lambda desplegado" if deployment.get("source") == "lambda"
              else "modelo por default del código")
    typical = (f"error típico medido ±{accuracy['medianAbsErrTotal'] * 100:.0f}% contra {accuracy['records']} documentos reales"
               if accuracy.get("medianAbsErrTotal") is not None else "sin validación contra costos reales todavía")
    print(f"{DIM}  Estimado = Textract (exacto) + Bedrock (tokens del prompt real × tarifa del modelo) + Lambda; {source}. "
          f"Tope = selección en fail-open y salida en p90. {typical} (pnpm costos:validar).{RESET}")


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        sys.stdout.flush()
        print(f"{RED}Sin terminal interactiva para confirmar el gasto: agrega --yes.{RESET}", file=sys.stderr)
        return False
    return input(question).strip().lower() in {"y", "yes", "s", "si", "sí"}


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def execute_dataset(target: Target, files: list[Path], *, doc_type: str, type_spec: dict | None, expected: dict,
                    out_dir: Path, run_id: str, wait: int, concurrency: int, docs_dir: Path,
                    exercise_metadata: bool = False, min_approval_rate: float | None = None,
                    min_field_index: float | None = None, previous: dict | None = None,
                    estimate: dict | None = None) -> dict:
    """Corre todos los archivos en paralelo, persiste cada caso y devuelve el
    análisis agregado (ya impreso y escrito en `out_dir/analisis.{json,md}`).
    `estimate` es el plan de costo de estos archivos: cada caso guarda su
    estimado para compararlo con lo que la auditoría registró."""
    total = len(files)
    lock = threading.Lock()
    unique_fields = ea.unique_key_fields(type_spec)
    estimates = {f["file"]: f for f in (estimate or {}).get("files") or []}

    def work(index: int, path: Path) -> dict:
        log = CaseLog(live=False)
        started = time.time()
        with lock:
            print(f"{DIM}[{index}/{total}] ▶ {path.name}{RESET}", flush=True)
        try:
            summary, result = run_case(
                target, path, doc_type=doc_type, wait=wait, spec=(expected.get("files") or {}).get(path.name),
                run_id=run_id, exercise_metadata=exercise_metadata and index == 1,
                unique_fields=unique_fields, log=log,
            )
        except Exception as error:  # un caso roto no tumba la corrida: queda registrado como falla
            log(f"{RED}ERROR: {type(error).__name__}: {error}{RESET}")
            summary, result = {
                "schemaVersion": 1, "file": path.name, "docType": doc_type, "environment": target.env,
                "passed": False, "infraPassed": False, "groundTruthPassed": None,
                "error": f"{type(error).__name__}: {str(error)[:300]}", "durationSeconds": round(time.time() - started, 3),
            }, None
        summary["fixtureSha256"] = qh.sha256_file(path)
        if path.name in estimates:
            summary["costEstimate"] = ce.file_estimate_summary(estimates[path.name])
        _write_private(out_dir / "cases" / f"{index:03d}.json", summary)
        _write_private(out_dir / "cases" / f"{index:03d}.result.json", result or {})
        _write_private(out_dir / "logs" / f"{index:03d}-{path.stem}.log", log.text())
        docs = [d for d in (result or {}).get("documents") or [] if d.get("documentType") == doc_type]
        statuses = ",".join(d.get("status") or "?" for d in docs) or summary.get("executionStatus") or "ERROR"
        cost = f"${summary['costUsd']:.4f}" if summary.get("costUsd") is not None else "—"
        if (summary.get("costEstimate") or {}).get("totalUsd") is not None:
            cost += f" (est. ${summary['costEstimate']['totalUsd']:.4f})"
        with lock:
            mark = f"{GREEN}✓{RESET}" if summary.get("passed") else f"{RED}✗{RESET}"
            print(f"{mark} [{index}/{total}] {path.name} — {statuses} · negocio {summary.get('businessStatus') or '—'}"
                  f" · {summary.get('durationSeconds', 0):.0f}s · {cost}", flush=True)
            if not summary.get("passed"):
                print("\n".join(f"      {line}" for line in log.text().splitlines() if line.strip()), flush=True)
        return {"file": path.name, "summary": summary, "result": result}

    pool = ThreadPoolExecutor(max_workers=max(1, concurrency))
    try:
        futures = [pool.submit(work, i, p) for i, p in enumerate(files, 1)]
        cases = [f.result() for f in futures]
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrumpido. Las ejecuciones ya enviadas siguen corriendo en AWS; "
              f"los casos terminados quedaron en {out_dir}.{RESET}", flush=True)
        pool.shutdown(wait=False, cancel_futures=True)
        os._exit(130)
    pool.shutdown()

    analysis = ea.analyze(cases, doc_type, type_spec, env=target.env, docs_dir=str(docs_dir),
                          min_approval_rate=min_approval_rate, min_field_index=min_field_index)
    ea.apply_comparison(analysis, ea.compare_runs(analysis, previous))
    print(ea.render_text(analysis, color=sys.stdout.isatty()))
    _write_private(out_dir / "analisis.json", analysis)
    _write_private(out_dir / "analisis.md", ea.render_markdown(analysis))
    return analysis


def dataset_main(args: argparse.Namespace, api_key: str | None, expected: dict) -> int:
    docs_dir = Path(args.docs)
    files = list_dataset_files(docs_dir)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"{RED}No hay archivos soportados ({', '.join(sorted(SUPPORTED_SUFFIXES))}) en {docs_dir}{RESET}")
        return 2
    type_spec = ea.load_type_spec(args.doc_type)
    if type_spec is None:
        print(f"{YELLOW}AVISO: '{args.doc_type}' no está en catalogs/base ni catalogs/custom — el análisis por "
              f"parámetro no sabrá qué campos son obligatorios.{RESET}")
    wait = args.wait or 1200

    print(f"{CYAN}=== E2E dataset: {len(files)} archivo(s) de {docs_dir} como {args.doc_type} "
          f"contra {args.env.upper()} ==={RESET}")
    print(f"{DIM}Plan de costo (lectura local de páginas y del Lambda desplegado; sin llamadas pagadas):{RESET}")
    plan = estimate_plan([(args.doc_type, files)], args.env, args.stack, args.region)[0]
    print_plan([plan], expected, detail=True)
    print_plan_footer([plan])
    est = plan["total"]
    print(f"{DIM}  Concurrencia {args.concurrency}, espera máx {wait}s por archivo.{RESET}")
    if args.max_cost_usd is not None and est["ceilingUsd"] > args.max_cost_usd:
        print(f"{RED}El tope ${est['ceilingUsd']:.2f} excede --max-cost-usd ${args.max_cost_usd:.2f}; cancelado.{RESET}")
        return 2
    env_label = f"{RED}{BOLD}PROD{RESET}" if args.env == "prod" else args.env
    if not confirm(f"¿Procesar {len(files)} archivo(s) en {env_label} por ≈ ${est['estimateUsd']:.2f} "
                   f"(tope ${est['ceilingUsd']:.2f})? [y/N] ", args.yes):
        return 130

    target = resolve_target(args.env, args.stack, args.region, api_key)
    print(f"API:    {target.endpoint}\nBucket: {target.bucket}\n")
    history_root = Path(args.history_dir)
    run_dir, record = qh.create_run("e2e-dataset", args.env, history_root)
    previous = ea.load_analysis(Path(args.compare_with)) if args.compare_with else (
        None if args.no_compare else ea.find_previous_analysis(history_root / args.env, args.doc_type, docs_dir.name, exclude=run_dir))

    started = time.monotonic()
    analysis = execute_dataset(
        target, files, doc_type=args.doc_type, type_spec=type_spec, expected=expected, out_dir=run_dir,
        run_id=record["runId"], wait=wait, concurrency=args.concurrency, docs_dir=docs_dir,
        exercise_metadata=args.exercise_metadata, min_approval_rate=args.min_approval_rate,
        min_field_index=args.min_field_index, previous=previous, estimate=plan,
    )
    record["summary"] = ea.run_summary(analysis, estimate=est, duration_seconds=time.monotonic() - started)
    record["cases"] = [{"file": f["file"], "passed": bool(f["passed"]), "documentId": f["documentId"]}
                       for f in analysis["perFile"]]
    record["completedAt"] = qh.utc_now()
    record["status"] = "PASSED" if analysis["verdict"]["level"] != "NO_CONFIABLE" else "FAILED"
    qh.write_record(run_dir, record)
    print(f"\nReporte → {run_dir / 'analisis.md'}\nDatos   → {run_dir / 'analisis.json'} (casos y logs en la misma carpeta)")
    return 0 if record["status"] == "PASSED" else 1


def single_main(args: argparse.Namespace, api_key: str | None, expected: dict) -> int:
    pdf_path = Path(args.docs) / args.file
    if not pdf_path.exists():
        print(f"{RED}No existe {pdf_path}{RESET}")
        return 2
    spec = (expected.get("files") or {}).get(args.file)
    if not spec:
        print(f"{YELLOW}No hay ground truth para '{args.file}' en {EXPECTED_FILE.relative_to(REPO)}: "
              f"se valida infraestructura, sin comparar valores.{RESET}")

    stack = args.stack or (PROD_STACK if args.env == "prod" else DEV_STACK)
    print(f"{CYAN}=== E2E real: {args.file} contra '{stack}' ({args.env}) ==={RESET}")
    print(f"{DIM}Flujo: API POST /documents → S3 raw/ → SQS → Step Functions → Lambdas → DynamoDB/S3{RESET}\n")
    target = resolve_target(args.env, args.stack, args.region, api_key)
    print(f"API:    {target.endpoint}")
    print(f"Bucket: {target.bucket}")

    summary, result = run_case(
        target, pdf_path, doc_type=args.doc_type, wait=args.wait or 600, spec=spec, run_id=args.run_id,
        exercise_metadata=args.exercise_metadata, unique_fields=ea.unique_key_fields(ea.load_type_spec(args.doc_type)),
    )
    if args.json_out:
        target_path = Path(args.json_out)
        _write_private(target_path, summary)
        _write_private(target_path.with_suffix(".result.json"), result or {})
    return 0 if summary["passed"] else 1


class UnixHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """`--help` con secciones de página man (USO, DESCRIPCIÓN, ARGUMENTOS,
    OPCIONES, EJEMPLOS, ENTORNO, ARCHIVOS, ESTADO DE SALIDA), en español como
    el resto del CLI. Compartido con test-e2e-suite.py."""

    def __init__(self, prog: str, **kwargs: Any) -> None:
        kwargs.setdefault("max_help_position", 34)
        super().__init__(prog, **kwargs)

    def add_usage(self, usage, actions, groups, prefix=None):  # noqa: D102 - firma de argparse
        super().add_usage(usage, actions, groups, prefix="USO:\n")


def unix_parser(prog: str, usage: str, description: str, epilog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, usage=usage, description=description, epilog=epilog,
                                     formatter_class=UnixHelpFormatter, add_help=False)
    parser._positionals.title = "ARGUMENTOS"
    parser._optionals.title = "OPCIONES"
    parser.add_argument("-h", "--help", action="help", help="muestra esta ayuda y sale")
    return parser


USAGE = """\
  %(prog)s -f ARCHIVO [-d DIR] [-t TIPO] [-e ENV] [-w SEG]
  %(prog)s -d DIR -t TIPO [-e ENV] [-n N] [-j N] [-y] [-m USD]
                          [--min-approval-rate R] [--min-field-index I]"""

DESCRIPTION = """\
DESCRIPCIÓN:
  E2E real contra el stack desplegado: sube documentos por POST /documents,
  espera su ejecución de Step Functions y valida infraestructura, API pública,
  auditoría de costo y ground truth (tests/integration/expected_pedimentos.json).

  Con -f procesa un archivo. Sin -f procesa todos los archivos de DIR como TIPO:
  muestra el costo estimado, pide confirmación y termina con un análisis de
  confianza (aprobados, índice por parámetro, reglas, coherencia, costo)."""

EPILOG = """\
EJEMPLOS:
  pnpm test:e2e:pedimento -f normal_pend.pdf
      un archivo contra dev
  pnpm test:e2e:pedimento -d ./test-docs/full-dataset-pedimentos -t PEDIMENTO_ERP -n 2
      prueba barata: los 2 primeros archivos del dataset
  pnpm test:e2e:pedimento -d ./test-docs/full-dataset-pedimentos -t PEDIMENTO_ERP -e prod
      dataset completo contra prod
  pnpm test:e2e:pedimento -d DIR -t PEDIMENTO_ERP -y -m 5 --min-field-index 90
      sin preguntar (CI): cancela si el tope pasa de $5, falla si un obligatorio baja de 90

ENTORNO:
  API_KEY       obligatoria con -e prod; ahí nunca se lee sola
  API_KEY_APP   en dev, se usa automáticamente si .env.dev tiene DEV_API_KEY_REQUIRED=true

ARCHIVOS:
  resultados-pruebas/<env>/<corrida>/analisis.md    reporte del dataset (y .json, casos, logs)
  tests/integration/expected_pedimentos.json        ground truth por nombre de archivo

ESTADO DE SALIDA:
  0     terminó (con -f: E2E PASS; sin -f: veredicto distinto de NO CONFIABLE)
  1     con -f: E2E FAIL; sin -f: veredicto NO CONFIABLE
  2     no se pudo empezar (sin archivos, sin API_KEY en prod, tope mayor que -m)
  130   cancelado en la confirmación o con Ctrl+C

Guía completa: documentacion/TESTS.md, secciones 0 y 5."""


def build_parser() -> argparse.ArgumentParser:
    parser = unix_parser("pnpm test:e2e:pedimento", USAGE, DESCRIPTION, EPILOG)
    parser.add_argument("-f", "--file", metavar="ARCHIVO", help="procesa solo este archivo de DIR")
    parser.add_argument("-d", "--docs", metavar="DIR", default=str(DOCS_DIR),
                        help="carpeta de documentos (default: test-docs/pedimentos docs/docs)")
    parser.add_argument("-t", "--doc-type", metavar="TIPO", default="PEDIMENTO_ADUANAL",
                        help="TYPE_ID declarado al subir (default PEDIMENTO_ADUANAL)")
    parser.add_argument("-e", "--env", metavar="ENV", choices=("dev", "prod"), default="dev", help="dev | prod (default dev)")
    parser.add_argument("-w", "--wait", metavar="SEG", type=int,
                        help="espera máxima por archivo (default 600 con -f, 1200 sin -f)")
    parser.add_argument("--stack", metavar="NOMBRE", help="stack explícito; gana sobre -e")
    parser.add_argument("--region", metavar="REGION", default="us-east-1", help="default us-east-1")
    parser.add_argument("--run-id", metavar="ID", help="etiqueta test-run-id en la metadata del documento")
    parser.add_argument("--json-out", metavar="RUTA", help="con -f: escribe el resumen del caso en RUTA")
    parser.add_argument("--exercise-metadata", action="store_true", help="prueba PATCH de metadata (sin -f: solo el 1er archivo)")
    dataset = parser.add_argument_group("OPCIONES DE DATASET (sin -f)")
    dataset.add_argument("-n", "--limit", metavar="N", type=int, help="solo los primeros N archivos")
    dataset.add_argument("-j", "--concurrency", metavar="N", type=int, default=3, help="archivos en paralelo (default 3)")
    dataset.add_argument("-y", "--yes", action="store_true", help="no pedir confirmación del costo")
    dataset.add_argument("-m", "--max-cost-usd", metavar="USD", type=float, help="cancela si el tope estimado supera USD")
    dataset.add_argument("--min-approval-rate", metavar="R", type=float, help="NO CONFIABLE si la tasa de APPROVED < R (0-1)")
    dataset.add_argument("--min-field-index", metavar="I", type=float, help="NO CONFIABLE si un obligatorio tiene índice < I (0-100)")
    dataset.add_argument("--compare-with", metavar="JSON", help="compara contra ese analisis.json (default: la corrida anterior)")
    dataset.add_argument("--no-compare", action="store_true", help="no compara contra la corrida anterior")
    dataset.add_argument("--history-dir", metavar="DIR", default=str(qh.DEFAULT_HISTORY),
                         help="raíz de resultados (default resultados-pruebas/)")
    return parser


def main() -> int:
    args = build_parser().parse_args([a for a in sys.argv[1:] if a != "--"])
    api_key = resolve_api_key(args.env)
    if args.env == "prod" and not api_key:
        print(f"{RED}prod exige x-api-key: exporta API_KEY antes de correr (no se autodetecta en prod, a propósito; "
              f"ver documentacion/AMBIENTES_Y_SEGURIDAD.md).{RESET}", file=sys.stderr)
        return 2
    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    return single_main(args, api_key, expected) if args.file else dataset_main(args, api_key, expected)


if __name__ == "__main__":
    sys.exit(main())
