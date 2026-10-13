#!/usr/bin/env python3
"""Valida los ``deterministic_extraction`` activos sin llamar AWS.

Se ejecuta antes de build/sync para que una regex, grupo, transform, perfil o
ruta de campo inválida no llegue a DynamoDB y falle silenciosamente en runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CATALOGS = REPO / "catalogs"
LAMBDA = REPO / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA))

import document_catalog  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline.deterministic_text import validate_entry  # noqa: E402


def active_files() -> list[Path]:
    return sorted(
        path for path in CATALOGS.rglob("*.json")
        if not path.name.startswith("_") and "save" not in path.relative_to(CATALOGS).parts
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", action="append", default=[], help="catálogo concreto; repetible")
    args = parser.parse_args()
    files = [Path(value).resolve() for value in args.file] if args.file else active_files()
    shared_path = CATALOGS / "_shared.json"
    shared = json.loads(shared_path.read_text(encoding="utf-8")) if shared_path.exists() else {}
    common = shared.get("_common_item_fields") or {}
    # _parse_field_tree resolves top-level $presets through these module
    # registries in production. Mirror that loader state so lint sees the
    # exact same field tree as the Lambda, including preset-provided fields.
    document_catalog._COMMON_ITEM_FIELDS = common
    document_catalog._FIELD_PRESETS = shared.get("_field_presets") or {}
    failures: list[str] = []
    checked = 0

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - CLI reports every bad file together
            failures.append(f"{path}: JSON inválido: {exc}")
            continue
        entries = [(name, raw) for name, raw in data.items() if not name.startswith("_")]
        for type_id, raw in entries:
            if not isinstance(raw, dict):
                continue
            response_format = raw.get("ai_response_format")
            if response_format is not None and response_format not in {"compact", "legacy"}:
                failures.append(
                    f"{path.relative_to(REPO)}:{type_id}: ai_response_format debe ser compact o legacy"
                )
            if not isinstance(raw.get("deterministic_extraction"), dict):
                continue
            checked += 1
            try:
                entry = _parse_entry(type_id, raw, common_item_fields=common)
                failures.extend(
                    f"{path.relative_to(REPO)}:{type_id}: {problem}"
                    for problem in validate_entry(entry)
                )
            except Exception as exc:  # noqa: BLE001 - malformed catalog must block sync
                failures.append(f"{path.relative_to(REPO)}:{type_id}: no se pudo analizar: {exc}")

    if failures:
        print("Mapeos determinísticos inválidos:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Mapeos determinísticos válidos: {checked} tipo(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
