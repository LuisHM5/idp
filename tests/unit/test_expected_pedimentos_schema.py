"""Offline schema sanity check for tests/integration/expected_pedimentos.json.

Catches typos in the ground-truth fixture (e.g. `DESTINO` instead of
`DESTINO_PAIS`) before they burn a real Bedrock/Textract run and look like
"the pipeline didn't extract the field" when the bug is actually in the
fixture. Finds `<TYPE_ID>.json` recursively in the active catalog tree and
reads it with `catalogs/_shared.json` (the source of truth), not the generated
bundle, so it works without `pnpm build`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
CATALOGS_DIR = REPO / "catalogs"
EXPECTED_FILE = REPO / "tests" / "integration" / "expected_pedimentos.json"

_SEGMENT_RE = re.compile(r"^([A-Za-z0-9_]+)(\[\d+\])?$")


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _catalog_path(type_id: str) -> Path:
    matches = [
        path
        for path in CATALOGS_DIR.rglob(f"{type_id}.json")
        if "save" not in path.relative_to(CATALOGS_DIR).parts
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one active catalog for {type_id}, found {len(matches)}"
        )
    return matches[0]


def _load_type_fields(type_id: str) -> dict[str, Any]:
    path = _catalog_path(type_id)
    entry = _load_json(path)[type_id]
    if "fields" not in entry:
        raise AssertionError(
            f"{path.relative_to(REPO)} uses the legacy DSL (no 'fields' key) — "
            "this schema check only supports the field-centric DSL"
        )
    return entry["fields"]


def _common_item_fields() -> dict[str, Any]:
    shared = _load_json(CATALOGS_DIR / "_shared.json")
    return {
        k: v
        for k, v in shared.get("_common_item_fields", {}).items()
        if not k.startswith("__")
    }


def field_path_exists(fields_tree: dict[str, Any], path: str, common_item_fields: dict[str, Any]) -> bool:
    """Resolve a dot/bracket field path (e.g. `FACTURAS[0].FECHA`,
    `CUADRO_LIQUIDACION.TOTAL`, `PATENTE`) against a field-centric `fields` tree.
    """
    segments = path.split(".")
    node = fields_tree
    for i, seg in enumerate(segments):
        m = _SEGMENT_RE.match(seg)
        if not m:
            return False
        name, indexed = m.group(1), m.group(2) is not None
        if name not in node:
            return False
        field_def = node[name]
        is_last = i == len(segments) - 1
        if is_last:
            return True
        if indexed:
            if field_def.get("type") != "array":
                return False
            item_fields = field_def.get("item_fields", {})
            node = {**{k: v for k, v in item_fields.items() if k != "$common"}}
            for common_name in item_fields.get("$common", []):
                node[common_name] = common_item_fields.get(common_name, {})
        else:
            if field_def.get("type") != "object":
                return False
            node = field_def.get("fields", {})
    return True


@pytest.fixture(scope="module")
def expected_data() -> dict:
    return _load_json(EXPECTED_FILE)


def _iter_doc_expectations(expected_data: dict):
    for file_name, entry in expected_data.get("files", {}).items():
        for i, doc in enumerate(entry.get("expect_documents", [])):
            yield file_name, i, doc


def test_expected_pedimentos_json_is_valid(expected_data: dict) -> None:
    assert "files" in expected_data
    assert isinstance(expected_data["files"], dict)
    assert expected_data["files"], "expected_pedimentos.json has no files entries"


def test_every_paid_fixture_has_a_sha256(expected_data: dict) -> None:
    for file_name, entry in expected_data["files"].items():
        assert re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")), file_name


def test_expect_documents_have_at_least_one_declared_type(expected_data: dict) -> None:
    for file_name, i, doc in _iter_doc_expectations(expected_data):
        types = doc.get("type")
        assert types, f"{file_name} expect_documents[{i}] has no 'type'"


def test_pedimento_consecutivo_is_seven_digits(expected_data: dict) -> None:
    for file_name, i, doc in _iter_doc_expectations(expected_data):
        consecutivo = doc.get("pedimento_consecutivo")
        if consecutivo is None:
            continue
        assert re.fullmatch(r"\d{7}", consecutivo), (
            f"{file_name} expect_documents[{i}].pedimento_consecutivo="
            f"{consecutivo!r} must be exactly 7 digits"
        )


def test_field_names_exist_in_catalog(expected_data: dict) -> None:
    common_item_fields = _common_item_fields()
    fields_tree_cache: dict[str, Any] = {}
    errors: list[str] = []

    for file_name, i, doc in _iter_doc_expectations(expected_data):
        for type_id in doc.get("type", []):
            if type_id not in fields_tree_cache:
                fields_tree_cache[type_id] = _load_type_fields(type_id)
            fields_tree = fields_tree_cache[type_id]

            paths = list(doc.get("fields", {}).keys()) + list(doc.get("required_present", []))
            for path in paths:
                if not field_path_exists(fields_tree, path, common_item_fields):
                    errors.append(
                        f"{file_name} expect_documents[{i}]: field path "
                        f"'{path}' does not exist in {_catalog_path(type_id).relative_to(REPO)}"
                    )

    assert not errors, "\n" + "\n".join(errors)
