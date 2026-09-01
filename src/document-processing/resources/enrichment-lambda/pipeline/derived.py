"""Config-driven derived fields: one value chosen by condition, not extracted.

Some fields are not printed anywhere on the document — they are a business
rule over values that WERE extracted. A pedimento never prints "tipo de pago";
the ERP derives it from the liquidation total. Expressing that used to mean
writing a module per rule (see `liquidacion.py`, and the note that closes
catalogs/README.md's LIQUIDACION_NORMALIZADA section), which bakes a business
threshold into Python.

This module reads the rule from the catalog instead, off a per-field `derived`
block:

    "Tipo_Pago": {
      "type": "number", "computed": true,
      "derived": {
        "from": "CUADRO_LIQUIDACION.TOTAL",
        "cases": [
          {"when": {"gt": 383}, "value": 1, "label": "Anticipo y Pasivo"},
          {"when": {"lte": 383}, "value": 3, "label": "Solo Pasivo"}
        ]
      }
    }

Cases are ordered — the first matching `when` wins — and several keys inside
one `when` are ANDed. There is no expression evaluation of any kind: the
operator set below is closed and `value` is emitted verbatim, so a catalog can
never execute code. Nothing here is type-specific: a type that declares no
`derived` block gets no work done and no output change.

`label` is optional and never replaces `value`: the field keeps the code the
destination system stores (the ERP's Tipo_Pago column is numeric) while the
result also says what that code means, so nobody reading it needs the lookup
table. It rides alongside as `label` on the resolved node.

`derived` is for a CONDITION over already-extracted fields. Aggregation over a
concept array is still `LIQUIDACION_NORMALIZADA`'s job (`liquidacion.py`), and
anything that needs the document actually read stays a normal field with a
`hint` for the model.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, NamedTuple, Optional, Tuple

from .liquidacion import _resolve_path
from .normalize import parse_amount

logger = logging.getLogger(__name__)

# A derived value was computed by this module from fields already established,
# not read off the document by the model. Same label `pipeline/expediente.py`
# uses for the pedimento number it rebuilds from its components — both are
# "the system worked this out", as opposed to QR_RECOVERY's "printed on the
# document and decoded deterministically".
SOURCE_DERIVED = "DERIVED"

class DerivedFields(NamedTuple):
    """What one derived pass produced, in the two shapes the engine takes:
    `sources` -> `resolve_field_tree(source_overrides=)`, `labels` ->
    `resolve_field_tree(value_labels=)`. The values themselves went straight
    into the entity map."""

    sources: Dict[str, str]
    labels: Dict[str, Any]


_NUMERIC_OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


def _text(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split()).upper()


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _equal(value: Any, expected: Any) -> bool:
    """Numeric comparison when BOTH sides parse as numbers, text otherwise —
    so `{"eq": 0}` matches the string "0.00" and `{"eq": "A1"}` matches " a1 "."""
    got, want = parse_amount(value), parse_amount(expected)
    if got is not None and want is not None:
        return abs(got - want) <= 0.0001
    return _text(value) == _text(expected)


def _matches(value: Any, when: Dict[str, Any]) -> bool:
    """True when `value` satisfies EVERY operator in `when` (AND).

    An unknown operator makes the case fail rather than silently pass: a typo
    in a catalog must not turn into a rule that matches everything. Same
    reasoning for a numeric operator against a non-numeric value.
    """
    if not isinstance(when, dict) or not when:
        return False
    for op, expected in when.items():
        if op in _NUMERIC_OPS:
            got, want = parse_amount(value), parse_amount(expected)
            if got is None or want is None or not _NUMERIC_OPS[op](got, want):
                return False
        elif op == "eq":
            if not _equal(value, expected):
                return False
        elif op == "ne":
            if _equal(value, expected):
                return False
        elif op == "in":
            if not isinstance(expected, list) or not any(_equal(value, e) for e in expected):
                return False
        elif op == "matches":
            if _is_empty(value) or not re.search(str(expected), str(value), re.IGNORECASE):
                return False
        elif op == "empty":
            if _is_empty(value) is not bool(expected):
                return False
        elif op == "not_empty":
            if _is_empty(value) is bool(expected):
                return False
        else:
            logger.warning("derived: unknown operator %r ignored -> case does not match", op)
            return False
    return True


def _evaluate(spec: Dict[str, Any], entity_map: Dict[str, Any]) -> Tuple[Any, Any, Optional[str]]:
    """Return (value, label, source_path) for one `derived` block.

    `label` is the matched case's optional human-readable tag, or None.
    `source_path` is the path the value was decided from — the caller uses it
    to inherit that field's confidence — or None when nothing matched and the
    block's `default` was used (no source, so no confidence and no label:
    labelling a value nothing decided would be inventing meaning).
    """
    from_path = spec.get("from")
    if not from_path:
        logger.warning("derived: block without 'from' skipped")
        return spec.get("default"), None, None
    value = _resolve_path(entity_map, str(from_path))
    for case in spec.get("cases") or []:
        if isinstance(case, dict) and _matches(value, case.get("when") or {}):
            return case.get("value"), case.get("label"), str(from_path)
    return spec.get("default"), None, None


def apply_derived_fields(
    entity_map: Dict[str, Any],
    confidences: Dict[str, float],
    entry,
) -> DerivedFields:
    """Fill every field in `entry.field_tree` that declares a `derived` block.

    Mutates `entity_map` (the value) and `confidences` (inherited from the
    field the value was decided from, so a derived field is exactly as
    trustworthy as its input and never more). Returns the two per-path views
    `engine.resolve_field_tree` takes: `sources` ({path: SOURCE_DERIVED}, so
    the output says the system computed the value instead of implying a model
    extracted it) and `labels` ({path: label} for the cases that declared one).

    Call it AFTER `build_liquidacion_normalizada` so a rule can read either the
    raw extraction or a normalized concept total.
    """
    sources: Dict[str, str] = {}
    labels: Dict[str, Any] = {}

    def has_derived(specs: Dict[str, Any]) -> bool:
        """Whether anything in this subtree declares a `derived` block — checked
        before descending so an object field with nothing derived under it is
        left exactly as extracted (no empty `{}` materialized in entity_map)."""
        for spec in (specs or {}).values():
            if isinstance((getattr(spec, "params", {}) or {}).get("derived"), dict):
                return True
            if getattr(spec, "type", None) == "object" and has_derived(getattr(spec, "fields", None) or {}):
                return True
        return False

    def visit(specs: Dict[str, Any], container: Dict[str, Any], prefix: str) -> None:
        for name, spec in (specs or {}).items():
            path = f"{prefix}{name}"
            block = (getattr(spec, "params", {}) or {}).get("derived")
            if isinstance(block, dict):
                try:
                    value, label, from_path = _evaluate(block, entity_map)
                except Exception:
                    # A bad rule must never kill the document — same contract as
                    # validators.engine._dispatch_rule. The field stays empty and
                    # its `required` check reports it, which is the honest outcome.
                    logger.exception("derived: failed to evaluate %s", path)
                    continue
                container[name] = value
                sources[path] = SOURCE_DERIVED
                if label is not None:
                    labels[path] = label
                inherited = confidences.get(from_path) if from_path else None
                if inherited is not None:
                    confidences[path] = inherited
                else:
                    confidences.pop(path, None)
            elif getattr(spec, "type", None) == "object" and has_derived(getattr(spec, "fields", None) or {}):
                nested = container.get(name)
                if not isinstance(nested, dict):
                    nested = {}
                    container[name] = nested
                visit(spec.fields, nested, f"{path}.")

    visit(getattr(entry, "field_tree", None) or {}, entity_map, "")
    return DerivedFields(sources, labels)
