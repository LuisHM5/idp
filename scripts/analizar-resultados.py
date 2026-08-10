#!/usr/bin/env python3
"""Analiza TODOS los resultados que ya dejó `pnpm procesar` (scripts/procesar-docs.py)
y arma un reporte HTML — pensado para un director o alguien no técnico: qué tan
bien le fue a cada modelo, por tipo de documento, en cuántos documentos, con
costo y calidad reales.

No corre el pipeline ni llama a AWS: solo lee los `<archivo>.json` que ya
existen bajo `<dir>/[<modelo>/]<TIPO>/<nombre-doc>/NNN/<archivo>.json` (la
estructura que arma procesar-docs.py) y calcula todo localmente. Es robusto a
corridas viejas/mezcladas: solo toma en cuenta un json cuya carpeta contenedora
sea un número (NNN) — cualquier otro archivo/carpeta suelta bajo `resultados/`
se ignora en vez de romper el análisis.

El modelo y el tipo de cada documento se leen del CONTENIDO del json
(`tracking.schema.model`, `documents[].documentType`), no de la ruta — así
funciona sin importar cuántos niveles de carpeta haya.

Uso:
    pnpm analizar                              # analiza toda resultados/
    pnpm analizar -- --dir otra_carpeta         # otra carpeta de resultados
    pnpm analizar -- --out reporte.html         # dónde dejar el HTML (default: <dir>/_analisis.html)
    pnpm analizar -- --open                     # abrirlo en el navegador al terminar
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))

DEFAULT_DIR = REPO / "resultados"

# Paleta y roles de estado — ver dataviz skill (references/palette.md). Los
# colores de estado son fijos (nunca "serie 4"); los categóricos (uno por
# modelo) siguen el orden validado, nunca se reasignan por posición/filtro.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
STATUS_ROLE_COLOR = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}
STATUS_ROLE = {
    "APPROVED": "good",
    "PARTIALLY_APPROVED": "warning",
    "REVIEW_REQUIRED": "serious",
    "REJECTED": "critical",
    "PROCESSING_ERROR": "critical",
    "ERROR": "critical",
}
STATUS_LABEL_ES = {
    "APPROVED": "Aprobado",
    "PARTIALLY_APPROVED": "Aprobado parcial",
    "REVIEW_REQUIRED": "Requiere revisión",
    "REJECTED": "Rechazado",
    "PROCESSING_ERROR": "Error de proceso",
    "ERROR": "Error al procesar",
}
STATUS_SEVERITY = {
    "PROCESSING_ERROR": 4, "ERROR": 4, "REJECTED": 3,
    "REVIEW_REQUIRED": 2, "PARTIALLY_APPROVED": 1, "APPROVED": 0,
}


def _walk_field_tree(node: Any, path: str):
    """Mismo recorrido que procesar-docs.py: yields (nombre, valid, confidence)
    por cada hoja escalar de un árbol de campos ya resuelto (ver
    validators.engine.resolve_field_tree)."""
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


def field_quality(doc: dict) -> list[tuple[str, Optional[bool], Optional[float]]]:
    if doc.get("fieldTreeShape", False):
        out = []
        for name, node in (doc.get("extractedEntities") or {}).items():
            out.extend(_walk_field_tree(node, name))
        return out
    out = []
    for name, slot in (doc.get("fields") or {}).items():
        status = slot.get("status")
        if not status:
            continue
        out.append((name, status == "valid", slot.get("confidence")))
    return out


def resolve_model(data: dict) -> str:
    fmt = data.get("sourceFormat")
    if fmt in ("xml", "xlsx", "csv"):
        return "(sin IA — extracción determinística)"
    model = ((data.get("tracking") or {}).get("schema") or {}).get("model")
    return model or "(modelo desconocido)"


def resolve_tipo(data: dict) -> str:
    types = sorted({
        d.get("documentType") for d in data.get("documents", [])
        if d.get("documentType") and d.get("documentType") != "OTRO"
    })
    if len(types) == 1:
        return types[0]
    if len(types) > 1:
        return "EXPEDIENTE (" + ", ".join(types) + ")"
    return "SIN_TIPO"


def build_row(json_path: Path, bedrock_cost, textract_cost) -> Optional[dict]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    status = data.get("status") or "DESCONOCIDO"
    pages = data.get("pages") or {}
    total_pages = pages.get("totalPages") or 0
    model = resolve_model(data)
    tipo = resolve_tipo(data)
    nombre = Path(data.get("sourceFile") or json_path.stem).stem

    # Costo real: mismo cálculo que procesar-docs.py (pipeline/pricing.py).
    # El modelo se pasa explícito (no el de usage.*.modelId, que a veces sale
    # None) para que "approximatePricing" refleje de verdad si ese modelo
    # tiene tarifa registrada o no.
    usage = data.get("usage") or {}
    seg, ext = usage.get("segmentation") or {}, usage.get("extraction") or {}
    seg_cost = bedrock_cost(seg.get("usage"), model)
    ext_cost = bedrock_cost(ext.get("usage"), model)
    ocr_cost = textract_cost(pages.get("ocrPages"))
    cost = round(seg_cost["costUsd"] + ext_cost["costUsd"] + ocr_cost["costUsd"], 6)
    approx = bool(seg_cost.get("approximatePricing") or ext_cost.get("approximatePricing"))

    duration_s = data.get("processingSeconds")
    if duration_s is None:
        ms = data.get("processingMs")
        duration_s = (ms / 1000.0) if ms is not None else None

    field_ok = field_total = 0
    conf_sum, conf_n = 0.0, 0
    field_stats: dict[str, dict] = {}
    for doc in data.get("documents", []):
        for name, valid, conf in field_quality(doc):
            field_total += 1
            stat = field_stats.setdefault(name, {"ok": 0, "total": 0})
            stat["total"] += 1
            if valid:
                field_ok += 1
                stat["ok"] += 1
            if isinstance(conf, (int, float)):
                conf_sum += conf
                conf_n += 1

    return {
        "path": json_path,
        "model": model,
        "tipo": tipo,
        "nombre": nombre,
        "run": json_path.parent.name,
        "status": status,
        "pages": total_pages,
        "cost": cost,
        "cost_approx": approx,
        "duration_s": duration_s,
        "field_ok": field_ok,
        "field_total": field_total,
        "conf_avg": (conf_sum / conf_n) if conf_n else None,
        "field_stats": field_stats,
        "warnings": data.get("warnings") or [],
    }


def _new_bucket() -> dict:
    return {
        "rows": [], "status_counts": Counter(), "cost": 0.0, "cost_approx": False,
        "pages": 0, "duration_sum": 0.0, "duration_n": 0,
        "field_ok": 0, "field_total": 0, "conf_sum": 0.0, "conf_n": 0,
        "field_stats": {},
    }


def _add_to_bucket(bucket: dict, r: dict) -> None:
    bucket["rows"].append(r)
    bucket["status_counts"][r["status"]] += 1
    bucket["cost"] += r["cost"]
    bucket["cost_approx"] = bucket["cost_approx"] or r["cost_approx"]
    bucket["pages"] += r["pages"]
    if r["duration_s"] is not None:
        bucket["duration_sum"] += r["duration_s"]
        bucket["duration_n"] += 1
    bucket["field_ok"] += r["field_ok"]
    bucket["field_total"] += r["field_total"]
    if r["conf_avg"] is not None:
        bucket["conf_sum"] += r["conf_avg"]
        bucket["conf_n"] += 1
    for name, s in r["field_stats"].items():
        fs = bucket["field_stats"].setdefault(name, {"ok": 0, "total": 0})
        fs["ok"] += s["ok"]
        fs["total"] += s["total"]


def bucket_metrics(b: dict) -> dict:
    n = len(b["rows"])
    approved = b["status_counts"].get("APPROVED", 0)
    return {
        "n": n,
        "approval_rate": (approved / n) if n else None,
        "total_cost": b["cost"],
        "avg_cost": (b["cost"] / n) if n else None,
        "cost_per_page": (b["cost"] / b["pages"]) if b["pages"] else None,
        "cost_approx": b["cost_approx"],
        "avg_duration": (b["duration_sum"] / b["duration_n"]) if b["duration_n"] else None,
        "field_rate": (b["field_ok"] / b["field_total"]) if b["field_total"] else None,
        "avg_conf": (b["conf_sum"] / b["conf_n"]) if b["conf_n"] else None,
        "status_counts": b["status_counts"],
    }


def aggregate(rows: list[dict]) -> dict:
    overall = _new_bucket()
    by_model: dict[str, dict] = {}
    by_model_type: dict[str, dict[str, dict]] = {}
    for r in rows:
        _add_to_bucket(overall, r)
        by_model.setdefault(r["model"], _new_bucket())
        _add_to_bucket(by_model[r["model"]], r)
        by_model_type.setdefault(r["model"], {}).setdefault(r["tipo"], _new_bucket())
        _add_to_bucket(by_model_type[r["model"]][r["tipo"]], r)
    return {"overall": overall, "by_model": by_model, "by_model_type": by_model_type}


def worst_rows(rows: list[dict], limit: int = 15) -> list[dict]:
    # Solo documentos con algo real que revisar — sin este filtro, un dataset
    # chico (menos de `limit` documentos) rellenaba la tabla con documentos
    # 100% aprobados nada más para llegar al tope.
    def is_problematic(r: dict) -> bool:
        if r["status"] != "APPROVED":
            return True
        return bool(r["field_total"]) and r["field_ok"] < r["field_total"]

    def key(r: dict):
        sev = STATUS_SEVERITY.get(r["status"], 3)
        rate = (r["field_ok"] / r["field_total"]) if r["field_total"] else 1.0
        return (-sev, rate)

    return sorted((r for r in rows if is_problematic(r)), key=key)[:limit]


def worst_fields(rows: list[dict], limit: int = 15) -> list[tuple[str, dict]]:
    agg: dict[str, dict] = {}
    for r in rows:
        for name, s in r["field_stats"].items():
            fs = agg.setdefault(name, {"ok": 0, "total": 0})
            fs["ok"] += s["ok"]
            fs["total"] += s["total"]
    ranked = sorted(agg.items(), key=lambda kv: (kv[1]["ok"] / kv[1]["total"]) if kv[1]["total"] else 1.0)
    return [(name, s) for name, s in ranked if s["total"] and s["ok"] < s["total"]][:limit]


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def pct(x: Optional[float]) -> str:
    return f"{x:.0%}" if x is not None else "—"


def money(x: Optional[float]) -> str:
    return f"${x:,.4f}" if x is not None else "—"


def secs(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x*1000:.0f}ms" if x < 1 else f"{x:.1f}s"


def status_stack_bar(counts: Counter, n: int) -> str:
    if not n:
        return ""
    segs = []
    for status, count in counts.most_common():
        if count <= 0:
            continue
        color = STATUS_ROLE_COLOR[STATUS_ROLE.get(status, "critical")]
        label = STATUS_LABEL_ES.get(status, status)
        width = count / n * 100
        segs.append(
            f'<div class="stack-seg" style="width:{width:.3f}%;background:{color}" '
            f'title="{esc(label)}: {count} de {n}"></div>'
        )
    return f'<div class="stack-bar">{"".join(segs)}</div>'


def h_bar(value: float, max_value: float, color: str, label: str, value_text: str) -> str:
    pct_width = 0 if max_value <= 0 else max(2.0, value / max_value * 100)
    return f'''<div class="bar-row">
      <div class="bar-label">{esc(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:{pct_width:.2f}%;background:{color}"></div></div>
      <div class="bar-value">{esc(value_text)}</div>
    </div>'''


def status_legend() -> str:
    items = []
    seen = set()
    for status, role in STATUS_ROLE.items():
        if role in seen:
            continue
        seen.add(role)
        color = STATUS_ROLE_COLOR[role]
        items.append(
            f'<span class="legend-item"><span class="dot" style="background:{color}"></span>'
            f'{esc(STATUS_LABEL_ES.get(status, status))}</span>'
        )
    return f'<div class="legend">{"".join(items)}</div>'


def kpi_tile(value: str, label: str, sub: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{esc(sub)}</div>' if sub else ""
    return f'''<div class="kpi-tile">
      <div class="kpi-value">{value}</div>
      <div class="kpi-label">{esc(label)}</div>
      {sub_html}
    </div>'''


def model_comparison_section(by_model: dict, model_color: dict) -> str:
    ranked = sorted(
        by_model.items(),
        key=lambda kv: (bucket_metrics(kv[1])["approval_rate"] or 0),
        reverse=True,
    )
    bars = "".join(
        h_bar(
            (bucket_metrics(b)["approval_rate"] or 0) * 100, 100,
            model_color[model], f"{model}  ({bucket_metrics(b)['n']} docs)",
            pct(bucket_metrics(b)["approval_rate"]),
        )
        for model, b in ranked
    )

    rows_html = []
    for model, b in ranked:
        m = bucket_metrics(b)
        approx_mark = " *" if m["cost_approx"] else ""
        rows_html.append(f'''<tr>
          <td><span class="dot" style="background:{model_color[model]}"></span>{esc(model)}</td>
          <td class="num">{m['n']}</td>
          <td class="num">{pct(m['approval_rate'])}</td>
          <td class="num">{money(m['total_cost'])}{approx_mark}</td>
          <td class="num">{money(m['avg_cost'])}</td>
          <td class="num">{money(m['cost_per_page'])}</td>
          <td class="num">{secs(m['avg_duration'])}</td>
          <td class="num">{pct(m['field_rate'])}</td>
          <td class="num">{pct(m['avg_conf'])}</td>
        </tr>''')

    approx_note = (
        '<p class="note">* costo aproximado — este modelo no tiene tarifa Bedrock '
        'verificada en <code>pipeline/pricing.py</code>, se usó la tarifa de referencia.</p>'
        if any(bucket_metrics(b)["cost_approx"] for _, b in ranked) else ""
    )

    return f'''
    <section class="card">
      <h2>Comparación entre modelos</h2>
      <p class="section-sub">Tasa de aprobación — de mayor a menor. Un documento "aprobado" pasó todas las reglas del catálogo sin necesitar revisión humana.</p>
      <div class="bars">{bars}</div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Modelo</th><th class="num">Docs</th><th class="num">Aprobación</th>
            <th class="num">Costo total</th><th class="num">$/doc</th><th class="num">$/página</th>
            <th class="num">Tiempo prom.</th><th class="num">Campos válidos</th><th class="num">Confianza</th>
          </tr></thead>
          <tbody>{"".join(rows_html)}</tbody>
        </table>
      </div>
      {approx_note}
    </section>'''


def model_detail_section(by_model: dict, by_model_type: dict, model_color: dict) -> str:
    ranked_models = sorted(
        by_model.items(),
        key=lambda kv: (bucket_metrics(kv[1])["approval_rate"] or 0),
        reverse=True,
    )
    blocks = []
    for model, mbucket in ranked_models:
        mm = bucket_metrics(mbucket)
        types = by_model_type.get(model, {})
        ranked_types = sorted(
            types.items(), key=lambda kv: bucket_metrics(kv[1])["n"], reverse=True
        )
        cards = []
        for tipo, tbucket in ranked_types:
            tm = bucket_metrics(tbucket)
            cards.append(f'''<div class="type-card">
              <div class="type-card-head">
                <span class="type-name">{esc(tipo)}</span>
                <span class="type-n">{tm['n']} doc{'s' if tm['n'] != 1 else ''}</span>
              </div>
              {status_stack_bar(tm['status_counts'], tm['n'])}
              <div class="type-metrics">
                <div><span class="mlabel">Costo</span><span class="mvalue">{money(tm['total_cost'])}</span></div>
                <div><span class="mlabel">Tiempo prom.</span><span class="mvalue">{secs(tm['avg_duration'])}</span></div>
                <div><span class="mlabel">Campos válidos</span><span class="mvalue">{pct(tm['field_rate'])}</span></div>
                <div><span class="mlabel">Confianza</span><span class="mvalue">{pct(tm['avg_conf'])}</span></div>
              </div>
            </div>''')
        blocks.append(f'''
        <div class="model-block">
          <h3><span class="dot" style="background:{model_color[model]}"></span>{esc(model)}
            <span class="model-head-meta">{mm['n']} documento{'s' if mm['n'] != 1 else ''} · {pct(mm['approval_rate'])} aprobación · {money(mm['total_cost'])}</span>
          </h3>
          <div class="type-grid">{"".join(cards)}</div>
        </div>''')

    return f'''
    <section class="card">
      <h2>Detalle por modelo y tipo de documento</h2>
      {"".join(blocks)}
    </section>'''


def worst_docs_section(rows: list[dict]) -> str:
    worst = worst_rows(rows)
    if not worst:
        return '''
    <section class="card">
      <h2>Documentos con más problemas</h2>
      <p class="section-sub">Ningún documento tiene campos inválidos ni quedó fuera de "Aprobado" — sin pendientes que revisar.</p>
    </section>'''
    rows_html = []
    for r in worst:
        role = STATUS_ROLE.get(r["status"], "critical")
        color = STATUS_ROLE_COLOR[role]
        field_pct = pct(r["field_ok"] / r["field_total"]) if r["field_total"] else "—"
        warn = "; ".join(r["warnings"][:2]) if r["warnings"] else "—"
        rows_html.append(f'''<tr>
          <td>{esc(r['nombre'])}</td>
          <td>{esc(r['model'])}</td>
          <td>{esc(r['tipo'])}</td>
          <td class="num">{esc(r['run'])}</td>
          <td><span class="status-pill" style="background:{color}">{esc(STATUS_LABEL_ES.get(r['status'], r['status']))}</span></td>
          <td class="num">{field_pct}</td>
          <td class="muted">{esc(warn)}</td>
        </tr>''')
    return f'''
    <section class="card">
      <h2>Documentos con más problemas</h2>
      <p class="section-sub">Los peores {len(worst)} resultados de toda la corrida, para revisar primero.</p>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Documento</th><th>Modelo</th><th>Tipo</th><th class="num">Corrida</th>
            <th>Estado</th><th class="num">Campos válidos</th><th>Advertencias</th>
          </tr></thead>
          <tbody>{"".join(rows_html)}</tbody>
        </table>
      </div>
    </section>'''


def worst_fields_section(rows: list[dict]) -> str:
    worst = worst_fields(rows)
    if not worst:
        return ""
    rows_html = []
    for name, s in worst:
        rate = s["ok"] / s["total"]
        rows_html.append(f'''<tr>
          <td>{esc(name)}</td>
          <td class="num">{s['ok']}/{s['total']}</td>
          <td class="num">{rate:.0%}</td>
        </tr>''')
    return f'''
    <section class="card">
      <h2>Campos que más fallan</h2>
      <p class="section-sub">Agregado de todos los documentos y modelos — útil para saber qué campo del catálogo revisar primero.</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Campo</th><th class="num">Válidos</th><th class="num">Tasa</th></tr></thead>
          <tbody>{"".join(rows_html)}</tbody>
        </table>
      </div>
    </section>'''


CSS = '''
:root {
  color-scheme: light;
  --surface: #fcfcfb;
  --plane: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --gridline: #e1e0d9;
  --border: rgba(11,11,11,0.10);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --gridline: #2c2c2a;
    --border: rgba(255,255,255,0.10);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 64px;
  background: var(--plane); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 15px; line-height: 1.5;
}
.wrap { max-width: 1120px; margin: 0 auto; }
header.page-head { margin-bottom: 28px; }
header.page-head h1 { font-size: 26px; margin: 0 0 6px; }
header.page-head .meta { color: var(--text-secondary); font-size: 14px; }
.kpi-row {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px; margin-bottom: 24px;
}
.kpi-tile {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 18px 20px;
}
.kpi-value { font-size: 30px; font-weight: 650; }
.kpi-label { color: var(--text-secondary); font-size: 13px; margin-top: 2px; }
.kpi-sub { color: var(--text-muted); font-size: 12px; margin-top: 6px; }
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
  padding: 24px 26px; margin-bottom: 20px;
}
.card h2 { font-size: 19px; margin: 0 0 4px; }
.section-sub { color: var(--text-secondary); font-size: 13px; margin: 0 0 16px; }
.bars { display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px; }
.bar-row { display: grid; grid-template-columns: 220px 1fr 70px; align-items: center; gap: 10px; }
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
.note { color: var(--text-muted); font-size: 12px; margin: 12px 0 0; }
.note code { background: var(--gridline); padding: 1px 5px; border-radius: 4px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 7px; vertical-align: middle; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 22px; }
.legend-item { font-size: 12.5px; color: var(--text-secondary); }
.stack-bar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; gap: 2px; margin: 8px 0 12px; background: var(--gridline); }
.stack-seg { height: 100%; }
.status-pill { color: #fff; font-size: 11.5px; padding: 2px 9px; border-radius: 999px; white-space: nowrap; }
.model-block { margin-bottom: 26px; }
.model-block:last-child { margin-bottom: 0; }
.model-block h3 { font-size: 16px; margin: 0 0 12px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.model-head-meta { color: var(--text-secondary); font-size: 12.5px; font-weight: 400; }
.type-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.type-card { border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.type-card-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }
.type-name { font-weight: 600; font-size: 13.5px; }
.type-n { color: var(--text-muted); font-size: 12px; }
.type-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; margin-top: 10px; }
.type-metrics .mlabel { display: block; color: var(--text-muted); font-size: 11px; }
.type-metrics .mvalue { display: block; font-size: 13.5px; font-variant-numeric: tabular-nums; }
footer { color: var(--text-muted); font-size: 12px; text-align: center; margin-top: 32px; }
'''


def render_report(rows: list[dict], scanned_dir: Path) -> str:
    agg = aggregate(rows)
    overall = bucket_metrics(agg["overall"])
    all_models = sorted({r["model"] for r in rows})
    model_color = {m: CATEGORICAL[i % len(CATEGORICAL)] for i, m in enumerate(all_models)}
    all_types = sorted({r["tipo"] for r in rows})

    kpis = "".join([
        kpi_tile(str(overall["n"]), "Documentos analizados"),
        kpi_tile(pct(overall["approval_rate"]), "Tasa de aprobación global"),
        kpi_tile(money(overall["total_cost"]), "Costo total real",
                  "aproximado para modelos sin tarifa registrada" if overall["cost_approx"] else ""),
        kpi_tile(pct(overall["avg_conf"]), "Confianza promedio del modelo"),
        kpi_tile(str(len(all_models)), "Modelos comparados"),
        kpi_tile(str(len(all_types)), "Tipos de documento"),
    ])

    body = f'''<div class="wrap">
      <header class="page-head">
        <h1>Reporte de calidad — extracción de documentos</h1>
        <div class="meta">Generado {datetime.now():%Y-%m-%d %H:%M} · fuente: <code>{esc(scanned_dir)}</code></div>
      </header>
      <div class="kpi-row">{kpis}</div>
      {status_legend()}
      {model_comparison_section(agg["by_model"], model_color) if len(all_models) > 1 else ""}
      {model_detail_section(agg["by_model"], agg["by_model_type"], model_color)}
      {worst_docs_section(rows)}
      {worst_fields_section(rows)}
      <footer>Generado por <code>pnpm analizar</code> (scripts/analizar-resultados.py) — costo y calidad calculados de los resultados reales, no estimados.</footer>
    </div>'''

    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reporte de calidad — extracción de documentos</title>
<style>{CSS}</style>
</head>
<body>{body}</body>
</html>'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="carpeta raíz con los resultados de pnpm procesar (default: resultados/)")
    parser.add_argument("--out", help="dónde dejar el HTML (default: <dir>/_analisis.html)")
    parser.add_argument("--open", action="store_true", help="abrir el reporte en el navegador al terminar")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    from pipeline.pricing import bedrock_cost, textract_cost

    root = Path(args.dir)
    if not root.exists():
        print(f"No existe la carpeta {root}")
        return 2

    json_paths = sorted(p for p in root.rglob("*.json") if p.parent.name.isdigit())
    if not json_paths:
        print(
            f"No encontré resultados con la estructura de pnpm procesar bajo {root}/ "
            "(busco <TIPO>/<nombre-doc>/NNN/*.json — corre `pnpm procesar` primero)."
        )
        return 2

    print(f"Analizando {len(json_paths)} resultado(s) bajo {root}/ ...")
    rows = []
    for p in json_paths:
        r = build_row(p, bedrock_cost, textract_cost)
        if r is not None:
            rows.append(r)
    if not rows:
        print("Ninguno de los archivos encontrados se pudo leer como JSON válido.")
        return 2

    html_out = render_report(rows, root)
    out_path = Path(args.out) if args.out else root / "_analisis.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")

    print(f"{len(rows)} documento(s), {len({r['model'] for r in rows})} modelo(s), {len({r['tipo'] for r in rows})} tipo(s)")
    print(f"Reporte → {out_path}")

    if args.open:
        webbrowser.open(out_path.resolve().as_uri())

    return 0


if __name__ == "__main__":
    sys.exit(main())
