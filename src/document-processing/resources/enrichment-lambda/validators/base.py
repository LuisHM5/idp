"""Base validator interface and shared validation utilities.

This module provides:
- The validate() function signature that each validator module must implement
- Shared regex validation functions for RFC and CURP
- Date parsing and expiry checking helpers
- Required fields checking utility

All validator modules dispatch through the generic `validate` function which
applies each ValidationRule based on its rule_type.
"""

import re
from datetime import date, datetime
from typing import Dict, List

from document_catalog import (
    CURP_PATTERN,
    RFC_PATTERN_PERSONA_FISICA,
    RFC_PATTERN_PERSONA_MORAL,
    CheckResult,
    ValidationRule,
)

# Precompiled regex patterns for performance
_RFC_FISICA_RE = re.compile(RFC_PATTERN_PERSONA_FISICA)
_RFC_MORAL_RE = re.compile(RFC_PATTERN_PERSONA_MORAL)
_CURP_RE = re.compile(CURP_PATTERN)

# Supported date formats for parsing
_DATE_FORMATS = [
    "%Y-%m-%d",      # 2024-12-31
    "%d/%m/%Y",      # 31/12/2024
    "%d-%m-%Y",      # 31-12-2024
    "%Y/%m/%d",      # 2024/12/31
    "%d.%m.%Y",      # 31.12.2024
]


def validate_rfc(value: str) -> CheckResult:
    """Validate an RFC string against physical person and legal entity patterns.

    Physical person: 4 uppercase letters (including &, Ñ) + 6 digits + 3 alphanumeric = 13 chars
    Legal entity: 3 uppercase letters (including &, Ñ) + 6 digits + 3 alphanumeric = 12 chars

    Returns a CheckResult indicating whether the RFC is valid.
    """
    if _RFC_FISICA_RE.match(value):
        return CheckResult(
            field="RFC",
            rule="regex",
            valid=True,
            message="RFC matches physical person pattern",
        )
    if _RFC_MORAL_RE.match(value):
        return CheckResult(
            field="RFC",
            rule="regex",
            valid=True,
            message="RFC matches legal entity pattern",
        )
    return CheckResult(
        field="RFC",
        rule="regex",
        valid=False,
        message="RFC does not match physical person or legal entity pattern",
    )


def validate_curp(value: str) -> CheckResult:
    """Validate a CURP string against the official 18-character pattern.

    Pattern: 4 uppercase letters + 6 digits (date) + 1 letter (H/M) +
             2 uppercase letters (state) + 3 uppercase consonants +
             1 alphanumeric + 1 digit

    Returns a CheckResult indicating whether the CURP is valid.
    """
    if _CURP_RE.match(value):
        return CheckResult(
            field="CURP",
            rule="regex",
            valid=True,
            message="CURP matches official 18-character pattern",
        )
    return CheckResult(
        field="CURP",
        rule="regex",
        valid=False,
        message="CURP does not match official 18-character pattern",
    )


def parse_date(value: str) -> date | None:
    """Parse a date string trying multiple common formats.

    Supported formats: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, YYYY/MM/DD, DD.MM.YYYY

    Returns the parsed date or None if no format matches.
    """
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def validate_date_expiry(value: str, field_name: str = "VIGENCIA") -> CheckResult:
    """Validate that a date string represents a future date (not expired).

    Returns valid if the date is strictly in the future, invalid if today or past.
    Returns invalid with a descriptive message if the date cannot be parsed.
    """
    parsed = parse_date(value)
    if parsed is None:
        return CheckResult(
            field=field_name,
            rule="date_expiry",
            valid=False,
            message=f"Could not parse date value: {value}",
        )
    today = date.today()
    if parsed > today:
        return CheckResult(
            field=field_name,
            rule="date_expiry",
            valid=True,
            message="Document has not expired",
        )
    return CheckResult(
        field=field_name,
        rule="date_expiry",
        valid=False,
        message="Document has expired or expires today",
    )


def validate_date_max_age(value: str, max_years: int, field_name: str) -> CheckResult:
    """Validate that a date is not older than max_years from today.

    Returns valid if the date is within the allowed age, invalid if too old.
    """
    parsed = parse_date(value)
    if parsed is None:
        return CheckResult(
            field=field_name,
            rule="date_max_age",
            valid=False,
            message=f"Could not parse date value: {value}",
        )
    today = date.today()
    cutoff = today.replace(year=today.year - max_years)
    if parsed >= cutoff:
        return CheckResult(
            field=field_name,
            rule="date_max_age",
            valid=True,
            message=f"Document is within {max_years} year(s) of age",
        )
    return CheckResult(
        field=field_name,
        rule="date_max_age",
        valid=False,
        message=f"Document is older than {max_years} year(s)",
    )
    return CheckResult(
        field=field_name,
        rule="date_expiry",
        valid=False,
        message="Document has expired or expires today",
    )


def validate_required_fields(
    entity_map: Dict[str, str], required_fields: List[str]
) -> List[CheckResult]:
    """Check that each required field exists in entity_map with a non-empty value.

    A field is considered missing if it is absent from the map or its value
    is empty or contains only whitespace.

    Returns a list of CheckResult, one per required field.
    """
    results: List[CheckResult] = []
    for field_name in required_fields:
        value = entity_map.get(field_name, "")
        if value and value.strip():
            results.append(
                CheckResult(
                    field=field_name,
                    rule="presence",
                    valid=True,
                    message=f"Required field '{field_name}' is present",
                )
            )
        else:
            results.append(
                CheckResult(
                    field=field_name,
                    rule="presence",
                    valid=False,
                    message=f"Required field '{field_name}' is missing or empty",
                )
            )
    return results


def validate_regex(value: str, pattern: str, field_name: str) -> CheckResult:
    """Apply a generic regex pattern to a field value.

    Returns valid if the value matches the pattern, invalid otherwise.
    """
    try:
        if re.match(pattern, value):
            return CheckResult(
                field=field_name,
                rule="regex",
                valid=True,
                message=f"Field '{field_name}' matches expected pattern",
            )
        return CheckResult(
            field=field_name,
            rule="regex",
            valid=False,
            message=f"Field '{field_name}' does not match expected pattern",
        )
    except re.error:
        return CheckResult(
            field=field_name,
            rule="regex",
            valid=False,
            message=f"Invalid regex pattern for field '{field_name}'",
        )


def validate_boolean_true(value: str, field_name: str) -> CheckResult:
    """Validate that a field has an affirmative/true value.

    Recognized affirmative values (case-insensitive): SI, SÍ, YES, TRUE, 1, PRESENTE
    """
    affirmative_values = {"si", "sí", "yes", "true", "1", "presente"}
    if value.strip().lower() in affirmative_values:
        return CheckResult(
            field=field_name,
            rule="boolean_true",
            valid=True,
            message=f"Field '{field_name}' has affirmative value",
        )
    return CheckResult(
        field=field_name,
        rule="boolean_true",
        valid=False,
        message=f"Field '{field_name}' does not have an affirmative value",
    )


def validate(
    entity_map: Dict[str, str], rules: List[ValidationRule]
) -> List[CheckResult]:
    """Apply validation rules to an entity map.

    This is the main entry point for rule-based validation. It iterates over
    each ValidationRule and dispatches to the appropriate validation function
    based on rule_type:
      - "regex": applies regex pattern matching
      - "date_expiry": checks if date is in the future
      - "presence": checks field exists and is non-empty
      - "boolean_true": checks field has affirmative value

    Fields that are not present in entity_map are skipped for regex and
    date_expiry rules (presence rules handle missing fields explicitly).

    Args:
        entity_map: Dictionary mapping field names to their extracted values.
        rules: List of ValidationRule objects to apply.

    Returns:
        List of CheckResult objects, one per rule applied.
    """
    results: List[CheckResult] = []

    for rule in rules:
        value = entity_map.get(rule.field, "")

        if rule.rule_type == "regex":
            if not value or not value.strip():
                # Field not present - skip regex validation
                # (presence rules will catch missing required fields)
                continue
            if rule.pattern:
                result = validate_regex(value, rule.pattern, rule.field)
                # Use the rule's custom message if provided
                if rule.message:
                    result = CheckResult(
                        field=result.field,
                        rule=result.rule,
                        valid=result.valid,
                        message=rule.message if not result.valid else result.message,
                    )
                results.append(result)

        elif rule.rule_type == "date_expiry":
            if not value or not value.strip():
                continue
            result = validate_date_expiry(value, rule.field)
            if rule.message and not result.valid:
                result = CheckResult(
                    field=result.field,
                    rule=result.rule,
                    valid=result.valid,
                    message=rule.message,
                )
            results.append(result)

        elif rule.rule_type == "presence":
            if value and value.strip():
                results.append(
                    CheckResult(
                        field=rule.field,
                        rule="presence",
                        valid=True,
                        message=rule.message or f"Field '{rule.field}' is present",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        field=rule.field,
                        rule="presence",
                        valid=False,
                        message=rule.message
                        or f"Field '{rule.field}' is missing or empty",
                    )
                )

        elif rule.rule_type == "boolean_true":
            if not value or not value.strip():
                results.append(
                    CheckResult(
                        field=rule.field,
                        rule="boolean_true",
                        valid=False,
                        message=rule.message
                        or f"Field '{rule.field}' is missing or empty",
                    )
                )
            else:
                result = validate_boolean_true(value, rule.field)
                if rule.message and not result.valid:
                    result = CheckResult(
                        field=result.field,
                        rule=result.rule,
                        valid=result.valid,
                        message=rule.message,
                    )
                results.append(result)

        elif rule.rule_type == "date_max_age":
            if not value or not value.strip():
                continue
            max_years = int(rule.pattern) if rule.pattern else 5
            result = validate_date_max_age(value, max_years, rule.field)
            if rule.message and not result.valid:
                result = CheckResult(
                    field=result.field,
                    rule=result.rule,
                    valid=result.valid,
                    message=rule.message,
                )
            results.append(result)

    return results
