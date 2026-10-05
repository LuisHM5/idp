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
from dataclasses import dataclass, field as dc_field, replace
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from document_catalog import TypeRegistryEntry, ValidationRule
from pipeline import confidence as conf_policy
from pipeline.derived import OPERATORS as _WHEN_OPERATORS, _matches as _condition_matches
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
    # "error"   -> tumba el documento (RECHAZADO / PARCIAL)
    # "warning" -> lo manda a revisión
    # "info"    -> se reporta y se audita, pero NO cambia el status: para lo
    #              que hay que saber sin que bloquee (un campo OPCIONAL leído
    #              por debajo del umbral de confianza, p. ej.)
    severity: str = "error"  # "error" | "warning" | "info"
    skipped: bool = False    # rule could not run (missing target doc, no expectation)
    # True solo cuando la regla declara `on_fail: "reject"` en el catálogo: un
    # fallo así RECHAZA el documento él solo, aunque el resto de los campos
    # esté bien. Sin esto un "error" entre N checks buenos da PARCIAL (que el
    # API reporta como REQUIERE_REVISION), y una señal determinista de fraude
    # nunca podría rechazar. Ver `_apply_on_fail`.
    blocking: bool = False
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
    """Resolve a dotted path against an entity map, walking every segment.

    "FIELD" returns that field's value. "ARRAY.SUBFIELD" returns a list (one
    value per row) when ARRAY is a list — the legacy convention field-centric
    rules (sum_array, custom_formula) rely on. "OBJECT.SUBFIELD" descends
    into a nested object (the field-centric DSL's `type: "object"` fields,
    see catalogs/_TEMPLATE.json) and returns the scalar there. Longer chains
    like "CUADRO_LIQUIDACION.CONCEPTOS.IMPORTE" walk each segment in turn:
    dict → descend by key, list → collect that key from every row (so a
    trailing list step still yields "one value per row", same as the
    single-segment case). Backward compatible with the old single-split
    behavior for exactly 2 segments over a top-level list.
    """
    if "." not in path:
        return entity_map.get(path)
    parts = path.split(".")
    current: Any = entity_map.get(parts[0])
    for part in parts[1:]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            values = []
            for item in current:
                if isinstance(item, dict):
                    values.append(item.get(part))
                else:
                    values.append(item)
            current = values
        else:
            return None
    return current


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
) -> Tuple[Optional[float], List[str], List[str]]:
    """Safely evaluate an arithmetic formula over entity fields.

    Supports identifiers, + - * /, parentheses and SUM(ARRAY.FIELD).
    Missing or non-numeric operands evaluate to 0 and are reported in the
    returned assumption list. Returns (value, assumptions, operand_fields) or
    (None, errors, operand_fields) when the formula is malformed — the third
    element lists every plain identifier the formula referenced (present or
    not), in first-seen order, so a caller can attribute a failed comparison
    back to each field it depends on instead of only the field the rule was
    declared under (see `_check_formula`'s `operand_fields` detail).
    """
    assumptions: List[str] = []
    operand_fields: List[str] = []

    def sum_repl(m: re.Match) -> str:
        total, counted, skipped = _sum_path(entity_map, m.group(1))
        if skipped:
            assumptions.append(f"SUM({m.group(1)}): {skipped} non-numeric items ignored")
        return repr(total)

    expr = _SUM_CALL_RE.sub(sum_repl, formula)

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None, [f"malformed formula: {formula}"], operand_fields

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
            if name not in operand_fields:
                operand_fields.append(name)
            raw = entity_map.get(name)
            amount = parse_amount(raw)
            if amount is None:
                assumptions.append(f"{name} missing/non-numeric, counted as 0")
                return 0.0
            return amount
        raise ValueError(f"expression not allowed: {ast.dump(node)}")

    try:
        return float(ev(tree)), assumptions, operand_fields
    except (ValueError, ZeroDivisionError) as e:
        return None, [str(e)], operand_fields


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
    declared = parse_amount(_get_field(entity_map, rule.field) if "." in rule.field else entity_map.get(rule.field))
    if declared is None:
        return RuleCheck(
            rule.field, rule.rule_type, False,
            f"'{rule.field}' has no numeric value to compare against formula",
            skipped=True,
        )
    computed, assumptions, operand_fields = evaluate_formula(formula, entity_map)
    if computed is None:
        return RuleCheck(rule.field, rule.rule_type, False, "; ".join(assumptions), skipped=True)
    tol = _tolerance(rule)
    diff = abs(declared - computed)
    ok = diff <= tol
    detail = {"declared": declared, "computed": round(computed, 4), "tolerance": tol}
    if assumptions:
        detail["assumptions"] = assumptions
    if not ok and operand_fields:
        # Which named field is actually wrong is ambiguous from a failed sum
        # alone (e.g. VALOR_ADUANA vs PRECIO_PAGADO_VALOR_COMERCIAL look
        # alike on the page and get swapped) — record every operand so
        # `resolve_field_tree` can flag each one as a possible culprit
        # instead of only the field the rule happens to be declared under.
        detail["operand_fields"] = operand_fields
    return RuleCheck(
        rule.field, rule.rule_type, ok,
        f"{rule.field}={declared} matches formula ({computed:.2f} ± {tol})" if ok
        else (rule.message or f"{rule.field}={declared} differs from formula result {computed:.2f} by {diff:.2f}"),
        details=detail,
    )


def _check_sum_array(rule: ValidationRule, entity_map: Dict[str, Any]) -> RuleCheck:
    source = rule.params.get("source", "")
    comparison_field = rule.params.get("comparison_field") or rule.field
    declared = parse_amount(_get_field(entity_map, comparison_field) if "." in comparison_field else entity_map.get(comparison_field))
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


def _check_equals_field(rule: ValidationRule, entity_map: Dict[str, Any]) -> RuleCheck:
    """`equals_field`: this field's value must equal another field/path's
    value (e.g. TOTAL_PEDIMENTO must equal CUADRO_LIQUIDACION.TOTAL). Was
    declared in catalogs but had no handler — silently skipped as an
    unknown rule type — until `_get_field`'s multi-segment resolution made
    nested-object comparisons like this meaningful."""
    source = rule.params.get("source", "")
    declared = parse_amount(_get_field(entity_map, rule.field) if "." in rule.field else entity_map.get(rule.field))
    other = parse_amount(_get_field(entity_map, source) if "." in source else entity_map.get(source))
    if declared is None or other is None:
        return RuleCheck(
            rule.field, "equals_field", False,
            f"'{rule.field}' or '{source}' has no numeric value to compare", skipped=True,
        )
    tol = _tolerance(rule)
    diff = abs(declared - other)
    ok = diff <= tol
    detail = {"declared": declared, "other": other, "tolerance": tol}
    if not ok:
        detail["operand_fields"] = [source]
    return RuleCheck(
        rule.field, "equals_field", ok,
        f"{rule.field}={declared} matches {source} ({other})" if ok
        else (rule.message or f"{rule.field}={declared} differs from {source}={other} by {diff:.2f}"),
        details=detail,
    )


def _check_array_length(rule: ValidationRule, entity_map: Dict[str, Any]) -> RuleCheck:
    """`array_length`: this field's declared count must equal the length of
    another array field (e.g. NUMERO_TOTAL_PARTIDAS vs len(PARTIDAS))."""
    source = rule.params.get("source", "")
    declared = parse_amount(entity_map.get(rule.field))
    values = _get_field(entity_map, source) if "." in source else entity_map.get(source)
    count = len(values) if isinstance(values, list) else 0
    if declared is None:
        return RuleCheck(
            rule.field, "array_length", False,
            f"'{rule.field}' has no numeric value to compare", skipped=True,
        )
    ok = int(declared) == count
    return RuleCheck(
        rule.field, "array_length", ok,
        f"{rule.field}={int(declared)} matches {source} length ({count})" if ok
        else (rule.message or f"{rule.field}={int(declared)} does not match {source} length ({count})"),
        details={"declared": int(declared), "counted": count},
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


def _check_boolean_true(rule: ValidationRule, value: Any) -> Optional[RuleCheck]:
    # `allow_empty` da el tercer estado "no evaluado": un booleano `computed`
    # que el pipeline no pudo determinar (la etapa no aplica a este archivo,
    # no hay referencias registradas, ...) queda vacío y NO cuenta como "no".
    # Sin `allow_empty` el comportamiento es el de siempre: vacío = falla.
    if _is_empty(value) and _allow_empty(rule):
        return None
    affirmative = {"si", "sí", "yes", "true", "1", "presente"}
    ok = not _is_empty(value) and str(value).strip().lower() in affirmative
    return RuleCheck(
        rule.field, "boolean_true", ok,
        "affirmative" if ok else (rule.message or f"'{rule.field}' is not affirmative"),
    )


# ---------------------------------------------------------------- engine


_ON_FAIL_VALUES = ("reject", "review", "info")


def _apply_on_fail(rule: ValidationRule, check: Optional[RuleCheck]) -> Optional[RuleCheck]:
    """Apply the rule's optional `on_fail` param to its outcome.

    `on_fail` is how a CATALOG says what a failure of this rule means for the
    document, for any `rule_type`:

      "reject" -> severity "error" + `blocking`: the document is REJECTED by
                  this failure alone (see `document_status_from_checks`).
      "review" -> severity "warning": the document goes to review.
      "info"   -> severity "info": reported and audited, status untouched.

    Absent (every catalog before this param existed) the check keeps the
    severity its handler chose. A skipped check is left alone: a rule that
    could not run has no failure to grade. An unrecognized value is ignored
    rather than guessed at.
    """
    if check is None or check.skipped:
        return check
    on_fail = str(rule.params.get("on_fail") or "").strip().lower()
    if on_fail not in _ON_FAIL_VALUES:
        return check
    if on_fail == "reject":
        check.severity = "error"
        check.blocking = True
    elif on_fail == "review":
        check.severity = "warning"
    else:
        check.severity = "info"
    return check


def _rule_applies(
    rule: ValidationRule, entity_map: Dict[str, Any]
) -> Tuple[bool, Optional[RuleCheck]]:
    """Evaluate the rule's optional `when` param: does this rule apply to
    THIS document at all?

    `when` is how a CATALOG scopes a rule to the documents it makes sense for,
    for any `rule_type` — one type that accepts a tax certificate from any
    country can keep the Mexican RFC pattern for the Mexican ones only:

        {"rule_type": "regex", "pattern": "...",
         "when": {"field": "PAIS", "eq": "MEX"}}

    A condition names the `field` it reads (a dotted path, resolved against
    the same `entity_map` the rule itself sees — so inside `item_fields` it is
    a column of the SAME row) plus one or more operators from the closed set
    `pipeline.derived` already uses for `derived` cases, ANDed. A list of
    conditions is ANDed too.

    Returns `(applies, problem)`. A condition that is false means the rule has
    no verdict for this document — the same "nothing to say" an empty optional
    field gets — and leaves no check behind. A MALFORMED condition (no
    `field`, no operator, a misspelled operator) is a different thing: it
    comes back as a `skipped` check, like an unknown `rule_type`, because a
    typo must neither turn a rule on for everyone nor silence it unnoticed.
    """
    when = rule.params.get("when")
    if when is None:
        return True, None
    conditions = when if isinstance(when, list) else [when]
    problem = None if conditions else "no conditions"
    for condition in conditions:
        if not isinstance(condition, dict) or not str(condition.get("field") or "").strip():
            problem = "each condition needs a 'field'"
        else:
            unknown = sorted(set(condition) - _WHEN_OPERATORS - {"field"})
            if len(condition) == 1:
                problem = f"condition on '{condition['field']}' has no operator"
            elif unknown:
                problem = f"unknown operator(s) {unknown}"
        if problem:
            break
    if problem:
        return False, RuleCheck(
            rule.field, rule.rule_type, True,
            f"invalid 'when' ({problem}): rule skipped", severity="warning", skipped=True,
        )
    for condition in conditions:
        path = str(condition["field"]).strip()
        value = _get_field(entity_map, path) if "." in path else entity_map.get(path)
        if not _condition_matches(value, {k: v for k, v in condition.items() if k != "field"}):
            return False, None
    return True, None


def _dispatch_rule(
    rule: ValidationRule, value: Any, entity_map: Dict[str, Any], ctx: ValidationContext
) -> Optional[RuleCheck]:
    """Run one rule against its already-resolved `value` and return the
    outcome (or None when the rule type has no verdict for this value, e.g.
    an empty optional field, or a rule whose `when` does not hold for this
    document). Shared by `run_rules` and `resolve_field_tree` so the
    field-centric DSL path evaluates rules identically instead of duplicating
    this dispatch."""
    applies, problem = _rule_applies(rule, entity_map)
    if not applies:
        return problem
    return _apply_on_fail(rule, _evaluate_rule(rule, value, entity_map, ctx))


def _evaluate_rule(
    rule: ValidationRule, value: Any, entity_map: Dict[str, Any], ctx: ValidationContext
) -> Optional[RuleCheck]:
    """The `rule_type` switch itself — the sole arbiter of which rule types
    exist. Called only through `_dispatch_rule`, which grades the outcome."""
    try:
        rt = rule.rule_type
        if rt == "regex":
            return _check_regex(rule, value)
        if rt == "regex_each":
            return _check_regex_each(rule, entity_map)
        if rt == "presence":
            present = not _is_empty(value)
            return RuleCheck(rule.field, "presence", present,
                              rule.message if (rule.message and not present) else
                              (f"'{rule.field}' present" if present else f"'{rule.field}' missing"))
        if rt == "boolean_true":
            return _check_boolean_true(rule, value)
        if rt == "date_expiry":
            return _check_date_expiry(rule, value, ctx)
        if rt == "date_max_age":
            return _check_date_max_age(rule, value, ctx)
        if rt == "minimum_items":
            return _check_minimum_items(rule, value)
        if rt in ("custom_formula", "cfdi_total"):
            return _check_formula(rule, entity_map)
        if rt == "sum_array":
            return _check_sum_array(rule, entity_map)
        if rt == "equals_field":
            return _check_equals_field(rule, entity_map)
        if rt == "array_length":
            return _check_array_length(rule, entity_map)
        if rt in ("cross_document_match", "cross_document_amount"):
            return _check_cross_document(rule, value, ctx)
        if rt == "negative_keyword":
            return _check_negative_keyword(rule, value, ctx)
        if rt == "expected_value_by_app":
            return _check_expected_value_by_app(rule, value, ctx)
        if rt == "page_ranges_no_overlap":
            return _check_page_ranges(rule, value)
        return RuleCheck(rule.field, rt, True, f"unknown rule type '{rt}' skipped", severity="warning", skipped=True)
    except Exception as e:  # a bad rule must never kill the document
        return RuleCheck(rule.field, rule.rule_type, False, f"rule error: {e}", severity="warning", skipped=True)


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
        result = _dispatch_rule(rule, value, entity_map, ctx)
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
        raw_confidence = confidences.get(name)
        # Same confidence contract as `resolve_field_tree`: always a number,
        # and a value under the discard floor is dropped rather than handed on
        # (see `pipeline.confidence`).
        confidence = conf_policy.UNKNOWN if raw_confidence is None else conf_policy.clamp(raw_confidence)
        discarded = not _is_empty(value) and conf_policy.discarded(
            confidence, type_entry.discard_below_confidence
        )
        entry: Dict[str, Any] = {
            "extractedValue": value,
            "normalizedValue": normalize_field(name, value),
            "confidence": confidence,
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
        elif discarded:
            entry["status"] = LOW_CONFIDENCE
        elif name in warnings_by_field:
            entry["status"] = REVIEW_REQUIRED
        elif confidence < type_entry.min_confidence:
            entry["status"] = LOW_CONFIDENCE
        else:
            entry["status"] = VALIDATED

        entry["value"] = entry["normalizedValue"] if entry["normalizedValue"] is not None else entry["extractedValue"]
        if discarded:
            entry["value"] = None
        entry["valid"] = entry["status"] in (VALIDATED, NOT_APPLICABLE)
        if entry["valid"]:
            entry["reason"] = None
        else:
            reasons = [c["message"] for c in entry["checks"] if not c["valid"] and not c["skipped"]]
            if reasons:
                entry["reason"] = "; ".join(reasons)
            elif entry["status"] == NOT_FOUND:
                entry["reason"] = f"required field '{name}' missing or empty"
            elif discarded:
                entry["reason"] = (
                    f"value discarded: confidence {confidence} below discard floor "
                    f"{type_entry.discard_below_confidence}"
                )
            elif entry["status"] == LOW_CONFIDENCE:
                entry["reason"] = f"confidence {entry['confidence']} below minimum {type_entry.min_confidence}"
            else:
                entry["reason"] = entry["status"]

        resolved[name] = entry
    return resolved


def _flatten_field_tree_rules(field_tree: Dict[str, Any]) -> Tuple[List[ValidationRule], List[str]]:
    """Collect every field's own `validation` rules + required-field paths,
    recursively, from a field-centric DSL field_tree. Array `item_fields` are
    NOT included here — a flat `NAME` would collide across rows; they are
    evaluated per-row by `resolve_field_tree`'s `evaluate_row_rules`, under an
    indexed path (`FACTURAS[0].Factura`)."""
    rules: List[ValidationRule] = []
    required: List[str] = []

    def walk(tree: Dict[str, Any], prefix: str) -> None:
        for name, spec in tree.items():
            path = f"{prefix}{name}"
            rules.extend(spec.validation)
            if spec.required:
                required.append(path)
            if spec.type == "object":
                walk(spec.fields, path + ".")

    walk(field_tree, "")
    return rules, required


def resolve_field_tree(
    entity_map: Dict[str, Any],
    confidences: Dict[str, float],
    type_entry: TypeRegistryEntry,
    ctx: Optional[ValidationContext] = None,
    computed_field_names: Optional[set] = None,
    default_source: str = "MODEL_EXTRACTION",
    source_overrides: Optional[Dict[str, str]] = None,
    value_labels: Optional[Dict[str, Any]] = None,
    row_confidences: Optional[Dict[str, Dict[str, float]]] = None,
    default_confidence: Optional[float] = None,
) -> Tuple[Dict[str, Any], List[RuleCheck], Dict[str, Optional[float]]]:
    """Field-centric DSL (schema_version 4.x) equivalent of
    `resolve_fields()` — walks `type_entry.field_tree` and returns ONE
    recursive structure per field instead of the legacy trio of raw values
    (`_public_entities`) + scalars-only value/valid (`resolve_fields`) +
    compact-envelope filtering (`_scalar_entities`/`_fields_validity`) that
    used to represent the same field in three different shapes (see the
    catalog-dsl-v4-migration memory for why). Shape, matching what the
    consumer confirmed:
    - scalar  -> {value, confidence, valid, reason, source}
    - object  -> {valid, reason, fields: {SUBFIELD: {...same shape...}}}
    - array   -> {items: [ {...same shape per row...} ]}

    `source` is "NORMALIZED" when `pipeline.normalize.normalize_field`
    changed the value, or when the field's whole subtree belongs to
    `computed_field_names` (deterministic post-extraction fields such as
    LIQUIDACION_NORMALIZADA — see FieldSpec.computed); otherwise
    `default_source`. Callers whose entity_map didn't come from the model
    (e.g. `pipeline.xml_document`, which parses deterministic XML source data)
    pass their own label here so the output honestly describes where a value
    came from instead of implying an LLM extracted it. `source_overrides`
    does the same per FIELD rather than per document, for the mixed case:
    most of a pedimento is model-extracted, but the fields
    `pipeline.expediente` recovered deterministically (from the QR, or by
    concatenating what the QR already proved) are not model output and must
    not be reported as such — `{path: label}`, keyed by the same dotted path
    as `confidences`, so it works whatever the catalog named the field.

    `value_labels` adds a human-readable `label` next to a scalar's `value`,
    keyed by the same dotted path. It exists for codes that are stored as a
    number but mean something in words (the ERP's `Tipo_Pago`: 1 = "Anticipo y
    Pasivo"): the field keeps the numeric value the destination system wants,
    and the reader still sees what it means without a lookup table. The `label`
    key is only present on nodes that actually have one — a field nobody
    labelled comes back in exactly the shape it did before.

    `row_confidences` ({row_key: {campo: conf}}, see `pipeline.confidence`)
    gives each ARRAY ROW CELL its own number. Without it every cell of every
    row reports the array's segment-level OCR score, which says nothing about
    the individual row the reader is looking at.

    `default_confidence` is what a value inherits when the extractor attached
    no number of its own — callers pass how well the SOURCE was read (the
    segment's OCR confidence for the PDF pipeline, 1.0 for the deterministic
    XML/tabular ones). It matters because of the discard rule below: without
    it, a pipeline that simply forgot to register a field's confidence would
    have its values deleted as untrustworthy. With it, a 0.0 means "nothing
    supports this value", never "nobody measured it".

    Confidence contract — `confidence` is ALWAYS a number in 0-1, never null:
    - A field nobody could measure (no value on the page, no extractor number,
      nothing to inherit) reports 0.0 — "no lo sé". `null` forced every
      consumer to special-case the absence of a number before comparing it.
    - A computed field inherits the confidence of what produced it: the mean
      of the cells a total summed (`pipeline.liquidacion`), or the source
      field a `derived` block read (`pipeline.derived`).
    - Below `type_entry.discard_below_confidence` (0.50 by default) a value is
      DISCARDED, not just flagged: `value` becomes null, `valid` false and the
      reason says so, while `confidence` keeps the low number that caused it.
      A wrong-looking amount that still parses is worse downstream than an
      explicit blank. This is stricter than `min_confidence`, which flags the
      field but keeps the value for a human to judge, and it only ever applies
      to a field that HAS a value — an empty optional field is not "discarded",
      it was never there.

    Trust determination (`valid`/`confidence`) is not raw model self-report
    alone — it is deliberately conservative:
    - A `custom_formula`/`cfdi_total`/`equals_field` check failure is
      attributed not just to the field the rule is declared under, but to
      every field its formula reads (`RuleCheck.details["operand_fields"]`,
      see `_check_formula`/`_check_equals_field`): when two fields can be
      confused on the source document (e.g. VALOR_ADUANA vs
      PRECIO_PAGADO_VALOR_COMERCIAL), a mismatch flags both as possible
      culprits (severity "warning") instead of only the one the rule
      happens to live on, leaving the actual bad field looking `valid`.
    - `confidence < min_confidence` now also appends a `RuleCheck` (it used
      to only set the node's own `valid`/`reason`), so
      `document_status_from_checks` — the function every field-tree caller
      uses for the document-level status — actually sees it instead of
      silently missing every LOW_CONFIDENCE-equivalent field.
    - A field whose `valid` is False for any of the above reasons never
      reports a `confidence` at or above `min_confidence` — the self/OCR
      combined number is capped below threshold, because a model/OCR that's
      "sure" about a value the system independently knows is wrong is not
      a trustworthy number (see `documentacion/TESTS.md`'s
      `raise_min_confidence` finding: raw self-reported confidence alone is
      not a reliable correctness signal).

    Returns `(resolved_tree, checks, effective_confidences)` — the third
    element is `path -> confidence` for every scalar leaf actually resolved
    (post-capping), so a caller computing a document-level average (see
    `pipeline/expediente.py`) reflects the same trust determination as the
    per-field values instead of averaging the pre-cap raw numbers. A leaf with
    no value maps to None there (not to the 0.0 its node reports): the
    document's own confidence describes what was READ, and averaging in a zero
    per field that was never on the page would sink it for no reason.
    """
    ctx = ctx or ValidationContext()
    computed_field_names = computed_field_names or set()
    source_overrides = source_overrides or {}
    value_labels = value_labels or {}
    row_confidences = row_confidences or {}
    discard_floor = type_entry.discard_below_confidence
    own_rules, own_required = _flatten_field_tree_rules(type_entry.field_tree)

    checks: List[RuleCheck] = []
    for name in own_required:
        value = _get_field(entity_map, name) if "." in name else entity_map.get(name)
        present = not _is_empty(value)
        checks.append(RuleCheck(
            name, "presence", present,
            f"required field '{name}' present" if present else f"required field '{name}' missing or empty",
        ))
    for rule in own_rules + type_entry.validation_rules:
        value = _get_field(entity_map, rule.field) if "." in rule.field else entity_map.get(rule.field)
        result = _dispatch_rule(rule, value, entity_map, ctx)
        if result is not None:
            checks.append(result)

    # A failed cross-field check (formula/equals_field) names every field it
    # read in `details["operand_fields"]`. Flag each one that isn't already
    # the rule's own field as a possible culprit — "warning" severity, since
    # we don't know WHICH operand is actually wrong, only that one of them
    # is. Without this, only the field the rule happens to be declared under
    # gets demoted, and a genuinely wrong sibling field keeps looking valid
    # with a high self-reported confidence (the exact failure mode
    # documented in documentacion/TESTS.md for PRECIO_PAGADO_VALOR_COMERCIAL
    # vs VALOR_ADUANA).
    operand_checks: List[RuleCheck] = []
    for c in checks:
        if c.skipped or c.valid or not c.details:
            continue
        for operand in c.details.get("operand_fields") or ():
            if operand == c.field:
                continue
            operand_checks.append(RuleCheck(
                operand, f"{c.rule}_operand", False,
                f"possible source of failed check on '{c.field}': {c.message}",
                severity="warning",
                details={"related_field": c.field, "related_rule": c.rule},
            ))
    checks.extend(operand_checks)

    checks_by_field: Dict[str, List[RuleCheck]] = {}
    for c in checks:
        if not c.skipped:
            checks_by_field.setdefault(c.field, []).append(c)

    def valid_reason(path: str) -> Tuple[bool, Optional[str]]:
        failed = [c for c in checks_by_field.get(path, []) if not c.valid]
        if not failed:
            return True, None
        return False, "; ".join(c.message for c in failed)

    def record(check: RuleCheck) -> RuleCheck:
        """Add a check discovered DURING the walk (a row's own rules).

        `checks_by_field` is built once, above, from the flat pass — so a
        check appended only to `checks` would never reach `valid_reason` and
        the node it belongs to would still render as valid.
        """
        checks.append(check)
        if not check.skipped:
            checks_by_field.setdefault(check.field, []).append(check)
        return check

    def evaluate_row_rules(
        item_fields: Dict[str, Any], row: Dict[str, Any], row_path: str
    ) -> List[RuleCheck]:
        """Evaluate ONE array row against the rules its `item_fields` declare.

        `_flatten_field_tree_rules` deliberately leaves `item_fields` out — a
        flat `Factura` check would collide across rows — and until this
        existed nothing evaluated them anywhere: a row could carry the whole
        COVE in `Factura`, or no `Factura` at all despite `required: true`,
        and still come back `valid: true`. The only check a repeatable group
        ever produced was the array's own presence.

        Each rule runs with the ROW as its `entity_map`, so a rule that
        crosses columns of the same row (a formula over its own cells) reads
        the right values — and is recorded under an indexed path
        (`FACTURAS[0].Factura`) so every row reports its own result.

        Nested `object` subfields recurse here (the flat pass doesn't reach
        inside a row either); nested `array` subfields do NOT — `walk_node`
        calls this itself for each of their rows when it renders them.
        """
        recorded: List[RuleCheck] = []
        for sub_name, sub_spec in item_fields.items():
            sub_path = f"{row_path}.{sub_name}"
            value = row.get(sub_name)
            if sub_spec.type == "object":
                recorded.extend(evaluate_row_rules(
                    sub_spec.fields, value if isinstance(value, dict) else {}, sub_path
                ))
                continue
            if sub_spec.type == "array":
                continue
            if sub_spec.required and not sub_spec.computed:
                present = not _is_empty(value)
                recorded.append(record(RuleCheck(
                    sub_path, "presence", present,
                    f"required field '{sub_path}' present" if present
                    else f"required field '{sub_path}' missing or empty",
                )))
            for rule in sub_spec.validation:
                result = _dispatch_rule(rule, value, row, ctx)
                if result is not None:
                    recorded.append(record(replace(result, field=sub_path)))
        return recorded

    def resolve_leaf(
        leaf_name: str, value: Any, computed: bool, path: Optional[str] = None,
        declared_type: Optional[str] = None,
    ) -> Tuple[Any, str]:
        """Resolve a scalar leaf's OUTPUT value together with its source tag.

        Previously this only decided the tag (`source_for`) while the caller
        kept returning the raw, un-normalized `value` regardless — so a field
        could come back tagged "NORMALIZED" while `value` still showed the
        dirty original (e.g. NUMERO_PEDIMENTO with spaces, tagged NORMALIZED,
        `value` still "26 07 0823 6001630" instead of the digit-only form).
        The tag must describe what's actually in `value`.

        `declared_type` is the field's catalog `type`, passed so normalization
        obeys what the catalog declared instead of guessing from the field
        NAME — a `string` field is never parsed into a number just because its
        name contains "VALOR" (see `pipeline.normalize.normalize_field`).
        An array of plain scalars passes its `item_type` ("string" unless the
        catalog says otherwise); the cells of an array of ROWS pass their own
        subfield type, like any other scalar.
        """
        if computed:
            resolved, label = value, "NORMALIZED"
        else:
            resolved, label = value, default_source
            if value is not None:
                normalized = normalize_field(leaf_name, value, declared_type)
                if normalized != value:
                    resolved, label = normalized, "NORMALIZED"
        # An override only replaces the LABEL — WHERE the value came from —
        # never the value itself: a recovered `computed` field (the QR's
        # boolean validity flag) must stay the boolean it is, not the
        # stringified form normalization would produce for it.
        return resolved, (source_overrides.get(path or leaf_name) or label)

    def walk_node(spec: Any, path: str, value: Any, computed: bool, fallback_conf: Optional[float]) -> Dict[str, Any]:
        computed = computed or spec.computed
        if spec.type == "object":
            valid, reason = valid_reason(path)
            sub_value = value if isinstance(value, dict) else {}
            return {
                "valid": valid,
                "reason": reason,
                "fields": {
                    sub_name: walk_node(sub_spec, f"{path}.{sub_name}", sub_value.get(sub_name), computed, fallback_conf)
                    for sub_name, sub_spec in spec.fields.items()
                },
            }
        if spec.type == "array":
            items = value if isinstance(value, list) else []
            array_conf = confidences.get(path, fallback_conf)
            if spec.item_fields:
                rendered = []
                for index, row in enumerate(items):
                    row = row if isinstance(row, dict) else {}
                    # Each cell falls back to what the extractor measured for
                    # THIS row (pipeline.confidence), then to the array's own
                    # number. Without the per-row map every cell of every row
                    # reported the same segment-level OCR score.
                    row_conf = row_confidences.get(conf_policy.row_key(row), {})
                    # The row's own rules, under a path that identifies THIS
                    # row — cells are rendered under it too, so each one
                    # resolves its own result instead of every row sharing
                    # `FACTURAS.Factura`. Safe for `confidences`: an array
                    # cell is never keyed by that path (its confidence comes
                    # from `row_confidences`, keyed by row content).
                    row_path = f"{path}[{index}]"
                    row_checks = evaluate_row_rules(spec.item_fields, row, row_path)
                    failed = [c for c in row_checks if not c.valid and not c.skipped]
                    rendered.append({
                        "valid": not failed,
                        "reason": "; ".join(c.message for c in failed) or None,
                        "fields": {
                            sub_name: walk_node(
                                sub_spec, f"{row_path}.{sub_name}", row.get(sub_name), computed,
                                row_conf.get(sub_name, array_conf),
                            )
                            for sub_name, sub_spec in spec.item_fields.items()
                        },
                    })
                return {"items": rendered}
            rendered_items = []
            for v in items:
                resolved_v, source = resolve_leaf(spec.name, v, computed, declared_type=spec.item_type)
                rendered_items.append({
                    "value": resolved_v,
                    "confidence": conf_policy.clamp(
                        array_conf if array_conf is not None else default_confidence
                    ),
                    "valid": True, "reason": None,
                    "source": source,
                })
            return {"items": rendered_items}
        # scalar
        valid, reason = valid_reason(path)
        resolved_value, source = resolve_leaf(spec.name, value, computed, path, spec.type)
        raw_confidence = confidences.get(path, fallback_conf)
        if raw_confidence is None:
            raw_confidence = default_confidence
        # Always a number from here on — see the confidence contract above.
        confidence = conf_policy.UNKNOWN if raw_confidence is None else conf_policy.clamp(raw_confidence)
        empty = _is_empty(resolved_value)
        if empty:
            # No value, no confidence: an absent field reports UNKNOWN, never
            # the document-level default (which describes how well the source
            # was READ — there is nothing here that was read).
            confidence = conf_policy.UNKNOWN
            # Nothing was read, so there is nothing to distrust: the node
            # reports UNKNOWN (0.0) and skips both thresholds below — a field
            # that isn't on the document is not a "discarded" one, and a
            # required one already failed its own presence check.
            pass
        elif conf_policy.discarded(confidence, discard_floor):
            # Too unreliable to hand on as data: drop the VALUE, keep the
            # number that explains why (see the confidence contract above).
            discard_reason = (
                f"value discarded: confidence {confidence} below discard floor {discard_floor}"
            )
            resolved_value = None
            valid = False
            reason = f"{reason}; {discard_reason}" if reason else discard_reason
            checks.append(RuleCheck(path, "discard_low_confidence", False, discard_reason, severity="warning"))
        elif valid and confidence < type_entry.min_confidence:
            valid, reason = False, f"confidence {confidence} below minimum {type_entry.min_confidence}"
            # El campo SIEMPRE queda marcado (`valid: false` + `reason`): se
            # leyó por debajo de la barra de confianza del tipo y quien
            # consuma tiene que saberlo.
            #
            # Lo que depende de si el campo es OBLIGATORIO es si además tumba
            # el documento entero a revisión. `document_status_from_checks`
            # convierte cualquier `warning` en REVIEW_REQUIRED, y con eso un
            # campo OPCIONAL bien leído — medido: TERMINOS_PAGO "NET 30" con
            # 0.7861 contra un mínimo de 0.80 — mandaba a revisión un
            # documento cuyos 33 campos restantes estaban perfectos. El status
            # del documento tiene que reflejar si el dato que EXIGISTE es
            # confiable; para lo opcional, marcar el campo es suficiente.
            # `severity="info"` lo deja auditable en `checks` sin cambiar el
            # status (ver `document_status_from_checks`, que solo mira
            # "error" y "warning").
            severity = "warning" if spec.required else "info"
            checks.append(RuleCheck(path, "min_confidence", False, reason, severity=severity))
        elif not valid and type_entry.min_confidence > 0 and confidence >= type_entry.min_confidence:
            # This field is untrustworthy for a reason the raw model×OCR
            # number can't see (its own rule failed, or it's an implicated
            # operand of a failed cross-field check elsewhere) — don't let a
            # high self-reported number contradict that. Cap it below
            # threshold so `confidence` alone tells the truth, not just
            # `valid`.
            confidence = round(max(0.0, min(confidence, type_entry.min_confidence - 0.01)), 4)
        # An empty field's 0.0 says "nothing here", not "read badly" — kept out
        # of the document-level average (see this function's docstring).
        effective_confidences[path] = None if empty else confidence
        node = {
            "value": resolved_value,
            "confidence": confidence,
            "valid": valid,
            "reason": reason,
            "source": source,
        }
        if path in value_labels:
            node["label"] = value_labels[path]
        return node

    effective_confidences: Dict[str, Optional[float]] = {}
    result = {
        name: walk_node(spec, name, entity_map.get(name), name in computed_field_names, None)
        for name, spec in type_entry.field_tree.items()
    }
    return result, checks, effective_confidences


def _has_blocking_failure(checks: List[RuleCheck]) -> bool:
    """A failed rule declared `on_fail: "reject"` rejects the document on its
    own, before the partial/rejected arithmetic below gets a say."""
    return any(not c.valid and not c.skipped and c.blocking for c in checks)


def document_status_from_checks(checks: List[RuleCheck]) -> str:
    """Document-level status from a flat check list alone — the
    `resolve_field_tree` counterpart of `document_status` (which needs the
    VALIDATED/INVALID/... status enum `resolve_fields` produces per scalar,
    something the recursive field-tree shape doesn't have)."""
    hard_failures = [c for c in checks if not c.valid and not c.skipped and c.severity == "error"]
    if _has_blocking_failure(checks):
        return DOC_REJECTED
    if hard_failures:
        return DOC_PARTIAL if len(hard_failures) < len(checks) else DOC_REJECTED
    # "info" queda fuera a propósito: se reporta en el campo y en `checks`,
    # pero no manda el documento a revisión (ver RuleCheck.severity).
    if any(not c.valid and c.severity == "warning" and not c.skipped for c in checks):
        return DOC_REVIEW
    return DOC_APPROVED


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
    if _has_blocking_failure(checks):
        return DOC_REJECTED
    if hard_failures or required_missing:
        # Some fields fine, some broken → partial; everything critical broken → rejected
        ok_count = sum(1 for s in statuses if s == VALIDATED)
        return DOC_PARTIAL if ok_count > 0 else DOC_REJECTED
    if any(s in (LOW_CONFIDENCE, REVIEW_REQUIRED) for s in statuses):
        return DOC_REVIEW
    if any(not c.valid and c.severity == "warning" and not c.skipped for c in checks):
        return DOC_REVIEW
    return DOC_APPROVED
