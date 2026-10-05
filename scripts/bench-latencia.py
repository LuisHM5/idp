#!/usr/bin/env python3
"""Compara modelos Bedrock por LATENCIA (y de paso costo y campos) sobre TUS
documentos, corriendo el pipeline real en local.

Por qué existe: el 85-90% del tiempo de un documento es UNA llamada a Bedrock,
cuyo wall clock es `TTFT + tokens_salida / throughput`. Cambiar de modelo es la
palanca grande, pero solo se puede decidir con una tabla de segundos/costo/campos
sobre documentos propios — no con el Intelligence Index.

No reimplementa nada: invoca `scripts/procesar-docs.py` (mismo pipeline que la
Lambda, con `--model`) una vez por documento×modelo×repetición, y lee los JSON
que ese script ya deja escritos.

Uso:
    pnpm bench:latencia -- --historico
        Gratis: NO llama a Bedrock. Lee las corridas que ya existen en
        `resultados/` y arma la tabla por modelo con lo ya medido.

    pnpm bench:latencia -- --docs test-docs/fletes --tipo FACTURA_FLETE_EXTRANJERO
        Corrida nueva y controlada. Cada documento se procesa con cada modelo.

    pnpm bench:latencia -- --doc test-docs/curp.pdf --tipo CURP --repeticiones 3
        Varias repeticiones: la latencia de un LLM varía MUCHO entre llamadas
        idénticas (medido: 14.6s a 58.5s para el mismo archivo y modelo), así
        que una sola corrida por modelo no decide nada. Se reporta la mediana.

    pnpm bench:latencia -- --modelos minimax.minimax-m2.5,moonshotai.kimi-k2.5
    pnpm bench:latencia -- --dry-run          # solo imprime la matriz y el costo estimado
    pnpm bench:latencia -- --max-costo-usd 2  # aborta si la cota estimada se pasa

El primer modelo de `--modelos` es la BASE contra la que se comparan los demás
(por default, el que está desplegado hoy).

Salidas en `bench-latencia/<timestamp>/`:
    resumen.md   tabla por modelo y por documento, y el bloque de decisión
    filas.csv    una fila por documento×modelo×repetición, para graficar
    <modelo>/    los resultados crudos que dejó procesar-docs.py

Lo que mide y lo que NO: mide segundos reales por etapa (`tracking.timings`),
tokens facturados (`usage`), costo con las tarifas de `pipeline/pricing.py`, y
si los campos extraídos CAMBIAN respecto al modelo base. "Campos distintos" no
es accuracy contra un ground truth — para eso están `pnpm test:e2e:suite` y
`tests/integration/expected_pedimentos.json`, que hay que correr antes de
adoptar un modelo.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))

GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m"
)

PROCESAR = REPO / "scripts" / "procesar-docs.py"
CALIBRATION_FILE = REPO / "scripts" / "cost_calibration.json"
RESULTADOS_DIR = REPO / "resultados"

# El modelo desplegado hoy va primero: es la BASE de la comparación.
DEFAULT_MODELS = [
    "minimax.minimax-m2.5",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "moonshotai.kimi-k2.5",
]

# Extensiones que SÍ pasan por Bedrock. Un .xml/.xlsx/.csv se extrae por
# XPath/celda (pipeline/xml_document.py, tabular_document.py): no hay modelo
# que comparar, así que no entra a la matriz.
MODEL_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-") or "x"


# --------------------------------------------------------------------------
# Lectura de un resultado de procesar-docs.py
# --------------------------------------------------------------------------

def _usage_of(result: dict) -> dict:
    """Tokens de TODAS las llamadas Bedrock del documento (segmentación +
    extracción + selección de páginas), sumados. `usage.<etapa>` puede venir
    en None cuando esa etapa no llamó al modelo."""
    total = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    for stage in (result.get("usage") or {}).values():
        if not isinstance(stage, dict):
            continue
        usage = stage.get("usage") if isinstance(stage.get("usage"), dict) else stage
        for key in total:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                total[key] += int(value)
    return total


def _leaf_values(node, path: str):
    """(nombre_calificado, valor) por cada hoja escalar del árbol resuelto —
    mismo recorrido que procesar-docs._walk_field_tree, pero quedándose con el
    VALOR, que es lo que se compara entre modelos."""
    if not isinstance(node, dict):
        return
    if isinstance(node.get("items"), list):
        for i, item in enumerate(node["items"]):
            yield from _leaf_values(item, f"{path}[{i}]")
        return
    if isinstance(node.get("fields"), dict):
        for name, child in node["fields"].items():
            yield from _leaf_values(child, f"{path}.{name}" if path else name)
        return
    if "value" in node:
        yield path, node.get("value")


def _doc_values(result: dict) -> dict:
    """Todos los valores extraídos del archivo, aplanados y prefijados por
    documento, para poder diferenciar dos modelos campo por campo."""
    values: dict[str, object] = {}
    for i, doc in enumerate(result.get("documents") or []):
        prefix = f"doc{i}:{doc.get('documentType') or 'SIN-TIPO'}"
        entities = doc.get("extractedEntities") or {}
        if doc.get("fieldTreeShape"):
            for name, node in entities.items():
                for leaf, value in _leaf_values(node, name):
                    values[f"{prefix}.{leaf}"] = value
        else:
            for name, slot in (doc.get("fields") or {}).items():
                if isinstance(slot, dict):
                    values[f"{prefix}.{name}"] = slot.get("normalizedValue") or slot.get("extractedValue")
    return values


def _field_quality(result: dict) -> tuple[int, int]:
    """(campos válidos, campos evaluados) según las reglas del catálogo."""
    ok = total = 0
    for doc in result.get("documents") or []:
        entities = doc.get("extractedEntities") or {}
        if doc.get("fieldTreeShape"):
            for name, node in entities.items():
                for _, valid, _conf in _walk_valid(node, name):
                    total += 1
                    ok += 1 if valid else 0
        else:
            for _name, slot in (doc.get("fields") or {}).items():
                status = slot.get("status") if isinstance(slot, dict) else None
                if status:
                    total += 1
                    ok += 1 if status == "valid" else 0
    return ok, total


def _walk_valid(node, path: str):
    if not isinstance(node, dict):
        return
    if isinstance(node.get("items"), list):
        for item in node["items"]:
            yield from _walk_valid(item, path)
        return
    if isinstance(node.get("fields"), dict):
        for name, child in node["fields"].items():
            yield from _walk_valid(child, f"{path}.{name}" if path else name)
        return
    if "valid" in node:
        yield path, node.get("valid"), node.get("confidence")


def row_from_result(path: Path, result: dict, modelo: str, rep: int) -> dict:
    from pipeline.pricing import bedrock_cost, textract_cost

    timings = (result.get("tracking") or {}).get("timings") or {}
    pages = result.get("pages") or {}
    usage = _usage_of(result)
    ocr_pages = int(pages.get("ocr") or pages.get("ocrPages") or 0)
    # El modelo que REALMENTE corrió puede no ser el pedido (fallback por
    # modelo no disponible), y el costo tiene que tarifarse con ese.
    usado = (result.get("tracking") or {}).get("schema", {}).get("model") or modelo
    seconds = float(result.get("processingSeconds") or 0.0)
    extraction_s = float(timings.get("extraction") or 0.0) / 1000.0
    out_tokens = usage["outputTokens"]
    return {
        "archivo": result.get("sourceFile") or path.stem,
        "modelo": modelo,
        "modeloUsado": usado,
        "rep": rep,
        "estado": result.get("status") or "-",
        "segundos": round(seconds, 2),
        "pageLoad_s": round(float(timings.get("pageLoad") or 0.0) / 1000.0, 2),
        "segmentacion_s": round(float(timings.get("segmentation") or 0.0) / 1000.0, 2),
        "extraccion_s": round(extraction_s, 2),
        "autenticidad_s": round(float(timings.get("authenticity") or 0.0) / 1000.0, 2),
        "validacion_s": round(float(timings.get("validation") or 0.0) / 1000.0, 2),
        "paginas": int(pages.get("total") or pages.get("totalPages") or 0),
        "paginasOcr": ocr_pages,
        "tokensEntrada": usage["inputTokens"],
        "tokensSalida": out_tokens,
        # Throughput efectivo de la etapa cara: es el número que explica la
        # latencia mejor que cualquier benchmark publicado.
        "tokensPorSegundo": round(out_tokens / extraction_s, 1) if extraction_s > 0 else None,
        "costoUsd": round(
            (bedrock_cost(usage, usado) or {}).get("costUsd", 0.0)
            + (textract_cost(ocr_pages) or {}).get("costUsd", 0.0),
            6,
        ),
        "_valores": _doc_values(result),
        "_calidad": _field_quality(result),
    }


# --------------------------------------------------------------------------
# Corrida
# --------------------------------------------------------------------------

def _page_count(path: Path) -> int:
    if path.suffix.lower() != ".pdf":
        return 1
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


def _estimate_usd(paginas: int, modelo: str, calibration: dict) -> float:
    """Cota GRUESA para el preflight (el costo real se calcula después con los
    tokens facturados). Sale de la calibración medida: caracteres por página
    OCR, chars/token del modelo y tokens de salida por llamada."""
    from pipeline.pricing import bedrock_cost, textract_cost

    default = calibration.get("default", {})
    cal = (calibration.get("models") or {}).get(modelo, {})
    chars_per_page = float(default.get("ocrCharsPerPage") or 2628)
    chars_per_token = float(cal.get("charsPerToken") or default.get("charsPerToken") or 3.2)
    out_per_call = float(
        ((cal.get("outputTokensPerCall") or {}).get("p90"))
        or ((default.get("outputTokensPerCall") or {}).get("p90"))
        or 4000
    )
    calls = max(1, round(paginas * chars_per_page / 28000))
    usage = {
        "inputTokens": int(paginas * chars_per_page / chars_per_token) + 2000 * calls,
        "outputTokens": int(out_per_call * calls),
    }
    return (bedrock_cost(usage, modelo) or {}).get("costUsd", 0.0) + (
        textract_cost(paginas) or {}
    ).get("costUsd", 0.0)


def run_one(doc: Path, modelo: str, tipo: str | None, out_root: Path, timeout: int) -> tuple[dict | None, str]:
    """Corre procesar-docs.py para UN documento con UN modelo. Devuelve
    (resultado_json, mensaje_de_error)."""
    before = {p for p in out_root.rglob("*.json")}
    cmd = [sys.executable, str(PROCESAR), str(doc), "--out", str(out_root), "--model", modelo]
    if tipo:
        cmd += ["--type", tipo]
    try:
        proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timeout tras {timeout}s"
    nuevos = [
        p for p in out_root.rglob("*.json")
        if p not in before and not p.name.startswith("_prompt")
    ]
    if not nuevos:
        cola = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return None, f"sin resultado (exit {proc.returncode}): {' | '.join(cola)}"
    nuevo = max(nuevos, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(nuevo.read_text(encoding="utf-8")), ""
    except Exception as exc:  # pragma: no cover - defensivo
        return None, f"JSON ilegible en {nuevo}: {exc}"


# --------------------------------------------------------------------------
# Modo histórico (gratis)
# --------------------------------------------------------------------------

def historico_rows() -> list[dict]:
    rows = []
    for path in sorted(RESULTADOS_DIR.rglob("*.json")):
        if path.name.startswith("_prompt"):
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(result, dict) or "documents" not in result:
            continue
        modelo = (result.get("tracking") or {}).get("schema", {}).get("model") or "(sin registrar)"
        row = row_from_result(path, result, modelo, rep=1)
        row["cuando"] = (result.get("processedAt") or "")[:16]
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Reporte
# --------------------------------------------------------------------------

def _median(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else 0.0


def agregados(rows: list[dict]) -> dict:
    """Por (archivo, modelo): mediana de segundos y de costo entre repeticiones."""
    grupos: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grupos.setdefault((row["archivo"], row["modelo"]), []).append(row)
    salida = {}
    for key, group in grupos.items():
        segundos = [r["segundos"] for r in group]
        # Solo las corridas que SÍ traen `tracking.timings` entran a la
        # mediana por etapa: un resultado viejo sin timings reporta 0 y
        # arrastraría la mediana a cero, que se lee como "instantáneo".
        extraccion = [r["extraccion_s"] for r in group if r["extraccion_s"] > 0]
        tps = [r["tokensPorSegundo"] for r in group if r["tokensPorSegundo"]]
        ok_total = [r["_calidad"] for r in group]
        salida[key] = {
            "n": len(group),
            "segundos": _median(segundos),
            "min": round(min(segundos), 2),
            "max": round(max(segundos), 2),
            "extraccion_s": _median(extraccion) if extraccion else None,
            "tokensPorSegundo": _median(tps) if tps else None,
            "costoUsd": round(statistics.median([r["costoUsd"] for r in group]), 6),
            "paginas": group[0]["paginas"],
            "tokensSalida": int(statistics.median([r["tokensSalida"] for r in group])),
            "campos_ok": sum(c[0] for c in ok_total) // max(1, len(ok_total)),
            "campos_total": sum(c[1] for c in ok_total) // max(1, len(ok_total)),
            "valores": group[0]["_valores"],
            "estado": group[0]["estado"],
        }
    return salida


def diff_vs_base(valores_base: dict, valores: dict) -> tuple[int, int, list[str]]:
    """(campos comparados, campos distintos, ejemplos). Un campo que un modelo
    no devolvió y el otro sí cuenta como distinto: esa es justo la regresión
    que interesa detectar."""
    claves = set(valores_base) | set(valores)
    distintos = [k for k in sorted(claves) if _norm(valores_base.get(k)) != _norm(valores.get(k))]
    return len(claves), len(distintos), distintos[:5]


def _norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value).strip().upper()


def escribir_reporte(rows: list[dict], modelos: list[str], out_dir: Path, titulo: str) -> Path:
    agg = agregados(rows)
    base = modelos[0]
    archivos = sorted({a for a, _ in agg})

    lineas = [f"# {titulo}", ""]
    lineas.append(f"Generado {datetime.now(timezone.utc).isoformat(timespec='seconds')} · "
                  f"modelo base: `{base}`")
    lineas += ["", "## Por documento", "",
               "| Documento | pág | Modelo | s (mediana) | min-max | extracción s | tok salida | tok/s | $ | campos ok | vs base |",
               "|---|---:|---|---:|---|---:|---:|---:|---:|---|---|"]
    for archivo in archivos:
        base_key = (archivo, base)
        for modelo in modelos:
            info = agg.get((archivo, modelo))
            if not info:
                continue
            if modelo == base or base_key not in agg:
                vs = "—"
            else:
                comparados, distintos, ejemplos = diff_vs_base(agg[base_key]["valores"], info["valores"])
                vs = f"{distintos}/{comparados} distintos"
                if ejemplos:
                    vs += " (" + ", ".join(e.split(".")[-1] for e in ejemplos[:3]) + ")"
            lineas.append(
                f"| {archivo} | {info['paginas']} | `{modelo}` | {info['segundos']} | "
                f"{info['min']}-{info['max']} | {info['extraccion_s'] if info['extraccion_s'] is not None else '—'} | {info['tokensSalida']} | "
                f"{info['tokensPorSegundo'] or '—'} | ${info['costoUsd']:.4f} | "
                f"{info['campos_ok']}/{info['campos_total']} | {vs} |"
            )

    lineas += ["", "## Decisión por modelo", "",
               "| Modelo | docs | s/doc (mediana) | vs base | $/doc | vs base | campos distintos |",
               "|---|---:|---:|---:|---:|---:|---|"]
    resumen_modelo = {}
    for modelo in modelos:
        infos = [agg[(a, m)] for (a, m) in agg if m == modelo]
        if not infos:
            continue
        segundos = _median([i["segundos"] for i in infos])
        costo = round(statistics.median([i["costoUsd"] for i in infos]), 6)
        distintos = 0
        comparados = 0
        for archivo in archivos:
            if (archivo, modelo) in agg and (archivo, base) in agg and modelo != base:
                c, d, _ = diff_vs_base(agg[(archivo, base)]["valores"], agg[(archivo, modelo)]["valores"])
                comparados += c
                distintos += d
        resumen_modelo[modelo] = {"segundos": segundos, "costo": costo, "docs": len(infos)}
        b = resumen_modelo.get(base, {})
        d_s = f"{segundos - b.get('segundos', segundos):+.1f}s" if modelo != base else "—"
        d_c = f"{(costo - b.get('costo', costo)) * 1000:+.2f}‰" if modelo != base else "—"
        lineas.append(
            f"| `{modelo}` | {len(infos)} | {segundos} | {d_s} | ${costo:.4f} | {d_c} | "
            + (f"{distintos}/{comparados}" if modelo != base else "—") + " |"
        )

    lineas += [
        "", "## Cómo leer esto", "",
        "- **tok/s** es el throughput efectivo de la etapa de extracción "
        "(`tokens de salida / segundos de extracción`): es lo que explica la latencia, "
        "porque la llamada es `Converse` no-streaming y su wall clock es "
        "`TTFT + tokens_salida / throughput`.",
        "- **$/doc** usa las tarifas de `pipeline/pricing.py`. Un modelo sin tarifa verificada "
        "ahí se tarifa con la de Haiku 4.5 como aproximación (ver `_BEDROCK_PRICING`).",
        "- **campos distintos** NO es accuracy: es cuántos valores cambian respecto al modelo base. "
        "Antes de adoptar un modelo hay que correr `pnpm test:e2e:suite` y el ground truth de "
        "`tests/integration/expected_pedimentos.json`.",
        "- La latencia de un LLM varía mucho entre llamadas idénticas: mirar la columna **min-max** "
        "antes de creerle a una mediana de pocas repeticiones.",
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "resumen.md"
    md.write_text("\n".join(lineas) + "\n", encoding="utf-8")

    csv_path = out_dir / "filas.csv"
    campos = [k for k in rows[0] if not k.startswith("_")] if rows else []
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=campos, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return md


def imprimir_tabla(rows: list[dict], modelos: list[str]) -> None:
    agg = agregados(rows)
    base = modelos[0]
    print(f"\n{BOLD}Latencia por documento y modelo{RESET}  {DIM}(mediana de las repeticiones){RESET}")
    print(f"  {'documento':<34}{'modelo':<46}{'s':>8}{'extr s':>9}{'tok/s':>8}{'$':>10}")
    for archivo in sorted({a for a, _ in agg}):
        for modelo in modelos:
            info = agg.get((archivo, modelo))
            if not info:
                continue
            mas_rapido = modelo != base and info["segundos"] < agg.get((archivo, base), info)["segundos"]
            segundos = f"{GREEN}{info['segundos']:>8.1f}{RESET}" if mas_rapido else f"{info['segundos']:>8.1f}"
            extraccion = f"{info['extraccion_s']:>9.1f}" if info["extraccion_s"] is not None else f"{'—':>9}"
            tps = f"{info['tokensPorSegundo']:>8.0f}" if info["tokensPorSegundo"] else f"{'—':>8}"
            print(f"  {archivo[:33]:<34}{modelo[:45]:<46}{segundos}{extraccion}{tps}"
                  f"{info['costoUsd']:>10.4f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs", help="carpeta con los documentos a medir")
    parser.add_argument("--doc", action="append", default=[], help="un documento (repetible)")
    parser.add_argument("--tipo", help="TYPE_ID declarado (se aplica a todos los documentos)")
    parser.add_argument("--modelos", help=f"lista separada por coma; el primero es la base (default: {','.join(DEFAULT_MODELS)})")
    parser.add_argument("--repeticiones", type=int, default=2, help="corridas por documento×modelo (default 2)")
    parser.add_argument("--out", help="carpeta de salida (default: bench-latencia/<timestamp>/)")
    parser.add_argument("--timeout", type=int, default=900, help="segundos máximos por documento (default 900)")
    parser.add_argument("--max-costo-usd", type=float, help="aborta si la cota estimada lo excede")
    parser.add_argument("--dry-run", action="store_true", help="imprime la matriz y el costo estimado, no llama a Bedrock")
    parser.add_argument("--si", action="store_true", help="no pide confirmación antes de gastar")
    parser.add_argument("--historico", action="store_true", help="gratis: analiza las corridas ya guardadas en resultados/")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else REPO / "bench-latencia" / stamp

    if args.historico:
        rows = historico_rows()
        if not rows:
            print(f"{YELLOW}No hay corridas previas en {RESULTADOS_DIR}/ — corre el benchmark sin --historico.{RESET}")
            return 1
        modelos = sorted({r["modelo"] for r in rows})
        # La base es el modelo desplegado si aparece; si no, el más frecuente.
        if DEFAULT_MODELS[0] in modelos:
            modelos.remove(DEFAULT_MODELS[0])
            modelos.insert(0, DEFAULT_MODELS[0])
        imprimir_tabla(rows, modelos)
        md = escribir_reporte(rows, modelos, out_dir, "Benchmark histórico (corridas ya guardadas en resultados/)")
        print(f"\n{DIM}Ojo: son corridas de fechas y catálogos distintos, no un experimento controlado.{RESET}")
        print(f"{CYAN}Reporte → {md}{RESET}")
        return 0

    modelos = [m.strip() for m in (args.modelos.split(",") if args.modelos else DEFAULT_MODELS) if m.strip()]
    docs: list[Path] = [Path(d) for d in args.doc]
    if args.docs:
        docs += sorted(p for p in Path(args.docs).iterdir() if p.is_file())
    if not docs:
        print(f"{RED}Nada que medir: pasa --doc <archivo> o --docs <carpeta>.{RESET}")
        return 2
    saltados = [p for p in docs if p.suffix.lower() not in MODEL_EXTENSIONS]
    docs = [p for p in docs if p.suffix.lower() in MODEL_EXTENSIONS]
    for p in saltados:
        print(f"{DIM}saltado {p.name}: {p.suffix} no pasa por Bedrock (extracción determinística){RESET}")
    if not docs:
        print(f"{RED}Ningún documento llega a Bedrock — no hay modelos que comparar.{RESET}")
        return 2

    calibration = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8")) if CALIBRATION_FILE.exists() else {}
    paginas = {p: _page_count(p) for p in docs}
    total_paginas = sum(paginas.values()) * args.repeticiones
    estimado = sum(
        _estimate_usd(paginas[p], modelo, calibration) * args.repeticiones
        for p in docs for modelo in modelos
    )

    print(f"{BOLD}Matriz del benchmark{RESET}")
    print(f"  documentos  : {len(docs)} ({sum(paginas.values())} páginas)")
    print(f"  modelos     : {', '.join(modelos)}   {DIM}(base: {modelos[0]}){RESET}")
    print(f"  repeticiones: {args.repeticiones}")
    print(f"  corridas    : {len(docs) * len(modelos) * args.repeticiones} "
          f"({total_paginas * len(modelos)} páginas-modelo)")
    print(f"  costo estimado (cota gruesa): {YELLOW}${estimado:.2f}{RESET}")
    if args.max_costo_usd and estimado > args.max_costo_usd:
        print(f"{RED}Abortado: la cota estimada (${estimado:.2f}) supera --max-costo-usd {args.max_costo_usd}.{RESET}")
        return 3
    if args.dry_run:
        return 0
    if not args.si and sys.stdin.isatty():
        if input("¿Correr? Esto SÍ gasta en Bedrock/Textract [s/N]: ").strip().lower() not in ("s", "si", "sí", "y"):
            print("Cancelado.")
            return 0

    rows: list[dict] = []
    fallos: list[str] = []
    total = len(docs) * len(modelos) * args.repeticiones
    hecho = 0
    inicio = time.time()
    for rep in range(1, args.repeticiones + 1):
        for modelo in modelos:
            for doc in docs:
                hecho += 1
                etiqueta = f"[{hecho}/{total}] {doc.name} · {modelo} · rep {rep}"
                print(f"{DIM}{etiqueta}{RESET}", flush=True)
                result, error = run_one(doc, modelo, args.tipo, out_dir / "corridas" / f"rep{rep}", args.timeout)
                if error:
                    print(f"  {RED}{error}{RESET}")
                    fallos.append(f"{etiqueta}: {error}")
                    continue
                row = row_from_result(doc, result, modelo, rep)
                rows.append(row)
                print(f"  {row['segundos']}s  extracción {row['extraccion_s']}s  "
                      f"{row['tokensSalida']} tok salida  ${row['costoUsd']:.4f}  {row['estado']}")

    if not rows:
        print(f"{RED}Ninguna corrida produjo resultado.{RESET}")
        return 1

    imprimir_tabla(rows, modelos)
    md = escribir_reporte(rows, modelos, out_dir, "Benchmark de latencia por modelo")
    costo_real = sum(r["costoUsd"] for r in rows)
    print(f"\n  costo real de este benchmark: ${costo_real:.4f}  "
          f"(estimado ${estimado:.2f})  ·  {time.time() - inicio:.0f}s de reloj")
    if fallos:
        print(f"  {YELLOW}{len(fallos)} corridas fallaron:{RESET}")
        for f in fallos[:5]:
            print(f"    {f}")
    print(f"{CYAN}Reporte → {md}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
