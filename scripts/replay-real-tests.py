#!/usr/bin/env python3
"""Reevalúa gratis resultados E2E guardados contra el ground truth vigente."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXPECTED = REPO / "tests/integration/expected_pedimentos.json"
loader = importlib.util.spec_from_file_location("test_pedimentos_replay", REPO / "scripts/test-pedimentos.py")
tp = importlib.util.module_from_spec(loader)
assert loader and loader.loader
loader.loader.exec_module(tp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", default=str(REPO / "resultados-pruebas"))
    parser.add_argument("--junit-out", required=True)
    args = parser.parse_args()
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    artifacts = sorted(Path(args.history_dir).glob("*/**/cases/*.result.json"))
    # Una copia por fixture: el resultado más reciente es el contrafactual más útil.
    latest = {path.name.removesuffix(".result.json"): path for path in artifacts}
    cases = []
    for path in latest.values():
        raw = json.loads(path.read_text(encoding="utf-8"))
        summary_path = path.with_name(path.name.replace(".result.json", ".json"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        fixture = summary.get("file")
        spec = (expected.get("files") or {}).get(fixture) or {}
        evaluations = [tp.evaluate_expectation(item, raw.get("documents") or [])
                       for item in spec.get("expect_documents", [])]
        cases.append({"file": fixture, "passed": bool(evaluations) and all(x["passed"] for x in evaluations)})
    suite = ET.Element("testsuite", name="replay-real", tests=str(len(cases)), failures=str(sum(not c["passed"] for c in cases)))
    for case in cases:
        node = ET.SubElement(suite, "testcase", classname="replay-real", name=case["file"] or "unknown")
        if not case["passed"]:
            ET.SubElement(node, "failure", message="el resultado guardado ya no cumple el ground truth vigente")
    ET.ElementTree(suite).write(args.junit_out, encoding="utf-8", xml_declaration=True)
    print(f"Replay gratuito: {sum(c['passed'] for c in cases)}/{len(cases)} casos históricos cumplen.")
    return 0 if all(c["passed"] for c in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
