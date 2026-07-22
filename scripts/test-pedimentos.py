#!/usr/bin/env python3
"""Integration runner: process the pedimento test docs with the full pipeline.

Runs the real pipeline (pypdf + Textract + Bedrock) against every PDF in
`test-docs/pedimentos docs/docs` and verifies the ground-truth expectations
in `tests/integration/expected_pedimentos.json`.

Requires AWS credentials with textract:DetectDocumentText and
bedrock:InvokeModel (Converse). Costs a few cents per full run (Textract on
~54 scanned pages + Haiku on everything else).

Usage:
    .venv/bin/python scripts/test-pedimentos.py                # all docs
    .venv/bin/python scripts/test-pedimentos.py --file B2401416.pdf
    .venv/bin/python scripts/test-pedimentos.py --save results/  # dump JSONs
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAMBDA_DIR = REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

DOCS_DIR = REPO / "test-docs" / "pedimentos docs" / "docs"
EXPECTED_FILE = REPO / "tests" / "integration" / "expected_pedimentos.json"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _norm_str(v) -> str:
    return str(v).strip().upper()


def _match_field(doc_fields: dict, entities: dict, name: str, expected) -> tuple[bool, str]:
    """Check one expected field. `name` may be 'A|B' (any alias matches).

    Compares against the field's normalizedValue (fallback extractedValue).
    Numbers use 0.01 tolerance.
    """
    for alias in name.split("|"):
        slot = doc_fields.get(alias) or {}
        value = slot.get("normalizedValue")
        if value in (None, ""):
            value = slot.get("extractedValue")
        if value in (None, ""):
            value = entities.get(alias)
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


def _check_doc_expectation(exp: dict, documents: list) -> tuple[bool, list[str]]:
    """Find a document matching the expectation and verify its fields."""
    from pipeline.normalize import pedimento_consecutivo

    allowed_types = exp.get("type", [])
    candidates = [d for d in documents if d.get("documentType") in allowed_types] or []
    if not candidates:
        found = [d.get("documentType") for d in documents]
        return False, [f"no document of type {allowed_types} (found: {found})"]

    best_notes: list[str] = []
    for doc in candidates:
        notes: list[str] = []
        ok = True
        fields = doc.get("fields", {})
        entities = doc.get("extractedEntities", {})

        for fname, fexpected in (exp.get("fields") or {}).items():
            passed, note = _match_field(fields, entities, fname, fexpected)
            notes.append(("✓ " if passed else "✗ ") + note)
            ok = ok and passed

        if "pedimento_consecutivo" in exp:
            refs = [
                entities.get(n)
                for n in ("NUMERO_PEDIMENTO_NORMALIZADO", "NUMERO_PEDIMENTO", "PEDIMENTO_REFERENCE")
            ]
            consecutivos = {pedimento_consecutivo(r) for r in refs if r} - {None}
            want = exp["pedimento_consecutivo"]
            passed = want in consecutivos
            notes.append(("✓ " if passed else "✗ ") + f"pedimento_consecutivo {consecutivos or '(none)'} vs {want}")
            ok = ok and passed

        for req in exp.get("required_present", []):
            slot = fields.get(req) or {}
            present = slot.get("extractedValue") not in (None, "", []) or entities.get(req) not in (None, "", [])
            notes.append(("✓ " if present else "✗ ") + f"{req} present" if present else f"✗ {req} MISSING")
            ok = ok and present

        if ok:
            return True, [f"matched {doc.get('documentType')} (p{doc.get('pageStart')}-{doc.get('pageEnd')}, status={doc.get('status')})"] + notes
        if not best_notes or sum(n.startswith("✓") for n in notes) > sum(n.startswith("✓") for n in best_notes):
            best_notes = [f"closest: {doc.get('documentType')} (p{doc.get('pageStart')}-{doc.get('pageEnd')})"] + notes
    return False, best_notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="process only this file (substring match)")
    parser.add_argument("--docs", default=str(DOCS_DIR), help="docs directory")
    parser.add_argument("--save", help="directory to dump full result JSONs")
    parser.add_argument("--model", help="Bedrock model id override")
    args = parser.parse_args()

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

        file_ok = True
        for exp in spec.get("expect_documents", []):
            ok, notes = _check_doc_expectation(exp, result.get("documents", []))
            color = GREEN if ok else RED
            print(f"  {color}{'PASS' if ok else 'FAIL'}{RESET} expectation {exp.get('type')}")
            for n in notes:
                print(f"      {n}")
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
        # Haiku 4.5 pricing reference: $1/M input, $5/M output (verify in AWS calculator)
        approx = inp / 1e6 * 1.0 + out / 1e6 * 5.0
        print(f"Bedrock usage: {inp:,} in / {out:,} out tokens (~${approx:.3f} extraction)")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
