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
from typing import Any, Dict, Iterable, Mapping, Optional

# Confidence reported for a field nobody could measure (absent value, no
# extractor number, no source to inherit from). Not "we're 0% sure the value
# is right" — there is no value.
UNKNOWN = 0.0

# Below this, a value is discarded instead of returned (see rule 3 above).
DISCARD_BELOW = 0.50


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
