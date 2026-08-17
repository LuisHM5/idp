#!/usr/bin/env python3
"""Reporte ÚNICO e interactivo: une la tabla de costos (`cost_audit.py`) y la
tabla de tracking de documentos (`indexing.py`) por `DocumentId` en un solo
dashboard — costo, tokens, confianza, estado de negocio y salud operativa,
todo en un archivo. `pnpm reporte:costos` y `pnpm reporte:documentos` se
quedan como reportes independientes (mismo dato, cada uno enfocado en su
tema); este es el que junta todo para no tener que abrir dos.

El HTML es una página autocontenida (sin librerías externas, sin servidor):
los datos van embebidos como JSON y todo el filtrado/orden/detalle corre en
el navegador con JavaScript plano. Filtros por fecha, tipo de documento,
modelo, estado de ejecución y estado de negocio recalculan KPIs y tablas al
vuelo; click en un documento abre el detalle completo (costo por servicio +
calidad/campos). Las secciones con mucho contenido van en `<details>`
colapsables para no forzar scroll infinito.

Por default lee **dev**. Usa `--env prod` para el ambiente productivo.

Salida en `reportes/<env>/<timestamp>/`:
    reporte.md          resumen estático en texto/tablas
    reporte.html          dashboard interactivo (ábrelo en el navegador)
    datos_raw.xlsx         una fila por documento (costo + calidad) + hojas agregadas

Uso:
    pnpm reporte                              # dev, todo el historial
    pnpm reporte -- --env prod
    pnpm reporte -- --desde 2026-08-01 --hasta 2026-08-17
    pnpm reporte -- --open
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _report_lib import (  # noqa: E402
    CYAN,
    DIM,
    RED,
    RESET,
    YELLOW,
    cost_table_name,
    esc,
    normalize_cost_item,
    normalize_doc_item,
    resolve_processing_table,
    scan_all,
    stack_id,
)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT_ROOT = REPO / "reportes"


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def _model_from_services(services: Dict[str, Any]) -> Optional[str]:
    """Last-resort model attribution when the tracking table has none —
    walks the cost breakdown's per-service `modelId` in the same phase
    priority the tracking side uses. Covers documents processed before
    `Tracking.ModelIds` existed at all (an older bug, since fixed, meant the
    key it did have often didn't match anyway — see `normalize_doc_item`)."""
    for name in ("BedrockExtraction", "BedrockSegmentation", "BedrockProcessingUpstream", "BedrockClassificationUpstream"):
        svc = services.get(name)
        if svc and svc.get("model_id"):
            return svc["model_id"]
    return None


def join_rows(cost_rows: List[Dict[str, Any]], doc_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Full outer join por DocumentId — un documento puede tener costo sin
    tracking indexado (raro) o tracking sin costo (tabla de costos vacía o
    escritura best-effort que falló); ambos casos se marcan explícitos
    (`hasCost`/`hasQuality`) en vez de fingir un cero."""
    cost_by_id = {r["document_id"]: r for r in cost_rows if r["document_id"]}
    doc_by_id = {r["document_id"]: r for r in doc_rows if r["document_id"]}
    ids = set(cost_by_id) | set(doc_by_id)

    unified: List[Dict[str, Any]] = []
    for doc_id in ids:
        c = cost_by_id.get(doc_id)
        d = doc_by_id.get(doc_id)
        base = d or c  # identity fields — prefer tracking table, fall back to cost table
        doc_model = (d or {}).get("model")
        if not doc_model or doc_model == "(desconocido)":
            doc_model = _model_from_services((c or {}).get("services") or {}) or doc_model or "(desconocido)"
        unified.append({
            "id": doc_id,
            "type": base["document_type"],
            "typeName": base["document_type_name"],
            "name": (d or {}).get("document_name") or (c or {}).get("source_file") or "",
            "appOrigin": (d or {}).get("app_origin") or (c or {}).get("app_origin") or "",
            "processedAt": (d or {}).get("processed_at") or (c or {}).get("processed_at") or "",
            "hasQuality": d is not None,
            "hasCost": c is not None,
            "workflowStatus": (d or {}).get("workflow_status") or "",
            "validationStatus": (d or {}).get("validation_status") or "",
            "model": doc_model,
            "processingSeconds": (d or {}).get("processing_seconds"),
            "avgConfidence": (d or {}).get("avg_confidence"),
            "subDocCount": (d or {}).get("sub_document_count") or 0,
            "warnings": (d or {}).get("warnings") or [],
            "fieldStats": (d or {}).get("field_stats") or {},
            "pricingVersion": (c or {}).get("pricing_version") or "",
            "pagesTotal": (c or {}).get("pages_total") or 0,
            "pagesNative": (c or {}).get("pages_native") or 0,
            "pagesOcr": (c or {}).get("pages_ocr") or 0,
            "pagesEmpty": (c or {}).get("pages_empty") or 0,
            "totalCost": (c or {}).get("total_cost"),
            "costPerPage": (c or {}).get("cost_per_page"),
            "totalInputTokens": (c or {}).get("total_input_tokens"),
            "totalOutputTokens": (c or {}).get("total_output_tokens"),
            "services": (c or {}).get("services") or {},
        })
    return unified


# ---------------------------------------------------------------------------
# Agregación server-side — solo para el .md estático y el .xlsx crudo. El
# HTML recalcula todo en el navegador (ver JS embebido) porque tiene que
# reaccionar a los filtros; esto de aquí es una foto fija sin filtrar.
# ---------------------------------------------------------------------------

def money(x: Optional[float]) -> str:
    return f"${x:,.4f}" if x is not None else "—"


def num(x: Optional[float]) -> str:
    return f"{x:,.0f}" if x is not None else "—"


def pct(x: Optional[float]) -> str:
    return f"{x:.0%}" if x is not None else "—"


def overall_snapshot(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    completed = [r for r in rows if r["workflowStatus"] == "complete"]
    val_counts = Counter(r["validationStatus"] for r in completed if r["validationStatus"])
    with_cost = [r for r in rows if r["hasCost"]]
    confs = [r["avgConfidence"] for r in rows if r["avgConfidence"] is not None]
    total_cost = sum(r["totalCost"] or 0 for r in with_cost)
    total_pages = sum(r["pagesTotal"] or 0 for r in rows)
    dates = sorted(r["processedAt"] for r in rows if r["processedAt"])
    return {
        "n": n,
        "n_completed": len(completed),
        "approval_rate": (val_counts.get("APROBADO", 0) / len(completed)) if completed else None,
        "avg_confidence": (sum(confs) / len(confs)) if confs else None,
        "total_cost": total_cost,
        "avg_cost_doc": (total_cost / len(with_cost)) if with_cost else None,
        "total_input_tokens": sum(r["totalInputTokens"] or 0 for r in with_cost),
        "total_output_tokens": sum(r["totalOutputTokens"] or 0 for r in with_cost),
        "total_pages": total_pages,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "n_with_cost": len(with_cost),
    }


def by_key_snapshot(rows: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        buckets[r[key]].append(r)
    return {k: overall_snapshot(rs) for k, rs in buckets.items()}


def render_markdown(rows: List[Dict[str, Any]], env: str) -> str:
    overall = overall_snapshot(rows)
    by_type = by_key_snapshot(rows, "type")
    by_model = by_key_snapshot(rows, "model")

    lines = [
        f"# Reporte — {env}",
        "",
        f"Generado {datetime.now():%Y-%m-%d %H:%M} · {overall['n']} documento(s)"
        + (f" · {overall['first_date'][:10]} a {overall['last_date'][:10]}" if overall["first_date"] else ""),
        "",
        "Este resumen es una foto fija sin filtrar. Para filtrar por fecha/tipo/modelo/estado "
        "y ver el detalle de un documento, abre `reporte.html`.",
        "",
        "## Totales",
        "",
        "| Métrica | Valor |",
        "| --- | --- |",
        f"| Documentos | {overall['n']} |",
        f"| Ejecuciones completas | {overall['n_completed']} |",
        f"| Tasa de aprobación | {pct(overall['approval_rate'])} |",
        f"| Confianza promedio | {pct(overall['avg_confidence'])} |",
        f"| Documentos con datos de costo | {overall['n_with_cost']} |",
        f"| Costo total | {money(overall['total_cost'])} |",
        f"| Costo promedio / documento | {money(overall['avg_cost_doc'])} |",
        f"| Páginas procesadas | {num(overall['total_pages'])} |",
        f"| Tokens de entrada (total) | {num(overall['total_input_tokens'])} |",
        f"| Tokens de salida (total) | {num(overall['total_output_tokens'])} |",
        "",
        "## Por tipo de documento",
        "",
        "| Tipo | Docs | Aprobación | Confianza | Costo total | $/doc |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for t, m in sorted(by_type.items(), key=lambda kv: kv[1]["n"], reverse=True):
        lines.append(f"| {t} | {m['n']} | {pct(m['approval_rate'])} | {pct(m['avg_confidence'])} | {money(m['total_cost'])} | {money(m['avg_cost_doc'])} |")

    lines += ["", "## Por modelo", "", "| Modelo | Docs | Aprobación | Confianza | Costo total | $/doc |", "| --- | --- | --- | --- | --- | --- |"]
    for mdl, m in sorted(by_model.items(), key=lambda kv: kv[1]["n"], reverse=True):
        lines.append(f"| {mdl} | {m['n']} | {pct(m['approval_rate'])} | {pct(m['avg_confidence'])} | {money(m['total_cost'])} | {money(m['avg_cost_doc'])} |")

    lines += ["", "_Generado por `pnpm reporte` (scripts/reporte.py) — dashboard interactivo: `reporte.html` · datos crudos: `datos_raw.xlsx`._"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Excel (datos crudos)
# ---------------------------------------------------------------------------

def write_excel(out_path: Path, rows: List[Dict[str, Any]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    bold = Font(bold=True)

    def write_sheet(ws, headers: List[str], data_rows: List[List[Any]]) -> None:
        ws.append(headers)
        for cell in ws[1]:
            cell.font = bold
        for row in data_rows:
            ws.append(row)
        for col_idx, header in enumerate(headers, start=1):
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max(12, min(40, len(header) + 4))

    service_names = sorted({name for r in rows for name in r["services"]})
    ws_docs = wb.active
    ws_docs.title = "Documentos"
    headers = [
        "DocumentId", "DocumentType", "DocumentTypeName", "DocumentName", "AppOrigin", "ProcessedAt",
        "WorkflowStatus", "ValidationStatus", "Model", "ProcessingSeconds", "AvgConfidence", "SubDocumentCount",
        "PagesTotal", "PagesNative", "PagesOcr", "PagesEmpty",
        "TotalCostUsd", "CostPerPageUsd", "TotalInputTokens", "TotalOutputTokens", "PricingVersion",
        "HasCostData", "HasQualityData",
    ]
    for svc in service_names:
        headers += [f"{svc}.CostUsd", f"{svc}.InputTokens", f"{svc}.OutputTokens", f"{svc}.ModelId"]
    data_rows = []
    for r in rows:
        row = [
            r["id"], r["type"], r["typeName"], r["name"], r["appOrigin"], r["processedAt"],
            r["workflowStatus"], r["validationStatus"], r["model"], r["processingSeconds"], r["avgConfidence"],
            r["subDocCount"], r["pagesTotal"], r["pagesNative"], r["pagesOcr"], r["pagesEmpty"],
            r["totalCost"], r["costPerPage"], r["totalInputTokens"], r["totalOutputTokens"], r["pricingVersion"],
            r["hasCost"], r["hasQuality"],
        ]
        for svc in service_names:
            s = r["services"].get(svc)
            row += [s["cost"], s["input_tokens"], s["output_tokens"], s["model_id"]] if s else [None, None, None, None]
        data_rows.append(row)
    write_sheet(ws_docs, headers, data_rows)

    ws_type = wb.create_sheet("PorTipo")
    by_type = by_key_snapshot(rows, "type")
    write_sheet(
        ws_type,
        ["DocumentType", "Documentos", "TasaAprobacion", "ConfianzaPromedio", "CostoTotalUsd", "CostoPorDocUsd"],
        [[t, m["n"], m["approval_rate"], m["avg_confidence"], m["total_cost"], m["avg_cost_doc"]] for t, m in by_type.items()],
    )

    ws_model = wb.create_sheet("PorModelo")
    by_model = by_key_snapshot(rows, "model")
    write_sheet(
        ws_model,
        ["Model", "Documentos", "TasaAprobacion", "ConfianzaPromedio", "CostoTotalUsd", "CostoPorDocUsd"],
        [[m_, s["n"], s["approval_rate"], s["avg_confidence"], s["total_cost"], s["avg_cost_doc"]] for m_, s in by_model.items()],
    )

    ws_svc = wb.create_sheet("PorServicio")
    svc_agg: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "docs": 0})
    for r in rows:
        for name, s in r["services"].items():
            if not s["cost"] and not s["input_tokens"] and not s["output_tokens"]:
                continue
            agg = svc_agg[name]
            agg["cost"] += s["cost"]
            agg["input_tokens"] += s["input_tokens"]
            agg["output_tokens"] += s["output_tokens"]
            agg["docs"] += 1
    write_sheet(
        ws_svc,
        ["Servicio", "Documentos", "InputTokens", "OutputTokens", "CostoUsd"],
        [[name, a["docs"], a["input_tokens"], a["output_tokens"], a["cost"]] for name, a in svc_agg.items()],
    )

    ws_vol = wb.create_sheet("VolumenDiario")
    vol: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "cost": 0.0, "val": Counter()})
    for r in rows:
        day = (r["processedAt"] or "")[:10]
        if not day:
            continue
        v = vol[day]
        v["n"] += 1
        v["cost"] += r["totalCost"] or 0
        if r["workflowStatus"] == "complete" and r["validationStatus"]:
            v["val"][r["validationStatus"]] += 1
    write_sheet(
        ws_vol,
        ["Dia", "Documentos", "CostoUsd", "Aprobado", "RequiereRevision", "Rechazado"],
        [
            [day, v["n"], v["cost"], v["val"].get("APROBADO", 0), v["val"].get("REQUIERE_REVISION", 0), v["val"].get("RECHAZADO", 0)]
            for day, v in sorted(vol.items())
        ],
    )

    overall = overall_snapshot(rows)
    ws_tot = wb.create_sheet("Totales")
    write_sheet(
        ws_tot,
        ["Metrica", "Valor"],
        [
            ["Documentos", overall["n"]],
            ["Ejecuciones completas", overall["n_completed"]],
            ["Tasa de aprobacion", overall["approval_rate"]],
            ["Confianza promedio", overall["avg_confidence"]],
            ["Documentos con datos de costo", overall["n_with_cost"]],
            ["Costo total USD", overall["total_cost"]],
            ["Costo promedio por documento USD", overall["avg_cost_doc"]],
            ["Paginas procesadas", overall["total_pages"]],
            ["Tokens de entrada totales", overall["total_input_tokens"]],
            ["Tokens de salida totales", overall["total_output_tokens"]],
            ["Primer documento", overall["first_date"]],
            ["Ultimo documento", overall["last_date"]],
        ],
    )

    wb.save(out_path)


# ---------------------------------------------------------------------------
# HTML interactivo
# ---------------------------------------------------------------------------

EXTRA_CSS = '''
.filters-card { position: sticky; top: 12px; z-index: 5; }
.filters { display: flex; flex-wrap: wrap; gap: 18px; align-items: flex-start; margin-top: 4px; }
.filter-group { display: flex; flex-direction: column; gap: 6px; }
.filter-group .fg-label { font-size: 11.5px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .03em; }
.date-inputs { display: flex; gap: 8px; align-items: center; }
.date-inputs input[type=date] { background: var(--plane); color: var(--text-primary); border: 1px solid var(--border); border-radius: 8px; padding: 6px 8px; font-size: 13px; }
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; max-width: 480px; }
.chip { border: 1px solid var(--border); background: var(--plane); color: var(--text-secondary); border-radius: 999px; padding: 4px 12px; font-size: 12.5px; cursor: pointer; user-select: none; white-space: nowrap; }
.chip.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.filters-actions { display: flex; align-items: flex-end; }
#f-reset { background: var(--plane); border: 1px solid var(--border); color: var(--text-secondary); border-radius: 8px; padding: 8px 14px; font-size: 13px; cursor: pointer; }
#f-reset:hover { color: var(--text-primary); }
.filters-summary { margin-top: 14px; color: var(--text-secondary); font-size: 12.5px; }
details.card > summary { cursor: pointer; font-size: 19px; font-weight: 600; margin: 0 0 4px; list-style: none; }
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary::before { content: "▸ "; color: var(--text-muted); font-size: 15px; }
details.card[open] > summary::before { content: "▾ "; }
details.card[open] > summary { margin-bottom: 14px; }
th.sortable { cursor: pointer; }
th.sortable:hover { color: var(--text-primary); }
th.sortable .arrow { opacity: .5; margin-left: 3px; }
tbody tr.doc-row { cursor: pointer; }
tbody tr.doc-row:hover { background: var(--plane); }
dialog#detail-dialog { border: none; border-radius: 14px; padding: 0; background: var(--surface); color: var(--text-primary); max-width: 720px; width: 92vw; max-height: 86vh; }
dialog#detail-dialog::backdrop { background: rgba(0,0,0,0.5); }
.detail-inner { padding: 24px 26px; overflow-y: auto; max-height: 86vh; }
.detail-inner h3 { margin: 18px 0 8px; font-size: 15px; }
.detail-inner h3:first-child { margin-top: 0; }
.detail-close { position: sticky; top: 10px; float: right; background: var(--plane); border: 1px solid var(--border); color: var(--text-secondary); border-radius: 999px; width: 30px; height: 30px; cursor: pointer; font-size: 15px; }
.kv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 16px; font-size: 13px; }
.kv-grid .k { color: var(--text-muted); }
.warn-list { margin: 4px 0 0; padding-left: 18px; font-size: 13px; color: var(--text-secondary); }
.more-btn { margin-top: 12px; background: var(--plane); border: 1px solid var(--border); color: var(--text-secondary); border-radius: 8px; padding: 7px 14px; font-size: 13px; cursor: pointer; }
.empty-hint { color: var(--text-muted); font-size: 13px; }
'''

JS_TEMPLATE = r'''
const DATA = JSON.parse(document.getElementById('report-data').textContent);
const ROWS = DATA.rows;
const ROWS_BY_ID = new Map(ROWS.map(r => [r.id, r]));
const CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
const STATUS_ROLE_COLOR = {good: "#0ca30c", warning: "#fab219", critical: "#d03b3b", neutral: "#898781"};
const VALIDATION_ROLE = {APROBADO: "good", REQUIERE_REVISION: "warning", RECHAZADO: "critical"};
const WORKFLOW_ROLE = {"complete":"good","pending":"neutral","classification-complete":"neutral","processing-complete":"neutral","classification-failure":"critical","processing-failure":"critical","enrichment-failure":"critical"};
const SERVICE_LABEL = {
  BedrockClassificationUpstream: "Bedrock · Clasificación (upstream)",
  BedrockProcessingUpstream: "Bedrock · Procesamiento (upstream)",
  BedrockSegmentation: "Bedrock · Segmentación",
  BedrockExtraction: "Bedrock · Extracción",
  TextractOcr: "Textract · OCR",
  LambdaCompute: "Lambda · Cómputo (aprox.)",
};

function esc(s){ const d=document.createElement('div'); d.textContent = (s===null||s===undefined) ? '' : String(s); return d.innerHTML; }
function money(x){ return (x===null||x===undefined) ? '—' : '$'+Number(x).toLocaleString('en-US',{minimumFractionDigits:4,maximumFractionDigits:4}); }
function num(x){ return (x===null||x===undefined) ? '—' : Number(x).toLocaleString('en-US',{maximumFractionDigits:0}); }
function pctFmt(x){ return (x===null||x===undefined) ? '—' : (x*100).toFixed(0)+'%'; }
function secsFmt(x){ if (x===null||x===undefined) return '—'; return x<1 ? Math.round(x*1000)+'ms' : x.toFixed(1)+'s'; }

// ---------------------------------------------------------------------
// Estado de filtros
// ---------------------------------------------------------------------
const state = {
  desde: '', hasta: '',
  types: new Set(), models: new Set(), workflow: new Set(), validation: new Set(),
  hiddenServices: new Set(),
  sortKey: 'processedAt', sortDir: -1,
  docLimit: 200,
};

function distinct(key) { return [...new Set(ROWS.map(r => r[key]).filter(Boolean))].sort(); }

function filteredRows() {
  return ROWS.filter(r => {
    if (state.desde && (r.processedAt||'').slice(0,10) < state.desde) return false;
    if (state.hasta && (r.processedAt||'').slice(0,10) > state.hasta) return false;
    if (state.types.size && !state.types.has(r.type)) return false;
    if (state.models.size && !state.models.has(r.model)) return false;
    if (state.workflow.size && !state.workflow.has(r.workflowStatus)) return false;
    if (state.validation.size && !state.validation.has(r.validationStatus)) return false;
    return true;
  });
}

// ---------------------------------------------------------------------
// Agregación (misma forma que el lado Python, recalculada en cliente)
// ---------------------------------------------------------------------
function aggOverall(rows) {
  const n = rows.length;
  const wfCounts = {};
  const completed = [];
  rows.forEach(r => { const k = r.workflowStatus || '(sin dato)'; wfCounts[k] = (wfCounts[k]||0)+1; if (r.workflowStatus === 'complete') completed.push(r); });
  const valCounts = {};
  completed.forEach(r => { if (r.validationStatus) valCounts[r.validationStatus] = (valCounts[r.validationStatus]||0)+1; });
  const approved = valCounts['APROBADO'] || 0;
  const confs = rows.filter(r => r.avgConfidence !== null && r.avgConfidence !== undefined).map(r => r.avgConfidence);
  const durs = rows.filter(r => r.processingSeconds !== null && r.processingSeconds !== undefined).map(r => r.processingSeconds);
  const withCost = rows.filter(r => r.hasCost);
  const totalCost = withCost.reduce((a,r) => a + (r.totalCost||0), 0);
  const totalPages = rows.reduce((a,r) => a + (r.pagesTotal||0), 0);
  const totalIn = withCost.reduce((a,r) => a + (r.totalInputTokens||0), 0);
  const totalOut = withCost.reduce((a,r) => a + (r.totalOutputTokens||0), 0);
  const dates = rows.map(r => r.processedAt).filter(Boolean).sort();
  return {
    n, wfCounts, nCompleted: completed.length, valCounts,
    approvalRate: completed.length ? approved/completed.length : null,
    avgConfidence: confs.length ? confs.reduce((a,b)=>a+b,0)/confs.length : null,
    avgDuration: durs.length ? durs.reduce((a,b)=>a+b,0)/durs.length : null,
    totalCost, nWithCost: withCost.length,
    avgCostDoc: withCost.length ? totalCost/withCost.length : null,
    avgCostPage: totalPages ? totalCost/totalPages : null,
    totalPages, totalInputTokens: totalIn, totalOutputTokens: totalOut,
    firstDate: dates[0]||null, lastDate: dates[dates.length-1]||null,
  };
}

function aggBy(rows, keyFn) {
  const buckets = new Map();
  rows.forEach(r => { const k = keyFn(r); if (!buckets.has(k)) buckets.set(k, []); buckets.get(k).push(r); });
  const out = [];
  for (const [k, rs] of buckets) out.push([k, aggOverall(rs)]);
  return out;
}

function aggByService(rows) {
  const svc = {};
  rows.forEach(r => {
    Object.entries(r.services || {}).forEach(([name, s]) => {
      if (!s.cost && !s.input_tokens && !s.output_tokens) return;
      if (!svc[name]) svc[name] = {cost:0, inputTokens:0, outputTokens:0, docs:0};
      svc[name].cost += s.cost || 0;
      svc[name].inputTokens += s.input_tokens || 0;
      svc[name].outputTokens += s.output_tokens || 0;
      svc[name].docs += 1;
    });
  });
  return svc;
}

function dailyVolume(rows) {
  const byDay = new Map();
  rows.forEach(r => {
    const day = (r.processedAt || '').slice(0,10);
    if (!day) return;
    if (!byDay.has(day)) byDay.set(day, {day, n:0, cost:0, val:{}});
    const b = byDay.get(day);
    b.n += 1;
    b.cost += r.totalCost || 0;
    if (r.workflowStatus === 'complete' && r.validationStatus) b.val[r.validationStatus] = (b.val[r.validationStatus]||0)+1;
  });
  return [...byDay.values()].sort((a,b) => a.day.localeCompare(b.day));
}

function worstFields(rows, limit) {
  const agg = {};
  rows.forEach(r => {
    Object.entries(r.fieldStats || {}).forEach(([name, s]) => {
      if (!agg[name]) agg[name] = {ok:0, total:0};
      agg[name].ok += s.ok; agg[name].total += s.total;
    });
  });
  return Object.entries(agg)
    .filter(([,s]) => s.total && s.ok < s.total)
    .sort((a,b) => (a[1].ok/a[1].total) - (b[1].ok/b[1].total))
    .slice(0, limit);
}

// ---------------------------------------------------------------------
// Render: bloques reusables
// ---------------------------------------------------------------------
function kpiTile(value, label, sub) {
  return `<div class="kpi-tile"><div class="kpi-value">${esc(value)}</div><div class="kpi-label">${esc(label)}</div>${sub?`<div class="kpi-sub">${esc(sub)}</div>`:''}</div>`;
}
function hBar(value, maxValue, color, label, valueText) {
  const w = maxValue <= 0 ? 0 : Math.max(2, value/maxValue*100);
  return `<div class="bar-row"><div class="bar-label">${esc(label)}</div><div class="bar-track"><div class="bar-fill" style="width:${w.toFixed(2)}%;background:${color}"></div></div><div class="bar-value">${esc(valueText)}</div></div>`;
}
function stackBar(counts, n, roleMap) {
  if (!n) return '<p class="empty-hint">Sin datos con los filtros actuales.</p>';
  const entries = Object.entries(counts).sort((a,b) => b[1]-a[1]);
  const segs = entries.filter(([,c]) => c>0).map(([status,count]) => {
    const color = STATUS_ROLE_COLOR[roleMap[status] || 'neutral'];
    const w = count/n*100;
    return `<div class="stack-seg" style="width:${w.toFixed(3)}%;background:${color}" title="${esc(status)}: ${count} de ${n}"></div>`;
  }).join('');
  return `<div class="stack-bar">${segs}</div>`;
}

function renderFilters() {
  const mk = (containerId, values, activeSet) => {
    const el = document.getElementById(containerId);
    el.innerHTML = values.map(v => `<span class="chip${activeSet.has(v)?' active':''}" data-value="${esc(v)}">${esc(v)}</span>`).join('');
  };
  mk('f-types', distinct('type'), state.types);
  mk('f-models', distinct('model'), state.models);
  mk('f-workflow', distinct('workflowStatus'), state.workflow);
  mk('f-validation', distinct('validationStatus'), state.validation);
  mk('f-services', distinct === null ? [] : Object.keys(SERVICE_LABEL).filter(s => ROWS.some(r => r.services[s])), state.hiddenServices);
  // hiddenServices marca "oculto", no "activo" — invertimos la clase abajo en el listener.
}

function attachChipHandlers() {
  document.querySelectorAll('#f-types .chip').forEach(el => el.addEventListener('click', () => { toggle(state.types, el.dataset.value); render(); }));
  document.querySelectorAll('#f-models .chip').forEach(el => el.addEventListener('click', () => { toggle(state.models, el.dataset.value); render(); }));
  document.querySelectorAll('#f-workflow .chip').forEach(el => el.addEventListener('click', () => { toggle(state.workflow, el.dataset.value); render(); }));
  document.querySelectorAll('#f-validation .chip').forEach(el => el.addEventListener('click', () => { toggle(state.validation, el.dataset.value); render(); }));
  document.querySelectorAll('#f-services .chip').forEach(el => {
    if (!state.hiddenServices.has(el.dataset.value)) el.classList.add('active');
    el.addEventListener('click', () => { toggle(state.hiddenServices, el.dataset.value); renderServiceSection(filteredRows()); attachServiceChipHandlers(); });
  });
}
function attachServiceChipHandlers() {
  document.querySelectorAll('#f-services .chip').forEach(el => {
    el.classList.toggle('active', !state.hiddenServices.has(el.dataset.value));
    el.onclick = () => { toggle(state.hiddenServices, el.dataset.value); renderServiceSection(filteredRows()); attachServiceChipHandlers(); };
  });
}
function toggle(set, value) { set.has(value) ? set.delete(value) : set.add(value); }

function renderServiceSection(rows) {
  const bySvc = aggByService(rows);
  const visible = Object.entries(bySvc).filter(([name]) => !state.hiddenServices.has(name));
  const maxCost = Math.max(0, ...visible.map(([,a]) => a.cost));
  const bars = visible.sort((a,b) => b[1].cost - a[1].cost)
    .map(([name,a],i) => hBar(a.cost, maxCost, CATEGORICAL[i % CATEGORICAL.length], SERVICE_LABEL[name]||name, money(a.cost)))
    .join('');
  document.getElementById('by-service-bars').innerHTML = bars || '<p class="empty-hint">Sin datos de costo con los filtros actuales.</p>';
}

function renderDocumentsTable(rows) {
  const sorted = [...rows].sort((a,b) => {
    const ka = a[state.sortKey], kb = b[state.sortKey];
    if (ka === null || ka === undefined) return 1;
    if (kb === null || kb === undefined) return -1;
    if (ka < kb) return -1 * state.sortDir;
    if (ka > kb) return 1 * state.sortDir;
    return 0;
  });
  const shown = sorted.slice(0, state.docLimit);
  document.getElementById('doc-count').textContent = rows.length;
  const cols = [
    ['id','Documento'], ['typeName','Tipo'], ['workflowStatus','Ejecución'], ['validationStatus','Negocio'],
    ['model','Modelo'], ['avgConfidence','Confianza'], ['totalCost','Costo'], ['processedAt','Procesado'],
  ];
  const head = cols.map(([key,label]) => {
    const arrow = state.sortKey === key ? (state.sortDir === 1 ? '↑' : '↓') : '';
    return `<th class="sortable" data-key="${key}">${esc(label)} <span class="arrow">${arrow}</span></th>`;
  }).join('');
  const body = shown.map(r => `<tr class="doc-row" data-id="${esc(r.id)}">
      <td class="mono">${esc(r.id)}</td>
      <td>${esc(r.typeName || r.type)}</td>
      <td>${esc(r.workflowStatus || '—')}</td>
      <td>${esc(r.validationStatus || '—')}</td>
      <td class="muted">${esc(r.model)}</td>
      <td class="num">${pctFmt(r.avgConfidence)}</td>
      <td class="num">${r.hasCost ? money(r.totalCost) : '—'}</td>
      <td class="muted">${esc((r.processedAt||'').slice(0,19))}</td>
    </tr>`).join('');
  document.getElementById('documents-table').innerHTML =
    `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  const more = document.getElementById('documents-more');
  more.innerHTML = rows.length > state.docLimit
    ? `<button class="more-btn" id="show-more">Mostrar ${Math.min(200, rows.length-state.docLimit)} más (${rows.length-state.docLimit} restantes)</button>`
    : '';
  const btn = document.getElementById('show-more');
  if (btn) btn.addEventListener('click', () => { state.docLimit += 200; renderDocumentsTable(filteredRows()); });
  document.querySelectorAll('#documents-table th.sortable').forEach(th => th.addEventListener('click', () => {
    const key = th.dataset.key;
    state.sortDir = (state.sortKey === key) ? -state.sortDir : -1;
    state.sortKey = key;
    renderDocumentsTable(filteredRows());
  }));
  document.querySelectorAll('#documents-table tbody tr.doc-row').forEach(tr => tr.addEventListener('click', () => openDetail(tr.dataset.id)));
}

function openDetail(id) {
  const r = ROWS_BY_ID.get(id);
  if (!r) return;
  const fieldRows = Object.entries(r.fieldStats||{}).map(([name,s]) =>
    `<tr><td>${esc(name)}</td><td class="num">${s.ok}/${s.total}</td></tr>`).join('');
  const svcRows = Object.entries(r.services||{}).filter(([,s]) => s.cost || s.input_tokens || s.output_tokens || s.pages || s.duration_ms).map(([name,s]) =>
    `<tr><td>${esc(SERVICE_LABEL[name]||name)}</td><td class="num">${money(s.cost)}</td><td class="num">${num(s.input_tokens)}</td><td class="num">${num(s.output_tokens)}</td><td class="muted">${esc(s.model_id||'—')}</td></tr>`).join('');
  const warnings = (r.warnings||[]).length ? `<ul class="warn-list">${r.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul>` : '<p class="empty-hint">Sin advertencias.</p>';

  document.getElementById('detail-content').innerHTML = `
    <button class="detail-close" id="detail-close-btn">✕</button>
    <h2 style="margin:0 0 4px">${esc(r.typeName || r.type)}</h2>
    <p class="section-sub" style="margin-bottom:14px">${esc(r.id)} · ${esc(r.name || 'sin nombre de archivo')}</p>

    <h3>Calidad</h3>
    ${r.hasQuality ? `<div class="kv-grid">
      <div class="k">Estado de ejecución</div><div>${esc(r.workflowStatus||'—')}</div>
      <div class="k">Estado de negocio</div><div>${esc(r.validationStatus||'—')}</div>
      <div class="k">Modelo</div><div>${esc(r.model)}</div>
      <div class="k">Confianza promedio</div><div>${pctFmt(r.avgConfidence)}</div>
      <div class="k">Tiempo de proceso</div><div>${secsFmt(r.processingSeconds)}</div>
      <div class="k">Documentos en el expediente</div><div>${r.subDocCount}</div>
      <div class="k">Procesado</div><div>${esc((r.processedAt||'').slice(0,19))}</div>
      <div class="k">App de origen</div><div>${esc(r.appOrigin||'—')}</div>
    </div>
    <h3>Advertencias</h3>${warnings}
    ${fieldRows ? `<h3>Campos (cobertura parcial en expedientes multi-documento)</h3>
      <div class="table-wrap"><table><thead><tr><th>Campo</th><th class="num">Válidos</th></tr></thead><tbody>${fieldRows}</tbody></table></div>` : ''}
    ` : '<p class="empty-hint">Sin datos de calidad para este documento (no está en la tabla de tracking).</p>'}

    <h3>Costo</h3>
    ${r.hasCost ? `<div class="kv-grid">
      <div class="k">Costo total</div><div>${money(r.totalCost)}</div>
      <div class="k">Costo por página</div><div>${r.costPerPage!==null&&r.costPerPage!==undefined?money(r.costPerPage):'—'}</div>
      <div class="k">Páginas</div><div>${r.pagesTotal} (${r.pagesNative} nativas, ${r.pagesOcr} OCR, ${r.pagesEmpty} vacías)</div>
      <div class="k">Tokens de entrada</div><div>${num(r.totalInputTokens)}</div>
      <div class="k">Tokens de salida</div><div>${num(r.totalOutputTokens)}</div>
      <div class="k">Versión de tarifa</div><div>${esc(r.pricingVersion||'—')}</div>
    </div>
    <div class="table-wrap"><table><thead><tr><th>Servicio</th><th class="num">Costo</th><th class="num">Tokens in</th><th class="num">Tokens out</th><th>Modelo</th></tr></thead><tbody>${svcRows}</tbody></table></div>
    ` : '<p class="empty-hint">Sin datos de costo para este documento (tabla de costos vacía, o el registro no se llegó a escribir).</p>'}
  `;
  document.getElementById('detail-close-btn').addEventListener('click', () => document.getElementById('detail-dialog').close());
  document.getElementById('detail-dialog').showModal();
}

function render() {
  const rows = filteredRows();
  renderFilters();
  attachChipHandlers();

  document.getElementById('f-summary').textContent =
    `Mostrando ${rows.length} de ${ROWS.length} documento(s) con los filtros actuales.`;

  const overall = aggOverall(rows);
  document.getElementById('kpis').innerHTML = [
    kpiTile(num(overall.n), 'Documentos'),
    kpiTile(num(overall.nCompleted), 'Ejecuciones completas'),
    kpiTile(pctFmt(overall.approvalRate), 'Tasa de aprobación'),
    kpiTile(pctFmt(overall.avgConfidence), 'Confianza promedio'),
    kpiTile(money(overall.totalCost), 'Costo total', overall.nWithCost < overall.n ? `${overall.nWithCost} de ${overall.n} con dato de costo` : ''),
    kpiTile(money(overall.avgCostDoc), 'Costo promedio / doc'),
    kpiTile(num(overall.totalInputTokens), 'Tokens de entrada'),
    kpiTile(num(overall.totalOutputTokens), 'Tokens de salida'),
  ].join('');

  document.getElementById('wf-stack').innerHTML = stackBar(overall.wfCounts, overall.n, WORKFLOW_ROLE);
  document.getElementById('validation-stack').innerHTML = stackBar(overall.valCounts, overall.nCompleted, VALIDATION_ROLE);

  const volume = dailyVolume(rows);
  if (volume.length) {
    const maxN = Math.max(...volume.map(v => v.n));
    const bars = volume.map(v => `<div class="vol-bar" style="height:${Math.max(4, v.n/maxN*100).toFixed(1)}%" title="${esc(v.day)}: ${v.n} documento(s), ${money(v.cost)}"></div>`).join('');
    document.getElementById('volume-chart').innerHTML =
      `<div class="vol-chart">${bars}</div><div class="vol-labels"><span>${esc(volume[0].day)}</span><span>${esc(volume[volume.length-1].day)}</span></div>`;
  } else {
    document.getElementById('volume-chart').innerHTML = '<p class="empty-hint">Sin datos con los filtros actuales.</p>';
  }

  const byType = aggBy(rows, r => r.type).sort((a,b) => b[1].n - a[1].n);
  document.getElementById('by-type-table').innerHTML = byType.length ? `<div class="table-wrap"><table>
    <thead><tr><th>Tipo</th><th class="num">Docs</th><th class="num">Aprobación</th><th class="num">Confianza</th><th class="num">Costo total</th><th class="num">$/doc</th></tr></thead>
    <tbody>${byType.map(([t,m]) => `<tr><td>${esc(t)}</td><td class="num">${m.n}</td><td class="num">${pctFmt(m.approvalRate)}</td><td class="num">${pctFmt(m.avgConfidence)}</td><td class="num">${money(m.totalCost)}</td><td class="num">${money(m.avgCostDoc)}</td></tr>`).join('')}</tbody>
  </table></div>` : '<p class="empty-hint">Sin datos con los filtros actuales.</p>';

  const byModel = aggBy(rows, r => r.model).sort((a,b) => b[1].n - a[1].n);
  const maxModelN = Math.max(0, ...byModel.map(([,m]) => m.n));
  document.getElementById('by-model-bars').innerHTML = byModel.map(([name,m],i) =>
    hBar(m.n, maxModelN, CATEGORICAL[i % CATEGORICAL.length], `${name} (${m.n} docs)`, pctFmt(m.approvalRate))).join('');
  document.getElementById('by-model-table').innerHTML = byModel.length ? `<div class="table-wrap"><table>
    <thead><tr><th>Modelo</th><th class="num">Docs</th><th class="num">Aprobación</th><th class="num">Confianza</th><th class="num">Costo total</th></tr></thead>
    <tbody>${byModel.map(([name,m]) => `<tr><td>${esc(name)}</td><td class="num">${m.n}</td><td class="num">${pctFmt(m.approvalRate)}</td><td class="num">${pctFmt(m.avgConfidence)}</td><td class="num">${money(m.totalCost)}</td></tr>`).join('')}</tbody>
  </table></div>` : '';

  renderServiceSection(rows);
  attachServiceChipHandlers();

  const wf = worstFields(rows, 25);
  document.getElementById('worst-fields-table').innerHTML = wf.length ? `<div class="table-wrap"><table>
    <thead><tr><th>Campo</th><th class="num">Válidos</th><th class="num">Tasa</th></tr></thead>
    <tbody>${wf.map(([name,s]) => `<tr><td>${esc(name)}</td><td class="num">${s.ok}/${s.total}</td><td class="num">${(s.ok/s.total*100).toFixed(0)}%</td></tr>`).join('')}</tbody>
  </table></div>` : '<p class="empty-hint">Sin campos con fallas registradas en los documentos filtrados.</p>';

  renderDocumentsTable(rows);
}

document.getElementById('f-desde').addEventListener('change', e => { state.desde = e.target.value; render(); });
document.getElementById('f-hasta').addEventListener('change', e => { state.hasta = e.target.value; render(); });
document.getElementById('f-reset').addEventListener('click', () => {
  state.desde = ''; state.hasta = ''; state.types.clear(); state.models.clear(); state.workflow.clear(); state.validation.clear();
  document.getElementById('f-desde').value = ''; document.getElementById('f-hasta').value = '';
  render();
});

render();
'''


def _json_default(o: Any) -> Any:
    """Safety net for the browser-embedded JSON: `_report_lib.normalize_*`
    already casts every known DynamoDB numeric field away from Decimal, but a
    future field added there (or upstream in cost_audit.py/indexing.py)
    could reintroduce one — better a correct float here than a crashed
    report generation."""
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def render_html(rows: List[Dict[str, Any]], env: str) -> str:
    from _report_lib import BASE_CSS

    overall = overall_snapshot(rows)
    date_range = f"{overall['first_date'][:10]} a {overall['last_date'][:10]}" if overall["first_date"] else "sin datos"

    body = f'''<div class="wrap">
      <header class="page-head">
        <h1>Reporte — {esc(env)}</h1>
        <div class="meta">Generado {datetime.now():%Y-%m-%d %H:%M} · rango: {esc(date_range)} · {overall['n']} documento(s)</div>
      </header>

      <section class="card filters-card">
        <h2>Filtros</h2>
        <div class="filters">
          <div class="filter-group">
            <span class="fg-label">Fecha</span>
            <div class="date-inputs">
              <input type="date" id="f-desde"> <span class="muted">a</span> <input type="date" id="f-hasta">
            </div>
          </div>
          <div class="filter-group"><span class="fg-label">Tipo de documento</span><div class="chip-row" id="f-types"></div></div>
          <div class="filter-group"><span class="fg-label">Modelo</span><div class="chip-row" id="f-models"></div></div>
          <div class="filter-group"><span class="fg-label">Estado de ejecución</span><div class="chip-row" id="f-workflow"></div></div>
          <div class="filter-group"><span class="fg-label">Estado de negocio</span><div class="chip-row" id="f-validation"></div></div>
          <div class="filters-actions"><button id="f-reset">Limpiar filtros</button></div>
        </div>
        <div class="filters-summary" id="f-summary"></div>
      </section>

      <div class="kpi-row" id="kpis"></div>

      <section class="card">
        <h2>Estado de las ejecuciones</h2>
        <p class="section-sub">Completa / en progreso / fallida.</p>
        <div id="wf-stack"></div>
      </section>

      <section class="card">
        <h2>Resultado de negocio</h2>
        <p class="section-sub">Aprobado / requiere revisión / rechazado, solo de ejecuciones completas.</p>
        <div id="validation-stack"></div>
      </section>

      <details class="card" open>
        <summary>Volumen diario</summary>
        <div id="volume-chart"></div>
      </details>

      <details class="card" open>
        <summary>Por tipo de documento</summary>
        <div id="by-type-table"></div>
      </details>

      <details class="card" open>
        <summary>Por modelo</summary>
        <div class="bars" id="by-model-bars"></div>
        <div id="by-model-table"></div>
      </details>

      <details class="card">
        <summary>Costo por servicio AWS</summary>
        <p class="section-sub">Click en un servicio para ocultarlo/mostrarlo en la gráfica.</p>
        <div class="chip-row" id="f-services" style="margin-bottom:14px"></div>
        <div class="bars" id="by-service-bars"></div>
      </details>

      <details class="card">
        <summary>Campos que más fallan</summary>
        <p class="section-sub">Cobertura parcial en expedientes multi-documento — el detalle completo de cada documento interno solo vive en <code>results/&#123;id&#125;.json</code>.</p>
        <div id="worst-fields-table"></div>
      </details>

      <details class="card" open>
        <summary>Documentos (<span id="doc-count">0</span>)</summary>
        <p class="section-sub">Click en un renglón para ver el detalle completo (costo + calidad). Click en un encabezado para ordenar.</p>
        <div class="table-wrap" id="documents-table"></div>
        <div id="documents-more"></div>
      </details>

      <dialog id="detail-dialog"><div class="detail-inner" id="detail-content"></div></dialog>

      <footer>Generado por <code>pnpm reporte</code> (scripts/reporte.py) · une la tabla de costos y la de tracking de documentos por <code>DocumentId</code> · datos crudos en <code>datos_raw.xlsx</code> · reportes individuales: <code>pnpm reporte:costos</code> / <code>pnpm reporte:documentos</code>.</footer>
    </div>
    <script id="report-data" type="application/json">{json.dumps({"rows": rows}, ensure_ascii=False, default=_json_default)}</script>
    <script>{JS_TEMPLATE}</script>'''

    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reporte — {esc(env)}</title>
<style>{BASE_CSS}{EXTRA_CSS}</style>
</head>
<body>{body}</body>
</html>'''


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=("dev", "prod"), default="dev", help="ambiente a consultar (default: dev)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack", help="nombre del stack (default: resuelto de --env)")
    parser.add_argument("--table", help="tabla de tracking de documentos, salta la resolución vía CloudFormation")
    parser.add_argument("--cost-table", help="tabla de costos (default: resuelta de --env)")
    parser.add_argument("--desde", help="filtra ProcessedAt >= YYYY-MM-DD (también filtrable interactivo en el HTML)")
    parser.add_argument("--hasta", help="filtra ProcessedAt <= YYYY-MM-DD")
    parser.add_argument("--out", help="carpeta de salida (default: reportes/<env>/<timestamp>/)")
    parser.add_argument("--open", action="store_true", help="abrir el HTML en el navegador al terminar")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    stack = args.stack or stack_id(args.env)
    cost_table = args.cost_table or cost_table_name(args.env)

    cost_raw: List[Dict[str, Any]] = []
    try:
        cost_raw = scan_all(cost_table, args.region)
    except Exception as e:
        print(f"{YELLOW}No pude leer la tabla de costos {cost_table}: {e} — el reporte sigue sin datos de costo.{RESET}")

    try:
        doc_table = args.table or resolve_processing_table(stack, args.region)
    except Exception as e:
        print(f"{RED}{e}{RESET}")
        return 1

    print(f"Leyendo {doc_table} y {cost_table} en {args.region} (stack {stack})...")
    try:
        doc_raw = scan_all(doc_table, args.region)
    except Exception as e:
        print(f"{RED}No pude leer la tabla {doc_table} en {args.region}: {e}{RESET}")
        return 1

    if not cost_raw and not doc_raw:
        print(f"{YELLOW}Ambas tablas están vacías — aún no se ha procesado ningún documento con este stack.{RESET}")
        return 0

    cost_rows = [normalize_cost_item(it) for it in cost_raw]
    doc_rows = [normalize_doc_item(it) for it in doc_raw]
    rows = join_rows(cost_rows, doc_rows)

    if args.desde or args.hasta:
        rows = [r for r in rows if (not args.desde or r["processedAt"][:10] >= args.desde) and (not args.hasta or r["processedAt"][:10] <= args.hasta)]
        if not rows:
            print(f"{YELLOW}Ningún documento cae en el rango de fechas pedido.{RESET}")
            return 0

    out_dir = Path(args.out) if args.out else DEFAULT_OUT_ROOT / args.env / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "reporte.md").write_text(render_markdown(rows, args.env), encoding="utf-8")
    (out_dir / "reporte.html").write_text(render_html(rows, args.env), encoding="utf-8")
    write_excel(out_dir / "datos_raw.xlsx", rows)

    overall = overall_snapshot(rows)
    print(
        f"\n{CYAN}{overall['n']} documento(s) ({overall['n_with_cost']} con costo) · "
        f"aprobación {pct(overall['approval_rate'])} · costo total {money(overall['total_cost'])}{RESET}"
    )
    print(f"Reporte → {out_dir}/")
    print("  reporte.md        (resumen estático)")
    print("  reporte.html       (dashboard interactivo — ábrelo en el navegador)")
    print("  datos_raw.xlsx     (datos crudos — Excel)")

    if args.open:
        webbrowser.open((out_dir / "reporte.html").resolve().as_uri())

    return 0


if __name__ == "__main__":
    sys.exit(main())
