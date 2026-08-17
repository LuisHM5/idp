#!/usr/bin/env python3
"""Genera un reporte local de SALUD OPERATIVA del pipeline: cuántos documentos
se han procesado, en qué estado quedaron (aprobado/rechazado/requiere
revisión, y también ejecuciones fallidas/atoradas), qué tan segura está la IA
de sus extracciones, qué campos fallan más y cómo va el volumen día a día —
para responder "¿mi proyecto va fluyendo bien?" con datos reales, no con una
sensación.

Lee la tabla de tracking de documentos del blueprint (`BedrockDocumentProcessing/
DocumentProcessingTable` — la misma que usa `GET /documents`), resuelta
automáticamente desde el stack desplegado. Por cada documento, el campo
`EnrichmentResult` (el envelope canónico que ya calculó la Lambda) trae el
status de negocio, la confianza por documento dentro del expediente y —para
tipos de DSL legacy, y para el documento "principal" de un expediente field-
centric— el detalle de validez por campo (`fieldsValidity`). Esa cobertura de
campo por campo es PARCIAL para expedientes multi-documento (el detalle
completo de cada documento interno solo vive en `results/{id}.json`, no en
esta tabla) — el reporte lo señala donde aplica en vez de fingir precisión que
no tiene.

Como todos los reportes de este repo: por default lee **dev**. Usa
`--env prod` para el ambiente productivo.

Salida en `reportes-documentos/<env>/<timestamp>/`:
    reporte.md          resumen en texto/tablas
    reporte.html          dashboard con KPIs, confianza, volumen y comparativas
    datos_raw.xlsx        una fila por documento + hojas agregadas, para Excel

Uso:
    pnpm reporte:documentos                          # dev, todo el historial
    pnpm reporte:documentos -- --env prod
    pnpm reporte:documentos -- --desde 2026-08-01 --hasta 2026-08-17
    pnpm reporte:documentos -- --limit 30 --open
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _report_lib import (  # noqa: E402
    BASE_CSS,
    CATEGORICAL,
    CYAN,
    DIM,
    RED,
    RESET,
    VALIDATION_STATUS_ROLE,
    WORKFLOW_ROLE,
    YELLOW,
    esc,
    h_bar,
    kpi_tile,
    normalize_doc_item as normalize_item,
    num,
    pct,
    resolve_processing_table as resolve_table,
    scan_all,
    secs,
    stack_bar,
    stack_id,
)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT_ROOT = REPO / "reportes-documentos"


def filter_by_date(rows: List[Dict[str, Any]], desde: Optional[str], hasta: Optional[str]) -> List[Dict[str, Any]]:
    if not desde and not hasta:
        return rows
    out = []
    for r in rows:
        day = r["processed_at"][:10]
        if desde and day < desde:
            continue
        if hasta and day > hasta:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Agregación
# ---------------------------------------------------------------------------

def aggregate_overall(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    workflow_counts = Counter(r["workflow_status"] for r in rows)
    completed = [r for r in rows if r["workflow_status"] == "complete"]
    validation_counts = Counter(r["validation_status"] for r in completed if r["validation_status"])
    confs = [r["avg_confidence"] for r in rows if r["avg_confidence"] is not None]
    durations = [r["processing_seconds"] for r in rows if r["processing_seconds"] is not None]
    dates = sorted(r["processed_at"] for r in rows if r["processed_at"])
    approved = validation_counts.get("APROBADO", 0)
    return {
        "n": n,
        "workflow_counts": workflow_counts,
        "n_completed": len(completed),
        "validation_counts": validation_counts,
        "approval_rate": (approved / len(completed)) if completed else None,
        "avg_confidence": (sum(confs) / len(confs)) if confs else None,
        "avg_duration_s": (sum(durations) / len(durations)) if durations else None,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
    }


def _new_bucket() -> Dict[str, Any]:
    return {"rows": [], "validation_counts": Counter(), "conf_sum": 0.0, "conf_n": 0, "dur_sum": 0.0, "dur_n": 0}


def _add(bucket: Dict[str, Any], r: Dict[str, Any]) -> None:
    bucket["rows"].append(r)
    if r["workflow_status"] == "complete" and r["validation_status"]:
        bucket["validation_counts"][r["validation_status"]] += 1
    if r["avg_confidence"] is not None:
        bucket["conf_sum"] += r["avg_confidence"]
        bucket["conf_n"] += 1
    if r["processing_seconds"] is not None:
        bucket["dur_sum"] += r["processing_seconds"]
        bucket["dur_n"] += 1


def _bucket_metrics(b: Dict[str, Any]) -> Dict[str, Any]:
    n = len(b["rows"])
    completed_n = sum(b["validation_counts"].values())
    approved = b["validation_counts"].get("APROBADO", 0)
    return {
        "n": n,
        "approval_rate": (approved / completed_n) if completed_n else None,
        "avg_confidence": (b["conf_sum"] / b["conf_n"]) if b["conf_n"] else None,
        "avg_duration_s": (b["dur_sum"] / b["dur_n"]) if b["dur_n"] else None,
        "validation_counts": b["validation_counts"],
    }


def aggregate_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = defaultdict(_new_bucket)
    for r in rows:
        _add(buckets[r[key]], r)
    return dict(buckets)


def daily_volume(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_day: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "validation_counts": Counter()})
    for r in rows:
        day = r["processed_at"][:10]
        if not day:
            continue
        agg = by_day[day]
        agg["n"] += 1
        if r["workflow_status"] == "complete" and r["validation_status"]:
            agg["validation_counts"][r["validation_status"]] += 1
    return [{"day": d, **agg} for d, agg in sorted(by_day.items())]


def worst_documents(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    def is_problematic(r: Dict[str, Any]) -> bool:
        return r["workflow_status"] != "complete" or r["validation_status"] != "APROBADO"

    severity = {"RECHAZADO": 3, "REQUIERE_REVISION": 2, "": 3, "APROBADO": 0}

    def key(r: Dict[str, Any]):
        wf_penalty = 0 if r["workflow_status"] == "complete" else 4
        sev = severity.get(r["validation_status"], 3)
        conf = r["avg_confidence"] if r["avg_confidence"] is not None else 0
        return (-(wf_penalty + sev), conf)

    return sorted((r for r in rows if is_problematic(r)), key=key)[:limit]


def worst_fields(rows: List[Dict[str, Any]], limit: int) -> List[tuple]:
    agg: Dict[str, Dict[str, int]] = {}
    for r in rows:
        for name, s in r["field_stats"].items():
            fs = agg.setdefault(name, {"ok": 0, "total": 0})
            fs["ok"] += s["ok"]
            fs["total"] += s["total"]
    ranked = sorted(agg.items(), key=lambda kv: (kv[1]["ok"] / kv[1]["total"]) if kv[1]["total"] else 1.0)
    return [(name, s) for name, s in ranked if s["total"] and s["ok"] < s["total"]][:limit]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(
    rows: List[Dict[str, Any]], overall: Dict[str, Any], by_type: Dict[str, Dict[str, Any]],
    by_model: Dict[str, Dict[str, Any]], volume: List[Dict[str, Any]], worst_docs: List[Dict[str, Any]],
    worst_fld: List[tuple], env: str,
) -> str:
    lines = [
        f"# Reporte de salud operativa — {env}",
        "",
        f"Generado {datetime.now():%Y-%m-%d %H:%M} · {overall['n']} documento(s)"
        + (f" · {overall['first_date'][:10]} a {overall['last_date'][:10]}" if overall["first_date"] else ""),
        "",
        "## Salud operativa",
        "",
        "| Estado de ejecución | Documentos |",
        "| --- | --- |",
    ]
    for status, count in overall["workflow_counts"].most_common():
        lines.append(f"| {status} | {count} |")
    lines += [
        "",
        "## Resultado de negocio (solo ejecuciones completas)",
        "",
        "| Métrica | Valor |",
        "| --- | --- |",
        f"| Documentos completados | {overall['n_completed']} |",
        f"| Tasa de aprobación | {pct(overall['approval_rate'])} |",
        f"| Confianza promedio del modelo | {pct(overall['avg_confidence'])} |",
        f"| Tiempo de procesamiento promedio | {secs(overall['avg_duration_s'])} |",
        "",
    ]
    for status, count in overall["validation_counts"].most_common():
        lines.append(f"- **{status}**: {count} ({pct(count / overall['n_completed']) if overall['n_completed'] else '—'})")
    lines.append("")

    lines += ["## Por tipo de documento", "", "| Tipo | Docs | Aprobación | Confianza | Tiempo prom. |", "| --- | --- | --- | --- | --- |"]
    for dtype, b in sorted(by_type.items(), key=lambda kv: _bucket_metrics(kv[1])["n"], reverse=True):
        m = _bucket_metrics(b)
        lines.append(f"| {dtype} | {m['n']} | {pct(m['approval_rate'])} | {pct(m['avg_confidence'])} | {secs(m['avg_duration_s'])} |")
    lines.append("")

    lines += ["## Por modelo", "", "| Modelo | Docs | Aprobación | Confianza | Tiempo prom. |", "| --- | --- | --- | --- | --- |"]
    for model, b in sorted(by_model.items(), key=lambda kv: _bucket_metrics(kv[1])["n"], reverse=True):
        m = _bucket_metrics(b)
        lines.append(f"| {model} | {m['n']} | {pct(m['approval_rate'])} | {pct(m['avg_confidence'])} | {secs(m['avg_duration_s'])} |")
    lines.append("")

    lines += ["## Volumen diario", "", "| Día | Docs | Aprobado | Requiere revisión | Rechazado |", "| --- | --- | --- | --- | --- |"]
    for v in volume:
        vc = v["validation_counts"]
        lines.append(f"| {v['day']} | {v['n']} | {vc.get('APROBADO', 0)} | {vc.get('REQUIERE_REVISION', 0)} | {vc.get('RECHAZADO', 0)} |")
    lines.append("")

    if worst_fld:
        lines += ["## Campos que más fallan (cobertura parcial — ver nota abajo)", "", "| Campo | Válidos | Tasa |", "| --- | --- | --- |"]
        for name, s in worst_fld:
            lines.append(f"| {name} | {s['ok']}/{s['total']} | {s['ok']/s['total']:.0%} |")
        lines.append("")

    lines += [f"## Documentos que requieren atención (top {len(worst_docs)})", "", "| Documento | Tipo | Estado ejecución | Estado negocio | Confianza | Procesado |", "| --- | --- | --- | --- | --- | --- |"]
    for r in worst_docs:
        lines.append(
            f"| `{r['document_id']}` | {r['document_type_name'] or r['document_type']} | {r['workflow_status']} | "
            f"{r['validation_status'] or '—'} | {pct(r['avg_confidence'])} | {r['processed_at'][:19]} |"
        )
    lines.append("")
    lines.append(
        "_Nota: el detalle de campos por campo (`fieldsValidity`) viene de la tabla de tracking, que solo trae ese "
        "detalle completo para el documento principal de cada expediente y para tipos de catálogo legacy — un "
        "expediente multi-documento (varios PDFs en un mismo archivo) puede subcontar campos de sus documentos "
        "internos. Para el detalle exacto de un documento puntual usa `pnpm resultado -- --id <id>`._"
    )
    lines.append("")
    lines.append("_Generado por `pnpm reporte:documentos` (scripts/reporte-documentos.py) — datos crudos: `datos_raw.xlsx`._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def volume_chart(volume: List[Dict[str, Any]]) -> str:
    if not volume:
        return ""
    max_n = max(v["n"] for v in volume)
    bars = "".join(
        f'<div class="vol-bar" style="height:{max(4, v["n"] / max_n * 100):.1f}%" title="{esc(v["day"])}: {v["n"]} documento(s)"></div>'
        for v in volume
    )
    first_day, last_day = volume[0]["day"], volume[-1]["day"]
    return (
        f'<div class="vol-chart">{bars}</div>'
        f'<div class="vol-labels"><span>{esc(first_day)}</span><span>{esc(last_day)}</span></div>'
    )


def render_html(
    rows: List[Dict[str, Any]], overall: Dict[str, Any], by_type: Dict[str, Dict[str, Any]],
    by_model: Dict[str, Dict[str, Any]], volume: List[Dict[str, Any]], worst_docs: List[Dict[str, Any]],
    worst_fld: List[tuple], env: str,
) -> str:
    wf_stack = stack_bar(overall["workflow_counts"], overall["n"], WORKFLOW_ROLE)
    validation_stack = stack_bar(overall["validation_counts"], overall["n_completed"], VALIDATION_STATUS_ROLE)

    type_rows = "".join(
        f'''<tr>
          <td>{esc(dtype)}</td><td class="num">{m['n']}</td><td class="num">{pct(m['approval_rate'])}</td>
          <td class="num">{pct(m['avg_confidence'])}</td><td class="num">{secs(m['avg_duration_s'])}</td>
        </tr>'''
        for dtype, b in sorted(by_type.items(), key=lambda kv: _bucket_metrics(kv[1])["n"], reverse=True)
        for m in [_bucket_metrics(b)]
    )

    models_ranked = [(m, _bucket_metrics(b)) for m, b in sorted(by_model.items(), key=lambda kv: _bucket_metrics(kv[1])["n"], reverse=True)]
    model_color = {m: CATEGORICAL[i % len(CATEGORICAL)] for i, (m, _) in enumerate(models_ranked)}
    max_model_n = max((m["n"] for _, m in models_ranked), default=0)
    model_bars = "".join(h_bar(m["n"], max_model_n, model_color[name], f"{name} ({m['n']} docs)", pct(m["approval_rate"])) for name, m in models_ranked)
    model_rows = "".join(
        f'''<tr>
          <td><span class="dot" style="background:{model_color[name]}"></span>{esc(name)}</td>
          <td class="num">{m['n']}</td><td class="num">{pct(m['approval_rate'])}</td>
          <td class="num">{pct(m['avg_confidence'])}</td><td class="num">{secs(m['avg_duration_s'])}</td>
        </tr>'''
        for name, m in models_ranked
    )

    worst_doc_rows = "".join(
        f'''<tr>
          <td class="mono">{esc(r['document_id'])}</td>
          <td>{esc(r['document_type_name'] or r['document_type'])}</td>
          <td>{esc(r['workflow_status'])}</td>
          <td>{esc(r['validation_status'] or '—')}</td>
          <td class="num">{pct(r['avg_confidence'])}</td>
          <td class="muted">{esc(r['processed_at'][:19])}</td>
        </tr>'''
        for r in worst_docs
    )

    worst_field_rows = "".join(
        f'<tr><td>{esc(name)}</td><td class="num">{s["ok"]}/{s["total"]}</td><td class="num">{s["ok"]/s["total"]:.0%}</td></tr>'
        for name, s in worst_fld
    )
    worst_fields_section = (
        f'''
      <section class="card">
        <h2>Campos que más fallan</h2>
        <p class="section-sub">Cobertura parcial en expedientes multi-documento — ver nota al final.</p>
        <div class="table-wrap"><table>
          <thead><tr><th>Campo</th><th class="num">Válidos</th><th class="num">Tasa</th></tr></thead>
          <tbody>{worst_field_rows}</tbody>
        </table></div>
      </section>''' if worst_fld else ""
    )

    kpis = "".join([
        kpi_tile(str(overall["n"]), "Documentos totales"),
        kpi_tile(str(overall["n_completed"]), "Ejecuciones completas"),
        kpi_tile(pct(overall["approval_rate"]), "Tasa de aprobación"),
        kpi_tile(pct(overall["avg_confidence"]), "Confianza promedio"),
        kpi_tile(secs(overall["avg_duration_s"]), "Tiempo de proceso promedio"),
        kpi_tile(str(len(by_type)), "Tipos de documento"),
        kpi_tile(str(len(by_model)), "Modelos en uso"),
    ])

    date_range = f"{overall['first_date'][:10]} a {overall['last_date'][:10]}" if overall["first_date"] else "sin datos"

    body = f'''<div class="wrap">
      <header class="page-head">
        <h1>Salud operativa del pipeline — {esc(env)}</h1>
        <div class="meta">Generado {datetime.now():%Y-%m-%d %H:%M} · rango: {esc(date_range)}</div>
      </header>
      <div class="kpi-row">{kpis}</div>

      <section class="card">
        <h2>Estado de las ejecuciones</h2>
        <p class="section-sub">Completa / en progreso / fallida — si algo se está atorando, aparece aquí.</p>
        {wf_stack}
      </section>

      <section class="card">
        <h2>Resultado de negocio</h2>
        <p class="section-sub">Aprobado / requiere revisión / rechazado, solo de ejecuciones completas.</p>
        {validation_stack}
      </section>

      <section class="card">
        <h2>Volumen diario</h2>
        {volume_chart(volume)}
      </section>

      <section class="card">
        <h2>Por tipo de documento</h2>
        <div class="table-wrap"><table>
          <thead><tr><th>Tipo</th><th class="num">Docs</th><th class="num">Aprobación</th><th class="num">Confianza</th><th class="num">Tiempo prom.</th></tr></thead>
          <tbody>{type_rows}</tbody>
        </table></div>
      </section>

      <section class="card">
        <h2>Por modelo</h2>
        <div class="bars">{model_bars}</div>
        <div class="table-wrap"><table>
          <thead><tr><th>Modelo</th><th class="num">Docs</th><th class="num">Aprobación</th><th class="num">Confianza</th><th class="num">Tiempo prom.</th></tr></thead>
          <tbody>{model_rows}</tbody>
        </table></div>
      </section>

      {worst_fields_section}

      <section class="card">
        <h2>Documentos que requieren atención (top {len(worst_docs)})</h2>
        <div class="table-wrap"><table>
          <thead><tr><th>Documento</th><th>Tipo</th><th>Estado ejecución</th><th>Estado negocio</th><th class="num">Confianza</th><th>Procesado</th></tr></thead>
          <tbody>{worst_doc_rows}</tbody>
        </table></div>
      </section>

      <footer>Generado por <code>pnpm reporte:documentos</code> (scripts/reporte-documentos.py) · datos crudos en <code>datos_raw.xlsx</code> · el detalle de campos es parcial en expedientes multi-documento, ver <code>reporte.md</code>.</footer>
    </div>'''

    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Salud operativa — {esc(env)}</title>
<style>{CSS}</style>
</head>
<body>{body}</body>
</html>'''


CSS = BASE_CSS


# ---------------------------------------------------------------------------
# Excel (datos crudos)
# ---------------------------------------------------------------------------

def write_excel(
    out_path: Path, rows: List[Dict[str, Any]], overall: Dict[str, Any], by_type: Dict[str, Dict[str, Any]],
    by_model: Dict[str, Dict[str, Any]], volume: List[Dict[str, Any]],
) -> None:
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

    ws_docs = wb.active
    ws_docs.title = "Documentos"
    write_sheet(
        ws_docs,
        ["DocumentId", "DocumentType", "DocumentTypeName", "DocumentName", "AppOrigin", "WorkflowStatus",
         "ValidationStatus", "Model", "ProcessedAt", "ProcessingSeconds", "AvgConfidence", "SubDocumentCount", "WarningCount"],
        [
            [r["document_id"], r["document_type"], r["document_type_name"], r["document_name"], r["app_origin"],
             r["workflow_status"], r["validation_status"], r["model"], r["processed_at"], r["processing_seconds"],
             r["avg_confidence"], r["sub_document_count"], len(r["warnings"])]
            for r in rows
        ],
    )

    ws_type = wb.create_sheet("PorTipo")
    write_sheet(
        ws_type,
        ["DocumentType", "Documentos", "TasaAprobacion", "ConfianzaPromedio", "TiempoPromedioSeg"],
        [
            [t, m["n"], m["approval_rate"], m["avg_confidence"], m["avg_duration_s"]]
            for t, b in by_type.items() for m in [_bucket_metrics(b)]
        ],
    )

    ws_model = wb.create_sheet("PorModelo")
    write_sheet(
        ws_model,
        ["Model", "Documentos", "TasaAprobacion", "ConfianzaPromedio", "TiempoPromedioSeg"],
        [
            [mdl, m["n"], m["approval_rate"], m["avg_confidence"], m["avg_duration_s"]]
            for mdl, b in by_model.items() for m in [_bucket_metrics(b)]
        ],
    )

    ws_vol = wb.create_sheet("VolumenDiario")
    write_sheet(
        ws_vol,
        ["Dia", "Documentos", "Aprobado", "RequiereRevision", "Rechazado"],
        [
            [v["day"], v["n"], v["validation_counts"].get("APROBADO", 0),
             v["validation_counts"].get("REQUIERE_REVISION", 0), v["validation_counts"].get("RECHAZADO", 0)]
            for v in volume
        ],
    )

    ws_tot = wb.create_sheet("Totales")
    write_sheet(
        ws_tot,
        ["Metrica", "Valor"],
        [
            ["Documentos totales", overall["n"]],
            ["Ejecuciones completas", overall["n_completed"]],
            ["Tasa de aprobacion", overall["approval_rate"]],
            ["Confianza promedio", overall["avg_confidence"]],
            ["Tiempo de proceso promedio (s)", overall["avg_duration_s"]],
            ["Primer documento", overall["first_date"]],
            ["Ultimo documento", overall["last_date"]],
        ] + [[f"Ejecuciones en estado '{k}'", v] for k, v in overall["workflow_counts"].items()]
        + [[f"Resultado de negocio '{k}'", v] for k, v in overall["validation_counts"].items()],
    )

    wb.save(out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=("dev", "prod"), default="dev", help="ambiente a consultar (default: dev)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack", help="nombre del stack (default: resuelto de --env)")
    parser.add_argument("--table", help="nombre de tabla, salta la resolución vía CloudFormation")
    parser.add_argument("--desde", help="filtra ProcessedAt >= YYYY-MM-DD")
    parser.add_argument("--hasta", help="filtra ProcessedAt <= YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=20, help="documentos en 'requieren atención' (default 20)")
    parser.add_argument("--out", help="carpeta de salida (default: reportes-documentos/<env>/<timestamp>/)")
    parser.add_argument("--open", action="store_true", help="abrir el HTML en el navegador al terminar")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    stack = args.stack or stack_id(args.env)
    try:
        table = args.table or resolve_table(stack, args.region)
    except Exception as e:
        print(f"{RED}{e}{RESET}")
        return 1

    print(f"Leyendo {table} en {args.region} (stack {stack})...")
    try:
        raw_items = scan_all(table, args.region)
    except Exception as e:
        print(f"{RED}No pude leer la tabla {table} en {args.region}: {e}{RESET}")
        return 1

    if not raw_items:
        print(f"{YELLOW}La tabla {table} está vacía — aún no se ha procesado ningún documento con este stack.{RESET}")
        return 0

    rows = [normalize_item(it) for it in raw_items]
    rows = filter_by_date(rows, args.desde, args.hasta)
    if not rows:
        print(f"{YELLOW}Ningún documento cae en el rango de fechas pedido.{RESET}")
        return 0

    overall = aggregate_overall(rows)
    by_type = aggregate_by(rows, "document_type")
    by_model = aggregate_by(rows, "model")
    volume = daily_volume(rows)
    worst_docs = worst_documents(rows, args.limit)
    worst_fld = worst_fields(rows, args.limit)

    out_dir = Path(args.out) if args.out else DEFAULT_OUT_ROOT / args.env / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "reporte.md").write_text(
        render_markdown(rows, overall, by_type, by_model, volume, worst_docs, worst_fld, args.env), encoding="utf-8"
    )
    (out_dir / "reporte.html").write_text(
        render_html(rows, overall, by_type, by_model, volume, worst_docs, worst_fld, args.env), encoding="utf-8"
    )
    write_excel(out_dir / "datos_raw.xlsx", rows, overall, by_type, by_model, volume)

    print(
        f"\n{CYAN}{overall['n']} documento(s) ({overall['n_completed']} completos) · "
        f"aprobación {pct(overall['approval_rate'])} · confianza {pct(overall['avg_confidence'])}{RESET}"
    )
    print(f"Reporte → {out_dir}/")
    print("  reporte.md        (resumen en texto)")
    print("  reporte.html       (dashboard — ábrelo en el navegador)")
    print("  datos_raw.xlsx     (datos crudos — Excel)")

    if args.open:
        webbrowser.open((out_dir / "reporte.html").resolve().as_uri())

    return 0


if __name__ == "__main__":
    sys.exit(main())
