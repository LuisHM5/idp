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
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from . import confidence as conf_policy
from .normalize import parse_amount

logger = logging.getLogger(__name__)


class LiquidacionNormalizada(NamedTuple):
    """What `build_liquidacion_normalizada` returns: the computed values and,
    per computed field, the confidence it inherited from its sources.

    A total is exactly as trustworthy as the cells it added up, so its
    confidence is the MEAN of theirs (`pipeline.confidence.combine`). A
    concept that simply isn't in the table keeps the confidence of the table
    it was looked up in — "we read the liquidation cleanly and this concept
    is not there" is a measured 0.00, not an unknown one. Without this every
    LIQUIDACION_NORMALIZADA subfield came back with `confidence: null`.
    """

    values: Dict[str, Any]
    confidences: Dict[str, Optional[float]]


def _num(value: Any) -> Any:
    """Coerce DynamoDB Decimals to JSON-serializable numbers; pass others through."""
    if isinstance(value, Decimal):
        f = float(value)
        return int(f) if f.is_integer() else f
    return value

# "CONTRIBUCIONES[CLAVE_CONTRIBUCION=IEPS].FORMA_PAGO" or, with a nested
# array field (field-centric DSL), "PARTIDAS.CONTRIBUCIONES_PARTIDA[CLAVE_
# CONTRIBUCION=IEPS].FORMA_PAGO" → (array path, key, val, field).
_PATH_RE = re.compile(r"^([\w.]+)\[(\w+)=([^\]]+)\]\.(\w+)$")


def _norm(text: Any) -> str:
    return str(text or "").strip().upper()


def _find_cuadro_config(form_mapping: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Locate `cuadro_liquidacion_fields` inside any declared form (legacy DSL:
    a separate `form_mapping` block alongside `structured_fields`)."""
    for form in (form_mapping or {}).values():
        if isinstance(form, dict) and form.get("cuadro_liquidacion_fields"):
            return form["cuadro_liquidacion_fields"]
    return None


def _config_from_field_tree(entry) -> Optional[Dict[str, Any]]:
    """Derive the same target -> {source, concept_aliases, value_field,
    filters, default} mapping straight from the field-centric DSL, when the
    type declares no separate `form_mapping` block. Each subfield of the
    computed `LIQUIDACION_NORMALIZADA` object carries its own mapping keys
    directly (see catalogs/base/aduanas/PEDIMENTO_ADUANAL.json) instead of duplicating the
    field name in a parallel config structure — `FieldSpec.params` already
    holds every JSON key beyond type/required/hint/validation. A bare
    `concept_aliases` with no explicit `source` defaults to summing
    CUADRO_LIQUIDACION.CONCEPTOS (the common case: DTA, IGI, IVA, ...).
    """
    field_tree = getattr(entry, "field_tree", None) or {}
    node = field_tree.get("LIQUIDACION_NORMALIZADA")
    if node is None or node.type != "object":
        return None
    config: Dict[str, Any] = {}
    for sub_name, sub_spec in node.fields.items():
        params = dict(sub_spec.params)
        if not params:
            continue
        if "concept_aliases" in params and "source" not in params:
            params["source"] = "CUADRO_LIQUIDACION.CONCEPTOS"
        config[sub_name] = params
    return config or None


def _resolve_path(entity_map: Dict[str, Any], path: str) -> Any:
    """Resolve a dotted path against an entity map that may use either
    convention: the legacy DSL's flat dotted keys (`entity_map["CUADRO_
    LIQUIDACION.CONCEPTOS"]`) or the field-centric DSL's genuine nesting
    (`entity_map["CUADRO_LIQUIDACION"]["CONCEPTOS"]`). Tries the flat literal
    key first (cheap, and correct for the legacy shape), then walks the path
    segment by segment through nested dicts."""
    if path in entity_map:
        return entity_map[path]
    if "." not in path:
        return entity_map.get(path)
    current: Any = entity_map
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            # A list step (e.g. PARTIDAS) followed by another segment means
            # "collect that key from every row" — and flatten when the
            # collected value is itself a list (PARTIDAS.CONTRIBUCIONES_PARTIDA:
            # each partida's own list of contributions, concatenated across
            # all partidas), mirroring validators.engine._get_field.
            collected: List[Any] = []
            for item in current:
                if not isinstance(item, dict):
                    continue
                v = item.get(part)
                if isinstance(v, list):
                    collected.extend(v)
                else:
                    collected.append(v)
            current = collected
        else:
            return None
    return current


def _sum_concepts(
    concepts: List[Dict[str, Any]],
    aliases: List[str],
    value_field: str,
    row_confidences: Optional[Dict[str, Dict[str, float]]] = None,
) -> Tuple[float, List[Optional[float]]]:
    """Sum `value_field` over concepts whose clave/description matches an alias.

    Returns the total together with the confidence of every cell that fed it,
    so the caller can report the mean (see `LiquidacionNormalizada`).

    `concepts` may be rows from either CUADRO_LIQUIDACION.CONCEPTOS (clave
    field: CLAVE_CONCEPTO / CLAVE_CONCEPTO_NORMALIZADA) or CONTRIBUCIONES
    (clave field: CLAVE_CONTRIBUCION) — `concept_pool` in
    `build_liquidacion_normalizada` picks whichever the model actually
    populated, so both shapes must be checked here."""
    wanted = {_norm(a) for a in aliases}
    total = 0.0
    confidences: List[Optional[float]] = []
    for c in concepts:
        if not isinstance(c, dict):
            continue
        clave = _norm(
            c.get("CLAVE_CONCEPTO_NORMALIZADA") or c.get("CLAVE_CONCEPTO") or c.get("CLAVE_CONTRIBUCION")
        )
        desc = _norm(c.get("DESCRIPCION"))
        if clave in wanted or desc in wanted:
            amt = parse_amount(c.get(value_field))
            if amt is not None:
                total += amt
                confidences.append(conf_policy.cell_confidence(row_confidences, c, value_field))
    return round(total, 2), confidences


def _sum_contribuciones(
    rows: List[Dict[str, Any]],
    filters: Dict[str, Any],
    value_field: str,
    row_confidences: Optional[Dict[str, Dict[str, float]]] = None,
) -> Tuple[float, List[Optional[float]]]:
    """Sum `value_field` over CONTRIBUCIONES rows matching every filter, with
    the confidence of each summed cell (same contract as `_sum_concepts`)."""
    total = 0.0
    confidences: List[Optional[float]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if not _row_matches(r, filters):
            continue
        amt = parse_amount(r.get(value_field))
        if amt is not None:
            total += amt
            confidences.append(conf_policy.cell_confidence(row_confidences, r, value_field))
    return round(total, 2), confidences


def _row_matches(row: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    for key, expected in filters.items():
        if key == "TASA":
            got = parse_amount(row.get("TASA"))
            if got is None or abs(got - float(expected)) > 0.01:
                return False
        elif key == "TASA_NOT_IN":
            got = parse_amount(row.get("TASA"))
            if got is None or any(abs(got - float(v)) <= 0.01 for v in expected):
                return False
        elif _norm(row.get(key)) != _norm(expected):
            return False
    return True


def _first_by_path(
    entity_map: Dict[str, Any],
    path: str,
    row_confidences: Optional[Dict[str, Dict[str, float]]] = None,
) -> Tuple[Optional[str], Optional[float]]:
    """Resolve 'ARRAY[KEY=VAL].FIELD' to the first matching row's field value,
    plus that cell's own confidence."""
    m = _PATH_RE.match(path)
    if not m:
        return None, None
    array_name, key, val, out_field = m.groups()
    for r in _resolve_path(entity_map, array_name) or []:
        if isinstance(r, dict) and _norm(r.get(key)) == _norm(val):
            v = r.get(out_field)
            if v not in (None, "", []):
                return str(v).strip(), conf_policy.cell_confidence(row_confidences, r, out_field)
    return None, None


def build_liquidacion_normalizada(
    entity_map: Dict[str, Any],
    entry,
    confidences: Optional[Dict[str, float]] = None,
    row_confidences: Optional[Dict[str, Dict[str, float]]] = None,
) -> Optional[LiquidacionNormalizada]:
    """Build LIQUIDACION_NORMALIZADA from the catalog's cuadro config.

    The mapping is read from `form_mapping.<FORM>.cuadro_liquidacion_fields`
    (legacy DSL) when present, else derived straight from the field-centric
    DSL's own subfield params (`_config_from_field_tree`). Returns None when
    the type declares neither, so callers only attach the object for types
    that actually define the mapping.

    `confidences` (per extracted path) and `row_confidences` (per array row,
    see `pipeline.confidence`) are what the extractor measured; they are read
    only to decide how much to trust each computed value — never to change
    one. Omit them and the values come back identical, with every confidence
    None: nothing was measured, so the field falls back to how well the
    document itself was read (`engine.resolve_field_tree`'s
    `default_confidence`) rather than to a 0.0 that would get it discarded.
    """
    config = (_find_cuadro_config(getattr(entry, "form_mapping", {}) or {})
              or _config_from_field_tree(entry))
    if not config:
        return None

    # Start from declared defaults (shape + zero/null values), then fill.
    # Field-centric DSL types (schema_version 4.x) declare the shape via
    # field_tree instead of structured_fields — derive the same null/[]
    # skeleton from there so array containers not covered by the config loop
    # below (CONCEPTOS, POR_FORMA_PAGO, ...) still come back present.
    field_tree = getattr(entry, "field_tree", None)
    if field_tree and "LIQUIDACION_NORMALIZADA" in field_tree:
        defaults = {
            k: ([] if v.type == "array" else None)
            for k, v in field_tree["LIQUIDACION_NORMALIZADA"].fields.items()
        }
    else:
        defaults = ((getattr(entry, "structured_fields", {}) or {})
                    .get("LIQUIDACION_NORMALIZADA") or {})
    out: Dict[str, Any] = dict(defaults)
    confidences = confidences or {}
    out_conf: Dict[str, Optional[float]] = {}

    def resolved_conf(cells: List[Optional[float]], source_paths: Tuple[str, ...]) -> Optional[float]:
        """Confidence for one computed field: the mean of the cells that fed
        it, or — when nothing matched and the declared `default` was used —
        the confidence of the table it was looked up in.

        None when neither exists (a source the type doesn't even declare, or
        one the extractor never filled): the caller registers nothing and the
        field falls back to how well the DOCUMENT was read, like any other
        unmeasured value. Reporting 0.0 here would delete legitimate zeros —
        a pedimento with no IEPS partidas has `CL_IEPS30 = 0` for a good
        reason, and the discard floor would turn it into null."""
        combined = conf_policy.combine(cells)
        if combined is not None:
            return combined
        return conf_policy.combine([confidences.get(p) for p in source_paths])

    concepts = [c for c in (_resolve_path(entity_map, "CUADRO_LIQUIDACION.CONCEPTOS") or []) if isinstance(c, dict)]
    contribuciones = [c for c in (entity_map.get("CONTRIBUCIONES") or []) if isinstance(c, dict)]
    # In many pedimentos the model files the liquidation line items under
    # CONTRIBUCIONES (same shape: CLAVE_CONTRIBUCION/DESCRIPCION/FORMA_PAGO/
    # IMPORTE) and leaves CUADRO_LIQUIDACION.CONCEPTOS empty. Use CONTRIBUCIONES
    # as the concept source in that case so DTA/IGI/IVA/PRV are not lost. Only
    # one of the two is populated per document, so there is no double counting.
    concept_pool = concepts if concepts else contribuciones
    pool_path = "CUADRO_LIQUIDACION.CONCEPTOS" if concepts else "CONTRIBUCIONES"

    for target, spec in config.items():
        if not isinstance(spec, dict):
            continue
        source = spec.get("source", "")
        value_field = spec.get("value_field", "IMPORTE")
        default = spec.get("default", 0)

        if source == "CUADRO_LIQUIDACION.CONCEPTOS":
            aliases = spec.get("concept_aliases") or [target]
            val, cells = _sum_concepts(concept_pool, aliases, value_field, row_confidences)
            out[target] = val if val else default
            out_conf[target] = resolved_conf(cells, (pool_path,))
        elif source == "CONTRIBUCIONES":
            val, cells = _sum_contribuciones(
                contribuciones, spec.get("filters") or {}, value_field, row_confidences
            )
            out[target] = val if val else default
            out_conf[target] = resolved_conf(cells, ("CONTRIBUCIONES",))
        elif source == "PARTIDAS.CONTRIBUCIONES_PARTIDA":
            # Per-partida contributions (e.g. IEPS, which is declared per
            # PARTIDA rather than once at pedimento level): flatten every
            # partida's CONTRIBUCIONES_PARTIDA rows, then filter same as the
            # top-level CONTRIBUCIONES case. Was previously unreachable — the
            # exact-string branches above never matched this source, so every
            # IEPS_* bucket silently stayed at its `default` (0) regardless of
            # what was actually printed on the pedimento.
            partida_rows: List[Dict[str, Any]] = []
            for partida in (entity_map.get("PARTIDAS") or []):
                if isinstance(partida, dict):
                    partida_rows.extend(
                        r for r in (partida.get("CONTRIBUCIONES_PARTIDA") or []) if isinstance(r, dict)
                    )
            val, cells = _sum_contribuciones(
                partida_rows, spec.get("filters") or {}, value_field, row_confidences
            )
            out[target] = val if val else default
            out_conf[target] = resolved_conf(cells, ("PARTIDAS.CONTRIBUCIONES_PARTIDA", "PARTIDAS"))
        elif "[" in source and "]" in source:  # path like ARRAY[K=V].FIELD
            picked, cell_conf = _first_by_path(entity_map, source, row_confidences)
            out[target] = picked or default
            array_path = source.split("[", 1)[0]
            out_conf[target] = resolved_conf([cell_conf], (array_path,))
        else:  # DOCUMENT source: a plain scalar field name (e.g. CUADRO_LIQUIDACION.TOTAL)
            raw = _resolve_path(entity_map, source)
            source_paths = (source,)
            if raw in (None, "", []):
                # Fall back to a same-named scalar (TOTAL, TOTAL_PEDIMENTO)
                raw = entity_map.get(target) or entity_map.get("TOTAL_PEDIMENTO")
                source_paths = (target, "TOTAL_PEDIMENTO")
            amt = parse_amount(raw)
            out[target] = amt if amt is not None else (raw if raw not in (None, "") else default)
            out_conf[target] = resolved_conf([], source_paths)

    # Payment-form totals live as top-level scalars on the pedimento.
    for total_key in ("TOTAL_EFECTIVO", "TOTAL_OTROS"):
        amt = parse_amount(entity_map.get(total_key))
        if amt is not None:
            out[total_key] = amt
            out_conf[total_key] = resolved_conf([], (total_key,))

    return LiquidacionNormalizada(
        values={k: _num(v) for k, v in out.items()},
        confidences=out_conf,
    )


def apply_liquidacion_normalizada(
    entity_map: Dict[str, Any],
    confidences: Dict[str, float],
    entry,
    row_confidences: Optional[Dict[str, Dict[str, float]]] = None,
) -> Optional[Dict[str, Any]]:
    """Build the object, attach it to `entity_map` and register each computed
    subfield's confidence under its dotted path — the one call every pipeline
    makes, mirroring `pipeline.derived.apply_derived_fields`.

    Registering the confidences is what keeps `LIQUIDACION_NORMALIZADA.*` out
    of `null` territory downstream: `engine.resolve_field_tree` reads the same
    `confidences` map for computed fields as for extracted ones.

    Returns the values (or None when the type declares no cuadro mapping).
    """
    result = build_liquidacion_normalizada(entity_map, entry, confidences, row_confidences)
    if result is None:
        return None
    entity_map["LIQUIDACION_NORMALIZADA"] = result.values
    for sub_name, conf in result.confidences.items():
        if conf is not None:
            confidences[f"LIQUIDACION_NORMALIZADA.{sub_name}"] = conf
    return result.values
