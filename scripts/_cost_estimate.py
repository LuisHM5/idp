"""Costo estimado de procesar un archivo ANTES de subirlo, calculado igual que
lo cobra el pipeline — no un $/página promedio.

Reconstruye localmente, con el MISMO código del enrichment Lambda, cada cosa
por la que AWS cobra (ver `cost_audit.build_cost_breakdown`):

- **Textract**: cada página sin capa de texto utilizable va a OCR
  (`pdf_pages.load_pages`, mismo umbral) — todas, antes de la selección de
  páginas. Exacto: páginas × `pricing.textract_cost`.
- **Bedrock, extracción**: el modelo que resolvería el Lambda para ese tipo
  (`bedrock.explain_model_resolution` con las variables del Lambda desplegado
  y el catálogo JSON + DynamoDB del ambiente); las páginas que manda la
  selección (`page_selector.select`); los chunks (`text_extractor._chunk_pages`)
  × particiones del schema (`_field_tree_call_partitions`); y por llamada, los
  caracteres REALES del prompt fijo, del texto de las páginas y del JSON del
  tool (`_build_field_tree_prompts`, `_field_tree_tool_schema`). Tokens =
  caracteres ÷ densidad calibrada del modelo; salida = tokens por llamada
  calibrados. Precio: `pricing.bedrock_cost`, la misma función que el registro
  de auditoría.
- **Bedrock, segmentación / upstream**: $0 para un tipo declarado (no hay
  llamada); se verifica corriendo `segmenter.segment_pages` con Bedrock
  bloqueado.
- **Lambda**: duración estimada × memoria del Lambda desplegado
  (`pricing.lambda_cost`).

Lo que NO se puede saber sin pagar se declara, no se inventa: el texto de una
página escaneada (se usa un tamaño calibrado) y, en documentos con páginas
escaneadas, qué páginas escogerá la selección (rango entre el tamaño esperado
del tipo y fail-open). Las constantes calibradas viven en
`scripts/cost_calibration.json` y las regenera `pnpm costos:validar -- --write`,
que además mide el error del estimado contra documentos reales.

Ninguna llamada pagada puede ocurrir: `block_paid_calls()` sustituye los
clientes de Bedrock y Textract por uno que lanza excepción.
"""

from __future__ import annotations

import contextlib
import json
import logging
import numbers
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

REPO = Path(__file__).resolve().parent.parent
LAMBDA_DIR = REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"
CALIBRATION_FILE = REPO / "scripts" / "cost_calibration.json"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
DETERMINISTIC_SUFFIXES = {".xml", ".xlsx", ".csv"}
PIPELINE_ENV_KEYS = (
    "BEDROCK_MODEL_ID", "BEDROCK_EXTRACTOR_MODEL_ID", "BEDROCK_SEGMENTER_MODEL_ID", "BEDROCK_RECOVERY_MODEL_ID",
    "CATALOG_TABLE", "FALLBACK_MODELS_TABLE", "PAGE_SELECTION_ENABLED", "MAX_PAGES", "CHUNK_WORKERS",
    "FIELD_TREE_SPLIT_SCHEMA_CHARS", "MAX_OUTPUT_TOKENS",
)


class PaidCallBlocked(RuntimeError):
    """Una estimación intentó llamar a Bedrock o Textract."""


# ---------------------------------------------------------------------------
# Despliegue y calibración
# ---------------------------------------------------------------------------

@dataclass
class Deployment:
    """Lo que el Lambda desplegado usa para decidir costo."""
    env: str
    function: Optional[str]
    memory_mb: int
    variables: Dict[str, str]
    source: str  # "lambda" (leído del Lambda) o "default" (no se pudo leer)
    warnings: List[str] = field(default_factory=list)


def load_deployment(env: str, stack: Optional[str] = None, region: str = "us-east-1") -> Deployment:
    """Variables y memoria del enrichment Lambda desplegado (solo lectura)."""
    stack_name = stack or ("DocumentValidationStack" if env == "prod" else "DocumentValidationStack-Dev")
    try:
        resources = subprocess.run(
            ["aws", "cloudformation", "describe-stack-resources", "--stack-name", stack_name, "--region", region,
             "--query", "StackResources[?ResourceType=='AWS::Lambda::Function'].[LogicalResourceId,PhysicalResourceId]",
             "--output", "json"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        function = next(phys for logical, phys in json.loads(resources.stdout) if logical.startswith("EnrichmentLambda"))
        config = subprocess.run(
            ["aws", "lambda", "get-function-configuration", "--function-name", function, "--region", region,
             "--output", "json"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        data = json.loads(config.stdout)
        variables = (data.get("Environment") or {}).get("Variables") or {}
        return Deployment(env=env, function=function, memory_mb=int(data.get("MemorySize") or 1024),
                          variables={k: v for k, v in variables.items() if k in PIPELINE_ENV_KEYS}, source="lambda")
    except Exception as error:  # sin credenciales/permiso: estimado con defaults, avisado
        return Deployment(env=env, function=None, memory_mb=1024, variables={}, source="default",
                          warnings=[f"no pude leer el Lambda de {stack_name} ({str(error)[:120]}); "
                                    "uso el modelo por default del código y 1024 MB"])


def model_alias(model: Optional[str]) -> str:
    """Nombre corto para tablas: `minimax.minimax-m2.5` → `minimax`."""
    model = model or ""
    for needle, alias in (("haiku", "haiku-4.5"), ("sonnet", "sonnet"), ("minimax", "minimax"),
                          ("kimi", "kimi-k2.5"), ("glm", "glm-5")):
        if needle in model:
            return alias
    return model.split(".")[-1][:10] or "—"


def load_calibration(path: Path = CALIBRATION_FILE) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"models": {}, "default": {}}


def _model_calibration(calibration: Dict[str, Any], model: str) -> Dict[str, Any]:
    base = dict(calibration.get("default") or {})
    base.update((calibration.get("models") or {}).get(model) or {})
    return base


# ---------------------------------------------------------------------------
# Entorno del pipeline, sin llamadas pagadas
# ---------------------------------------------------------------------------

def _import_pipeline():
    if str(LAMBDA_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDA_DIR))
    import document_catalog
    from pipeline import bedrock, expediente, page_selector, pdf_pages, pricing, segmenter, text_extractor

    return document_catalog, bedrock, expediente, page_selector, pdf_pages, pricing, segmenter, text_extractor


def _blocked(*_args: Any, **_kwargs: Any):
    raise PaidCallBlocked("llamada a Bedrock/Textract bloqueada durante la estimación")


@contextlib.contextmanager
def pipeline_context(deployment: Deployment) -> Iterator[Any]:
    """Aplica las variables del Lambda desplegado (modelo, catálogo DynamoDB,
    selección de páginas…), recarga el catálogo con ellas y bloquea cualquier
    cliente pagado. Restaura todo al salir."""
    saved = {k: os.environ.get(k) for k in PIPELINE_ENV_KEYS}
    for key in PIPELINE_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(deployment.variables)
    mods = _import_pipeline()
    document_catalog, bedrock, _, _, pdf_pages, _, _, _ = mods
    # Los avisos del pipeline ("page 1: native text layer looks corrupted…")
    # son para CloudWatch, no para el plan de costo.
    quiet = [logging.getLogger(name) for name in ("pipeline", "document_catalog")]
    levels = [logger.level for logger in quiet]
    for logger in quiet:
        logger.setLevel(logging.ERROR)
    document_catalog._CATALOG_TABLE = deployment.variables.get("CATALOG_TABLE")
    document_catalog.reload_catalog()
    original_client, original_textract = bedrock.client, pdf_pages._textract_client
    bedrock.client, pdf_pages._textract_client = _blocked, _blocked
    try:
        yield mods
    finally:
        bedrock.client, pdf_pages._textract_client = original_client, original_textract
        for logger, level in zip(quiet, levels):
            logger.setLevel(level)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        document_catalog._CATALOG_TABLE = os.environ.get("CATALOG_TABLE")
        document_catalog.reload_catalog()


# ---------------------------------------------------------------------------
# Estimación de un archivo
# ---------------------------------------------------------------------------

def _expected_pages(spec: Dict[str, Any]) -> Optional[int]:
    """Tamaño de UN documento del tipo (cota alta si es rango). El catálogo que
    viene de DynamoDB trae los números como Decimal, no int."""
    expected = spec.get("expected_pages")
    if isinstance(expected, (list, tuple)) and expected:
        expected = expected[-1]
    if isinstance(expected, numbers.Number) and not isinstance(expected, bool):
        return int(expected)
    return None


def _calls_for(mods, entry, pages: List[Any]) -> List[Dict[str, int]]:
    """Caracteres de cada llamada de extracción que haría `extract_document`."""
    _, _, _, _, _, _, _, text_extractor = mods
    chunks = text_extractor._chunk_pages(pages)
    calls = []
    for chunk in chunks:
        if entry.field_tree:
            for scope, tree in text_extractor._field_tree_call_partitions(entry.field_tree):
                prefix, user = text_extractor._build_field_tree_prompts(entry, chunk, tree, scope)
                tool = json.dumps(text_extractor._field_tree_tool_schema(tree), ensure_ascii=False)
                calls.append({"prefixChars": len(prefix), "userChars": len(user), "toolChars": len(tool)})
        else:
            prefix, user = text_extractor._build_prompts(entry, chunk, arrays_only=False)
            tool = json.dumps(text_extractor._tool_schema(entry, arrays_only=False), ensure_ascii=False)
            calls.append({"prefixChars": len(prefix), "userChars": len(user), "toolChars": len(tool)})
    return calls if chunks else []


def _price_calls(mods, calls: List[Dict[str, int]], n_chunks: int, model: str, cal: Dict[str, Any],
                 type_id: str, workers: int) -> Dict[str, Any]:
    """Tokens y $ de un conjunto de llamadas, con el caché de Anthropic si aplica
    (el pipeline marca cachePoint solo con >1 chunk; los modelos de terceros no
    tienen caché y el pipeline quita el cachePoint)."""
    _, _, _, _, _, pricing, _, _ = mods
    cpt = float(cal.get("charsPerToken", 3.2))
    overhead = int(cal.get("callOverheadTokens", 0))
    per_type = (cal.get("outputTokensPerCallByType") or {}).get(type_id) or {}
    out_mid = float(per_type.get("median") or cal.get("outputTokensPerCall", {}).get("median") or 2500)
    out_high = float(per_type.get("p90") or cal.get("outputTokensPerCall", {}).get("p90") or out_mid * 1.6)
    caches = bool(cal.get("supportsPromptCache")) and n_chunks > 1
    usage_mid: Dict[str, int] = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    for index, call in enumerate(calls):
        prefix_tok = round(call["prefixChars"] / cpt)
        rest_tok = round((call["userChars"] + call["toolChars"]) / cpt) + overhead
        if caches and index < workers:
            usage_mid["cacheWriteInputTokens"] += prefix_tok
            usage_mid["inputTokens"] += rest_tok
        elif caches:
            usage_mid["cacheReadInputTokens"] += prefix_tok
            usage_mid["inputTokens"] += rest_tok
        else:
            usage_mid["inputTokens"] += prefix_tok + rest_tok
    usage_high = dict(usage_mid)
    usage_mid["outputTokens"] = round(out_mid * len(calls))
    usage_high["outputTokens"] = round(out_high * len(calls))
    return {"mid": pricing.bedrock_cost(usage_mid, model), "high": pricing.bedrock_cost(usage_high, model)}


def estimate_file(path: Path, doc_type: str, deployment: Deployment, calibration: Optional[Dict[str, Any]] = None,
                  mods: Any = None) -> Dict[str, Any]:
    """Desglose de costo de un archivo declarado como `doc_type`. Llamar dentro
    de `pipeline_context(deployment)` (o dejar que `estimate_files` lo haga)."""
    calibration = calibration if calibration is not None else load_calibration()
    document_catalog, bedrock, expediente, page_selector, pdf_pages, pricing, segmenter, text_extractor = mods
    entry = document_catalog.get_type_entry(doc_type)
    model = bedrock.explain_model_resolution("extractor", entry)["resolved"]
    cal = _model_calibration(calibration, model)
    workers = int(deployment.variables.get("CHUNK_WORKERS") or text_extractor._CHUNK_WORKERS)
    suffix = path.suffix.lower()
    notes: List[str] = []
    result: Dict[str, Any] = {
        "file": path.name, "docType": doc_type, "model": model, "calibrated": model in (calibration.get("models") or {}),
        "pages": 0, "ocrPages": 0, "extractPages": 0, "extractPagesHigh": 0, "calls": 0, "chunks": 0,
        "selection": None, "notes": notes,
    }
    if not result["calibrated"]:
        notes.append(f"{model} no tiene calibración propia: densidad de tokens y salida por default")

    lambda_cal = calibration.get("lambda") or {}
    if suffix in DETERMINISTIC_SUFFIXES:
        # Pipelines XML/tabular: sin Bedrock ni Textract.
        seconds = float(lambda_cal.get("deterministicSeconds", 2))
        lam = pricing.lambda_cost(seconds * 1000, deployment.memory_mb)["costUsd"]
        return {**result, "textractUsd": 0.0, "bedrockUsd": 0.0, "bedrockHighUsd": 0.0, "lambdaUsd": lam,
                "totalUsd": round(lam, 6), "totalHighUsd": round(lam, 6), "inputTokens": 0, "outputTokens": 0,
                "outputTokensHigh": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0, "exact": True}

    by_type = ((calibration.get("ocrCharsPerPageByType") or {}).get(doc_type) or {}).get("median")
    ocr_chars = int(by_type or (calibration.get("default") or {}).get("ocrCharsPerPage") or 3500)
    if suffix in IMAGE_SUFFIXES:
        pages = [pdf_pages.PageText(1, "", "empty", 0.0)]
        total_pages = 1
    else:
        data = path.read_bytes()
        cap, _ = expediente._page_cap_for([doc_type])
        data, original = expediente._truncate_pdf(data, cap)
        pages = pdf_pages.load_pages(data, use_ocr=False)
        total_pages = len(pages)
        if original:
            notes.append(f"el pipeline trunca a {cap} de {original} páginas")
    scanned = [p for p in pages if p.source == "empty"]
    result.update(pages=total_pages, ocrPages=len(scanned))
    textract = pricing.textract_cost(len(scanned))["costUsd"]

    try:
        segments, seg_usage = segmenter.segment_pages(pages, declared_types=[doc_type])
    except PaidCallBlocked:
        segments, seg_usage = [], {"blocked": True}
        notes.append("la segmentación de este tipo llamaría al modelo: costo de segmentación no incluido")

    # Para contar caracteres, una página escaneada pesa lo que el OCR suele
    # devolver (calibrado); para la selección no tiene texto que puntuar.
    def as_text(page):
        return page if page.source != "empty" else pdf_pages.PageText(page.number, "x" * ocr_chars, "textract", 0.9)

    mid_calls: List[Dict[str, int]] = []
    high_calls: List[Dict[str, int]] = []
    chunks_mid = chunks_high = 0
    extract_mid = extract_high = 0
    for seg in segments:
        seg_entry = document_catalog.get_type_entry(seg.doc_type)
        if seg.doc_type == "OTRO" or not (seg_entry.extraction_fields or seg_entry.field_tree):
            continue
        seg_pages = [p for p in pages if seg.page_start <= p.number <= seg.page_end]
        seg_scanned = [p for p in seg_pages if p.source == "empty"]
        chosen = seg_pages
        if page_selector.enabled_for(seg_entry):
            spec = page_selector.spec_for(seg_entry) or {}
            selection = page_selector.select(seg_pages, spec)
            if not seg_scanned:
                if selection.fail_open and page_selector_wants_locator(mods, spec):
                    notes.append("la selección cae en fail-open y el tipo usa el localizador por IA: "
                                 "se estima como documento completo")
                chosen = page_selector.filter_pages(seg_pages, selection) if selection.filtered else seg_pages
                result["selection"] = {"exact": True, "failOpen": selection.fail_open, "reason": selection.reason,
                                       "selected": len(chosen), "considered": len(seg_pages)}
            else:
                # Sin OCR local no se sabe qué páginas escaneadas marcarán; cota
                # media: las nativas que sí seleccionaría + el tamaño esperado
                # del tipo; cota alta: fail-open (todas).
                native_chosen = [p for p in page_selector.filter_pages(seg_pages, selection)
                                 if p.source != "empty"] if selection.filtered else [p for p in seg_pages if p.source != "empty"]
                # Cuántas páginas suele quedarse la selección en documentos
                # escaneados de ESTE tipo (calibrado); si no hay historial, el
                # tamaño de un documento del tipo según el catálogo.
                observed = ((calibration.get("selectedScannedPagesByType") or {}).get(seg.doc_type) or {}).get("median")
                expected = int(observed) if observed else _expected_pages(spec)
                if expected is not None and len(seg_pages) > max(expected, int(spec.get("min_pages", 5))):
                    extra = seg_scanned[: max(0, expected - len(native_chosen))]
                    chosen = sorted(native_chosen + extra, key=lambda p: p.number)
                notes.append(f"{len(seg_scanned)} página(s) escaneada(s): la selección no se puede simular sin OCR; "
                             f"medio = {len(chosen)} pág, alto = {len(seg_pages)} pág (fail-open)")
                result["selection"] = {"exact": False, "selected": len(chosen), "considered": len(seg_pages)}
        mid_pages = [as_text(p) for p in chosen]
        high_pages = [as_text(p) for p in seg_pages]
        seg_mid = _calls_for(mods, seg_entry, mid_pages)
        seg_high = _calls_for(mods, seg_entry, high_pages)
        mid_calls += seg_mid
        high_calls += seg_high
        chunks_mid += len(text_extractor._chunk_pages(mid_pages))
        chunks_high += len(text_extractor._chunk_pages(high_pages))
        extract_mid += len(chosen)
        extract_high += len(seg_pages)

    priced_mid = _price_calls(mods, mid_calls, chunks_mid, model, cal, doc_type, workers)
    priced_high = _price_calls(mods, high_calls, chunks_high, model, cal, doc_type, workers)
    bedrock_mid, bedrock_high = priced_mid["mid"], priced_high["high"]

    per_ocr = float(lambda_cal.get("secondsPerOcrPage", 1.5))
    per_call = float(lambda_cal.get("secondsPerCall", 20))
    base = float(lambda_cal.get("baseSeconds", 8))
    seconds_mid = base + per_ocr * len(scanned) + per_call * len(mid_calls)
    seconds_high = base + per_ocr * len(scanned) + per_call * len(high_calls) * 1.5
    lam_mid = pricing.lambda_cost(min(seconds_mid, 900) * 1000, deployment.memory_mb)["costUsd"]
    lam_high = pricing.lambda_cost(min(seconds_high, 900) * 1000, deployment.memory_mb)["costUsd"]

    result.update(
        extractPages=extract_mid, extractPagesHigh=extract_high, calls=len(mid_calls), callsHigh=len(high_calls),
        chunks=chunks_mid, textractUsd=textract, bedrockUsd=bedrock_mid["costUsd"], bedrockHighUsd=bedrock_high["costUsd"],
        inputTokens=bedrock_mid["inputTokens"], outputTokens=bedrock_mid["outputTokens"],
        outputTokensHigh=priced_mid["high"]["outputTokens"], cacheReadTokens=bedrock_mid["cacheReadTokens"],
        cacheWriteTokens=bedrock_mid["cacheWriteTokens"], lambdaUsd=lam_mid,
        totalUsd=round(textract + bedrock_mid["costUsd"] + lam_mid, 6),
        totalHighUsd=round(textract + bedrock_high["costUsd"] + lam_high, 6),
        segmentationUsd=0.0, exact=not scanned and not seg_usage,
        approximatePricing=bool(bedrock_mid.get("approximatePricing")),
    )
    if bedrock_mid.get("approximatePricing"):
        notes.append(f"{model} no está en pipeline/pricing.py: precio aproximado con tarifas de Haiku")
    return result


def page_selector_wants_locator(mods, spec: Dict[str, Any]) -> bool:
    try:
        from pipeline import page_locator

        return bool(page_locator.wants_locator(spec))
    except Exception:
        return False


def estimate_files(paths: List[Path], doc_type: str, deployment: Deployment,
                   calibration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Estimación por archivo + total de un conjunto del mismo tipo."""
    calibration = calibration if calibration is not None else load_calibration()
    with pipeline_context(deployment) as mods:
        files = []
        for path in paths:
            try:
                files.append(estimate_file(path, doc_type, deployment, calibration, mods))
            except PaidCallBlocked as error:
                files.append({"file": path.name, "docType": doc_type, "error": str(error)})
            except Exception as error:  # archivo ilegible: se reporta, no tumba el plan
                files.append({"file": path.name, "docType": doc_type, "error": f"{type(error).__name__}: {str(error)[:160]}"})
        ok = [f for f in files if "error" not in f]
        models = sorted({f["model"] for f in ok})
        total = {
            "docType": doc_type, "files": len(files), "models": models,
            "pages": sum(f["pages"] for f in ok), "ocrPages": sum(f["ocrPages"] for f in ok),
            "extractPages": sum(f["extractPages"] for f in ok), "calls": sum(f["calls"] for f in ok),
            "textractUsd": round(sum(f["textractUsd"] for f in ok), 6),
            "bedrockUsd": round(sum(f["bedrockUsd"] for f in ok), 6),
            "bedrockHighUsd": round(sum(f["bedrockHighUsd"] for f in ok), 6),
            "lambdaUsd": round(sum(f["lambdaUsd"] for f in ok), 6),
            "estimateUsd": round(sum(f["totalUsd"] for f in ok), 4),
            "ceilingUsd": round(sum(f["totalHighUsd"] for f in ok), 4),
            "inputTokens": sum(f["inputTokens"] for f in ok), "outputTokens": sum(f["outputTokens"] for f in ok),
            "errors": [f for f in files if "error" in f],
            "uncertain": [f["file"] for f in ok if not f.get("exact")],
            "uncalibratedModels": sorted({f["model"] for f in ok if not f["calibrated"]}),
            "deployment": {"source": deployment.source, "memoryMb": deployment.memory_mb,
                           "modelEnv": deployment.variables.get("BEDROCK_MODEL_ID"), "warnings": deployment.warnings},
            "calibration": {"generatedAt": calibration.get("generatedAt"), "documents": calibration.get("documents"),
                            "accuracy": calibration.get("accuracy")},
        }
    return {"total": total, "files": files}


def file_estimate_summary(estimate: Dict[str, Any]) -> Dict[str, Any]:
    """Lo que se guarda en el resumen de cada caso del E2E para compararlo
    después contra el costo real del mismo archivo."""
    keys = ("model", "pages", "ocrPages", "extractPages", "calls", "inputTokens", "outputTokens",
            "textractUsd", "bedrockUsd", "lambdaUsd", "totalUsd", "totalHighUsd", "exact")
    return {k: estimate.get(k) for k in keys} if "error" not in estimate else {"error": estimate["error"]}
