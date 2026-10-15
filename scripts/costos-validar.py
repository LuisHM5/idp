#!/usr/bin/env python3
"""¿El costo estimado se parece al que AWS cobró? Lo mide contra documentos reales.

Toma los registros de la tabla de auditoría de costos (`document-validation-costs[-dev]`)
cuyo archivo original existe localmente (mismo nombre y mismo número de páginas),
baja su resultado completo de S3 y compara, servicio por servicio, el estimado
de `scripts/_cost_estimate.py` contra lo cobrado:

  Textract (páginas OCR), tokens de entrada, tokens de salida, Bedrock $, Lambda $ y total $.

El estimado se calcula con el MISMO modelo que corrió de verdad (así se mide la
reconstrucción, no si el modelo cambió desde entonces) y con una calibración que
EXCLUYE al archivo evaluado (leave-one-out), para que no se califique con sus
propios datos. También muestra el error del método anterior (páginas × $/página
promedio) para comparar.

Con `--write` regenera `scripts/cost_calibration.json` con todos los registros:
densidad de caracteres por token y caracteres por página OCR por modelo, tokens
de salida por llamada por modelo y por tipo, y la duración del Lambda.

Solo lectura en AWS (DynamoDB Scan, S3 GetObject, Lambda GetFunctionConfiguration).
Cero llamadas a Bedrock o Textract.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import _cost_estimate as ce  # noqa: E402
import _report_lib as rl  # noqa: E402

HISTORY = REPO / "resultados-pruebas" / "costos-validacion"
SKIP_TYPES = {"EXPEDIENTE_ADUANAL_MULTIDOCUMENTO", "OTRO"}
# Una página OCR con menos de 200 o más de 8000 caracteres derivados de tokens
# delata un prompt de otra versión del catálogo, no una página real.
OCR_CHARS_PLAUSIBLE = (200, 8000)
GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m"


def _load_e2e():
    spec = importlib.util.spec_from_file_location("e2e_for_costos", REPO / "scripts" / "test-e2e-pedimento.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e_for_costos"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Datos reales
# ---------------------------------------------------------------------------

def _bucket(env: str, region: str) -> Optional[str]:
    stack = "DocumentValidationStack" if env == "prod" else "DocumentValidationStack-Dev"
    out = subprocess.run(["aws", "cloudformation", "describe-stack-resources", "--stack-name", stack, "--region", region,
                          "--query", "StackResources[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId | [0]",
                          "--output", "text"], capture_output=True, text=True, timeout=30)
    value = out.stdout.strip()
    return value if out.returncode == 0 and value and value != "None" else None


def _result(env: str, bucket: str, document_id: str, region: str) -> Optional[dict]:
    cache = HISTORY / "cache" / env / f"{document_id}.json"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        got = subprocess.run(["aws", "s3", "cp", f"s3://{bucket}/results/{document_id}.json", str(cache),
                              "--region", region, "--only-show-errors"], capture_output=True, text=True, timeout=60)
        if got.returncode != 0:
            return None
        os.chmod(cache, 0o600)
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _local_pages(path: Path) -> Optional[int]:
    suffix = path.suffix.lower()
    if suffix in ce.IMAGE_SUFFIXES:
        return 1
    if suffix != ".pdf":
        return None
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def collect_records(envs: List[str], docs_root: Path, region: str, since: Optional[str]) -> List[dict]:
    local: Dict[str, List[Path]] = defaultdict(list)
    for path in docs_root.rglob("*"):
        if path.is_file():
            local[path.name].append(path)
    records = []
    for env in envs:
        bucket = _bucket(env, region)
        if not bucket:
            print(f"{YELLOW}No encontré el bucket de {env}; lo omito.{RESET}")
            continue
        with ce.pipeline_context(ce.Deployment(env=env, function=None, memory_mb=1024, variables={}, source="default")) as mods:
            pricing = mods[5]
        for raw in rl.scan_all(rl.cost_table_name(env), region):
            item = rl.normalize_cost_item(raw)
            services = item["services"]
            extraction = services.get("BedrockExtraction") or {}
            if item["document_type"] in SKIP_TYPES or not extraction.get("model_id"):
                continue
            if since and item["processed_at"][:10] < since:
                continue
            # Solo tipos declarados: la segmentación y el upstream no corrieron.
            if any((services.get(name) or {}).get("cost") for name in
                   ("BedrockSegmentation", "BedrockClassificationUpstream", "BedrockProcessingUpstream", "BedrockPageLocator")):
                continue
            candidates = local.get(item["source_file"]) or []
            path = next((p for p in candidates if _local_pages(p) == item["pages_total"]), None)
            if path is None:
                continue
            result = _result(env, bucket, item["document_id"], region)
            if not result:
                continue
            docs = [d for d in result.get("documents") or [] if d.get("documentType") == item["document_type"]]
            metas = [d.get("extractionMeta") or {} for d in docs]
            if not metas or not all(m.get("chunks") for m in metas):
                continue
            lam = services.get("LambdaCompute") or {}
            raw_extraction = (raw.get("Services") or {}).get("BedrockExtraction") or {}
            usage = {"inputTokens": int(raw_extraction.get("inputTokens") or 0),
                     "outputTokens": int(raw_extraction.get("outputTokens") or 0),
                     "cacheReadInputTokens": int(raw_extraction.get("cacheReadTokens") or 0),
                     "cacheWriteInputTokens": int(raw_extraction.get("cacheWriteTokens") or 0)}
            # Los tokens son el dato real; el $ guardado depende de las tarifas
            # vigentes el día que corrió. Antes de 2026-09-04 MiniMax/Kimi se
            # guardaban a tarifa de Haiku 4.5 (ver pipeline/pricing.py), así que se
            # re-tarifan con las de hoy para comparar peras con peras.
            bedrock_usd = pricing.bedrock_cost(usage, extraction["model_id"])["costUsd"]
            textract_usd = (services.get("TextractOcr") or {}).get("cost") or 0.0
            lambda_usd = lam.get("cost") or 0.0
            selections = [d.get("pageSelection") for d in docs if d.get("pageSelection")]
            records.append({
                "selection": selections[0] if len(selections) == 1 else None,
                "env": env, "documentId": item["document_id"], "processedAt": item["processed_at"],
                "file": item["source_file"], "path": str(path), "docType": item["document_type"],
                "model": extraction["model_id"], "pages": item["pages_total"], "ocrPages": item["pages_ocr"],
                "inputTokens": usage["inputTokens"], "outputTokens": usage["outputTokens"],
                "bedrockUsd": bedrock_usd, "textractUsd": textract_usd,
                "lambdaUsd": lambda_usd, "lambdaSeconds": (lam.get("duration_ms") or 0) / 1000,
                "totalUsd": round(bedrock_usd + textract_usd + lambda_usd, 6),
                "recordedTotalUsd": item["total_cost"], "pricingVersion": item["pricing_version"],
                "catalogVersion": json.dumps(((result.get("tracking") or {}).get("schema") or {}).get("catalogVersions")),
                "calls": sum(int(m.get("chunks") or 0) * int(m.get("schemaPartitions") or 1) for m in metas),
                "partitions": max(int(m.get("schemaPartitions") or 1) for m in metas),
                "extractedPages": sorted({n for m in metas for n in (m.get("pages") or [])}),
            })
    return records


# ---------------------------------------------------------------------------
# Estructura exacta de lo que se mandó (para calibrar)
# ---------------------------------------------------------------------------

def structure(record: dict, deployment: ce.Deployment, mods: Any) -> Optional[dict]:
    """Caracteres conocidos de las llamadas reales: prompt fijo + tool por
    llamada y texto nativo de las páginas que de verdad se extrajeron; las
    páginas OCR extraídas se dejan como incógnita (se calibran)."""
    document_catalog, bedrock, expediente, _, pdf_pages, _, _, text_extractor = mods
    entry = document_catalog.get_type_entry(record["docType"])
    path = Path(record["path"])
    if path.suffix.lower() in ce.IMAGE_SUFFIXES:
        pages = [pdf_pages.PageText(1, "", "empty", 0.0)]
    else:
        pages = pdf_pages.load_pages(path.read_bytes(), use_ocr=False)
    by_number = {p.number: p for p in pages}
    extracted = [by_number[n] for n in record["extractedPages"] if n in by_number]
    if not extracted:
        return None
    partitions = (text_extractor._field_tree_call_partitions(entry.field_tree)
                  if entry.field_tree else [("all", None)])
    fixed = 0
    for scope, tree in partitions:
        if entry.field_tree:
            prefix, _ = text_extractor._build_field_tree_prompts(entry, [], tree, scope)
            tool = json.dumps(text_extractor._field_tree_tool_schema(tree), ensure_ascii=False)
        else:
            prefix, _ = text_extractor._build_prompts(entry, [], arrays_only=False)
            tool = json.dumps(text_extractor._tool_schema(entry, arrays_only=False), ensure_ascii=False)
        fixed += len(prefix) + len(tool)
    chunks = max(1, record["calls"] // max(1, len(partitions)))
    header = len(text_extractor._pages_block([pdf_pages.PageText(1, "", "native", 1.0)]))  # "===== PAGINA 1 (native) =====\n"
    wrapper = chunks * len("TEXTO DEL DOCUMENTO:\n\n") + len(extracted) * (header + 2)
    native_chars = sum(len(p.text) for p in extracted if p.source != "empty")
    ocr_count = sum(1 for p in extracted if p.source == "empty")
    out = {"partitions": len(partitions), "calls": record["calls"], "ocrCharsPerPageExact": None, "selectedScanned": None}
    savings = (record.get("selection") or {}).get("savings") or {}
    if savings.get("charsSent") is not None:
        # La selección guardó los caracteres EXACTOS de las páginas enviadas
        # (nativas + OCR): el texto completo de la llamada se conoce sin suponer.
        out.update(knownChars=fixed * chunks + len(partitions) * (int(savings["charsSent"]) + wrapper), ocrExtracted=0)
        considered = [by_number[n] for n in range(1, int(record["selection"].get("pagesConsidered") or 0) + 1) if n in by_number]
        scanned = [p for p in considered if p.source == "empty"]
        total_chars = int(savings["charsSent"]) + int(savings.get("charsSkipped") or 0)
        if scanned:
            out["ocrCharsPerPageExact"] = (total_chars - sum(len(p.text) for p in considered if p.source != "empty")) / len(scanned)
            if not record["selection"].get("failOpen"):
                out["selectedScanned"] = len(record["selection"].get("selected") or [])
    else:
        out.update(knownChars=fixed * chunks + len(partitions) * (native_chars + wrapper), ocrExtracted=ocr_count)
    return out


# ---------------------------------------------------------------------------
# Calibración
# ---------------------------------------------------------------------------

def _q(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _lstsq(rows: List[List[float]], targets: List[float]) -> Optional[List[float]]:
    """Mínimos cuadrados con ecuaciones normales (sin numpy). None si es singular."""
    n = len(rows[0]) if rows else 0
    if len(rows) < n + 1:
        return None
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(n)] for i in range(n)]
    atb = [sum(r[i] * t for r, t in zip(rows, targets)) for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(ata[r][col]))
        if abs(ata[pivot][col]) < 1e-9:
            return None
        ata[col], ata[pivot], atb[col], atb[pivot] = ata[pivot], ata[col], atb[pivot], atb[col]
        for r in range(n):
            if r != col:
                factor = ata[r][col] / ata[col][col]
                ata[r] = [a - factor * b for a, b in zip(ata[r], ata[col])]
                atb[r] -= factor * atb[col]
    return [atb[i] / ata[i][i] for i in range(n)]


def fit(records: List[dict], previous: Dict[str, Any]) -> Dict[str, Any]:
    """Constantes de calibración a partir de registros con `structure` resuelta."""
    default = dict(previous.get("default") or {"charsPerToken": 3.2, "ocrCharsPerPage": 3500,
                                                "outputTokensPerCall": {"median": 2500, "p90": 4000}})
    models: Dict[str, Any] = {}
    # Caracteres que devuelve el OCR por página: propiedad del DOCUMENTO (una
    # factura escaneada trae mucho menos texto que un pedimento), no del modelo.
    # Primero el conteo exacto que guarda la selección de páginas; derivarlo de
    # tokens es solo respaldo, porque ahí se cuela cualquier cambio del prompt.
    ocr_exact: Dict[str, List[float]] = defaultdict(list)
    ocr_by_type: Dict[str, List[float]] = defaultdict(list)
    selected_by_type: Dict[str, List[int]] = defaultdict(list)
    by_model: Dict[str, List[dict]] = defaultdict(list)
    for rec in records:
        if rec.get("structure"):
            by_model[rec["model"]].append(rec)
            if rec["structure"].get("ocrCharsPerPageExact"):
                ocr_exact[rec["docType"]].append(rec["structure"]["ocrCharsPerPageExact"])
            if rec["structure"].get("selectedScanned"):
                selected_by_type[rec["docType"]].append(rec["structure"]["selectedScanned"])
    for model, recs in by_model.items():
        cal: Dict[str, Any] = {"documents": len(recs),
                               "supportsPromptCache": model.startswith(("us.anthropic.", "anthropic.", "global.anthropic."))}
        complete = [r for r in recs if r["structure"]["ocrExtracted"] == 0 and r["inputTokens"]]
        # tokens = chars / cpt  ->  cpt = chars / tokens (mediana: robusta a prompts de otra versión del catálogo)
        if complete:
            cal["charsPerToken"] = round(statistics.median(r["structure"]["knownChars"] / r["inputTokens"] for r in complete), 4)
        cpt = cal.get("charsPerToken") or default.get("charsPerToken", 3.2)
        for r in recs:
            if r["structure"]["ocrExtracted"] and r["inputTokens"] and r["docType"] not in ocr_exact:
                value = (r["inputTokens"] * cpt - r["structure"]["knownChars"]) / (
                    r["structure"]["ocrExtracted"] * r["structure"]["partitions"])
                if OCR_CHARS_PLAUSIBLE[0] <= value <= OCR_CHARS_PLAUSIBLE[1]:
                    ocr_by_type[r["docType"]].append(value)
        outs = [r["outputTokens"] / r["calls"] for r in recs if r["calls"] and r["outputTokens"]]
        if outs:
            cal["outputTokensPerCall"] = {"median": round(statistics.median(outs)), "p90": round(_q(outs, 0.9))}
        by_type: Dict[str, List[float]] = defaultdict(list)
        for r in recs:
            if r["calls"] and r["outputTokens"]:
                by_type[r["docType"]].append(r["outputTokens"] / r["calls"])
        cal["outputTokensPerCallByType"] = {t: {"median": round(statistics.median(v)), "p90": round(_q(v, 0.9)), "documents": len(v)}
                                            for t, v in by_type.items() if len(v) >= 2}
        models[model] = cal
    for doc_type, values in ocr_exact.items():
        ocr_by_type[doc_type] = values
    all_ocr = [v for values in ocr_by_type.values() for v in values]
    if all_ocr:
        default["ocrCharsPerPage"] = round(statistics.median(all_ocr))
    lam_rows = [[1.0, r["ocrPages"], r["calls"]] for r in records if r["lambdaSeconds"]]
    lam_targets = [r["lambdaSeconds"] for r in records if r["lambdaSeconds"]]
    coef = _lstsq(lam_rows, lam_targets)
    lam = dict(previous.get("lambda") or {"baseSeconds": 8, "secondsPerOcrPage": 1.5, "secondsPerCall": 20, "deterministicSeconds": 2})
    if coef and all(c >= 0 for c in coef):
        lam.update(baseSeconds=round(coef[0], 2), secondsPerOcrPage=round(coef[1], 3), secondsPerCall=round(coef[2], 2))
    return {"default": default, "models": models, "lambda": lam,
            "ocrCharsPerPageByType": {t: {"median": round(statistics.median(v)), "documents": len(v),
                                          "source": "exacto (selección de páginas)" if t in ocr_exact else "derivado de tokens"}
                                      for t, v in sorted(ocr_by_type.items())},
            "selectedScannedPagesByType": {t: {"median": round(statistics.median(v)), "documents": len(v)}
                                           for t, v in sorted(selected_by_type.items())}}


# ---------------------------------------------------------------------------
# Evaluación
# ---------------------------------------------------------------------------

def _pct_err(estimate: float, real: float) -> Optional[float]:
    return None if not real else (estimate - real) / real


def evaluate(records: List[dict], deployments: Dict[str, ce.Deployment], base_calibration: Dict[str, Any]) -> List[dict]:
    rows = []
    for rec in records:
        others = [r for r in records if r["file"] != rec["file"]]
        calibration = {**fit(others, base_calibration), "generatedAt": "leave-one-out"}
        dep = deployments[rec["env"]]
        like_for_like = ce.Deployment(env=dep.env, function=dep.function, memory_mb=dep.memory_mb,
                                      variables={**dep.variables, "BEDROCK_MODEL_ID": rec["model"]}, source=dep.source)
        with ce.pipeline_context(like_for_like) as mods:
            try:
                est = ce.estimate_file(Path(rec["path"]), rec["docType"], like_for_like, calibration, mods)
            except Exception as error:
                rows.append({**rec, "error": f"{type(error).__name__}: {error}"})
                continue
        # Método anterior: páginas × $/página promedio del tipo con los DEMÁS registros.
        same_type = [r for r in others if r["docType"] == rec["docType"]]
        rate = (sum(r["totalUsd"] for r in same_type) / sum(r["pages"] for r in same_type)) if same_type else None
        old = rec["pages"] * rate if rate else None
        rows.append({
            **rec, "estimate": est,
            "err": {
                "textract": est["textractUsd"] - rec["textractUsd"],
                "input": _pct_err(est["inputTokens"], rec["inputTokens"]),
                "output": _pct_err(est["outputTokens"], rec["outputTokens"]),
                "bedrock": _pct_err(est["bedrockUsd"], rec["bedrockUsd"]),
                "total": _pct_err(est["totalUsd"], rec["totalUsd"]),
                "oldTotal": _pct_err(old, rec["totalUsd"]) if old is not None else None,
            },
            "withinRange": est["totalUsd"] * 0.8 <= rec["totalUsd"] <= est["totalHighUsd"] * 1.2,
            "sameExtractedPages": est["extractPages"] == len(rec["extractedPages"]),
            "sameCalls": est["calls"] == rec["calls"],
        })
    return rows


def summarize(rows: List[dict]) -> dict:
    ok = [r for r in rows if "estimate" in r]
    same = [r for r in ok if r["sameCalls"] and r["sameExtractedPages"]]
    return {**_summary_of(ok), "repriced": sum(1 for r in ok if abs(r["recordedTotalUsd"] - r["totalUsd"]) > 0.0005),
            "errors": len(rows) - len(ok), "sameStructure": _summary_of(same)}


def _summary_of(ok: List[dict]) -> dict:
    def mape(key):
        values = [abs(r["err"][key]) for r in ok if r["err"].get(key) is not None]
        return round(statistics.median(values), 4) if values else None
    real = sum(r["totalUsd"] for r in ok)
    est = sum(r["estimate"]["totalUsd"] for r in ok)
    old_rows = [r for r in ok if r["err"]["oldTotal"] is not None]
    return {
        "records": len(ok), "files": len({r["file"] for r in ok}),
        "textractExact": sum(1 for r in ok if abs(r["err"]["textract"]) < 1e-9),
        "sameCalls": sum(1 for r in ok if r["sameCalls"]), "sameExtractedPages": sum(1 for r in ok if r["sameExtractedPages"]),
        "medianAbsErrInput": mape("input"), "medianAbsErrOutput": mape("output"),
        "medianAbsErrBedrock": mape("bedrock"), "medianAbsErrTotal": mape("total"),
        "medianAbsErrOldMethod": (round(statistics.median(abs(r["err"]["oldTotal"]) for r in old_rows), 4) if old_rows else None),
        "withinRange": sum(1 for r in ok if r["withinRange"]),
        "realTotalUsd": round(real, 4), "estimatedTotalUsd": round(est, 4),
        "aggregateErr": round((est - real) / real, 4) if real else None,
    }


def _fmt_pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:+.0f}%"


def _abs_pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"



def _summary_lines(s: dict, label: str, c) -> List[str]:
    if not s["records"]:
        return [f"  {label}: sin registros"]
    return [
        f"{c(BOLD)}{label}{c(RESET)} — {s['records']} registro(s), {s['files']} archivo(s)",
        f"  Textract exacto:                {s['textractExact']}/{s['records']}",
        f"  Mismas llamadas a Bedrock:      {s['sameCalls']}/{s['records']}    mismas páginas extraídas: {s['sameExtractedPages']}/{s['records']}",
        f"  Error mediano tokens entrada:   {_abs_pct(s['medianAbsErrInput'])}",
        f"  Error mediano tokens salida:    {_abs_pct(s['medianAbsErrOutput'])}",
        f"  Error mediano Bedrock $:        {_abs_pct(s['medianAbsErrBedrock'])}",
        f"  Error mediano TOTAL $:          {_abs_pct(s['medianAbsErrTotal'])}   (método anterior: {_abs_pct(s['medianAbsErrOldMethod'])})",
        f"  Real dentro del rango estimado: {s['withinRange']}/{s['records']}",
        f"  Suma real ${s['realTotalUsd']:.4f} · suma estimada ${s['estimatedTotalUsd']:.4f} ({_fmt_pct(s['aggregateErr'])})",
    ]


def render(rows: List[dict], summary: dict, color: bool) -> str:
    c = (lambda code: code) if color else (lambda code: "")
    lines = [f"\n{c(BOLD)}Estimado vs cobrado — {summary['records']} registro(s) de {summary['files']} archivo(s){c(RESET)}",
             c(DIM) + f"  {'Archivo':<26}{'Tipo':<22}{'Modelo':<10}{'Págs':>5}{'OCR':>4}{'Llam':>9}{'In real':>9}{'In est':>8}{'Out real':>9}{'Out est':>8}"
             f"{'Real $':>9}{'Est $':>9}{'Tope $':>9}{'Error':>7}{'Antes':>7}" + c(RESET)]
    for r in sorted((r for r in rows if "estimate" in r), key=lambda r: (r["file"], r["processedAt"])):
        e = r["estimate"]
        err = r["err"]["total"]
        colr = GREEN if err is not None and abs(err) <= 0.25 else YELLOW if err is not None and abs(err) <= 0.5 else RED
        calls = f"{e['calls']}/{r['calls']}"
        lines.append(f"  {r['file'][:25]:<26}{r['docType'][:21]:<22}{ce.model_alias(r["model"])[:9]:<10}{r['pages']:>5}{r['ocrPages']:>4}"
                     f"{calls:>9}{r['inputTokens']:>9}{e['inputTokens']:>8}{r['outputTokens']:>9}{e['outputTokens']:>8}"
                     f"{r['totalUsd']:>9.4f}{e['totalUsd']:>9.4f}{e['totalHighUsd']:>9.4f}{c(colr)}{_fmt_pct(err):>7}{c(RESET)}"
                     f"{_fmt_pct(r['err']['oldTotal']):>7}")
    for r in rows:
        if "error" in r:
            lines.append(f"  {c(RED)}✗ {r['file']}: {r['error']}{c(RESET)}")
    lines.append("")
    lines += _summary_lines(summary, "Todos los registros", c)
    lines.append("")
    lines += _summary_lines(summary["sameStructure"], "Solo los que corrieron con la misma estructura que hoy (mismas llamadas y páginas)", c)
    lines += [
        "",
        f"  {c(DIM)}«Llam» = llamadas estimadas/reales. «Antes» = páginas × $/página promedio del tipo (método anterior).",
        f"  «Real $» re-tarifado con pipeline/pricing.py vigente: {summary['repriced']} registro(s) se habían guardado con",
        "  tarifas viejas (antes de 2026-09-04 MiniMax/Kimi se cobraban a tarifa de Haiku 4.5; desde 2026-10-15 el modelo base es Haiku 5.5).",
        "  Un registro con otra estructura corrió con un catálogo anterior (el prompt cambió) o su selección de",
        f"  páginas no se puede simular sin OCR: ahí el error mezcla calibración con cambios del catálogo.{c(RESET)}",
    ]
    return "\n".join(lines)


def main() -> int:
    e2e = _load_e2e()
    usage = "  %(prog)s [-e ENV ...] [-d DIR] [--since FECHA] [--write]"
    epilog = """\
EJEMPLOS:
  pnpm costos:validar                     dev y prod, archivos de test-docs/
  pnpm costos:validar -e prod --since 2026-09-01
  pnpm costos:validar --write             además regenera scripts/cost_calibration.json

ARCHIVOS:
  scripts/cost_calibration.json                 constantes que usa el estimado (con --write)
  resultados-pruebas/costos-validacion/<fecha>/ reporte.md y reporte.json
  resultados-pruebas/costos-validacion/cache/   resultados de S3 bajados (privado)

ESTADO DE SALIDA:
  0     terminó
  2     no hubo registros comparables (sin archivos locales que coincidan)"""
    description = """\
DESCRIPCIÓN:
  Compara el costo estimado antes de subir un archivo contra lo que la tabla de
  auditoría registró que costó de verdad, servicio por servicio, usando solo
  documentos cuyo archivo existe localmente. Solo lectura; cero llamadas pagadas."""
    parser = e2e.unix_parser("pnpm costos:validar", usage, description, epilog)
    parser.add_argument("-e", "--env", metavar="ENV", action="append", choices=("dev", "prod"),
                        help="ambiente a leer; se puede repetir (default: dev y prod)")
    parser.add_argument("-d", "--docs", metavar="DIR", default=str(REPO / "test-docs"), help="dónde buscar los archivos originales (default test-docs/)")
    parser.add_argument("--since", metavar="FECHA", help="solo registros desde AAAA-MM-DD")
    parser.add_argument("--write", action="store_true", help="regenera scripts/cost_calibration.json con todos los registros")
    parser.add_argument("--region", metavar="REGION", default="us-east-1", help="default us-east-1")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    envs = args.env or ["dev", "prod"]
    print(f"{CYAN}Buscando registros reales con archivo local en {args.docs} ({', '.join(envs)})…{RESET}")
    records = collect_records(envs, Path(args.docs), args.region, args.since)
    if not records:
        print(f"{RED}No hay registros comparables: ningún documento procesado coincide con un archivo local.{RESET}")
        return 2
    deployments = {env: ce.load_deployment(env, region=args.region) for env in envs}
    for dep in deployments.values():
        for warning in dep.warnings:
            print(f"{YELLOW}{warning}{RESET}")
    for rec in records:
        dep = deployments[rec["env"]]
        like = ce.Deployment(env=dep.env, function=dep.function, memory_mb=dep.memory_mb,
                             variables={**dep.variables, "BEDROCK_MODEL_ID": rec["model"]}, source=dep.source)
        with ce.pipeline_context(like) as mods:
            try:
                rec["structure"] = structure(rec, like, mods)
            except Exception as error:
                rec["structure"] = None
                print(f"{YELLOW}! {rec['file']}: no pude reconstruir su estructura ({error}){RESET}")

    base = ce.load_calibration()
    rows = evaluate(records, deployments, base)
    summary = summarize(rows)
    print(render(rows, summary, color=sys.stdout.isatty()))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = HISTORY / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "summary": summary,
               "rows": [{k: v for k, v in r.items() if k != "path"} for r in rows]}
    (out_dir / "reporte.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "reporte.md").write_text("```\n" + render(rows, summary, color=False) + "\n```\n", encoding="utf-8")
    for name in ("reporte.json", "reporte.md"):
        os.chmod(out_dir / name, 0o600)
    print(f"\nReporte → {out_dir / 'reporte.md'}")

    if args.write:
        calibration = fit(records, base)
        calibration.update(generatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           documents=len([r for r in records if r.get("structure")]),
                           environments=envs,
                           # Error medido (leave-one-out) con la que el plan del E2E dice "error típico".
                           accuracy={"medianAbsErrTotal": summary["medianAbsErrTotal"],
                                     "medianAbsErrOldMethod": summary["medianAbsErrOldMethod"],
                                     "records": summary["records"], "files": summary["files"],
                                     "withinRange": summary["withinRange"], "textractExact": summary["textractExact"]},
                           source="pnpm costos:validar --write (tabla de auditoría de costos + resultados de S3)")
        ce.CALIBRATION_FILE.write_text(json.dumps(calibration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Calibración → {ce.CALIBRATION_FILE.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
