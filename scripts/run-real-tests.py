#!/usr/bin/env python3
"""Suite pagada: todo el corpus etiquetado, siempre por el E2E desplegado."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from quality_history import DEFAULT_HISTORY, create_run, sha256_file, utc_now, write_record

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "test-docs" / "pedimentos docs" / "docs"
EXPECTED = REPO / "tests" / "integration" / "expected_pedimentos.json"


def write_junit(path: Path, cases: list[dict]) -> None:
    suite = ET.Element("testsuite", name="real-e2e", tests=str(len(cases)),
                       failures=str(sum(not c.get("passed", False) for c in cases)))
    for case in cases:
        node = ET.SubElement(suite, "testcase", classname="real-e2e", name=case.get("file", "unknown"),
                             time=str(case.get("durationSeconds", 0)))
        if not case.get("passed"):
            fail = ET.SubElement(node, "failure", message="infraestructura o ground truth no cumplido")
            fail.text = json.dumps({"infraPassed": case.get("infraPassed"),
                                    "groundTruthPassed": case.get("groundTruthPassed")}, ensure_ascii=False)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=("dev", "prod"), required=True)
    parser.add_argument("--docs", default=str(DOCS))
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY))
    parser.add_argument("--wait", type=int, default=1200)
    parser.add_argument("--max-cost-usd", type=float, help="cancela si el máximo conservador estimado excede este monto")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--allow-prod", action="store_true")
    parser.add_argument("--skip-free", action="store_true", help="solo para diagnóstico; no recomendado")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])
    if args.env == "prod" and (not args.allow_prod or (args.yes and not os.environ.get("ALLOW_PROD_TESTS"))):
        print("Prod requiere --allow-prod; con --yes también requiere ALLOW_PROD_TESTS=1.", file=sys.stderr)
        return 2

    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    fixtures = []
    missing = []
    for name, spec in expected.get("files", {}).items():
        path = Path(args.docs) / name
        if not path.exists():
            missing.append(name)
        else:
            declared_hash = spec.get("sha256")
            actual_hash = sha256_file(path)
            if declared_hash and actual_hash != declared_hash:
                print(f"Fixture distinto al revisado: {name}\n  esperado {declared_hash}\n  actual   {actual_hash}", file=sys.stderr)
                return 2
            fixtures.append((path, spec))
    if missing:
        print("Faltan fixtures privados; no se iniciará ninguna llamada pagada:\n- " + "\n- ".join(missing), file=sys.stderr)
        return 2
    if not fixtures:
        print("No hay fixtures declarados.", file=sys.stderr)
        return 2

    print(f"Suite REAL E2E: {len(fixtures)} archivo(s) contra {args.env}.")
    print("Generará escrituras reales en S3, Lambda, DynamoDB, Bedrock y Textract.")
    estimate_low, estimate_high = len(fixtures) * 0.02, len(fixtures) * 0.35
    print(f"Costo orientativo del corpus etiquetado: ${estimate_low:.2f}–${estimate_high:.2f} USD (el uso real queda auditado).")
    if args.max_cost_usd is not None and estimate_high > args.max_cost_usd:
        print(f"El máximo conservador ${estimate_high:.2f} excede --max-cost-usd ${args.max_cost_usd:.2f}; cancelado.", file=sys.stderr)
        return 2
    if not args.yes and input("¿Continuar? [y/N] ").strip().lower() not in {"y", "yes", "s", "si", "sí"}:
        return 130

    if not args.skip_free:
        free = subprocess.run([str(REPO / ".venv/bin/python"), "scripts/run-quality-tests.py", "--env", args.env], cwd=REPO)
        if free.returncode:
            print("La suite gratuita falló; se cancela antes de gastar.", file=sys.stderr)
            return free.returncode

    run_dir, record = create_run("real-e2e", args.env, Path(args.history_dir))
    started = time.monotonic()
    cases = []
    for index, (pdf, spec) in enumerate(fixtures, 1):
        case_path = run_dir / "cases" / f"{index:03d}.json"
        log_path = run_dir / "logs" / f"{index:03d}-{pdf.stem}.log"
        print(f"\n[{index}/{len(fixtures)}] {pdf.name}")
        cmd = [str(REPO / ".venv/bin/python"), "scripts/test-e2e-pedimento.py",
               "--file", pdf.name, "--docs", str(Path(args.docs)), "--env", args.env,
               "--wait", str(args.wait), "--run-id", record["runId"], "--json-out", str(case_path)]
        if index == 1:
            cmd.append("--exercise-metadata")
        proc = subprocess.run(cmd, cwd=REPO, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log_path.write_text(proc.stdout, encoding="utf-8")
        print(proc.stdout, end="")
        if case_path.exists():
            case = json.loads(case_path.read_text(encoding="utf-8"))
        else:
            case = {"file": pdf.name, "passed": False, "infraPassed": False,
                    "groundTruthPassed": False, "error": "el E2E no produjo resumen"}
            case_path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        case["fixtureSha256"] = sha256_file(pdf)
        case["description"] = spec.get("description")
        case["exitCode"] = proc.returncode
        case_path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(case_path, 0o600)
        cases.append(case)
        record["cases"].append({"file": pdf.name, "path": str(case_path.relative_to(run_dir)),
                                "passed": bool(case.get("passed")), "documentId": case.get("documentId")})
        write_record(run_dir, record)

    field_checks = [x for c in cases for e in c.get("evaluations", [])
                    for x in e.get("fieldChecks", []) + e.get("referenceChecks", [])]
    required = [x for c in cases for e in c.get("evaluations", []) for x in e.get("requiredChecks", [])]
    classifications = [e for c in cases for e in c.get("evaluations", [])]
    passed = sum(bool(c.get("passed")) for c in cases)
    measured_costs = [c.get("costUsd") for c in cases if c.get("costUsd") is not None]
    record["summary"] = {
        "tests": len(cases), "failures": len(cases) - passed,
        "e2ePassRate": passed / len(cases),
        "classificationAccuracy": (sum(bool(e.get("classificationPassed")) for e in classifications) / len(classifications)) if classifications else None,
        "fieldAccuracy": (sum(bool(x.get("passed")) for x in field_checks) / len(field_checks)) if field_checks else None,
        "requiredPresenceRate": (sum(bool(x.get("passed")) for x in required) / len(required)) if required else None,
        "fieldChecks": len(field_checks), "requiredChecks": len(required),
        "estimatedCostRangeUsd": {"min": estimate_low, "max": estimate_high},
        "costUsd": sum(measured_costs) if measured_costs else None,
        "durationSeconds": round(time.monotonic() - started, 3),
    }
    record["completedAt"] = utc_now()
    record["status"] = "PASSED" if passed == len(cases) else "FAILED"
    write_junit(run_dir / "junit" / "real-e2e.xml", cases)
    write_record(run_dir, record)
    print(f"\nHistorial E2E → {run_dir}")
    report_proc = subprocess.run([str(REPO / ".venv/bin/python"), "scripts/reporte.py", "--env", args.env], cwd=REPO)
    record["reportExitCode"] = report_proc.returncode
    write_record(run_dir, record)
    return 0 if record["status"] == "PASSED" and report_proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
