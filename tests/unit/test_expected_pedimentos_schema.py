"""Offline sanity check for tests/integration/expected_pedimentos.json.

Catches a real mistake before it costs money: a typo'd or renamed field name
in the ground-truth fixture silently reads as "not extracted" when
scripts/test-pedimentos.py runs against real Bedrock/Textract (pnpm
test:pedimentos, ~$1/run) — the fixture is wrong, not the pipeline, but
nothing distinguishes the two without this check. This test costs nothing
and needs no AWS credentials, so it runs as part of the free `pnpm
test:unit` suite and should be trusted before spending on the paid
integration run.

Only validates expectations whose every allowed `type` is present in the
locally bundled catalog (config/catalogs.json, built from catalogs/*.json) —
this repo currently bundles PEDIMENTO_ADUANAL only, so entries referencing
customs sub-types that live only in the DynamoDB hot-reload catalog are
skipped rather than asserted against (there's nothing local to check them
against, and that's a deployment-config fact, not a fixture bug).
"""

import json
from pathlib import Path

import document_catalog as dc

REPO = Path(__file__).resolve().parent.parent.parent
EXPECTED_FILE = REPO / "tests" / "integration" / "expected_pedimentos.json"


def setup_module(module):
    dc.reload_catalog()


def _load_expected() -> dict:
    return json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))


def _known_fields(type_id: str) -> set[str]:
    """Every name resolve_fields()/matching logic can compare against for a
    type: scalar extraction_fields, plus repeatable-array subfield paths in
    "ARRAY.SUBFIELD" form (how expected_pedimentos.json's "A|B" aliases and
    scripts/test-pedimentos.py's dot-path lookups reference them)."""
    entry = dc.get_type_entry(type_id)
    names = set(entry.extraction_fields)
    for array_name, subfields in entry.repeatable_fields.items():
        names.add(array_name)
        for sf in subfields:
            names.add(f"{array_name}.{sf}")
    return names


class TestExpectedPedimentosSchema:
    def test_file_is_valid_json(self):
        _load_expected()  # raises on malformed JSON

    def test_field_names_exist_in_catalog(self):
        expected = _load_expected()
        catalog_type_ids = set(dc.get_all_types().keys())
        errors: list[str] = []

        for filename, spec in expected.get("files", {}).items():
            for i, exp in enumerate(spec.get("expect_documents", [])):
                allowed_types = exp.get("type", [])
                # Skip when the catalog doesn't have (any of) these types
                # bundled locally — nothing to validate names against.
                locally_known = [t for t in allowed_types if t in catalog_type_ids]
                if not locally_known:
                    continue

                # A field is valid if it exists on ANY of the locally-known
                # allowed types, matching how the runner matches a document
                # of any allowed type against the expectation.
                known = set()
                for t in locally_known:
                    known |= _known_fields(t)

                for fname in (exp.get("fields") or {}).keys():
                    aliases = fname.split("|")
                    if not any(a in known for a in aliases):
                        errors.append(
                            f"{filename} expect_documents[{i}].fields: "
                            f"'{fname}' not in {locally_known} schema "
                            f"(field_sets/extraction_fields/repeatable_fields)"
                        )

                for req in exp.get("required_present", []):
                    if req not in known:
                        errors.append(
                            f"{filename} expect_documents[{i}].required_present: "
                            f"'{req}' not in {locally_known} schema"
                        )

        assert not errors, "ground-truth fixture references unknown field names:\n" + "\n".join(errors)

    def test_pedimento_consecutivo_is_7_digits(self):
        """Catches a copy-paste mistake in the fixture itself (wrong length
        consecutivo would never match anything, silently failing every run)."""
        expected = _load_expected()
        for filename, spec in expected.get("files", {}).items():
            for i, exp in enumerate(spec.get("expect_documents", [])):
                if "pedimento_consecutivo" in exp:
                    value = exp["pedimento_consecutivo"]
                    assert value.isdigit() and len(value) == 7, (
                        f"{filename} expect_documents[{i}].pedimento_consecutivo "
                        f"= {value!r} is not exactly 7 digits"
                    )
