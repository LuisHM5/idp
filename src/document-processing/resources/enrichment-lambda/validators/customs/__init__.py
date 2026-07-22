"""Customs (pedimentos) validator modules.

Every module referenced by the customs catalog (`validators.customs.*`)
delegates to the dynamic engine — the rules live entirely in the catalog
JSON. The per-module files exist so `validator_module` resolution keeps
working and so type-specific logic can be added later without config changes.
"""

from typing import Any, Dict, List

from document_catalog import CheckResult, TypeRegistryEntry, ValidationRule
from validators.engine import ValidationContext, run_rules


def validate_document(
    entity_map: Dict[str, Any],
    rules: List[ValidationRule],
    required_fields: List[str],
) -> List[CheckResult]:
    """Handler-compatible entry point (legacy signature).

    Cross-document and app-expectation rules are skipped here (no context);
    the expediente pipeline calls the engine directly with full context.
    """
    entry = TypeRegistryEntry(
        type_id="_adhoc",
        extraction_fields=list(entity_map.keys()),
        required_fields=required_fields,
        validation_rules=rules,
    )
    checks = run_rules(entity_map, entry, ValidationContext())
    return [
        CheckResult(field=c.field, rule=c.rule, valid=c.valid or c.skipped, message=c.message)
        for c in checks
    ]
