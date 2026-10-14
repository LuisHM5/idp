"""Fill the `$common` bookkeeping cells of array rows after extraction.

`$common` (catalogs/_shared.json -> `_common_item_fields`) adds PAGINA, ORDEN,
VALOR_CRUDO and CONFIANZA to a table's rows. They describe WHERE a row was
read, not what it says, so the catalog loader marks them `computed`
(`document_catalog._parse_field_tree`) and they never reach the model: on a
real constancia VALOR_CRUDO alone was ~30% of the table JSON the model had to
generate. This module fills them from the same page text the extractor saw:

- ORDEN: the row's position in its table (1-based, in the extracted order).
- PAGINA / VALOR_CRUDO: the page and printed line where the row's own text
  appears. The row is located by its longest text cell, compared without
  whitespace and case so a value the model re-spaced still matches. A cell
  that wraps onto the next line(s) is matched against a short window starting
  at its first line. Nothing found -> both stay null; nothing is invented.
- CONFIANZA: the confidence reported for that row, when there is one.

Rows are keyed by content in `row_confidences` (`pipeline.confidence.row_key`),
so every row that changes is re-keyed here. Already-filled cells are kept,
which makes the fill idempotent across the model path and the hybrid merge.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from document_catalog import FieldSpec

from .confidence import row_key

# A line plus the next ones, so a cell that wraps still matches from its first
# line. The constancia's obligations wrap their due-date text onto 2-3 lines.
_WINDOW_LINES = 3
# Shorter needles (a "1", an "IVA") match too many lines to locate anything.
_MIN_NEEDLE_CHARS = 4

_SPACE_RE = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _SPACE_RE.sub("", text).lower()


class _PageIndex:
    """Printed lines of every page, squashed once for repeated lookups."""

    def __init__(self, pages: Sequence[Any]):
        self.lines: List[Tuple[int, int, str, str]] = []
        for page in pages:
            number = getattr(page, "number", None)
            for index, line in enumerate((getattr(page, "text", "") or "").splitlines()):
                if line.strip():
                    self.lines.append((number, index, line.strip(), _squash(line)))

    def locate(self, needle: str, used: Set[Tuple[int, int]]) -> Optional[Tuple[int, int, str]]:
        for position, (number, index, raw, squashed) in enumerate(self.lines):
            if (number, index) in used:
                continue
            window = squashed + "".join(
                item[3] for item in self.lines[position + 1:position + _WINDOW_LINES]
                if item[0] == number
            )
            found = window.find(needle)
            # The match must START on this line; otherwise it belongs to a
            # later line, which the loop reaches on its own.
            if found != -1 and found < len(squashed):
                return number, index, raw
        return None


def _needles(row: Dict[str, Any], item_fields: Dict[str, FieldSpec]) -> List[str]:
    values = [
        _squash(str(row.get(name)))
        for name, spec in item_fields.items()
        if not spec.computed and spec.type == "string" and isinstance(row.get(name), str)
    ]
    return sorted({value for value in values if len(value) >= _MIN_NEEDLE_CHARS}, key=len, reverse=True)


def _fill_rows(
    rows: List[Any],
    item_fields: Dict[str, FieldSpec],
    index: _PageIndex,
    row_confidences: Dict[str, Dict[str, float]],
) -> None:
    common = {
        name: str(spec.params.get("row_common"))
        for name, spec in item_fields.items()
        if spec.computed and spec.params.get("row_common")
    }
    used: Set[Tuple[int, int]] = set()
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        old_key = row_key(row)
        confidence = row_confidences.get(old_key) or {}
        row_conf = max(confidence.values()) if confidence else None
        located = None
        if any(kind in ("PAGINA", "VALOR_CRUDO") for kind in common.values()):
            for needle in _needles(row, item_fields):
                located = index.locate(needle, used)
                if located is not None:
                    used.add((located[0], located[1]))
                    break
        for name, kind in common.items():
            if row.get(name) not in (None, ""):
                continue
            if kind == "ORDEN":
                row[name] = position + 1
            elif kind == "PAGINA" and located is not None and located[0] is not None:
                row[name] = located[0]
            elif kind == "VALOR_CRUDO" and located is not None:
                row[name] = located[2]
            elif kind == "CONFIANZA" and row_conf is not None:
                row[name] = row_conf
        new_key = row_key(row)
        if new_key != old_key and old_key in row_confidences:
            moved = dict(row_confidences.pop(old_key))
            if row_conf is not None:
                moved.update({name: row_conf for name in common if row.get(name) is not None})
            row_confidences[new_key] = moved
        for spec_name, spec in item_fields.items():
            nested = row.get(spec_name)
            if spec.type == "array" and spec.item_fields and isinstance(nested, list):
                _fill_rows(nested, spec.item_fields, index, row_confidences)


def _has_common(tree: Dict[str, FieldSpec]) -> bool:
    for spec in tree.values():
        if spec.computed and spec.params.get("row_common"):
            return True
        if _has_common(spec.fields) or _has_common(spec.item_fields):
            return True
    return False


def fill_row_common_fields(
    entity: Dict[str, Any],
    tree: Optional[Dict[str, FieldSpec]],
    pages: Sequence[Any],
    row_confidences: Dict[str, Dict[str, float]],
) -> None:
    """Fill `$common` cells of every table in `entity`, in place."""
    if not tree or not isinstance(entity, dict) or not _has_common(tree):
        return
    index = _PageIndex(pages)

    def walk(node: Dict[str, Any], subtree: Dict[str, FieldSpec]) -> None:
        for name, spec in subtree.items():
            value = node.get(name)
            if spec.type == "object" and isinstance(value, dict):
                walk(value, spec.fields)
            elif spec.type == "array" and spec.item_fields and isinstance(value, list):
                _fill_rows(value, spec.item_fields, index, row_confidences)

    walk(entity, tree)
