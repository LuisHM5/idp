#!/usr/bin/env python3
"""Suite gratuita con JUnit, cobertura e histórico local inmutable."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from quality_history import DEFAULT_HISTORY, create_run, utc_now, write_record

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "tests" / "quality-baseline.json"


def junit_totals(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"tests": 0, "failures": 1, "errors": 0, "skipped": 0}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if suites:
        return {key: sum(int(s.get(key, 0)) for s in suites) for key in ("tests", "failures", "errors", "skipped")}
    cases = list(root.findall(".//testcase"))
    return {
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }


def run(command: list[str], log: Path, record: dict, cwd: Path = REPO) -> int:
    started = time.monotonic()
    proc = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write_text(proc.stdout, encoding="utf-8")
    record["commands"].append({
        "argv": command, "exitCode": proc.returncode,
        "durationSeconds": round(time.monotonic() - started, 3), "log": str(log.name),
    })
    print(proc.stdout, end="")
    return proc.returncode


def python_coverage(path: Path) -> tuple[float | None, float | None]:
    if not path.exists():
        return None, None
    totals = json.loads(path.read_text(encoding="utf-8")).get("totals") or {}
    line = totals.get("percent_covered")
    branches = totals.get("num_branches") or 0
    branch = (totals.get("covered_branches", 0) / branches * 100) if branches else None
    return line, branch


def lcov_coverage(path: Path) -> tuple[float | None, float | None]:
    totals = {"LF": 0, "LH": 0, "BRF": 0, "BRH": 0}
    if not path.exists():
        return None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        if key in totals:
            totals[key] += int(value)
    line_pct = totals["LH"] / totals["LF"] * 100 if totals["LF"] else None
    branch_pct = totals["BRH"] / totals["BRF"] * 100 if totals["BRF"] else None
    return line_pct, branch_pct


def coverage_gate(current: dict, baseline_path: Path, update: bool) -> list[str]:
    if update or not baseline_path.exists():
        baseline_path.write_text(json.dumps({"schemaVersion": 1, "tolerancePoints": 0.5, **current}, indent=2) + "\n", encoding="utf-8")
        return []
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    tolerance = float(baseline.get("tolerancePoints", 0.5))
    failures = []
    for runtime in ("python", "node"):
        for metric in ("lines", "branches"):
            old, new = (baseline.get(runtime) or {}).get(metric), (current.get(runtime) or {}).get(metric)
            if old is not None and (new is None or new < old - tolerance):
                failures.append(f"cobertura {runtime}.{metric}: {new} < baseline {old} - {tolerance}")
    return failures


def command_junit(path: Path, name: str, exit_code: int, skipped: bool = False) -> None:
    suite = ET.Element("testsuite", name=name, tests="1", failures="1" if exit_code else "0",
                       skipped="1" if skipped else "0")
    case = ET.SubElement(suite, "testcase", classname="quality-contract", name=name)
    if skipped:
        ET.SubElement(case, "skipped", message="Docker no está disponible; se validó build/TypeScript")
    elif exit_code:
        ET.SubElement(case, "failure", message=f"command exited {exit_code}")
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=("local", "dev", "prod"), default="local")
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY))
    parser.add_argument("--update-baseline", action="store_true", help="acepta explícitamente la cobertura actual")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])
    run_dir, record = create_run("quality-free", args.env, Path(args.history_dir))
    started = time.monotonic()
    py_junit = run_dir / "junit" / "python.xml"
    py_cov = run_dir / "coverage" / "python.json"
    py_html = run_dir / "coverage" / "python-html"
    node_junit = run_dir / "junit" / "node.xml"
    node_lcov = run_dir / "coverage" / "node.lcov"
    replay_junit = run_dir / "junit" / "replay.xml"
    infra_junit = run_dir / "junit" / "infrastructure.xml"

    py = run([
        str(REPO / ".venv" / "bin" / "python"), "-m", "pytest", "tests/unit", "-q",
        f"--junitxml={py_junit}", "--cov=src/document-processing/resources/enrichment-lambda",
        "--cov=src/document-processing/resources/async-failure-lambda", "--cov-branch",
        f"--cov-report=json:{py_cov}", f"--cov-report=html:{py_html}", "--cov-report=term",
    ], run_dir / "logs" / "python.log", record)
    node = run([
        "node", "--test", "--experimental-test-coverage",
        "--test-reporter=junit", f"--test-reporter-destination={node_junit}",
        "--test-reporter=lcov", f"--test-reporter-destination={node_lcov}",
        "tests/unit/node/*.test.mjs",
    ], run_dir / "logs" / "node.log", record)
    replay = run([
        str(REPO / ".venv" / "bin" / "python"), "scripts/replay-real-tests.py",
        "--history-dir", args.history_dir, "--junit-out", str(replay_junit),
    ], run_dir / "logs" / "replay.log", record)
    docker = subprocess.run(["docker", "info"], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if docker.returncode == 0:
        infrastructure = run(["pnpm", "test:encryption"], run_dir / "logs" / "infrastructure.log", record)
        command_junit(infra_junit, "cdk-encryption-contract", infrastructure)
    else:
        infrastructure = run(["pnpm", "build"], run_dir / "logs" / "infrastructure.log", record)
        command_junit(infra_junit, "cdk-encryption-contract", infrastructure, skipped=not infrastructure)
        if infrastructure == 0:
            print("SKIP: contrato CDK de cifrado requiere Docker; build/TypeScript sí fue validado.")

    py_totals, node_totals = junit_totals(py_junit), junit_totals(node_junit)
    replay_totals, infra_totals = junit_totals(replay_junit), junit_totals(infra_junit)
    line, branch = python_coverage(py_cov)
    node_line, node_branch = lcov_coverage(node_lcov)
    coverage_failures = coverage_gate({
        "python": {"lines": line, "branches": branch},
        "node": {"lines": node_line, "branches": node_branch},
    }, BASELINE, args.update_baseline)
    failures = sum(t["failures"] + t["errors"] for t in (py_totals, node_totals, replay_totals, infra_totals))
    record["summary"] = {
        "tests": sum(t["tests"] for t in (py_totals, node_totals, replay_totals, infra_totals)), "failures": failures,
        "skipped": sum(t["skipped"] for t in (py_totals, node_totals, replay_totals, infra_totals)),
        "lineCoverage": line, "branchCoverage": branch,
        "nodeLineCoverage": node_line, "nodeBranchCoverage": node_branch,
        "coverageGateFailures": coverage_failures,
        "durationSeconds": round(time.monotonic() - started, 3),
    }
    record["completedAt"] = utc_now()
    record["status"] = "PASSED" if py == 0 and node == 0 and replay == 0 and infrastructure == 0 and failures == 0 and not coverage_failures else "FAILED"
    write_record(run_dir, record)
    print(f"\nHistorial de calidad → {run_dir}")
    for failure in coverage_failures:
        print(f"REGRESIÓN: {failure}")
    return 0 if record["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
