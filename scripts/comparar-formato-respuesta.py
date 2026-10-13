#!/usr/bin/env python3
"""Compara compact vs legacy contra el stack desplegado usando la misma API.

Hace dos cargas reales del mismo archivo, espera ambos resultados y muestra
latencia, tokens facturados, fallback y diferencias de valores. Tiene costo
real: cada variante procesa nuevamente el documento.

Uso:
  pnpm comparar:formato -- archivo.pdf PEDIMENTO_ERP
  pnpm comparar:formato -- archivo.pdf PEDIMENTO_ERP --extraction-mode hybrid --yes
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOC_ID = re.compile(r"documentId:\s*([0-9a-f-]+)", re.IGNORECASE)


def _run(command: list[str]) -> str:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    if output:
        print(output)
    if result.returncode:
        raise RuntimeError(f"Falló ({result.returncode}): {' '.join(command)}")
    return output


def _send(args: argparse.Namespace, response_format: str) -> str:
    command = [
        "bash", "scripts/enviar-documento.sh", str(args.archivo), args.tipo,
        "--ai-response-format", response_format,
        "--env", args.env,
        "--extraction-mode", args.extraction_mode,
    ]
    if args.stack:
        command.extend(["--stack", args.stack])
    if args.model:
        command.extend(["--model", args.model])
    if args.occurrence_mode:
        command.extend(["--occurrence-mode", args.occurrence_mode])
    for value in args.meta:
        command.extend(["--meta", value])
    output = _run(command)
    match = DOC_ID.search(output)
    if not match:
        raise RuntimeError("La carga terminó sin imprimir documentId")
    return match.group(1)


def _wait_result(args: argparse.Namespace, document_id: str, destination: Path) -> dict[str, Any]:
    before = set(destination.glob("*.json"))
    command = [
        str(ROOT / ".venv/bin/python"), "scripts/ver-resultado.py",
        "--id", document_id, "--wait", "--save", str(destination), "--env", args.env,
    ]
    if args.stack:
        command.extend(["--stack", args.stack])
    _run(command)
    created = sorted(set(destination.glob("*.json")) - before, key=lambda path: path.stat().st_mtime)
    if not created:
        raise RuntimeError(f"No se guardó el resultado de {document_id}")
    return json.loads(created[-1].read_text(encoding="utf-8"))


def _usage(result: dict[str, Any]) -> dict[str, int]:
    extraction = ((result.get("usage") or {}).get("extraction") or {})
    usage = extraction.get("usage") or {}
    return {
        "input": int(usage.get("inputTokens") or 0),
        "output": int(usage.get("outputTokens") or 0),
        "total": int(usage.get("totalTokens") or 0),
    }


def _format_audit(result: dict[str, Any]) -> dict[str, Any]:
    audits = [
        doc.get("aiResponseFormat") or {}
        for doc in result.get("documents") or []
        if isinstance(doc, dict)
    ]
    effective = sorted({str(audit.get("effective")) for audit in audits if audit.get("effective")})
    return {
        "effective": ",".join(effective) or "-",
        "fallback": any(bool(audit.get("fallbackExecuted")) for audit in audits),
        "compactCalls": sum(int(audit.get("compactCalls") or 0) for audit in audits),
        "legacyCalls": sum(int(audit.get("legacyCalls") or 0) for audit in audits),
    }


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    usage = _usage(result)
    audit = _format_audit(result)
    tracking = result.get("tracking") or {}
    timings = tracking.get("timings") or {}
    return {
        "status": ((result.get("validation") or {}).get("status") or "-"),
        "totalMs": round(float(tracking.get("processingMs") or 0), 1),
        "extractionMs": round(float(timings.get("extraction") or 0), 1),
        "aiExecuted": bool(result.get("aiExecuted")),
        **usage,
        **audit,
    }


def _field_values(result: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}

    def visit(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if "value" in node:
            values[path] = node.get("value")
            return
        fields = node.get("fields")
        if isinstance(fields, dict):
            for name, child in fields.items():
                visit(child, f"{path}.{name}" if path else name)
        items = node.get("items")
        if isinstance(items, list):
            for index, item in enumerate(items):
                visit(item, f"{path}[{index}]")

    for index, document in enumerate(result.get("documents") or [], start=1):
        visit({"fields": document.get("fields") or {}}, f"documents[{index}]")
    return values


def _print_comparison(results: dict[str, dict[str, Any]]) -> None:
    summaries = {name: _summary(result) for name, result in results.items()}
    columns = [
        "status", "totalMs", "extractionMs", "aiExecuted", "input", "output", "total",
        "effective", "fallback", "compactCalls", "legacyCalls",
    ]
    print("\nCOMPARACIÓN REAL")
    print(f"{'métrica':<18} {'compact':>18} {'legacy':>18}")
    for column in columns:
        print(f"{column:<18} {str(summaries['compact'][column]):>18} {str(summaries['legacy'][column]):>18}")

    compact_fields = _field_values(results["compact"])
    legacy_fields = _field_values(results["legacy"])
    paths = sorted(set(compact_fields) | set(legacy_fields))
    differences = [path for path in paths if compact_fields.get(path) != legacy_fields.get(path)]
    print(f"\nValores comparados: {len(paths)}; diferencias: {len(differences)}")
    for path in differences[:30]:
        print(f"- {path}: compact={compact_fields.get(path)!r} | legacy={legacy_fields.get(path)!r}")
    if len(differences) > 30:
        print(f"- ... {len(differences) - 30} diferencias adicionales")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archivo", type=Path)
    parser.add_argument("tipo", help="TYPE_ID declarado; mantiene equivalente la prueba")
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument("--stack")
    parser.add_argument("--model")
    parser.add_argument(
        "--extraction-mode", choices=("catalog", "model", "hybrid", "deterministic"),
        default="catalog",
    )
    parser.add_argument("--occurrence-mode", choices=("single", "multiple"))
    parser.add_argument("--meta", action="append", default=[], metavar="CLAVE=VALOR")
    parser.add_argument("--yes", action="store_true", help="omite confirmación del doble costo real")
    args = parser.parse_args([arg for arg in sys.argv[1:] if arg != "--"])
    args.archivo = args.archivo.expanduser().resolve()
    if not args.archivo.is_file():
        parser.error(f"No existe el archivo: {args.archivo}")
    if not args.yes:
        answer = input("Se procesará el documento DOS veces en AWS (costo real). ¿Continuar? [y/N] ")
        if answer.lower() != "y":
            print("Cancelado.")
            return 1

    results: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="idp-formato-") as temp:
        destination = Path(temp)
        for response_format in ("compact", "legacy"):
            print(f"\n=== {response_format.upper()} ===")
            document_id = _send(args, response_format)
            results[response_format] = _wait_result(args, document_id, destination)
        _print_comparison(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
