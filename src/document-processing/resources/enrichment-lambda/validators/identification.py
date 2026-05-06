"""Identification document validator module.

Handles validation for: INE, PASAPORTE, CEDULA_PROFESIONAL.
"""

from typing import Dict, List

from document_catalog import CheckResult, ValidationRule
from validators.base import validate, validate_required_fields


def validate_document(
    entity_map: Dict[str, str], rules: List[ValidationRule], required_fields: List[str]
) -> List[CheckResult]:
    """Validate an identification document.

    Applies:
    1. Required fields check
    2. Rule-based validation (CURP regex, VIGENCIA date_expiry)
    """
    results: List[CheckResult] = []

    # Check required fields
    results.extend(validate_required_fields(entity_map, required_fields))

    # Apply catalog-defined rules (regex, date_expiry)
    results.extend(validate(entity_map, rules))

    return results
