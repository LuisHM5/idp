"""Fiscal document validator module.

Handles validation for: CONSTANCIA_SITUACION_FISCAL.
"""

from typing import Dict, List

from document_catalog import CheckResult, ValidationRule
from validators.base import validate, validate_required_fields, validate_rfc


def validate_document(
    entity_map: Dict[str, str], rules: List[ValidationRule], required_fields: List[str]
) -> List[CheckResult]:
    """Validate a fiscal document (CSF).

    Applies:
    1. Required fields check
    2. RFC validation (supports both physical person and legal entity)
    3. Rule-based validation from catalog (CURP regex, etc.)
    """
    results: List[CheckResult] = []

    # Check required fields
    results.extend(validate_required_fields(entity_map, required_fields))

    # Special RFC handling: try both patterns (persona fisica and moral)
    rfc_value = entity_map.get("RFC", "")
    if rfc_value and rfc_value.strip():
        rfc_result = validate_rfc(rfc_value.strip().upper())
        results.append(rfc_result)
        # Remove RFC from rules to avoid double-checking
        rules = [r for r in rules if r.field != "RFC"]

    # Apply remaining catalog-defined rules
    results.extend(validate(entity_map, rules))

    return results
