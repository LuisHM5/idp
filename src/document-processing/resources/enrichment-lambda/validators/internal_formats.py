"""Internal formats validator module.

Handles validation for: FORMATO_INTERNO_ALTA, FORMATO_INTERNO_BAJA, FORMATO_INTERNO_CAMBIO_DATOS.
"""

from typing import Dict, List

from document_catalog import CheckResult, ValidationRule
from validators.base import validate, validate_required_fields


def validate_document(
    entity_map: Dict[str, str], rules: List[ValidationRule], required_fields: List[str]
) -> List[CheckResult]:
    """Validate an internal format document.

    Applies:
    1. Required fields check
    2. Rule-based validation (FIRMA_PRESENTE boolean_true, RFC/CURP regex)
    """
    results: List[CheckResult] = []

    # Check required fields
    results.extend(validate_required_fields(entity_map, required_fields))

    # Apply catalog-defined rules (boolean_true, regex)
    results.extend(validate(entity_map, rules))

    return results
