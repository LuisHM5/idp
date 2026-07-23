"""Deterministic normalization of the customs liquidation table.

The pedimento catalog declares, under
`form_mapping.<FORM>.cuadro_liquidacion_fields`, how each normalized concept
(DTA, IGI, IVA, PRV, IEPS_*, ...) is derived from the raw extracted arrays
`CUADRO_LIQUIDACION.CONCEPTOS` and `CONTRIBUCIONES`. This module reads that
config and builds the flat `LIQUIDACION_NORMALIZADA` object, so the mapping
lives in the catalog (data) and not in code.

Nothing here is pedimento-specific beyond the field names in the catalog: a
type without `cuadro_liquidacion_fields` simply yields no output.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .normalize import parse_amount

logger = logging.getLogger(__name__)


def _num(value: Any) -> Any:
    """Coerce DynamoDB Decimals to JSON-serializable numbers; pass others through."""
    if isinstance(value, Decimal):
        f = float(value)
        return int(f) if f.is_integer() else f
    return value

# "CONTRIBUCIONES[CLAVE_CONTRIBUCION=IEPS].FORMA_PAGO" → (array, key, val, field)
_PATH_RE = re.compile(r"^(\w+)\[(\w+)=([^\]]+)\]\.(\w+)$")


def _norm(text: Any) -> str:
    return str(text or "").strip().upper()


def _find_cuadro_config(form_mapping: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Locate `cuadro_liquidacion_fields` inside any declared form."""
    for form in (form_mapping or {}).values():
        if isinstance(form, dict) and form.get("cuadro_liquidacion_fields"):
            return form["cuadro_liquidacion_fields"]
    return None


def _sum_concepts(concepts: List[Dict[str, Any]], aliases: List[str], value_field: str) -> float:
    """Sum `value_field` over concepts whose clave/description matches an alias."""
    wanted = {_norm(a) for a in aliases}
    total = 0.0
    for c in concepts:
        if not isinstance(c, dict):
            continue
        clave = _norm(c.get("CLAVE_CONTRIBUCION"))
        desc = _norm(c.get("DESCRIPCION"))
        if clave in wanted or desc in wanted:
            amt = parse_amount(c.get(value_field))
            if amt is not None:
                total += amt
    return round(total, 2)


def _sum_contribuciones(rows: List[Dict[str, Any]], filters: Dict[str, Any], value_field: str) -> float:
    """Sum `value_field` over CONTRIBUCIONES rows matching every filter."""
    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if not _row_matches(r, filters):
            continue
        amt = parse_amount(r.get(value_field))
        if amt is not None:
            total += amt
    return round(total, 2)


def _row_matches(row: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    for key, expected in filters.items():
        if key == "TASA":
            got = parse_amount(row.get("TASA"))
            if got is None or abs(got - float(expected)) > 0.01:
                return False
        elif _norm(row.get(key)) != _norm(expected):
            return False
    return True


def _first_by_path(entity_map: Dict[str, Any], path: str) -> Optional[str]:
    """Resolve 'ARRAY[KEY=VAL].FIELD' to the first matching row's field value."""
    m = _PATH_RE.match(path)
    if not m:
        return None
    array_name, key, val, out_field = m.groups()
    for r in entity_map.get(array_name) or []:
        if isinstance(r, dict) and _norm(r.get(key)) == _norm(val):
            v = r.get(out_field)
            if v not in (None, "", []):
                return str(v).strip()
    return None


def build_liquidacion_normalizada(entity_map: Dict[str, Any], entry) -> Optional[Dict[str, Any]]:
    """Build LIQUIDACION_NORMALIZADA from the catalog's cuadro config.

    Returns None when the type declares no cuadro_liquidacion_fields, so callers
    only attach the object for types that actually define the mapping.
    """
    config = _find_cuadro_config(getattr(entry, "form_mapping", {}) or {})
    if not config:
        return None

    # Start from declared defaults (shape + zero/null values), then fill.
    defaults = ((getattr(entry, "structured_fields", {}) or {})
                .get("LIQUIDACION_NORMALIZADA") or {})
    out: Dict[str, Any] = dict(defaults)

    concepts = [c for c in (entity_map.get("CUADRO_LIQUIDACION.CONCEPTOS") or []) if isinstance(c, dict)]
    contribuciones = [c for c in (entity_map.get("CONTRIBUCIONES") or []) if isinstance(c, dict)]

    for target, spec in config.items():
        if not isinstance(spec, dict):
            continue
        source = spec.get("source", "")
        value_field = spec.get("value_field", "IMPORTE")
        default = spec.get("default", 0)

        if source == "CUADRO_LIQUIDACION.CONCEPTOS":
            aliases = spec.get("concept_aliases") or [target]
            val = _sum_concepts(concepts, aliases, value_field)
            out[target] = val if val else default
        elif source == "CONTRIBUCIONES":
            val = _sum_contribuciones(contribuciones, spec.get("filters") or {}, value_field)
            out[target] = val if val else default
        elif "[" in source and "]" in source:  # path like ARRAY[K=V].FIELD
            out[target] = _first_by_path(entity_map, source) or default
        else:  # DOCUMENT source: a plain scalar field name (e.g. CUADRO_LIQUIDACION.TOTAL)
            raw = entity_map.get(source)
            if raw in (None, "", []):
                # Fall back to a same-named scalar (TOTAL, TOTAL_PEDIMENTO)
                raw = entity_map.get(target) or entity_map.get("TOTAL_PEDIMENTO")
            amt = parse_amount(raw)
            out[target] = amt if amt is not None else (raw if raw not in (None, "") else default)

    # Payment-form totals live as top-level scalars on the pedimento.
    for total_key in ("TOTAL_EFECTIVO", "TOTAL_OTROS"):
        amt = parse_amount(entity_map.get(total_key))
        if amt is not None:
            out[total_key] = amt

    return {k: _num(v) for k, v in out.items()}
