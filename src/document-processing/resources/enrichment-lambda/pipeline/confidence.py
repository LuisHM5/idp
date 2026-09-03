"""Confidence policy: how a field's trust number is decided, combined and floored.

One place for three rules that the pipeline applies to EVERY resolved field,
whatever extractor produced it (Bedrock, XML, tabular) and whatever the
catalog declared:

1. **Never null.** A resolved scalar always reports a number in 0-1. A field
   nobody could measure reports `UNKNOWN` (0.0) — "no lo sé" — instead of
   `null`, which forced every consumer to special-case the absence of a
   number before it could compare it against anything.
2. **A value computed from several source cells inherits the MEAN of their
   confidences** (`combine`) — it is exactly as trustworthy as the numbers it
   added up, no more. Used by `pipeline.liquidacion` for the
   `LIQUIDACION_NORMALIZADA` totals, which are sums over N rows of
   `CUADRO_LIQUIDACION.CONCEPTOS` / `CONTRIBUCIONES`.
3. **A value under the discard floor is not returned as a value.** Below
   `DISCARD_BELOW` (0.50 by default, per-type via the catalog's
   `discard_below_confidence`) the datum is worse than useless — it looks
   like data and isn't — so the field comes back with `value: null`,
   `valid: false` and a reason, keeping its (low) confidence number. This is
   deliberately stricter than `min_confidence`, which only FLAGS the field
   and keeps the value for a human to judge.

Row-level confidences (`row_confidences`) are keyed by the row's own CONTENT
(`row_key`), never by its index: chunked extraction concatenates and dedups
rows from several Bedrock calls (`text_extractor._merge_field_tree_entities`),
so an index assigned at parse time means nothing after the merge, while the
row dict itself survives it unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Mapping, NamedTuple, Optional

# Confidence reported for a field nobody could measure (absent value, no
# extractor number, no source to inherit from). Not "we're 0% sure the value
# is right" — there is no value.
UNKNOWN = 0.0

# Below this, a value is discarded instead of returned (see rule 3 above).
DISCARD_BELOW = 0.50

# A document-level shared confidence is useful as a summary only when at least
# this many populated leaves agree. It must never replace the confidence on
# the leaves themselves: each field remains self-contained for API consumers.
MIN_SHARED = 3


class ConfidenceSummary(NamedTuple):
    """One-pass aggregate over final, populated field confidences."""

    mean: Optional[float]
    shared: Optional[float]
    count: int


def clamp(value: Any) -> float:
    """Coerce anything to a usable 0-1 confidence, defaulting to `UNKNOWN`."""
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return UNKNOWN


def combine(values: Iterable[Optional[float]]) -> Optional[float]:
    """Mean of the confidences that actually exist, or None if none do.

    None (not 0.0) when there is nothing to average, so the caller can decide
    what an unmeasurable value should report — averaging in a 0 for every
    missing operand would understate a total that was read perfectly.
    """
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 4)


def summarize(values: Iterable[Optional[float]]) -> ConfidenceSummary:
    """Mean plus most-common confidence in one pass.

    `shared` preserves the old envelope's `fieldConfidence` semantics: the
    first confidence in traversal order wins a frequency tie, and the value
    is only exposed when at least `MIN_SHARED` populated fields share it.
    Callers can therefore compute the document average and compatibility
    summary together, without traversing the rendered field tree afterward.
    """
    total = 0.0
    count = 0
    counts: Dict[float, int] = {}
    first_seen: Dict[float, int] = {}
    shared_value: Optional[float] = None
    shared_count = 0

    for raw in values:
        if raw is None:
            continue
        value = float(raw)
        if value not in first_seen:
            first_seen[value] = count
        count += 1
        total += value
        value_count = counts.get(value, 0) + 1
        counts[value] = value_count
        if (
            value_count > shared_count
            or (
                value_count == shared_count
                and shared_value is not None
                and first_seen[value] < first_seen[shared_value]
            )
        ):
            shared_value = value
            shared_count = value_count

    mean = round(total / count, 4) if count else None
    shared = shared_value if shared_count >= MIN_SHARED else None
    return ConfidenceSummary(mean=mean, shared=shared, count=count)


def row_key(row: Mapping[str, Any]) -> str:
    """Content-addressed key for one array row (see module docstring)."""
    try:
        return json.dumps(row, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(row)


def cell_confidence(
    row_confidences: Optional[Dict[str, Dict[str, float]]],
    row: Any,
    field_name: str,
    default: Optional[float] = None,
) -> Optional[float]:
    """Confidence the extractor reported for ONE cell of ONE array row."""
    if not row_confidences or not isinstance(row, dict):
        return default
    value = (row_confidences.get(row_key(row)) or {}).get(field_name)
    return default if value is None else float(value)


def discarded(confidence: Optional[float], floor: float) -> bool:
    """Whether a (non-empty) value must be dropped rather than returned."""
    return floor > 0 and confidence is not None and confidence < floor
