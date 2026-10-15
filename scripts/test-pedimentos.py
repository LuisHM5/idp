#!/usr/bin/env python3
"""Integration runner: process the pedimento test docs with the full pipeline.

Runs the real pipeline (pypdf + Textract + Bedrock) against every PDF in
`test-docs/pedimentos docs/docs` and verifies the ground-truth expectations
in `tests/integration/expected_pedimentos.json`.

Requires AWS credentials with textract:DetectDocumentText and
bedrock:InvokeModel (Converse). Costs a few cents per full run (Textract on
~54 scanned pages + Claude Haiku 5.5 on everything else).

Usage:
    .venv/bin/python scripts/test-pedimentos.py                # all docs
    .venv/bin/python scripts/test-pedimentos.py --file B2401416.pdf
    .venv/bin/python scripts/test-pedimentos.py --save results/  # dump JSONs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
LAMBDA_DIR = REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

DOCS_DIR = REPO / "test-docs" / "pedimentos docs" / "docs"
EXPECTED_FILE = REPO / "tests" / "integration" / "expected_pedimentos.json"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _norm_str(v) -> str:
    return str(v).strip().upper()


# Nombres mixtos incluidos: PEDIMENTO_ERP usa las columnas del ERP
# (`No_Pedimento`, `FACTURAS[0].Factura`).
_PATH_SEG_RE = re.compile(r"^([A-Za-z0-9_]+)(?:\[(\d+)\])?$")


def _leaf_value(entities: dict, path: str) -> Any:
    """Resolve a dotted/indexed path (e.g. 'CUADRO_LIQUIDACION.TOTAL',
    'FACTURAS[0].NUMERO_FACTURA') against `doc["extractedEntities"]`'s
    field-centric-DSL recursive shape (see validators/engine.py's
    resolve_field_tree docstring) and return the leaf's plain value.

    Shape: scalar leaf -> {value, confidence, valid, reason, source};
    object -> {valid, reason, fields: {...}}; array -> {items: [...]} (each
    item shaped like an object node). Returns None when the path doesn't
    resolve at all — a ground-truth typo and a genuine extraction miss look
    the same on purpose, since both need a human to look.
    """
    node: Any = entities
    for i, seg in enumerate(path.split(".")):
        m = _PATH_SEG_RE.match(seg)
        if not m or not isinstance(node, dict):
            return None
        name, index = m.group(1), m.group(2)
        node = node.get(name) if i == 0 else (node.get("fields") or {}).get(name)
        if node is None:
            return None
        if index is not None:
            items = node.get("items") if isinstance(node, dict) else None
            if not isinstance(items, list) or int(index) >= len(items):
                return None
            node = items[int(index)]
    return node.get("value") if isinstance(node, dict) and "value" in node else None


def _match_field(entities: dict, name: str, expected) -> tuple[bool, str]:
    """Check one expected field. `name` may be 'A|B' (any alias matches) and
    may be a dotted/indexed path (see `_leaf_value`). Numbers use 0.01
    tolerance."""
    for alias in name.split("|"):
        value = _leaf_value(entities, alias)
        if value in (None, "", []):
            continue
        if isinstance(expected, (int, float)):
            try:
                got = float(str(value).replace(",", "").replace("$", ""))
            except ValueError:
                continue
            if abs(got - float(expected)) <= 0.01:
                return True, f"{alias}={got}"
            return False, f"{alias}={got} != {expected}"
        if _norm_str(value) == _norm_str(expected):
            return True, f"{alias}={value}"
        return False, f"{alias}={value!r} != {expected!r}"
    return False, f"{name} not extracted"


def _check_doc_expectation(exp: dict, documents: list) -> tuple[bool, list[str], dict | None]:
    """Find a document matching the expectation and verify its fields.

    Returns (passed, notes, matched_doc) — `matched_doc` is the best-matching
    document dict (or None if no document of the expected type exists at
    all), so callers can additionally inspect its own validation-engine
    `checks` for failures that don't show up as a ground-truth field
    mismatch (e.g. a cross-field formula rule) — see `_print_engine_checks`.
    """
    from pipeline.normalize import pedimento_consecutivo

    allowed_types = exp.get("type", [])
    candidates = [d for d in documents if d.get("documentType") in allowed_types] or []
    if not candidates:
        found = [d.get("documentType") for d in documents]
        return False, [f"no document of type {allowed_types} (found: {found})"], None

    best_notes: list[str] = []
    best_doc: dict | None = None
    for doc in candidates:
        notes: list[str] = []
        ok = True
        entities = doc.get("extractedEntities", {})

        for fname, fexpected in (exp.get("fields") or {}).items():
            passed, note = _match_field(entities, fname, fexpected)
            notes.append(("✓ " if passed else "✗ ") + note)
            ok = ok and passed

        if "pedimento_consecutivo" in exp:
            refs = [
                _leaf_value(entities, n)
                for n in ("NUMERO_PEDIMENTO_NORMALIZADO", "NUMERO_PEDIMENTO", "PEDIMENTO_REFERENCE")
            ]
            consecutivos = {pedimento_consecutivo(r) for r in refs if r} - {None}
            want = exp["pedimento_consecutivo"]
            passed = want in consecutivos
            notes.append(("✓ " if passed else "✗ ") + f"pedimento_consecutivo {consecutivos or '(none)'} vs {want}")
            ok = ok and passed

        for req in exp.get("required_present", []):
            present = _leaf_value(entities, req) not in (None, "", [])
            notes.append(("✓ " if present else "✗ ") + (f"{req} present" if present else f"{req} MISSING"))
            ok = ok and present

        if ok:
            return True, [f"matched {doc.get('documentType')} (p{doc.get('pageStart')}-{doc.get('pageEnd')}, status={doc.get('status')})"] + notes, doc
        if not best_notes or sum(n.startswith("✓") for n in notes) > sum(n.startswith("✓") for n in best_notes):
            best_notes = [f"closest: {doc.get('documentType')} (p{doc.get('pageStart')}-{doc.get('pageEnd')})"] + notes
            best_doc = doc
    return False, best_notes, best_doc


def evaluate_expectation(exp: dict, documents: list) -> dict:
    """Versión estructurada del comparador para JUnit, histórico y reportes.

    Los valores expected/actual se conservan en el artefacto privado del caso;
    el dashboard usa únicamente los contadores y nombres de campo.
    """
    allowed_types = exp.get("type", [])
    candidates = [d for d in documents if d.get("documentType") in allowed_types]
    if not candidates:
        return {
            "passed": False, "classificationPassed": False,
            "expectedTypes": allowed_types, "actualTypes": [d.get("documentType") for d in documents],
            "fieldChecks": [], "requiredChecks": [], "matchedType": None,
        }

    scored = []
    for doc in candidates:
        from pipeline.normalize import pedimento_consecutivo
        entities = doc.get("extractedEntities", {})
        fields = []
        for name, expected in (exp.get("fields") or {}).items():
            passed, note = _match_field(entities, name, expected)
            actual = next((_leaf_value(entities, alias) for alias in name.split("|")
                           if _leaf_value(entities, alias) not in (None, "", [])), None)
            fields.append({"field": name, "passed": passed, "expected": expected, "actual": actual, "detail": note})
        required = [{"field": name, "passed": _leaf_value(entities, name) not in (None, "", [])}
                    for name in exp.get("required_present", [])]
        references = []
        if exp.get("pedimento_consecutivo"):
            found = {pedimento_consecutivo(_leaf_value(entities, name))
                     for name in ("NUMERO_PEDIMENTO_NORMALIZADO", "NUMERO_PEDIMENTO", "PEDIMENTO_REFERENCE")}
            found.discard(None)
            references.append({"field": "pedimento_consecutivo", "passed": exp["pedimento_consecutivo"] in found,
                               "expected": exp["pedimento_consecutivo"], "actual": sorted(found)})
        score = sum(x["passed"] for x in fields + required + references)
        scored.append((score, doc, fields, required, references))
    _, doc, fields, required, references = max(scored, key=lambda row: row[0])
    expected_status = exp.get("expected_status")
    status_passed = expected_status is None or doc.get("status") == expected_status
    passed = all(x["passed"] for x in fields + required + references) and status_passed
    return {
        "passed": passed, "classificationPassed": True, "expectedTypes": allowed_types,
        "actualTypes": [d.get("documentType") for d in documents], "matchedType": doc.get("documentType"),
        "fieldChecks": fields, "requiredChecks": required,
        "referenceChecks": references,
        "expectedStatus": expected_status, "actualStatus": doc.get("status"), "statusPassed": status_passed,
    }


def _print_engine_checks(doc: dict, out=print) -> None:
    """Print the matched document's own failing validation-engine checks.

    Separate signal from the ground-truth field comparisons above: a field
    can match its expected value exactly and the document can still fail a
    cross-field rule (e.g. a formula reconciling VALOR_ADUANA against other
    fields) — this is the "why is the overall status not APPROVED" view,
    straight from `validators/engine.py`'s own rule results. `out` lets the
    parallel E2E dataset runner buffer the lines per case.
    """
    failing = [c for c in doc.get("checks", []) if not c.get("valid") and not c.get("skipped")]
    if not failing:
        out(f"  {DIM}engine: no failing validation rules{RESET}")
        return
    out(f"  {YELLOW}engine: {len(failing)} failing validation rule(s) (status={doc.get('status')}){RESET}")
    for c in failing:
        mark = RED if c.get("severity") == "error" else YELLOW
        out(f"      {mark}✗ [{c.get('rule')}] {c.get('field')}: {c.get('message')}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="process only this file (substring match)")
    parser.add_argument("--docs", default=str(DOCS_DIR), help="docs directory")
    parser.add_argument("--save", help="directory to dump full result JSONs")
    parser.add_argument("--model", help="Bedrock model id override")
    # pnpm forwards a literal "--" separator; argparse must not choke on it
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    importer_rfc = expected.get("importer_rfc")
    expected_values = {"IMPORTER_RFC": importer_rfc} if importer_rfc else {}

    from pipeline.expediente import process_pdf

    docs_dir = Path(args.docs)
    pdfs = sorted(p for p in docs_dir.glob("*.pdf"))
    if args.file:
        pdfs = [p for p in pdfs if args.file.lower() in p.name.lower()]
    if not pdfs:
        print(f"no PDFs found in {docs_dir}")
        return 2

    save_dir = Path(args.save) if args.save else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    total_pass = total_fail = 0
    grand_usage: dict[str, int] = {}

    for pdf in pdfs:
        spec = expected.get("files", {}).get(pdf.name)
        print(f"\n{'=' * 70}\n{pdf.name}")
        started = time.time()
        try:
            result = process_pdf(
                pdf.read_bytes(),
                expected_values=expected_values,
                model_id=args.model,
                source_name=pdf.name,
            )
        except Exception as e:
            print(f"  {RED}PROCESSING ERROR: {e}{RESET}")
            total_fail += 1
            continue
        elapsed = time.time() - started

        for doc in result.get("documents", []):
            usage = (doc.get("extractionMeta") or {}).get("usage") or {}
            for k, v in usage.items():
                if isinstance(v, int):
                    grand_usage[k] = grand_usage.get(k, 0) + v

        pages = result.get("pages", {})
        print(
            f"  status={result.get('status')}  pages={pages.get('totalPages')} "
            f"(native={pages.get('nativePages')} ocr={pages.get('ocrPages')})  "
            f"{elapsed:.1f}s"
        )
        for doc in result.get("documents", []):
            print(
                f"  {DIM}p{doc.get('pageStart'):>3}-{doc.get('pageEnd'):<3} "
                f"{doc.get('documentType'):<34} {doc.get('status', '-'):<20} "
                f"conf={doc.get('confidence')}{RESET}"
            )
        for check in result.get("crossDocumentChecks", []):
            mark = GREEN + "✓" if check.get("valid") else (YELLOW + "!" if check.get("severity") == "warning" else RED + "✗")
            print(f"  {mark} [{check.get('rule')}] {check.get('message')}{RESET}")

        if save_dir:
            out = save_dir / f"{pdf.stem}.json"
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"  {DIM}result → {out}{RESET}")

        if not spec:
            print(f"  {YELLOW}no expectations defined for this file — skipping asserts{RESET}")
            continue

        # A fixture may carry ground truth for several public mappings of the
        # same physical document (PEDIMENTO_ADUANAL and PEDIMENTO_ERP). This
        # runner auto-classifies, so evaluate only the mappings the run
        # actually produced; the declared-type E2E runner covers the others.
        actual_types = {d.get("documentType") for d in result.get("documents", [])}
        expectations = [
            exp for exp in spec.get("expect_documents", [])
            if actual_types.intersection(exp.get("type") or [])
        ]
        file_ok = bool(expectations)
        for exp in expectations:
            ok, notes, matched_doc = _check_doc_expectation(exp, result.get("documents", []))
            color = GREEN if ok else RED
            print(f"  {color}{'PASS' if ok else 'FAIL'}{RESET} expectation {exp.get('type')}")
            for n in notes:
                print(f"      {n}")
            if matched_doc:
                _print_engine_checks(matched_doc)
            file_ok = file_ok and ok

        if file_ok:
            total_pass += 1
            print(f"  {GREEN}FILE PASS{RESET}")
        else:
            total_fail += 1
            print(f"  {RED}FILE FAIL{RESET}")

    print(f"\n{'=' * 70}")
    print(f"RESULT: {total_pass} passed, {total_fail} failed of {total_pass + total_fail} files")
    if grand_usage:
        inp = grand_usage.get("inputTokens", 0)
        out = grand_usage.get("outputTokens", 0)
        # Claude Haiku 5.5 on Bedrock: $0.11/M input, $0.55/M output (pipeline/pricing.py)
        approx = inp / 1e6 * 0.11 + out / 1e6 * 0.55
        print(f"Bedrock usage: {inp:,} in / {out:,} out tokens (~${approx:.3f} extraction)")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
