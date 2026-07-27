"""Dynamic validation engine.

Evaluates catalog-defined rules against an extracted entity map, producing:
- per-rule checks (CheckResult-compatible dicts with rule params applied)
- per-field resolutions: VALIDATED / INVALID / LOW_CONFIDENCE / NOT_FOUND /
  NOT_APPLICABLE / REVIEW_REQUIRED / ERROR

Supported rule types (all configured in JSON, no code changes to add rules):
  regex, regex_each, presence, boolean_true, date_expiry, date_max_age,
  minimum_items, custom_formula, cfdi_total, sum_array,
  cross_document_match, cross_document_amount, negative_keyword,
  expected_value_by_app, page_ranges_no_overlap

Entity map values may be scalars (str) or lists of dicts (repeatable fields
such as PARTIDAS or CONCEPTOS). Missing numeric operands in formulas count
as 0 — the check records that assumption in its message.

Anti-hallucination policy: this engine never mutates extracted values. It
reads them, normalizes copies (pipeline.normalize) and reports outcomes.
"""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from document_catalog import TypeRegistryEntry, ValidationRule
from pipeline.normalize import (
    normalize_field,
    parse_amount,
    parse_date_iso,
    pedimento_consecutivo,
)

# Field statuses
VALIDATED = "VALIDATED"
INVALID = "INVALID"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
NOT_FOUND = "NOT_FOUND"
NOT_APPLICABLE = "NOT_APPLICABLE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
ERROR = "ERROR"

# Document statuses
DOC_APPROVED = "APPROVED"
DOC_REJECTED = "REJECTED"
DOC_PARTIAL = "PARTIALLY_APPROVED"
DOC_REVIEW = "REVIEW_REQUIRED"
DOC_ERROR = "PROCESSING_ERROR"


@dataclass
class RuleCheck:
    """Outcome of a single rule evaluation."""

    field: str
    rule: str
    valid: bool
    message: str
    severity: str = "error"  # "error" | "warning"
    skipped: bool = False    # rule could not run (missing target doc, no expectation)
    details: Dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class ValidationContext:
    """Extra inputs some rules need.

    expected_values: app-level expectations, e.g. {"IMPORTER_RFC": "OFU910626UQ0"}.
    sibling_documents: other docs in the expediente, as
        [{"type": ..., "entities": {...}}] — used by cross_document_* rules.
    document_text: full raw text of the document (negative_keyword scans it).
    today: injectable current date for deterministic tests.
    """

    expected_values: Dict[str, str] = dc_field(default_factory=dict)
    sibling_documents: List[Dict[str, Any]] = dc_field(default_factory=list)
    document_text: str = ""
    today: Optional[date] = None


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _get_field(entity_map: Dict[str, Any], path: str) -> Any:
    """Resolve "FIELD" or "ARRAY.SUBFIELD" (the latter returns a list of values)."""
    if "." not in path:
        return entity_map.get(path)
    root, sub = path.split(".", 1)
    items = entity_map.get(root)
    if not isinstance(items, list):
        return None
    values = []
    for item in items:
        if isinstance(item, dict):
            values.append(item.get(sub))
        else:
            values.append(item)
    return values


def _sum_path(entity_map: Dict[str, Any], path: str) -> Tuple[float, int, int]:
    """Sum numeric values at ARRAY.SUBFIELD. Returns (total, counted, skipped)."""
    values = _get_field(entity_map, path)
    if not isinstance(values, list):
        return 0.0, 0, 0
    total, counted, skipped = 0.0, 0, 0
    for v in values:
        amount = parse_amount(v)
        if amount is None:
            if not _is_empty(v):
                skipped += 1
            continue
        total += amount
        counted += 1
    return total, counted, skipped


# ---------------------------------------------------------------- formulas

_SUM_CALL_RE = re.compile(r"SUM\(\s*([A-Z0-9_]+\.[A-Z0-9_]+)\s*\)")
_IDENT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def evaluate_formula(
    formula: str, entity_map: Dict[str, Any]
) -> Tuple[Optional[float], List[str]]:
    """Safely evaluate an arithmetic formula over entity fields.

    Supports identifiers, + - * /, parentheses and SUM(ARRAY.FIELD).
    Missing or non-numeric operands evaluate to 0 and are reported in the
    returned assumption list. Returns (value, assumptions) or (None, errors)
    when the formula is malformed.
    """
    assumptions: List[str] = []

    def sum_repl(m: re.Match) -> str:
        total, counted, skipped = _sum_path(entity_map, m.group(1))
        if skipped:
            assumptions.append(f"SUM({m.group(1)}): {skipped} non-numeric items ignored")
        return repr(total)

    expr = _SUM_CALL_RE.sub(sum_repl, formula)

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None, [f"malformed formula: {formula}"]

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            return _ALLOWED_BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        if isinstance(node, ast.Name):
            name = node.id
            if not _IDENT_RE.match(name):
                raise ValueError(f"identifier not allowed: {name}")
            raw = entity_map.get(name)
            amount = parse_amount(raw)
            if amount is None:
                assumptions.append(f"{name} missing/non-numeric, counted as 0")
                return 0.0
            return amount
        raise ValueError(f"expression not allowed: {ast.dump(node)}")

    try:
        return float(ev(tree)), assumptions
    except (ValueError, ZeroDivisionError) as e:
        return None, [str(e)]


# ---------------------------------------------------------------- rule handlers


def _tolerance(rule: ValidationRule, default: float = 0.01) -> float:
    try:
        return abs(float(rule.params.get("tolerance", default)))
    except (TypeError, ValueError):
        return default


def _allow_empty(rule: ValidationRule) -> bool:
    return bool(rule.params.get("allow_empty", False))


def _check_regex(rule: ValidationRule, value: Any) -> Optional[RuleCheck]:
    if _is_empty(value):
        return None if _allow_empty(rule) else None  # presence rules handle missing
    if not rule.pattern:
        return RuleCheck(rule.field, "regex", False, "rule missing pattern", skipped=True)
    # Validate the normalized copy (Unicode dashes folded, RFC uppercased,
    # pedimento digits collapsed); fall back to the raw extracted text.
    candidate = normalize_field(rule.field, value)
    text = str(candidate if isinstance(candidate, (str, int, float)) else value).strip()
    try:
        ok = re.match(rule.pattern, text) is not None or re.match(rule.pattern, str(value).strip()) is not None
    except re.error:
        return RuleCheck(rule.field, "regex", False, f"invalid pattern: {rule.pattern}")
    return RuleCheck(
        rule.field, "regex", ok,
        f"'{rule.field}' matches pattern" if ok else (rule.message or f"'{rule.field}' does not match pattern"),
    )


def _check_regex_each(rule: ValidationRule, entity_map: Dict[str, Any]) -> Optional[RuleCheck]:
    values = _get_field(entity_map, rule.field)
    if values is None or (isinstance(values, list) and not values):
        return None if _allow_empty(rule) else None
    if not isinstance(values, list):
        values = [values]
    if not rule.pattern:
        return RuleCheck(rule.field, "regex_each", False, "rule missing pattern", skipped=True)
    bad = [str(v) for v in values if not _is_empty(v) and not re.match(rule.pattern, str(v).strip())]
    ok = not bad
    return RuleCheck(
        rule.field, "regex_each", ok,
        f"all {len(values)} items match" if ok else (rule.message or "items do not match pattern"),
        details={} if ok else {"failing_items": bad[:10]},
    )


def _check_minimum_items(rule: ValidationRule, value: Any) -> RuleCheck:
    minimum = int(rule.params.get("value", 1))
    count = len(value) if isinstance(value, list) else (0 if _is_empty(value) else 1)
    ok = count >= minimum
    return RuleCheck(
        rule.field, "minimum_items", ok,
        f"{count} item(s) found (minimum {minimum})" if ok else (rule.message or f"expected at least {minimum} item(s), found {count}"),
    )


def _check_formula(rule: ValidationRule, entity_map: Dict[str, Any]) -> RuleCheck:
    formula = rule.params.get("formula", "")
    declared = parse_amount(entity_map.get(rule.field))
    if declared is None:
        return RuleCheck(
            rule.field, rule.rule_type, False,
            f"'{rule.field}' has no numeric value to compare against formula",
            skipped=True,
        )
    computed, assumptions = evaluate_formula(formula, entity_map)
    if computed is None:
        return RuleCheck(rule.field, rule.rule_type, False, "; ".join(assumptions), skipped=True)
    tol = _tolerance(rule)
    diff = abs(declared - computed)
    ok = diff <= tol
    detail = {"declared": declared, "computed": round(computed, 4), "tolerance": tol}
    if assumptions:
        detail["assumptions"] = assumptions
    return RuleCheck(
        rule.field, rule.rule_type, ok,
        f"{rule.field}={declared} matches formula ({computed:.2f} ± {tol})" if ok
        else (rule.message or f"{rule.field}={declared} differs from formula result {computed:.2f} by {diff:.2f}"),
        details=detail,
    )


def _check_sum_array(rule: ValidationRule, entity_map: Dict[str, Any]) -> RuleCheck:
    source = rule.params.get("source", "")
    comparison_field = rule.params.get("comparison_field") or rule.field
    declared = parse_amount(entity_map.get(comparison_field))
    total, counted, skipped = _sum_path(entity_map, source)
    if declared is None:
        return RuleCheck(
            rule.field, "sum_array", False,
            f"'{comparison_field}' has no numeric value to compare",
            skipped=True,
        )
    if counted == 0:
        return RuleCheck(
            rule.field, "sum_array", False,
            f"no numeric items found at '{source}'",
            skipped=True,
        )
    tol = _tolerance(rule)
    diff = abs(declared - total)
    ok = diff <= tol
    return RuleCheck(
        rule.field, "sum_array", ok,
        f"{comparison_field}={declared} matches sum of {source} ({total:.2f})" if ok
        else (rule.message or f"{comparison_field}={declared} differs from sum {total:.2f} by {diff:.2f}"),
        details={"sum": round(total, 4), "items": counted, "ignored": skipped, "tolerance": tol},
    )


def _match_reference(field_name: str, a: Any, b: Any) -> bool:
    """Compare two references; pedimento-like fields match on consecutivo."""
    if a is None or b is None:
        return False
    name = field_name.upper()
    if "PEDIMENTO" in name or "CONSECUTIVO" in name:
        ca, cb = pedimento_consecutivo(a), pedimento_consecutivo(b)
        return ca is not None and ca == cb
    return str(a).strip().upper() == str(b).strip().upper()


def _check_cross_document(
    rule: ValidationRule, value: Any, ctx: ValidationContext
) -> RuleCheck:
    target_type = rule.params.get("target_document_type", "")
    target_field = rule.params.get("target_field", "")
    if _is_empty(value):
        if _allow_empty(rule):
            return RuleCheck(rule.field, rule.rule_type, True, f"'{rule.field}' empty, allowed", skipped=True)
        return RuleCheck(rule.field, rule.rule_type, False, rule.message or f"'{rule.field}' empty")
    targets = [d for d in ctx.sibling_documents if d.get("type") == target_type]
    if not targets:
        return RuleCheck(
            rule.field, rule.rule_type, True,
            f"no {target_type} in expediente to compare against",
            severity="warning", skipped=True,
        )
    target_values = [t.get("entities", {}).get(target_field) for t in targets]
    if rule.rule_type == "cross_document_amount":
        declared = parse_amount(value)
        tol = _tolerance(rule, 1.0)
        for tv in target_values:
            amount = parse_amount(tv)
            if declared is not None and amount is not None and abs(declared - amount) <= tol:
                return RuleCheck(rule.field, rule.rule_type, True, f"amount matches {target_type}.{target_field}")
        return RuleCheck(
            rule.field, rule.rule_type, False,
            rule.message or f"amount {value} does not match {target_type}.{target_field} ({target_values})",
        )
    for tv in target_values:
        if _match_reference(rule.field, value, tv):
            return RuleCheck(rule.field, rule.rule_type, True, f"matches {target_type}.{target_field}")
    return RuleCheck(
        rule.field, rule.rule_type, False,
        rule.message or f"'{value}' does not match {target_type}.{target_field} ({target_values})",
    )


def _check_negative_keyword(rule: ValidationRule, value: Any, ctx: ValidationContext) -> RuleCheck:
    keywords = [str(k).upper() for k in rule.params.get("keywords", [])]
    haystack = (ctx.document_text or "").upper()
    if not haystack and not _is_empty(value):
        haystack = str(value).upper()
    hits = [k for k in keywords if k in haystack]
    ok = not hits
    return RuleCheck(
        rule.field, "negative_keyword", ok,
        "no negative keywords found" if ok else (rule.message or f"negative keywords found: {hits}"),
        details={} if ok else {"hits": hits},
    )


def _check_expected_value_by_app(rule: ValidationRule, value: Any, ctx: ValidationContext) -> RuleCheck:
    expected = ctx.expected_values.get(rule.field)
    if expected is None:
        # Common alias: any *RFC* field checks against the configured importer RFC
        if "RFC" in rule.field.upper():
            expected = ctx.expected_values.get("IMPORTER_RFC")
    if expected is None:
        return RuleCheck(
            rule.field, "expected_value_by_app", True,
            "no expected value configured for this app",
            severity="warning", skipped=True,
        )
    if _is_empty(value):
        return RuleCheck(rule.field, "expected_value_by_app", False, rule.message or f"'{rule.field}' empty")
    ok = str(value).strip().upper() == str(expected).strip().upper()
    return RuleCheck(
        rule.field, "expected_value_by_app", ok,
        f"'{rule.field}' matches configured value" if ok else (rule.message or f"'{value}' != expected '{expected}'"),
    )


def _check_page_ranges(rule: ValidationRule, value: Any) -> RuleCheck:
    if not isinstance(value, list) or not value:
        return RuleCheck(rule.field, "page_ranges_no_overlap", False, "no page ranges to check", skipped=True)
    ranges = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start = parse_amount(item.get("PAGE_START"))
        end = parse_amount(item.get("PAGE_END"))
        if start is not None and end is not None:
            ranges.append((int(start), int(end)))
    ranges.sort()
    overlaps = [
        (a, b) for a, b in zip(ranges, ranges[1:]) if b[0] <= a[1]
    ]
    ok = not overlaps
    return RuleCheck(
        rule.field, "page_ranges_no_overlap", ok,
        "page ranges are disjoint" if ok else (rule.message or f"overlapping ranges: {overlaps}"),
    )


def _check_date_expiry(rule: ValidationRule, value: Any, ctx: ValidationContext) -> Optional[RuleCheck]:
    if _is_empty(value):
        if _allow_empty(rule):
            return None
        return RuleCheck(rule.field, "date_expiry", False, rule.message or f"'{rule.field}' empty")
    parsed = parse_date_iso(value)
    if parsed is None:
        return RuleCheck(rule.field, "date_expiry", False, f"cannot parse date: {value}")
    today = ctx.today or date.today()
    ok = date.fromisoformat(parsed) > today
    return RuleCheck(
        rule.field, "date_expiry", ok,
        "not expired" if ok else (rule.message or "document expired"),
        details={"date": parsed},
    )


def _check_date_max_age(rule: ValidationRule, value: Any, ctx: ValidationContext) -> Optional[RuleCheck]:
    if _is_empty(value):
        return None
    parsed = parse_date_iso(value)
    if parsed is None:
        return RuleCheck(rule.field, "date_max_age", False, f"cannot parse date: {value}")
    max_years = int(rule.pattern or rule.params.get("value", 5))
    today = ctx.today or date.today()
    cutoff = today.replace(year=today.year - max_years)
    ok = date.fromisoformat(parsed) >= cutoff
    return RuleCheck(
        rule.field, "date_max_age", ok,
        f"within {max_years} year(s)" if ok else (rule.message or f"older than {max_years} year(s)"),
    )


def _check_boolean_true(rule: ValidationRule, value: Any) -> RuleCheck:
    affirmative = {"si", "sí", "yes", "true", "1", "presente"}
    ok = not _is_empty(value) and str(value).strip().lower() in affirmative
    return RuleCheck(
        rule.field, "boolean_true", ok,
        "affirmative" if ok else (rule.message or f"'{rule.field}' is not affirmative"),
    )


# ---------------------------------------------------------------- engine


def run_rules(
    entity_map: Dict[str, Any],
    type_entry: TypeRegistryEntry,
    ctx: Optional[ValidationContext] = None,
) -> List[RuleCheck]:
    """Run required-field presence checks plus every catalog rule."""
    ctx = ctx or ValidationContext()
    checks: List[RuleCheck] = []

    for name in type_entry.required_fields:
        value = _get_field(entity_map, name) if "." in name else entity_map.get(name)
        present = not _is_empty(value)
        checks.append(
            RuleCheck(
                name, "presence", present,
                f"required field '{name}' present" if present else f"required field '{name}' missing or empty",
            )
        )

    for rule in type_entry.validation_rules:
        value = _get_field(entity_map, rule.field) if "." in rule.field else entity_map.get(rule.field)
        result: Optional[RuleCheck] = None
        try:
            rt = rule.rule_type
            if rt == "regex":
                result = _check_regex(rule, value)
            elif rt == "regex_each":
                result = _check_regex_each(rule, entity_map)
            elif rt == "presence":
                present = not _is_empty(value)
                result = RuleCheck(rule.field, "presence", present,
                                   rule.message if (rule.message and not present) else
                                   (f"'{rule.field}' present" if present else f"'{rule.field}' missing"))
            elif rt == "boolean_true":
                result = _check_boolean_true(rule, value)
            elif rt == "date_expiry":
                result = _check_date_expiry(rule, value, ctx)
            elif rt == "date_max_age":
                result = _check_date_max_age(rule, value, ctx)
            elif rt == "minimum_items":
                result = _check_minimum_items(rule, value)
            elif rt in ("custom_formula", "cfdi_total"):
                result = _check_formula(rule, entity_map)
            elif rt == "sum_array":
                result = _check_sum_array(rule, entity_map)
            elif rt in ("cross_document_match", "cross_document_amount"):
                result = _check_cross_document(rule, value, ctx)
            elif rt == "negative_keyword":
                result = _check_negative_keyword(rule, value, ctx)
            elif rt == "expected_value_by_app":
                result = _check_expected_value_by_app(rule, value, ctx)
            elif rt == "page_ranges_no_overlap":
                result = _check_page_ranges(rule, value)
            else:
                result = RuleCheck(
                    rule.field, rt, True,
                    f"unknown rule type '{rt}' skipped",
                    severity="warning", skipped=True,
                )
        except Exception as e:  # a bad rule must never kill the document
            result = RuleCheck(rule.field, rule.rule_type, False, f"rule error: {e}", severity="warning", skipped=True)
        if result is not None:
            checks.append(result)

    return checks


def resolve_fields(
    entity_map: Dict[str, Any],
    confidences: Dict[str, float],
    checks: List[RuleCheck],
    type_entry: TypeRegistryEntry,
) -> Dict[str, Dict[str, Any]]:
    """Build the per-field resolution block.

    Every catalog extraction field gets an entry with the original extracted
    value, a normalized copy, confidence, executed checks and a final status
    (VALIDATED / INVALID / LOW_CONFIDENCE / NOT_FOUND / NOT_APPLICABLE /
    REVIEW_REQUIRED) — plus a simple `value`/`valid`/`reason` view derived
    from that status, for callers that just want a pass/fail per field
    without the full enum: `valid` is True for VALIDATED and NOT_APPLICABLE
    (nothing wrong — the field legitimately doesn't apply), False otherwise;
    `reason` is populated only when `valid` is False.
    """
    failures_by_field: Dict[str, List[RuleCheck]] = {}
    warnings_by_field: Dict[str, List[RuleCheck]] = {}
    for c in checks:
        root = c.field.split(".", 1)[0]
        if c.skipped:
            continue
        if not c.valid:
            (failures_by_field if c.severity == "error" else warnings_by_field).setdefault(root, []).append(c)

    resolved: Dict[str, Dict[str, Any]] = {}
    for name in type_entry.extraction_fields:
        value = entity_map.get(name)
        confidence = confidences.get(name)
        entry: Dict[str, Any] = {
            "extractedValue": value,
            "normalizedValue": normalize_field(name, value),
            "confidence": round(confidence, 4) if confidence is not None else None,
            "checks": [
                {"rule": c.rule, "valid": c.valid, "message": c.message, "skipped": c.skipped}
                for c in checks if c.field.split(".", 1)[0] == name
            ],
        }
        if _is_empty(value):
            entry["status"] = NOT_FOUND if name in type_entry.required_fields else NOT_APPLICABLE
            entry["normalizedValue"] = None
        elif name in failures_by_field:
            entry["status"] = INVALID
        elif name in warnings_by_field:
            entry["status"] = REVIEW_REQUIRED
        elif confidence is not None and confidence < type_entry.min_confidence:
            entry["status"] = LOW_CONFIDENCE
        else:
            entry["status"] = VALIDATED

        entry["value"] = entry["normalizedValue"] if entry["normalizedValue"] is not None else entry["extractedValue"]
        entry["valid"] = entry["status"] in (VALIDATED, NOT_APPLICABLE)
        if entry["valid"]:
            entry["reason"] = None
        else:
            reasons = [c["message"] for c in entry["checks"] if not c["valid"] and not c["skipped"]]
            if reasons:
                entry["reason"] = "; ".join(reasons)
            elif entry["status"] == NOT_FOUND:
                entry["reason"] = f"required field '{name}' missing or empty"
            elif entry["status"] == LOW_CONFIDENCE:
                entry["reason"] = f"confidence {entry['confidence']} below minimum {type_entry.min_confidence}"
            else:
                entry["reason"] = entry["status"]

        resolved[name] = entry
    return resolved


def document_status(
    resolved_fields: Dict[str, Dict[str, Any]],
    checks: List[RuleCheck],
    type_entry: TypeRegistryEntry,
) -> str:
    """Collapse field resolutions into the document-level status."""
    hard_failures = [c for c in checks if not c.valid and not c.skipped and c.severity == "error"]
    statuses = [f["status"] for f in resolved_fields.values()]

    required_missing = any(
        resolved_fields.get(name, {}).get("status") == NOT_FOUND
        for name in type_entry.required_fields
        if "." not in name and name in resolved_fields
    )
    if hard_failures or required_missing:
        # Some fields fine, some broken → partial; everything critical broken → rejected
        ok_count = sum(1 for s in statuses if s == VALIDATED)
        return DOC_PARTIAL if ok_count > 0 else DOC_REJECTED
    if any(s in (LOW_CONFIDENCE, REVIEW_REQUIRED) for s in statuses):
        return DOC_REVIEW
    if any(not c.valid and c.severity == "warning" and not c.skipped for c in checks):
        return DOC_REVIEW
    return DOC_APPROVED
