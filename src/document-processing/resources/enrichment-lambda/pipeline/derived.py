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

Three refinements, all optional and all inert for a block that uses none:

- **Context variables.** A `from` that starts with `$` reads something the
  pipeline knows about the UPLOAD rather than a field of the document. Today
  that is `$FILE_NAME` (the uploaded file's base name): integrations routinely
  encode what a file is in its name (`2607-0823-6001803_GT1.pdf`), and that is
  a fact the uploader declared, not something to pay a model to guess. The set
  is closed (`build_context`), an unknown variable resolves to nothing, and a
  value decided from one carries `CONTEXT_CONFIDENCE`.
- **Per-case `from`.** A case may name its own `from`, overriding the block's
  for that case only — "the file name says so; otherwise the ISR retention
  says so; otherwise ..." is one ordered list instead of a chain of helper
  fields that would all leak into the output.
- **`keep_extracted: true`.** When no case matches, leave whatever the
  extraction put in the field instead of overwriting it with `default`. That
  is how a field can be deterministic when the cheap signal exists and
  model-read (a normal `hint`, so NOT `computed`) when it does not. The kept
  value is not relabelled: it stays MODEL_EXTRACTION with the model's
  confidence, because that is what it is.

Blocks may read other derived fields (`TIPO` from `SIGLAS`): evaluation is
ordered by dependency, NOT by declaration order, because a catalog synced to
DynamoDB comes back as a Map, and a Map does not keep its key order.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .liquidacion import _resolve_path
from .normalize import parse_amount

logger = logging.getLogger(__name__)

# A derived value was computed by this module from fields already established,
# not read off the document by the model. Same label `pipeline/expediente.py`
# uses for the pedimento number it rebuilds from its components — both are
# "the system worked this out", as opposed to QR_RECOVERY's "printed on the
# document and decoded deterministically".
SOURCE_DERIVED = "DERIVED"

# `from` paths with this prefix read the upload context, not the entity map.
CONTEXT_PREFIX = "$"
CONTEXT_FILE_NAME = "$FILE_NAME"
# A context value is not a reading that can be more or less sharp: the file
# name is whatever the uploader sent, byte for byte.
CONTEXT_CONFIDENCE = 1.0


def build_context(source_name: str = "") -> Dict[str, Any]:
    """The closed set of `$` variables a `derived` block may read.

    `source_name` is whatever the pipeline was handed — an S3 key
    (`raw/{TYPE}/{uuid}/archivo.pdf`) from the Lambda, a bare file name from
    `scripts/procesar-docs.py` — so only the last path segment is kept: the
    type and the uuid in the key must never be matched as if they were part of
    the name the uploader chose.
    """
    name = re.split(r"[\\/]", str(source_name or ""))[-1].strip()
    return {CONTEXT_FILE_NAME: name}


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


def _read(path: str, entity_map: Dict[str, Any], context: Dict[str, Any]) -> Any:
    """Resolve one `from`: a `$` context variable, or a dotted entity path."""
    if path.startswith(CONTEXT_PREFIX):
        if path not in context:
            logger.warning("derived: unknown context variable %r -> no value", path)
        return context.get(path)
    return _resolve_path(entity_map, path)


def _sources_of(spec: Dict[str, Any]) -> List[str]:
    """Every `from` a block can read: its own plus each case's override."""
    paths = [spec.get("from")] + [
        case.get("from") for case in spec.get("cases") or [] if isinstance(case, dict)
    ]
    return [str(p) for p in paths if p]


def _evaluate(
    spec: Dict[str, Any],
    entity_map: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, Any, Optional[str], bool]:
    """Return (value, label, source_path, matched) for one `derived` block.

    `label` is the matched case's optional human-readable tag, or None — a
    fallen-through `default` gets none, since labelling a value no case
    decided would be inventing meaning.

    `source_path` is the field the deciding case READ (its own `from`, else
    the block's), which the caller uses to inherit its confidence. It is
    reported for the `default` too — the block's `from` then: nothing matched
    *that input*, so the fallback is exactly as trustworthy as the input it
    was chosen for. Only a malformed block (no `from`) has no source at all,
    and its value is then a constant nobody measured.

    `matched` tells a `default` apart from a case that decided, which is what
    `keep_extracted` needs.
    """
    context = context or {}
    block_from = spec.get("from")
    for case in spec.get("cases") or []:
        if not isinstance(case, dict):
            continue
        from_path = case.get("from") or block_from
        if not from_path:
            continue
        value = _read(str(from_path), entity_map, context)
        if _matches(value, case.get("when") or {}):
            return case.get("value"), case.get("label"), str(from_path), True
    if not block_from:
        logger.warning("derived: block without 'from' fell through to its default")
        return spec.get("default"), None, None, False
    return spec.get("default"), None, str(block_from), False


def apply_derived_fields(
    entity_map: Dict[str, Any],
    confidences: Dict[str, float],
    entry,
    context: Optional[Dict[str, Any]] = None,
) -> DerivedFields:
    """Fill every field in `entry.field_tree` that declares a `derived` block.

    Mutates `entity_map` (the value) and `confidences` (inherited from the
    field the value was decided from, so a derived field is exactly as
    trustworthy as its input and never more — and never `null`: a field whose
    input was never extracted inherits nothing and is reported as UNKNOWN by
    `engine.resolve_field_tree`, see `pipeline.confidence`). Returns the two per-path views
    `engine.resolve_field_tree` takes: `sources` ({path: SOURCE_DERIVED}, so
    the output says the system computed the value instead of implying a model
    extracted it) and `labels` ({path: label} for the cases that declared one).

    `context` is `build_context(...)`'s dict of `$` variables; omitted, every
    `$` read resolves to nothing and only entity-backed cases can match.

    Call it AFTER `build_liquidacion_normalizada` so a rule can read either the
    raw extraction or a normalized concept total.
    """
    sources: Dict[str, str] = {}
    labels: Dict[str, Any] = {}
    context = context or {}

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

    # (path, name, container, block) for every derived field, collected first
    # so they can be evaluated in dependency order rather than in whatever
    # order the catalog's keys happened to arrive in.
    pending: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []

    def collect(specs: Dict[str, Any], container: Dict[str, Any], prefix: str) -> None:
        for name, spec in (specs or {}).items():
            path = f"{prefix}{name}"
            block = (getattr(spec, "params", {}) or {}).get("derived")
            if isinstance(block, dict):
                pending.append((path, name, container, block))
            elif getattr(spec, "type", None) == "object" and has_derived(getattr(spec, "fields", None) or {}):
                nested = container.get(name)
                if not isinstance(nested, dict):
                    nested = {}
                    container[name] = nested
                collect(spec.fields, nested, f"{path}.")

    def resolve(path: str, name: str, container: Dict[str, Any], block: Dict[str, Any]) -> None:
        try:
            value, label, from_path, matched = _evaluate(block, entity_map, context)
        except Exception:
            # A bad rule must never kill the document — same contract as
            # validators.engine._dispatch_rule. The field stays empty and
            # its `required` check reports it, which is the honest outcome.
            logger.exception("derived: failed to evaluate %s", path)
            return
        if not matched and block.get("keep_extracted") and not _is_empty(container.get(name)):
            # No case decided and the extraction did read something: that
            # reading stands, with its own source and confidence untouched.
            return
        container[name] = value
        sources[path] = SOURCE_DERIVED
        if label is not None:
            labels[path] = label
        # A derived field is exactly as trustworthy as the field it
        # read — including when no case matched and the block's
        # `default` was used: nothing matched THAT input, so the
        # fallback is as good as the input it was chosen for. A block
        # that produced no value inherits nothing: there is nothing to
        # trust, and `engine.resolve_field_tree` reports it as UNKNOWN.
        if from_path and from_path.startswith(CONTEXT_PREFIX):
            inherited: Optional[float] = CONTEXT_CONFIDENCE if from_path in context else None
        else:
            inherited = confidences.get(from_path) if from_path else None
        if inherited is not None and not _is_empty(value):
            confidences[path] = inherited
        else:
            confidences.pop(path, None)

    collect(getattr(entry, "field_tree", None) or {}, entity_map, "")

    # A block that reads another derived field waits for it. Each pass resolves
    # everything whose inputs are settled; a pass that settles nothing means a
    # cycle, and the rest are resolved as declared rather than dropped.
    while pending:
        waiting = {item[0] for item in pending}
        ready = [
            item for item in pending
            if not any(src in waiting and src != item[0] for src in _sources_of(item[3]))
        ]
        if not ready:
            logger.warning("derived: circular 'from' among %s -> resolved in declaration order", sorted(waiting))
            ready = list(pending)
        for item in ready:
            resolve(*item)
        pending = [item for item in pending if item not in ready]

    return DerivedFields(sources, labels)
