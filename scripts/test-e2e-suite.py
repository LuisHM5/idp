#!/usr/bin/env python3
"""Suite E2E multi-tipo: una carpeta por tipo de documento, costo estimado y
confirmación antes de gastar un centavo.

Estructura (raíz por default `test-docs/e2e-suite/`, ignorada por git):

    test-docs/e2e-suite/
      PEDIMENTO_ERP/            ← el nombre de la carpeta es el TYPE_ID exacto del catálogo
        26 07 3435 6041353 CG.pdf
        bad-pedimento.pdf
      CURP/
        curp.pdf
      INE/
        ine-frente.png
      CFDI_GASTO_TERCERO_XML/
        gasto.xml

Cada archivo se sube DECLARADO como el tipo de su carpeta y pasa por la
infraestructura real, igual que `pnpm test:e2e:pedimento` en modo dataset (esta
suite reutiliza ese runner y su análisis: aprobados, confianza por parámetro,
índices más bajos, reglas que fallan o no corren, coherencia interna, costo y
diferencias contra la corrida anterior). Una carpeta vacía queda "pendiente":
la llenas poco a poco y la siguiente corrida la incluye sola. Una carpeta cuyo
nombre no es un tipo activo se reporta y se ignora.

Antes de ejecutar muestra el plan por tipo con el costo estimado — con el
$/página MEDIDO en la tabla de auditoría de costos del ambiente cuando hay
historial suficiente de ese tipo, o con las tarifas de referencia de
documentacion/COSTOS.md — y un tope conservador; nada corre sin confirmar.

Uso (detalle con `pnpm test:e2e:suite --help`):
    pnpm test:e2e:init [-r DIR] [TIPO ...]
    pnpm test:e2e:suite [-e ENV] [-p] [-y] [-c] [-n N] [-j N] [-m USD] [-r DIR] [TIPO ...]
    pnpm test:e2e:suite TIPO -d DIR [-e ENV] [-p] [-y] [-n N] [-j N] [-m USD]
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO / "test-docs" / "e2e-suite"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # @dataclass resuelve anotaciones buscando su módulo en sys.modules
    spec.loader.exec_module(mod)
    return mod


e2e = _load_module("test_e2e_pedimento_suite", REPO / "scripts" / "test-e2e-pedimento.py")
ea, qh = e2e.ea, e2e.qh
GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = e2e.GREEN, e2e.RED, e2e.YELLOW, e2e.CYAN, e2e.DIM, e2e.BOLD, e2e.RESET
VERDICT_COLOR = {"CONFIABLE": GREEN, "CON_RESERVAS": YELLOW, "NO_CONFIABLE": RED}

README_TEXT = """Suite E2E multi-tipo (pnpm test:e2e:suite)

Una carpeta por tipo de documento; el nombre de la carpeta es el TYPE_ID exacto
del catálogo. Copia aquí los documentos de prueba de cada tipo cuando los tengas:
las carpetas vacías se reportan como pendientes y no cuestan nada.

Formatos: pdf, png, jpg, jpeg, xml, xlsx, csv.
Ground truth opcional: tests/integration/expected_pedimentos.json, por nombre de archivo.

  pnpm test:e2e:suite -- --plan --env prod    # ver archivos y costo estimado
  pnpm test:e2e:suite -- --env prod           # ejecutar (pide confirmación)
"""


def expected_format(entry: dict) -> str:
    """Qué archivos poner en la carpeta, deducido de la FORMA del catálogo
    (igual que el pipeline): campos con `xpath` → XML, con `cell`/`column` →
    hoja de cálculo, el resto → PDF o imagen."""
    def keys(fields: dict):
        for spec in fields.values():
            if isinstance(spec, dict):
                yield from spec.keys()
                for nested in ("fields", "item_fields"):
                    if isinstance(spec.get(nested), dict):
                        yield from keys(spec[nested])

    declared = set(keys(entry.get("fields") or {}))
    if "xpath" in declared:
        return "xml"
    if declared & {"cell", "column"}:
        return "xlsx/csv"
    return "pdf/png/jpg"


def init_structure(root: Path, entries: dict[str, dict]) -> int:
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "LEEME.txt"
    if not readme.exists():
        readme.write_text(README_TEXT, encoding="utf-8")
    print(f"{CYAN}Estructura de la suite E2E en {root}{RESET}")
    created = 0
    for type_id, entry in sorted(entries.items()):
        folder = root / type_id
        if folder.exists():
            count = len(e2e.list_dataset_files(folder))
            label, color = (f"{count} archivo(s)", GREEN) if count else ("ya existía, vacía", DIM)
        else:
            folder.mkdir()
            created += 1
            label, color = "nueva", CYAN
        name = entry.get("display_name") or ""
        print(f"  {type_id + '/':<36}{expected_format(entry):<13}{color}{label:<19}{RESET}{DIM}{name}{RESET}")
    print(f"\n{created} carpeta(s) creadas · {len(entries) - created} ya existían. "
          f"{DIM}Nada se sobrescribe ni se borra; correrlo de nuevo solo agrega tipos nuevos del catálogo.{RESET}")
    print(f"{DIM}Siguiente paso: copia documentos a cada carpeta y corre `pnpm test:e2e:suite -p`.{RESET}")
    return 0


def discover(root: Path, types: set[str], selected: set[str] | None, limit: int | None
             ) -> tuple[list[tuple[str, Path, list[Path]]], list[str], list[str]]:
    """(tipos con archivos como (tipo, carpeta, archivos), tipos pendientes,
    carpetas ignoradas). Con selección explícita, un tipo sin carpeta cuenta
    como pendiente y las carpetas ajenas no se reportan."""
    ready, pending, ignored = [], [], []
    folders = {p.name: p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")} if root.is_dir() else {}
    if not selected:
        ignored = sorted(name for name in folders if name not in types)
    for type_id in sorted(selected or (set(folders) & types)):
        folder = folders.get(type_id, root / type_id)
        files = e2e.list_dataset_files(folder)[:limit] if limit else e2e.list_dataset_files(folder)
        if files:
            ready.append((type_id, folder, files))
        else:
            pending.append(type_id)
    return ready, pending, ignored


def resolve_types(requested: list[str], types: list[str]) -> tuple[set[str], list[str]]:
    """Normaliza a mayúsculas y separa los desconocidos, cada uno con su
    sugerencia más cercana (`pedimento_er` → `PEDIMENTO_ERP`)."""
    selected, errors = set(), []
    for raw in requested:
        type_id = raw.strip().upper()
        if not type_id:
            continue
        if type_id in types:
            selected.add(type_id)
        else:
            close = difflib.get_close_matches(type_id, types, n=1, cutoff=0.6)
            errors.append(f"'{raw}' no es un tipo activo" + (f" — ¿quisiste decir {close[0]}?" if close else ""))
    return selected, errors


USAGE = """\
  %(prog)s [-e ENV] [-p] [-y] [-c] [-n N] [-j N] [-m USD] [-r DIR] [TIPO ...]
  %(prog)s TIPO -d DIR [-e ENV] [-p] [-y] [-n N] [-j N] [-m USD]
  pnpm test:e2e:init [-r DIR] [TIPO ...]"""

DESCRIPTION = """\
DESCRIPCIÓN:
  E2E real (API → S3 → SQS → Step Functions → DynamoDB) sobre una carpeta por
  tipo de documento, con un análisis de confianza por tipo. Antes de gastar
  muestra el plan con el costo estimado y pide confirmación.

  Sin TIPO corre todos los tipos cuya carpeta DIR/<TIPO>/ tenga archivos.
  Con uno o más TIPO corre solo esos. Con un solo TIPO, -d usa cualquier
  carpeta como su dataset en vez de DIR/<TIPO>/.

ARGUMENTOS:
  TIPO                    TYPE_ID del catálogo (mayúsculas o minúsculas);
                          se puede repetir"""

EPILOG = """\
EJEMPLOS:
  pnpm test:e2e:init
      crea test-docs/e2e-suite/<TIPO>/ para cada tipo activo
  pnpm test:e2e:suite -p -e prod
      plan y costo de todos los tipos con archivos, sin ejecutar
  pnpm test:e2e:suite -e prod
      ejecuta todos los tipos con archivos
  pnpm test:e2e:suite PEDIMENTO_ERP -e prod
      un solo tipo, desde test-docs/e2e-suite/PEDIMENTO_ERP/
  pnpm test:e2e:suite PEDIMENTO_ERP -d ./test-docs/full-dataset-pedimentos -e prod
      un solo tipo con un dataset en otra carpeta
  pnpm test:e2e:suite curp ine -n 2 -m 1
      dos tipos, 2 archivos cada uno, cancela si el tope pasa de $1
  pnpm test:e2e:suite -e prod -y -m 10
      sin preguntar (CI), con tope de $10

ENTORNO:
  API_KEY       obligatoria con -e prod; ahí nunca se lee sola
  API_KEY_APP   en dev, se usa automáticamente si .env.dev tiene DEV_API_KEY_REQUIRED=true

ARCHIVOS:
  test-docs/e2e-suite/<TIPO>/            documentos de entrada: pdf png jpg jpeg xml xlsx csv
  resultados-pruebas/<env>/<corrida>/    suite.md, run.json y <TIPO>/analisis.md

ESTADO DE SALIDA:
  0     terminó y ningún tipo quedó NO CONFIABLE (o -p, o nada que correr)
  1     algún tipo quedó NO CONFIABLE
  2     no se pudo empezar (tipo inválido, TIPO pedido sin archivos, -d sin un
        solo TIPO, sin API_KEY en prod, tope mayor que -m)
  130   cancelado en la confirmación o con Ctrl+C

Guía completa: documentacion/TESTS.md, secciones 0 y 5.2."""


def build_parser() -> argparse.ArgumentParser:
    parser = e2e.unix_parser("pnpm test:e2e:suite", USAGE, DESCRIPTION, EPILOG)
    parser.add_argument("types", nargs="*", metavar="TIPO", help=argparse.SUPPRESS)
    parser.add_argument("-e", "--env", metavar="ENV", choices=("dev", "prod"), default="dev", help="dev | prod (default dev)")
    parser.add_argument("-p", "--plan", action="store_true", help="solo muestra archivos y costo; no ejecuta")
    parser.add_argument("-y", "--yes", action="store_true", help="no pedir confirmación")
    parser.add_argument("-c", "--confirm-each", action="store_true", help="confirmar antes de cada tipo (N lo salta)")
    parser.add_argument("-n", "--limit", dest="limit_per_type", metavar="N", type=int, help="máximo de archivos por tipo")
    parser.add_argument("--limit-per-type", dest="limit_per_type", type=int, help=argparse.SUPPRESS)  # nombre anterior
    parser.add_argument("-j", "--concurrency", metavar="N", type=int, default=3, help="archivos en paralelo por tipo (default 3)")
    parser.add_argument("-m", "--max-cost-usd", metavar="USD", type=float, help="cancela si el tope total supera USD")
    parser.add_argument("-d", "--docs", metavar="DIR", help="con un solo TIPO: carpeta del dataset (en vez de DIR/<TIPO>/)")
    parser.add_argument("-t", "--doc-type", dest="type_flags", metavar="TIPO", action="append", default=[],
                        help="igual que TIPO posicional (mismo flag que test:e2e:pedimento)")
    parser.add_argument("-r", "--root", metavar="DIR", default=str(DEFAULT_ROOT), help="raíz con una carpeta por tipo (default test-docs/e2e-suite)")
    parser.add_argument("-w", "--wait", metavar="SEG", type=int, default=1200, help="espera máxima por archivo (default 1200)")
    parser.add_argument("--init", action="store_true", help="crea las carpetas de los tipos y termina (pnpm test:e2e:init)")
    parser.add_argument("--only", metavar="T1,T2", help="igual que TIPO..., separados por coma")
    parser.add_argument("--min-approval-rate", metavar="R", type=float, help="por tipo: NO CONFIABLE si APPROVED < R (0-1)")
    parser.add_argument("--min-field-index", metavar="I", type=float, help="por tipo: NO CONFIABLE si un obligatorio < I (0-100)")
    parser.add_argument("--no-compare", action="store_true", help="no compara contra la corrida anterior de cada tipo")
    parser.add_argument("--stack", metavar="NOMBRE", help="stack explícito; gana sobre -e")
    parser.add_argument("--region", metavar="REGION", default="us-east-1", help="default us-east-1")
    parser.add_argument("--history-dir", metavar="DIR", default=str(qh.DEFAULT_HISTORY), help="raíz de resultados (default resultados-pruebas/)")
    return parser


def render_suite(rows: list[dict], env: str, *, color: bool) -> tuple[str, str]:
    c = (lambda code: code) if color else (lambda code: "")
    text = [f"\n{c(CYAN)}{'═' * 92}{c(RESET)}", f"{c(BOLD)}RESUMEN DE LA SUITE · {env} · {len(rows)} tipo(s){c(RESET)}",
            f"{c(CYAN)}{'═' * 92}{c(RESET)}",
            c(DIM) + f"  {'Tipo':<30}{'Veredicto':<15}{'Arch':>5}{'Docs':>5}{'Aprob':>7}{'Índ.mín oblig.':>16}{'Reglas✗':>8}{'Inertes':>8}{'Estimado':>10}{'Real':>9}" + c(RESET)]
    md = [f"# Suite E2E multi-tipo ({env})\n",
          "| Tipo | Veredicto | Archivos | Docs | Aprobación | Campo obligatorio más débil | Reglas que fallan | Reglas inertes | Coherencia ✗ | Estimado | Real | Reporte |",
          "|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|"]
    total_est = total_real = 0.0
    for row in rows:
        a, est = row["analysis"], row["estimate"]
        if a is None:
            text.append(f"  {row['type']:<30}{c(DIM)}{'OMITIDO':<15}{c(RESET)}{est['files']:>5}")
            md.append(f"| {row['type']} | omitido | {est['files']} | — | — | — | — | — | — | ${est['estimateUsd']:.2f} | — | — |")
            continue
        level = a["verdict"]["level"]
        required = [f for f in a["fields"] if f["required"] and f["index"] is not None]
        weakest = min(required, key=lambda f: f["index"], default=None)
        weakest_txt = f"{weakest['index']:.1f} {weakest['path']}" if weakest else "—"
        real = a["execution"]["costUsd"].get("sum")
        total_est += est["estimateUsd"]
        total_real += real or 0.0
        approval = ea._fmt_pct(a["documents"]["approvalRate"])
        real_txt = f"${real:.2f}" if real is not None else "—"
        weakest_index = f"{weakest['index']:.1f}" if weakest else "—"
        text.append(f"  {row['type']:<30}{c(VERDICT_COLOR[level])}{level:<15}{c(RESET)}{a['execution']['files']:>5}"
                    f"{a['documents']['total']:>5}{approval:>7}{weakest_index:>16}"
                    f"{len(a['failingRules']):>8}{len(a['inertRules']):>8}{'$' + format(est['estimateUsd'], '.2f'):>10}{real_txt:>9}")
        if weakest:
            text.append(f"  {c(DIM)}{'':<30}campo más débil: {weakest['path']}{c(RESET)}")
        md.append(f"| {row['type']} | {level.replace('_', ' ')} | {a['execution']['files']} | {a['documents']['total']} | {approval} | "
                  f"{weakest_txt} | {len(a['failingRules'])} | {len(a['inertRules'])} | {len(a['coherence']['failures'])} | "
                  f"${est['estimateUsd']:.2f} | {real_txt} | [{row['type']}/analisis.md]({row['type']}/analisis.md) |")
    text.append(f"  {c(DIM)}Costo total: estimado ${total_est:.2f} · real ${total_real:.2f}{c(RESET)}")
    text.append(f"{c(CYAN)}{'═' * 92}{c(RESET)}")
    md.append(f"\nCosto total: estimado ${total_est:.2f} · real ${total_real:.2f}\n")
    for row in rows:
        if row["analysis"] and row["analysis"]["verdict"]["blockers"]:
            md.append(f"\n## {row['type']} — bloqueos\n")
            md.extend(f"- {b}" for b in row["analysis"]["verdict"]["blockers"])
    return "\n".join(text), "\n".join(md) + "\n"


def main() -> int:
    parser = build_parser()
    # intermixed: los TIPO pueden ir antes, después o entre opciones (`curp -n 2 ine`), como en cualquier CLI unix.
    args = parser.parse_intermixed_args([a for a in sys.argv[1:] if a != "--"])

    root = Path(args.root)
    types = ea.active_type_ids()
    requested = list(args.types) + list(args.type_flags) + (args.only.split(",") if args.only else [])
    selected, errors = resolve_types(requested, types)
    if args.docs and args.init:
        errors.append("-d no aplica con --init")
    elif args.docs and len(selected) != 1:
        errors.append(f"-d necesita exactamente un TIPO (recibí {len(selected)})")
    if errors:
        parser.print_usage(sys.stderr)
        for error in errors:
            print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 2
    if args.init:
        entries = {t: ea.load_type_spec(t) or {} for t in types if not selected or t in selected}
        return init_structure(root, entries)

    if args.docs:
        docs = Path(args.docs)
        if not docs.is_dir():
            print(f"{RED}No existe la carpeta {docs}.{RESET}", file=sys.stderr)
            return 2
        type_id = next(iter(selected))
        files = e2e.list_dataset_files(docs)
        files = files[: args.limit_per_type] if args.limit_per_type else files
        ready, pending, ignored = ([(type_id, docs, files)] if files else []), ([] if files else [type_id]), []
        source = docs
    else:
        if not selected and not root.is_dir():
            print(f"{RED}No existe {root}. Créala con `pnpm test:e2e:init`.{RESET}", file=sys.stderr)
            return 2
        ready, pending, ignored = discover(root, set(types), selected or None, args.limit_per_type)
        source = root
    if selected and not ready:
        where = args.docs or ", ".join(str(root / t) for t in sorted(selected))
        print(f"{RED}Sin archivos soportados ({', '.join(sorted(e2e.SUPPORTED_SUFFIXES))}) en {where}.{RESET}", file=sys.stderr)
        return 2
    api_key = e2e.resolve_api_key(args.env)
    if not args.plan and args.env == "prod" and not api_key:
        print(f"{RED}prod exige x-api-key: exporta API_KEY antes de correr la suite.{RESET}", file=sys.stderr)
        return 2
    expected = json.loads(e2e.EXPECTED_FILE.read_text(encoding="utf-8"))

    env_label = f"{RED}{BOLD}PROD{RESET}" if args.env == "prod" else args.env
    print(f"{CYAN}=== Suite E2E · {len(selected) or 'todos los'} tipo(s) · {source} · {env_label}{CYAN} ==={RESET}")
    if not ready:
        print(f"{YELLOW}Ninguna carpeta de tipo tiene archivos todavía.{RESET}")
    plan = []
    total_files = total_est = total_ceiling = 0
    if ready:
        print(f"\n{DIM}Plan de costo (lectura local de páginas y del Lambda desplegado; sin llamadas pagadas):{RESET}")
        estimates = e2e.estimate_plan([(type_id, files) for type_id, _, files in ready], args.env, args.stack, args.region)
        # Con un solo tipo, el detalle por archivo cabe y es lo más útil.
        totals = e2e.print_plan(estimates, expected, detail=len(ready) == 1)
        plan = [{"type": type_id, "folder": folder, "files": files, "estimate": estimate}
                for (type_id, folder, files), estimate in zip(ready, estimates)]
        total_files, total_est, total_ceiling = totals["files"], totals["estimateUsd"], totals["ceilingUsd"]
        if len(plan) > 1:
            print(f"  {BOLD}{'TOTAL':<46}{total_files:>5}{totals['pages']:>6}{totals['ocrPages']:>5}{totals['calls']:>5}"
                  f"{e2e._money(totals['textractUsd']):>10}{e2e._money(totals['bedrockUsd']):>10}{e2e._money(totals['lambdaUsd']):>9}"
                  f"{e2e._money(total_est):>10}{e2e._money(total_ceiling):>9}{RESET}")
        e2e.print_plan_footer(estimates)
    if pending:
        print(f"\n{DIM}Pendientes (carpeta vacía, sin costo): {', '.join(pending)}{RESET}")
    if ignored:
        print(f"{YELLOW}Ignoradas (no son un TYPE_ID activo): {', '.join(ignored)}{RESET}")
    if args.plan or not plan:
        return 0

    if args.max_cost_usd is not None and total_ceiling > args.max_cost_usd:
        print(f"{RED}El tope ${total_ceiling:.2f} excede --max-cost-usd ${args.max_cost_usd:.2f}; cancelado.{RESET}")
        return 2
    if not args.confirm_each and not e2e.confirm(
            f"\n¿Ejecutar {total_files} archivo(s) de {len(plan)} tipo(s) en {env_label} por ≈ ${total_est:.2f} "
            f"(tope ${total_ceiling:.2f})? [y/N] ", args.yes):
        return 130

    target = e2e.resolve_target(args.env, args.stack, args.region, api_key)
    history_root = Path(args.history_dir)
    run_dir, record = qh.create_run("e2e-suite", args.env, history_root)
    record["root"] = str(source)
    print(f"\nAPI: {target.endpoint} · resultados → {run_dir}")

    rows = []
    started = time.monotonic()
    for index, item in enumerate(plan, 1):
        type_id, folder, files, est = item["type"], item["folder"], item["files"], item["estimate"]["total"]
        if args.confirm_each and not e2e.confirm(
                f"\n[{index}/{len(plan)}] ¿Ejecutar {type_id}: {est['files']} archivo(s) ≈ ${est['estimateUsd']:.2f} "
                f"(tope ${est['ceilingUsd']:.2f})? [y/N] ", args.yes):
            rows.append({"type": type_id, "estimate": est, "analysis": None})
            continue
        print(f"\n{CYAN}━━━ [{index}/{len(plan)}] {type_id} · {len(files)} archivo(s) ━━━{RESET}")
        # Misma regla que test:e2e:pedimento (tipo + nombre de la carpeta), así
        # un dataset corrido por cualquiera de los dos comandos se compara igual.
        previous = None if args.no_compare else ea.find_previous_analysis(
            history_root / args.env, type_id, folder.name, exclude=run_dir)
        analysis = e2e.execute_dataset(
            target, files, doc_type=type_id, type_spec=ea.load_type_spec(type_id), expected=expected,
            out_dir=run_dir / type_id, run_id=record["runId"], wait=args.wait, concurrency=args.concurrency,
            docs_dir=folder, exercise_metadata=index == 1, min_approval_rate=args.min_approval_rate,
            min_field_index=args.min_field_index, previous=previous, estimate=item["estimate"],
        )
        rows.append({"type": type_id, "estimate": est, "analysis": analysis})
        record["cases"].append({"type": type_id, "verdict": analysis["verdict"]["level"],
                                "summary": ea.run_summary(analysis, estimate=est)})
        qh.write_record(run_dir, record)

    text, md = render_suite(rows, args.env, color=sys.stdout.isatty())
    print(text)
    e2e._write_private(run_dir / "suite.md", md)
    executed = [r["analysis"] for r in rows if r["analysis"]]
    failures = sum(1 for a in executed for f in a["perFile"] if not f["passed"])
    tests = sum(a["execution"]["files"] for a in executed)
    blocked = [r["type"] for r in rows if r["analysis"] and r["analysis"]["verdict"]["level"] == "NO_CONFIABLE"]
    real_costs = [a["execution"]["costUsd"].get("sum") for a in executed if a["execution"]["costUsd"].get("sum") is not None]
    record["summary"] = {
        "types": len(executed), "skippedTypes": len(rows) - len(executed), "pendingTypes": pending,
        "tests": tests, "failures": failures, "e2ePassRate": (tests - failures) / tests if tests else None,
        "blockedTypes": blocked, "estimatedCostUsd": round(total_est, 4),
        "costUsd": round(sum(real_costs), 6) if real_costs else None,
        "durationSeconds": round(time.monotonic() - started, 3),
    }
    record["completedAt"] = qh.utc_now()
    record["status"] = "FAILED" if blocked else "PASSED"
    qh.write_record(run_dir, record)
    print(f"\nResumen → {run_dir / 'suite.md'} (un analisis.md por tipo en su subcarpeta)")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
