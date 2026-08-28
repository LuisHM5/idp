"""Piezas compartidas por los scripts `reporte*.py` (costos, documentos, y el
reporte unificado): lectura de DynamoDB, normalización de items, formato y
CSS. No es un comando en sí — nada aquí tiene `pnpm run`; lo importan
`reporte-costos.py`, `reporte-documentos.py` y `reporte.py`.

Separado para que las tres salidas queden consistentes por construcción (un
documento se ve igual en el reporte de costos que en el unificado) en vez de
mantener tres copias de la misma lógica de normalización que puedan divergir.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

GREEN, RED, YELLOW, CYAN, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
STATUS_ROLE_COLOR = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b", "neutral": "#898781"}
VALIDATION_STATUS_ROLE = {"APROBADO": "good", "REQUIERE_REVISION": "warning", "RECHAZADO": "critical"}
WORKFLOW_ROLE = {
    "complete": "good",
    "pending": "neutral",
    "classification-complete": "neutral",
    "processing-complete": "neutral",
    "classification-failure": "critical",
    "processing-failure": "critical",
    "enrichment-failure": "critical",
}

BEDROCK_SERVICES = (
    "BedrockClassificationUpstream",
    "BedrockProcessingUpstream",
    "BedrockSegmentation",
    "BedrockExtraction",
)
SERVICE_LABEL_ES = {
    "BedrockClassificationUpstream": "Bedrock · Clasificación (upstream)",
    "BedrockProcessingUpstream": "Bedrock · Procesamiento (upstream)",
    "BedrockSegmentation": "Bedrock · Segmentación",
    "BedrockExtraction": "Bedrock · Extracción",
    "TextractOcr": "Textract · OCR",
    "LambdaCompute": "Lambda · Cómputo (aprox.)",
}


# ---------------------------------------------------------------------------
# Formato
# ---------------------------------------------------------------------------

def money(x: Optional[float]) -> str:
    return f"${x:,.4f}" if x is not None else "—"


def num(x: Optional[float]) -> str:
    return f"{x:,.0f}" if x is not None else "—"


def pct(x: Optional[float]) -> str:
    return f"{x:.0%}" if x is not None else "—"


def secs(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x*1000:.0f}ms" if x < 1 else f"{x:.1f}s"


# ---------------------------------------------------------------------------
# Resolución de stack/tablas
# ---------------------------------------------------------------------------

def stack_id(env: str) -> str:
    return "DocumentValidationStack" if env == "prod" else "DocumentValidationStack-Dev"


def cost_table_name(env: str) -> str:
    return "document-validation-costs" + ("" if env == "prod" else "-dev")


def resolve_processing_table(stack: str, region: str) -> str:
    """Nombre físico de la tabla de tracking de documentos del blueprint —
    su nombre lo genera CloudFormation, no es fijo como el de CostTable."""
    import boto3  # deferred: only needed once we know we're actually querying

    cfn = boto3.client("cloudformation", region_name=region)
    resp = cfn.describe_stack_resources(StackName=stack)
    tables = [
        r["PhysicalResourceId"]
        for r in resp["StackResources"]
        if r["ResourceType"] == "AWS::DynamoDB::Table" and "DocumentProcessingTable" in r["LogicalResourceId"]
    ]
    if not tables:
        raise RuntimeError(
            f"No encontré la tabla de tracking de documentos en el stack {stack}. "
            "Pásala manualmente con --table."
        )
    return tables[0]


def scan_all(table: str, region: str) -> List[Dict[str, Any]]:
    import boto3

    t = boto3.resource("dynamodb", region_name=region).Table(table)
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        resp = t.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ---------------------------------------------------------------------------
# Normalización — tabla de costos
# ---------------------------------------------------------------------------

def _page_selection(ps: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Atributo `PageSelection` de la tabla de costos, con tipos de Python.

    `estimated_cost_avoided` NO es dinero facturado: valora la llamada que no
    se hizo. Se mantiene aparte de `total_cost` en todos los reportes — nunca
    se suma a un total, o el reporte sobrestimaría el gasto real.
    """
    if not ps:
        return None
    return {
        "pages_considered": int(ps.get("pagesConsidered", 0) or 0),
        "pages_extracted": int(ps.get("pagesExtracted", 0) or 0),
        "pages_skipped": int(ps.get("pagesSkipped", 0) or 0),
        "chars_sent": int(ps.get("charsSent", 0) or 0),
        "chars_skipped": int(ps.get("charsSkipped", 0) or 0),
        "chunks_sent": int(ps.get("chunksSent", 0) or 0),
        "chunks_without_selection": int(ps.get("estimatedChunksWithoutSelection", 0) or 0),
        "billed_input_tokens": int(ps.get("billedInputTokens", 0) or 0),
        "billed_output_tokens": int(ps.get("billedOutputTokens", 0) or 0),
        "input_tokens_saved": int(ps.get("estimatedInputTokensSaved", 0) or 0),
        "output_tokens_saved": int(ps.get("estimatedOutputTokensSaved", 0) or 0),
        "input_saving_pct": float(ps["estimatedInputSavingPct"]) if ps.get("estimatedInputSavingPct") is not None else None,
        "estimated_cost_avoided": float(ps.get("estimatedCostAvoidedUsd", 0) or 0),
    }


def normalize_cost_item(it: Dict[str, Any]) -> Dict[str, Any]:
    services_raw = it.get("Services") or {}
    services: Dict[str, Dict[str, Any]] = {}
    for name, svc in services_raw.items():
        services[name] = {
            "cost": float(svc.get("costUsd", 0) or 0),
            "input_tokens": int(svc.get("inputTokens", 0) or 0),
            "output_tokens": int(svc.get("outputTokens", 0) or 0),
            "model_id": svc.get("modelId"),
            # DynamoDB (boto3 resource API) returns every Number attribute as
            # Decimal regardless of whether it was written as int or float —
            # cast explicitly, same as every other numeric field here, so a
            # real item never leaks a Decimal downstream (json.dumps in
            # reporte.py can't serialize one; a plain-int fixture in a test
            # would hide this, which is exactly how it shipped once already).
            "pages": int(svc["pages"]) if svc.get("pages") is not None else None,
            "duration_ms": float(svc["durationMs"]) if svc.get("durationMs") is not None else None,
            "approximate_pricing": bool(svc.get("approximatePricing")),
        }

    total_input = it.get("TotalInputTokens")
    total_output = it.get("TotalOutputTokens")
    if total_input is None or total_output is None:
        # Backward-compat: registros escritos antes de que cost_audit.py
        # agregara estos campos — se recalculan sumando cada servicio.
        total_input = sum(s["input_tokens"] for s in services.values())
        total_output = sum(s["output_tokens"] for s in services.values())

    pages = it.get("Pages") or {}
    return {
        # Lo que la selección de páginas evitó gastar (None cuando no filtró).
        # Todo casteado explícitamente: DynamoDB devuelve Decimal para CUALQUIER
        # número, y reporte.py embebe esto con json.dumps, que no serializa
        # Decimal — el mismo motivo por el que los campos de arriba se castean.
        "page_selection": _page_selection(it.get("PageSelection")),
        "document_id": it.get("DocumentId", ""),
        "document_type": str(it.get("DocumentType") or "OTRO"),
        "document_type_name": it.get("DocumentTypeName") or "",
        "source_file": it.get("SourceFile") or "",
        "processed_at": str(it.get("ProcessedAt") or ""),
        "app_origin": it.get("AppOrigin") or "",
        "pricing_version": it.get("PricingVersion") or "",
        "pages_total": int(pages.get("Total", 0) or 0),
        "pages_native": int(pages.get("Native", 0) or 0),
        "pages_ocr": int(pages.get("Ocr", 0) or 0),
        "pages_empty": int(pages.get("Empty", 0) or 0),
        "total_cost": float(it.get("TotalCostUsd", 0) or 0),
        "cost_per_page": float(it["CostPerPageUsd"]) if it.get("CostPerPageUsd") is not None else None,
        "total_input_tokens": int(total_input),
        "total_output_tokens": int(total_output),
        "services": services,
    }


# ---------------------------------------------------------------------------
# Normalización — tabla de tracking de documentos
# ---------------------------------------------------------------------------

def _doc_confidences(expediente: Dict[str, Any]) -> List[float]:
    out = []
    for d in expediente.get("documents", []) or []:
        c = d.get("confidence")
        if isinstance(c, (int, float)):
            out.append(float(c))
    return out


def _field_stats(expediente: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Agrega fieldsValidity de cada documento del expediente. Cobertura
    parcial en expedientes field-centric multi-documento: el detalle completo
    de cada documento interno solo vive en `results/{id}.json`, no en esta
    tabla — este reporte solo ve lo que el envelope compacto trae."""
    stats: Dict[str, Dict[str, int]] = {}
    for d in expediente.get("documents", []) or []:
        for name, f in (d.get("fieldsValidity") or {}).items():
            s = stats.setdefault(name, {"ok": 0, "total": 0})
            s["total"] += 1
            if f.get("valid"):
                s["ok"] += 1
    return stats


def normalize_doc_item(it: Dict[str, Any]) -> Dict[str, Any]:
    tracking = it.get("Tracking") or {}
    model_ids = tracking.get("ModelIds") or {}
    model = (
        model_ids.get("extraction")
        # `extractor`/`segmenter` are the raw internal stage names
        # `pipeline/bedrock.py` used before handler.py::_model_ids() started
        # renaming them to match `expediente.usage`'s "extraction"/
        # "segmentation" — documents processed before that fix keep the old
        # keys in `Tracking.ModelIds` forever (no retroactive backfill), so
        # both spellings need checking here.
        or model_ids.get("extractor")
        or model_ids.get("processing")
        or model_ids.get("segmentation")
        or model_ids.get("segmenter")
        or model_ids.get("classification")
        or "(desconocido)"
    )

    envelope: Dict[str, Any] = {}
    raw = it.get("EnrichmentResult")
    if isinstance(raw, str) and raw.strip():
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            envelope = {}
    expediente = envelope.get("expediente") or {}

    confidences = _doc_confidences(expediente)
    avg_conf = (sum(confidences) / len(confidences)) if confidences else None

    processing_seconds = tracking.get("ProcessingSeconds")
    processing_seconds = float(processing_seconds) if processing_seconds is not None else None

    return {
        "document_id": it.get("DocumentId", ""),
        "document_type": str(it.get("DocumentType") or "SIN_CLASIFICAR"),
        "document_type_name": it.get("DocumentTypeName") or "",
        "document_name": it.get("DocumentName") or "",
        "app_origin": it.get("AppOrigin") or tracking.get("AppOrigin") or "",
        "workflow_status": str(it.get("WorkflowStatus") or "desconocido"),
        "validation_status": str(it.get("ValidationStatus") or ""),
        "processed_at": str(it.get("ProcessedAt") or ""),
        "model": str(model),
        "processing_seconds": processing_seconds,
        "avg_confidence": avg_conf,
        "warnings": expediente.get("warnings") or [],
        "field_stats": _field_stats(expediente),
        "sub_document_count": len(expediente.get("documents") or []),
    }


# ---------------------------------------------------------------------------
# CSS base — compartido por los tres HTML. Cada script puede concatenar CSS
# extra propio (ver reporte.py: filtros, <details>, <dialog>).
# ---------------------------------------------------------------------------

BASE_CSS = '''
:root {
  color-scheme: light;
  --surface: #fcfcfb; --plane: #f9f9f7; --text-primary: #0b0b0b;
  --text-secondary: #52514e; --text-muted: #898781; --gridline: #e1e0d9; --border: rgba(11,11,11,0.10);
  --accent: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface: #1a1a19; --plane: #0d0d0d; --text-primary: #ffffff;
    --text-secondary: #c3c2b7; --text-muted: #898781; --gridline: #2c2c2a; --border: rgba(255,255,255,0.10);
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 24px 64px; background: var(--plane); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 15px; line-height: 1.5; }
.wrap { max-width: 1160px; margin: 0 auto; }
header.page-head { margin-bottom: 28px; }
header.page-head h1 { font-size: 26px; margin: 0 0 6px; }
header.page-head .meta { color: var(--text-secondary); font-size: 14px; }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 24px; }
.kpi-tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; }
.kpi-value { font-size: 24px; font-weight: 650; font-variant-numeric: tabular-nums; }
.kpi-label { color: var(--text-secondary); font-size: 12.5px; margin-top: 2px; }
.kpi-sub { color: var(--text-muted); font-size: 12px; margin-top: 6px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 24px 26px; margin-bottom: 20px; }
.card h2 { font-size: 19px; margin: 0 0 4px; }
.section-sub { color: var(--text-secondary); font-size: 13px; margin: 0 0 16px; }
.bars { display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px; }
.bar-row { display: grid; grid-template-columns: 240px 1fr 90px; align-items: center; gap: 10px; }
.bar-label { font-size: 13px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bar-track { background: var(--gridline); border-radius: 6px; height: 14px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 6px; }
.bar-value { font-size: 13px; text-align: right; font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--gridline); }
th { color: var(--text-secondary); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .02em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.muted { color: var(--text-muted); }
td.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.note { color: var(--text-muted); font-size: 12px; margin: 12px 0 0; }
.note code, footer code { background: var(--gridline); padding: 1px 5px; border-radius: 4px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 7px; vertical-align: middle; }
.stack-bar { display: flex; height: 12px; border-radius: 6px; overflow: hidden; gap: 2px; background: var(--gridline); }
.stack-seg { height: 100%; }
.vol-chart { display: flex; align-items: flex-end; gap: 3px; height: 90px; }
.vol-bar { flex: 1; background: var(--accent); border-radius: 3px 3px 0 0; min-height: 3px; }
.vol-labels { display: flex; justify-content: space-between; color: var(--text-muted); font-size: 11.5px; margin-top: 6px; }
footer { color: var(--text-muted); font-size: 12px; text-align: center; margin-top: 32px; }
'''


def esc(s: Any) -> str:
    import html as html_mod

    return html_mod.escape(str(s), quote=True)


def kpi_tile(value: str, label: str, sub: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{esc(sub)}</div>' if sub else ""
    return f'<div class="kpi-tile"><div class="kpi-value">{esc(value)}</div><div class="kpi-label">{esc(label)}</div>{sub_html}</div>'


def h_bar(value: float, max_value: float, color: str, label: str, value_text: str) -> str:
    pct_width = 0 if max_value <= 0 else max(2.0, value / max_value * 100)
    return (
        f'<div class="bar-row"><div class="bar-label">{esc(label)}</div>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{pct_width:.2f}%;background:{color}"></div></div>'
        f'<div class="bar-value">{esc(value_text)}</div></div>'
    )


def stack_bar(counts, n: int, role_map: Dict[str, str]) -> str:
    if not n:
        return ""
    segs = []
    for status, count in counts.most_common():
        if count <= 0:
            continue
        color = STATUS_ROLE_COLOR[role_map.get(status, "neutral")]
        width = count / n * 100
        segs.append(f'<div class="stack-seg" style="width:{width:.3f}%;background:{color}" title="{esc(status)}: {count} de {n}"></div>')
    return f'<div class="stack-bar">{"".join(segs)}</div>'
