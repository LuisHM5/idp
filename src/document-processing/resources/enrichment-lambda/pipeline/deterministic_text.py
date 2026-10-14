"""Catalog-driven extraction from the text already loaded for a document.

This module is deliberately small and closed: catalogs may select text with
regular expressions, but they cannot execute code.  It runs before the
Bedrock extractor and returns the same plain entity/confidence shapes, so the
rest of the pipeline (normalization, derived fields and validation) does not
need a second implementation.

The feature is opt-in through a type-level ``deterministic_extraction`` block
and per-field ``text_source`` blocks.  A miss is not an error: the caller can
send only the unresolved part of the schema to the model, or leave it empty
for normal validation/review.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from document_catalog import FieldSpec, TypeRegistryEntry

from .confidence import row_key
from .normalize import parse_amount, parse_date_iso
from .pdf_pages import PageText

logger = logging.getLogger(__name__)

SOURCE_DETERMINISTIC_TEXT = "DETERMINISTIC_TEXT"
_ALLOWED_FALLBACKS = {"missing_fields", "full_model", "review"}
_ALLOWED_MODEL_FALLBACKS = {"if_missing", "never"}
_ALLOWED_FLAGS = {
    "ignore_case": re.IGNORECASE,
    "multiline": re.MULTILINE,
    "dotall": re.DOTALL,
}
_MAX_PATTERN_CHARS = 8_000


@dataclass
class ExtractionResult:
    entity_map: Dict[str, Any] = field(default_factory=dict)
    confidences: Dict[str, float] = field(default_factory=dict)
    row_confidences: Dict[str, Dict[str, float]] = field(default_factory=dict)
    source_overrides: Dict[str, str] = field(default_factory=dict)
    resolved_paths: Set[str] = field(default_factory=set)
    attempted_paths: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)
    profile_id: Optional[str] = None
    profile_status: str = "not_configured"
    profile_scores: Dict[str, int] = field(default_factory=dict)
    expected_paths: Set[str] = field(default_factory=set)
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class _TextWindow:
    text: str
    pages: Tuple[PageText, ...]
    spans: Tuple[Tuple[int, int, PageText], ...]


def policy_for(entry: TypeRegistryEntry) -> Optional[Dict[str, Any]]:
    raw = getattr(entry, "deterministic_extraction", None)
    if not isinstance(raw, dict) or not raw.get("enabled", True):
        return None
    fallback = str(raw.get("fallback") or "missing_fields").strip().lower()
    if fallback not in _ALLOWED_FALLBACKS:
        logger.warning(
            "deterministic_extraction fallback inválido %r en %s; usando missing_fields",
            fallback,
            entry.type_id,
        )
        fallback = "missing_fields"
    return {**raw, "fallback": fallback}


def enabled_for(entry: TypeRegistryEntry) -> bool:
    return policy_for(entry) is not None and bool(entry.field_tree)


def _window(pages: Sequence[PageText], page_filter: Any = None) -> _TextWindow:
    selected = list(pages)
    if page_filter == "first":
        selected = selected[:1]
    elif isinstance(page_filter, list):
        wanted = {int(n) for n in page_filter if isinstance(n, (int, float, str)) and str(n).isdigit()}
        selected = [page for page in selected if page.number in wanted]

    parts: List[str] = []
    spans: List[Tuple[int, int, PageText]] = []
    cursor = 0
    for page in selected:
        separator = "" if not parts else "\n"
        parts.append(separator + (page.text or ""))
        start = cursor + len(separator)
        end = start + len(page.text or "")
        spans.append((start, end, page))
        cursor = end
    return _TextWindow("".join(parts), tuple(selected), tuple(spans))


def _flags(names: Any) -> int:
    if names is None:
        return 0
    if isinstance(names, str):
        names = [names]
    value = 0
    for name in names if isinstance(names, list) else []:
        normalized = str(name).strip().lower()
        if normalized not in _ALLOWED_FLAGS:
            raise ValueError(f"flag no permitido: {name}")
        value |= _ALLOWED_FLAGS[normalized]
    return value


def _compile(pattern: Any, flags: Any = None) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("falta regex")
    if len(pattern) > _MAX_PATTERN_CHARS:
        raise ValueError(f"regex excede {_MAX_PATTERN_CHARS} caracteres")
    return re.compile(pattern, _flags(flags))


def _region(window: _TextWindow, config: Dict[str, Any]) -> _TextWindow:
    region = config.get("region")
    if not isinstance(region, dict):
        return window
    text = window.text
    start = 0
    end = len(text)
    if region.get("start_regex"):
        match = _compile(region["start_regex"], region.get("flags")).search(text)
        if not match:
            return _TextWindow("", window.pages, ())
        start = match.start() if region.get("include_start") else match.end()
    if region.get("end_regex"):
        match = _compile(region["end_regex"], region.get("flags")).search(text, start)
        if match:
            end = match.end() if region.get("include_end") else match.start()
    spans = tuple(
        (max(0, left - start), min(end, right) - start, page)
        for left, right, page in window.spans
        if right > start and left < end
    )
    return _TextWindow(text[start:end], window.pages, spans)


def _confidence(window: _TextWindow, start: int, end: int) -> float:
    matched = [
        float(getattr(page, "confidence", 1.0))
        for left, right, page in window.spans
        if right > start and left < end
    ]
    if not matched:
        matched = [float(getattr(page, "confidence", 1.0)) for page in window.pages]
    return round(min(matched), 4) if matched else 0.0


def _clean(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = re.sub(r"[ \t]+", " ", value).strip()
    return value if value else None


def _one_transform(value: Any, transform: Any) -> Any:
    if value is None:
        return None
    if isinstance(transform, str):
        op, options = transform.strip().lower(), {}
    elif isinstance(transform, dict):
        op = str(transform.get("op") or "").strip().lower()
        options = transform
    else:
        raise ValueError(f"transform no permitido: {transform}")
    if op in ("", "strip"):
        return str(value).strip() if isinstance(value, str) else value
    if op == "collapse_whitespace":
        return re.sub(r"\s+", " ", str(value)).strip() or None
    if op == "digits":
        digits = re.sub(r"\D", "", str(value))
        return digits or None
    if op == "alnum":
        cleaned = re.sub(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", "", str(value))
        return cleaned or None
    if op == "upper":
        return str(value).upper()
    if op == "lower":
        return str(value).lower()
    if op == "number":
        return parse_amount(value)
    if op == "integer":
        number = parse_amount(value)
        return int(number) if number is not None and number.is_integer() else None
    if op == "date_iso":
        return parse_date_iso(value)
    if op == "replace":
        return str(value).replace(str(options.get("old") or ""), str(options.get("new") or ""))
    if op == "regex_sub":
        regex = _compile(options.get("pattern"), options.get("flags"))
        return regex.sub(str(options.get("replacement") or ""), str(value))
    if op == "map":
        values = options.get("values")
        if not isinstance(values, dict):
            raise ValueError("transform map requiere values")
        key = str(value)
        return values.get(key, values.get(key.upper(), value))
    raise ValueError(f"transform no permitido: {op}")


def _transform(value: Any, transform: Any = None, transforms: Any = None) -> Any:
    pipeline = transforms if transforms is not None else transform
    if pipeline is None:
        return value
    if not isinstance(pipeline, list):
        pipeline = [pipeline]
    current = value
    for item in pipeline:
        current = _one_transform(current, item)
        if current is None:
            break
    return current


def _group(match: re.Match[str], group: Any) -> Any:
    selected = 1 if group is None and match.lastindex else (0 if group is None else group)
    if isinstance(selected, str) and selected.isdigit():
        selected = int(selected)
    try:
        return _clean(match.group(selected))
    except (IndexError, KeyError):
        raise ValueError(f"grupo inexistente: {group}") from None


def _selector_value(
    pages: Sequence[PageText], selector: Dict[str, Any]
) -> Optional[Tuple[Any, float]]:
    window = _region(_window(pages, selector.get("pages")), selector)
    regex = _compile(selector.get("regex"), selector.get("flags"))
    matches = list(regex.finditer(window.text))
    if not matches:
        return None
    occurrence = str(selector.get("occurrence") or "first").lower()
    if occurrence not in {"first", "last"}:
        raise ValueError(f"occurrence no permitido: {occurrence}")
    match = matches[-1] if occurrence == "last" else matches[0]
    if "value" in selector:
        value = selector.get("value")
    else:
        value = _group(match, selector.get("group"))
    value = _transform(value, selector.get("transform"), selector.get("transforms"))
    if value is None:
        return None
    if selector.get("accept_regex"):
        accept = _compile(selector["accept_regex"], selector.get("accept_flags"))
        if not accept.fullmatch(str(value)):
            return None
    return value, _confidence(window, match.start(), match.end())


def _extract_scalar(
    pages: Sequence[PageText], source: Dict[str, Any]
) -> Optional[Tuple[Any, float]]:
    selectors = source.get("selectors")
    if selectors is None and source.get("regex"):
        selectors = [source]
    if not isinstance(selectors, list):
        return None
    for selector in selectors:
        if not isinstance(selector, dict):
            continue
        result = _selector_value(pages, selector)
        if result is not None:
            return result
    return None


def _required_row_complete(row: Dict[str, Any], item_fields: Dict[str, FieldSpec]) -> bool:
    return all(
        not spec.required or spec.computed or row.get(name) not in (None, "", [])
        for name, spec in item_fields.items()
    )


def _extract_array(
    pages: Sequence[PageText], spec: FieldSpec, source: Dict[str, Any]
) -> Optional[Tuple[List[Any], float, Dict[str, Dict[str, float]]]]:
    selectors = source.get("row_selectors")
    if selectors is None:
        selectors = [source]
    if not isinstance(selectors, list):
        raise ValueError("row_selectors debe ser una lista")
    selectors = [selector for selector in selectors if isinstance(selector, dict)]
    if not selectors:
        return None

    rows: List[Any] = []
    row_confidences: Dict[str, Dict[str, float]] = {}
    accepted_confidences: List[float] = []
    for selector in selectors:
        selector_config = {**source, **selector}
        window = _region(
            _window(pages, selector_config.get("pages")), selector_config
        )
        pattern = selector.get("row_regex") or selector.get("regex")
        if not pattern:
            continue
        regex = _compile(pattern, selector.get("flags", source.get("flags")))
        matches = list(regex.finditer(window.text))
        if spec.item_fields:
            groups = selector.get("groups", source.get("groups"))
            if not isinstance(groups, dict):
                raise ValueError("un array de objetos requiere groups")
            for match in matches:
                row: Dict[str, Any] = {}
                for field_name in spec.item_fields:
                    group_spec = groups.get(field_name)
                    if group_spec is None:
                        row[field_name] = None
                        continue
                    if isinstance(group_spec, dict):
                        group_name = group_spec.get("group")
                    else:
                        group_name = group_spec
                    try:
                        raw_value = _clean(match.group(group_name))
                        row[field_name] = _transform(
                            raw_value,
                            group_spec.get("transform") if isinstance(group_spec, dict) else None,
                            group_spec.get("transforms") if isinstance(group_spec, dict) else None,
                        )
                    except (IndexError, KeyError):
                        raise ValueError(
                            f"grupo inexistente para {field_name}: {group_name}"
                        ) from None
                if not _required_row_complete(row, spec.item_fields):
                    continue
                confidence = _confidence(window, match.start(), match.end())
                rows.append(row)
                accepted_confidences.append(confidence)
                row_confidences[row_key(row)] = {
                    name: confidence
                    for name, value in row.items()
                    if value not in (None, "", [])
                }
        else:
            group = selector.get("group", source.get("group"))
            for match in matches:
                value = _group(match, group)
                if value is not None:
                    rows.append(value)
                    accepted_confidences.append(
                        _confidence(window, match.start(), match.end())
                    )

    dedupe = source.get("deduplicate_by")
    if isinstance(dedupe, str):
        dedupe = [dedupe]
    if rows and isinstance(dedupe, list):
        seen: Set[Tuple[Any, ...]] = set()
        unique: List[Any] = []
        for row in rows:
            key = tuple(row.get(name) for name in dedupe) if isinstance(row, dict) else (row,)
            if key not in seen:
                seen.add(key)
                unique.append(row)
        rows = unique
    if not rows:
        return None
    _check_row_count(pages, source, len(rows))
    aggregate = min(accepted_confidences, default=0.0)
    return rows, round(aggregate, 4), row_confidences


def _check_row_count(pages: Sequence[PageText], source: Dict[str, Any], found: int) -> None:
    """Reject a partially read table.

    A table where SOME rows matched looks resolved, and the rows the regex
    missed would be lost without any trace. `row_count.regex` counts the rows
    the page prints (e.g. lines that start with the row number) inside the
    array's own `region`; any difference sends the whole array to the
    fallback instead of publishing an incomplete list.
    """
    config = source.get("row_count")
    if not isinstance(config, dict):
        return
    window = _region(_window(pages, source.get("pages")), source)
    printed = len(_compile(config.get("regex"), config.get("flags")).findall(window.text))
    if printed != found:
        raise ValueError(f"row_count: {found} filas leídas de {printed} impresas")


def _profile_marker_matches(text: str, marker: Any) -> bool:
    if isinstance(marker, str):
        return bool(_compile(marker, ["ignore_case"]).search(text))
    if isinstance(marker, dict):
        return bool(_compile(marker.get("regex"), marker.get("flags", ["ignore_case"])).search(text))
    raise ValueError("cada marker de profile debe ser regex string u objeto")


def _detect_profile(
    pages: Sequence[PageText], policy: Dict[str, Any], result: ExtractionResult
) -> Optional[Dict[str, Any]]:
    profiles = policy.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        return None
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    for profile in profiles:
        if not isinstance(profile, dict) or not profile.get("id"):
            result.warnings.append("deterministic_extraction profile inválido: falta id")
            continue
        window = _window(pages, profile.get("pages"))
        markers = profile.get("markers") or []
        try:
            score = sum(1 for marker in markers if _profile_marker_matches(window.text, marker))
        except (TypeError, ValueError, re.error) as exc:
            result.warnings.append(f"profile {profile.get('id')}: {exc}")
            continue
        profile_id = str(profile["id"])
        result.profile_scores[profile_id] = score
        minimum = int(profile.get("min_score") or 1)
        if score >= minimum:
            candidates.append((score, profile))
    if not candidates:
        result.profile_status = "unknown"
        return None
    best_score = max(score for score, _profile in candidates)
    winners = [profile for score, profile in candidates if score == best_score]
    if len(winners) != 1:
        result.profile_status = "ambiguous"
        return None
    winner = winners[0]
    result.profile_id = str(winner["id"])
    result.profile_status = "matched"
    result.expected_paths = {
        str(path).strip() for path in winner.get("expected_fields", []) if str(path).strip()
    }
    return winner


def _field_spec_at_path(tree: Dict[str, FieldSpec], path: str) -> Optional[FieldSpec]:
    current = tree
    spec: Optional[FieldSpec] = None
    for part in path.split("."):
        spec = current.get(part)
        if spec is None:
            return None
        current = spec.fields if spec.type == "object" else {}
    return spec


def field_spec_at_path(entry: TypeRegistryEntry, path: str) -> Optional[FieldSpec]:
    """Resolve a catalog leaf for callers that need provenance filtering."""
    return _field_spec_at_path(entry.field_tree or {}, path)


def _set_path(target: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _apply_profile_constants(
    result: ExtractionResult, entry: TypeRegistryEntry, profile: Optional[Dict[str, Any]], pages: Sequence[PageText]
) -> None:
    constants = profile.get("constants") if isinstance(profile, dict) else None
    if not isinstance(constants, dict):
        return
    usable = [float(getattr(page, "confidence", 1.0)) for page in pages if getattr(page, "source", "") != "empty"]
    confidence = round(min(usable), 4) if usable else 0.0
    for path, value in constants.items():
        path = str(path)
        spec = _field_spec_at_path(entry.field_tree or {}, path)
        if spec is None or spec.computed or spec.type in {"object", "array"}:
            result.warnings.append(f"profile {result.profile_id}: constant path inválido {path}")
            continue
        _set_path(result.entity_map, path, value)
        result.confidences[path] = confidence
        result.resolved_paths.add(path)
        result.attempted_paths.add(path)
        result.source_overrides[path] = SOURCE_DETERMINISTIC_TEXT


def _record_sources(result: ExtractionResult, path: str, spec: FieldSpec, value: Any) -> None:
    result.source_overrides[path] = SOURCE_DETERMINISTIC_TEXT
    if spec.type == "array" and isinstance(value, list):
        if spec.item_fields:
            for index, row in enumerate(value):
                if not isinstance(row, dict):
                    continue
                for name, cell in row.items():
                    if cell not in (None, "", []):
                        result.source_overrides[f"{path}[{index}].{name}"] = SOURCE_DETERMINISTIC_TEXT
        else:
            for index, cell in enumerate(value):
                if cell not in (None, "", []):
                    result.source_overrides[f"{path}[{index}]"] = SOURCE_DETERMINISTIC_TEXT


def _extract_tree(
    pages: Sequence[PageText], tree: Dict[str, FieldSpec], result: ExtractionResult,
    prefix: str = "",
) -> Dict[str, Any]:
    entity: Dict[str, Any] = {}
    for name, spec in tree.items():
        if spec.computed:
            continue
        path = f"{prefix}{name}"
        source = spec.params.get("text_source")
        if spec.type == "object":
            children = _extract_tree(pages, spec.fields, result, path + ".")
            if children:
                entity[name] = children
            continue
        if not isinstance(source, dict):
            continue
        result.attempted_paths.add(path)
        try:
            if spec.type == "array":
                extracted = _extract_array(pages, spec, source)
                if extracted is None:
                    continue
                value, confidence, rows = extracted
                result.row_confidences.update(rows)
            else:
                extracted = _extract_scalar(pages, source)
                if extracted is None:
                    continue
                value, confidence = extracted
            entity[name] = value
            result.confidences[path] = confidence
            result.resolved_paths.add(path)
            _record_sources(result, path, spec, value)
        except (TypeError, ValueError, re.error) as exc:
            warning = f"{path}: {exc}"
            result.warnings.append(warning)
            logger.warning("text_source inválido en %s: %s", path, exc)
    return entity


def extract(pages: Sequence[PageText], entry: TypeRegistryEntry) -> ExtractionResult:
    started = time.perf_counter()
    result = ExtractionResult()
    policy = policy_for(entry) or {}
    profile = _detect_profile(pages, policy, result)
    if profile is not None:
        invalid_expected = sorted(
            path for path in result.expected_paths
            if (
                (spec := _field_spec_at_path(entry.field_tree or {}, path)) is None
                or spec.type == "object"
                or spec.computed
            )
        )
        if invalid_expected or not result.expected_paths:
            result.warnings.extend(
                f"profile {result.profile_id}: expected field inválido {path}"
                for path in invalid_expected
            )
            if not result.expected_paths:
                result.warnings.append(
                    f"profile {result.profile_id}: expected_fields está vacío"
                )
            result.profile_status = "invalid_config"
            result.profile_id = None
            result.expected_paths.clear()
            profile = None
    result.entity_map = _extract_tree(pages, entry.field_tree or {}, result)
    _apply_profile_constants(result, entry, profile, pages)
    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return result


def _model_fallback(spec: FieldSpec) -> str:
    source = spec.params.get("text_source")
    raw = source.get("model_fallback", "if_missing") if isinstance(source, dict) else "if_missing"
    normalized = str(raw).strip().lower()
    if normalized not in _ALLOWED_MODEL_FALLBACKS:
        logger.warning("model_fallback inválido %r en %s; usando if_missing", raw, spec.name)
        return "if_missing"
    return normalized


def unresolved_tree(
    tree: Dict[str, FieldSpec], resolved_paths: Set[str], fallback: str,
    prefix: str = "", expected_paths: Optional[Set[str]] = None,
) -> Dict[str, FieldSpec]:
    """Return the exact field-tree subset that still needs model extraction."""
    if fallback == "review":
        return {}
    if fallback == "full_model":
        return {name: spec for name, spec in tree.items() if not spec.computed}

    unresolved: Dict[str, FieldSpec] = {}
    for name, spec in tree.items():
        if spec.computed:
            continue
        path = f"{prefix}{name}"
        if spec.type == "object":
            children = unresolved_tree(
                spec.fields, resolved_paths, fallback, path + ".", expected_paths
            )
            if children:
                unresolved[name] = replace(spec, fields=children)
            continue
        if expected_paths is not None and path not in expected_paths:
            continue
        if path in resolved_paths or _model_fallback(spec) == "never":
            continue
        unresolved[name] = spec
    return unresolved


def merge_entities(
    deterministic: Dict[str, Any], model: Dict[str, Any], tree: Dict[str, FieldSpec], prefix: str = "",
    resolved_paths: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    resolved_paths = resolved_paths or set()
    merged: Dict[str, Any] = {}
    for name, spec in tree.items():
        path = f"{prefix}{name}"
        if spec.type == "object":
            merged[name] = merge_entities(
                deterministic.get(name) if isinstance(deterministic.get(name), dict) else {},
                model.get(name) if isinstance(model.get(name), dict) else {},
                spec.fields,
                path + ".",
                resolved_paths,
            )
        elif path in resolved_paths:
            merged[name] = deterministic.get(name)
        else:
            merged[name] = model.get(name)
    return merged


def path_list(tree: Dict[str, FieldSpec], prefix: str = "") -> List[str]:
    paths: List[str] = []
    for name, spec in tree.items():
        path = f"{prefix}{name}"
        if spec.type == "object":
            paths.extend(path_list(spec.fields, path + "."))
        else:
            paths.append(path)
    return paths


def validate_entry(entry: TypeRegistryEntry) -> List[str]:
    """Return catalog errors for the closed deterministic DSL.

    Runtime remains fail-open, but build/sync tooling calls this validator so
    a typo normally never reaches DynamoDB or a deployed Lambda.
    """
    errors: List[str] = []
    raw_policy = getattr(entry, "deterministic_extraction", None)
    if not isinstance(raw_policy, dict):
        return errors
    fallback = str(raw_policy.get("fallback") or "missing_fields").lower()
    if fallback not in _ALLOWED_FALLBACKS:
        errors.append(f"deterministic_extraction.fallback inválido: {fallback}")
    profile_fallback = str(raw_policy.get("profile_fallback") or "full_model").lower()
    if profile_fallback not in {"full_model", "review"}:
        errors.append(f"deterministic_extraction.profile_fallback inválido: {profile_fallback}")

    profiles = raw_policy.get("profiles") or []
    if not isinstance(profiles, list):
        errors.append("deterministic_extraction.profiles debe ser lista")
        profiles = []

    seen_profiles: Set[str] = set()
    for index, profile in enumerate(profiles):
        base = f"deterministic_extraction.profiles[{index}]"
        if not isinstance(profile, dict):
            errors.append(f"{base} debe ser objeto")
            continue
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            errors.append(f"{base}.id es obligatorio")
        elif profile_id in seen_profiles:
            errors.append(f"{base}.id duplicado: {profile_id}")
        seen_profiles.add(profile_id)
        markers = profile.get("markers")
        if not isinstance(markers, list) or not markers:
            errors.append(f"{base}.markers debe contener al menos una regex")
        else:
            for marker_index, marker in enumerate(markers):
                try:
                    if isinstance(marker, str):
                        _compile(marker, ["ignore_case"])
                    elif isinstance(marker, dict):
                        _compile(marker.get("regex"), marker.get("flags", ["ignore_case"]))
                    else:
                        raise ValueError("debe ser string u objeto")
                except (TypeError, ValueError, re.error) as exc:
                    errors.append(f"{base}.markers[{marker_index}]: {exc}")
        try:
            minimum = int(profile.get("min_score") or 1)
            if minimum < 1 or (isinstance(markers, list) and minimum > len(markers)):
                errors.append(f"{base}.min_score fuera de rango")
        except (TypeError, ValueError):
            errors.append(f"{base}.min_score debe ser entero")
        expected = profile.get("expected_fields")
        if not isinstance(expected, list) or not expected:
            errors.append(f"{base}.expected_fields debe contener al menos un campo")
            expected = []
        expected_paths = {str(path) for path in expected}
        for path in expected:
            spec = _field_spec_at_path(entry.field_tree or {}, str(path))
            if spec is None:
                errors.append(f"{base}.expected_fields apunta a campo inexistente: {path}")
            elif spec.type == "object" or spec.computed:
                errors.append(f"{base}.expected_fields debe apuntar a hoja extraíble: {path}")
        constants = profile.get("constants") or {}
        if not isinstance(constants, dict):
            errors.append(f"{base}.constants debe ser objeto")
        else:
            for path in constants:
                spec = _field_spec_at_path(entry.field_tree or {}, str(path))
                if spec is None or spec.type in {"object", "array"} or spec.computed:
                    errors.append(f"{base}.constants apunta a campo no escalar: {path}")
                if str(path) not in expected_paths:
                    errors.append(f"{base}.constants debe incluirse en expected_fields: {path}")
        for path in expected_paths - {str(path) for path in constants}:
            spec = _field_spec_at_path(entry.field_tree or {}, path)
            source = spec.params.get("text_source") if spec is not None else None
            if isinstance(source, dict) and source.get("model_fallback") == "never":
                errors.append(
                    f"{base}.expected_fields no puede usar model_fallback=never: {path}"
                )

    def validate_region(config: Dict[str, Any], path: str) -> None:
        region = config.get("region")
        if region is None:
            return
        if not isinstance(region, dict):
            errors.append(f"{path}.region debe ser objeto")
            return
        for key in ("start_regex", "end_regex"):
            if region.get(key):
                try:
                    _compile(region[key], region.get("flags"))
                except (TypeError, ValueError, re.error) as exc:
                    errors.append(f"{path}.region.{key}: {exc}")

    def validate_transforms(config: Dict[str, Any], path: str) -> None:
        pipeline = config.get("transforms", config.get("transform"))
        if pipeline is None:
            return
        values = pipeline if isinstance(pipeline, list) else [pipeline]
        for index, item in enumerate(values):
            try:
                _one_transform("1", item)
            except (TypeError, ValueError, re.error) as exc:
                errors.append(f"{path}.transforms[{index}]: {exc}")

    def validate_selector(config: Dict[str, Any], path: str, row: bool = False) -> Optional[re.Pattern[str]]:
        pattern = config.get("row_regex") if row else config.get("regex")
        pattern = pattern or config.get("regex")
        try:
            compiled = _compile(pattern, config.get("flags"))
        except (TypeError, ValueError, re.error) as exc:
            errors.append(f"{path}: {exc}")
            return None
        validate_region(config, path)
        validate_transforms(config, path)
        return compiled

    def walk(tree: Dict[str, FieldSpec], prefix: str = "") -> None:
        for name, spec in tree.items():
            path = f"{prefix}{name}"
            source = spec.params.get("text_source")
            if spec.type == "object":
                walk(spec.fields, path + ".")
                continue
            if source is None:
                continue
            if spec.computed:
                errors.append(f"fields.{path}.text_source no aplica a computed")
                continue
            if not isinstance(source, dict):
                errors.append(f"fields.{path}.text_source debe ser objeto")
                continue
            if str(source.get("model_fallback") or "if_missing") not in _ALLOWED_MODEL_FALLBACKS:
                errors.append(f"fields.{path}.text_source.model_fallback inválido")
            validate_region(source, f"fields.{path}.text_source")
            if spec.type == "array":
                selectors = source.get("row_selectors")
                if selectors is None:
                    selectors = [source]
                if not isinstance(selectors, list) or not selectors:
                    errors.append(f"fields.{path}.text_source.row_selectors debe ser lista no vacía")
                    continue
                for index, selector in enumerate(selectors):
                    selector_path = f"fields.{path}.text_source.row_selectors[{index}]"
                    if not isinstance(selector, dict):
                        errors.append(f"{selector_path} debe ser objeto")
                        continue
                    compiled = validate_selector(selector, selector_path, row=True)
                    groups = selector.get("groups", source.get("groups"))
                    if spec.item_fields:
                        if not isinstance(groups, dict):
                            errors.append(f"{selector_path}.groups es obligatorio")
                            continue
                        unknown = sorted(set(groups) - set(spec.item_fields))
                        if unknown:
                            errors.append(f"{selector_path}.groups contiene campos inexistentes: {', '.join(unknown)}")
                        missing_required = sorted(
                            field_name for field_name, field_spec in spec.item_fields.items()
                            if field_spec.required and not field_spec.computed and field_name not in groups
                        )
                        if missing_required:
                            errors.append(f"{selector_path}.groups omite requeridos: {', '.join(missing_required)}")
                        if compiled is not None:
                            for field_name, group_spec in groups.items():
                                group = group_spec.get("group") if isinstance(group_spec, dict) else group_spec
                                try:
                                    compiled.groupindex.get(str(group)) if isinstance(group, str) else None
                                    if isinstance(group, str) and group not in compiled.groupindex and not group.isdigit():
                                        raise ValueError(f"grupo inexistente {group}")
                                except (TypeError, ValueError) as exc:
                                    errors.append(f"{selector_path}.groups.{field_name}: {exc}")
                                if isinstance(group_spec, dict):
                                    validate_transforms(group_spec, f"{selector_path}.groups.{field_name}")
                row_count = source.get("row_count")
                if row_count is not None:
                    try:
                        if not isinstance(row_count, dict):
                            raise ValueError("debe ser objeto {regex, flags}")
                        _compile(row_count.get("regex"), row_count.get("flags"))
                    except (TypeError, ValueError, re.error) as exc:
                        errors.append(f"fields.{path}.text_source.row_count: {exc}")
                dedupe = source.get("deduplicate_by") or []
                dedupe = [dedupe] if isinstance(dedupe, str) else dedupe
                for field_name in dedupe if isinstance(dedupe, list) else []:
                    if spec.item_fields and field_name not in spec.item_fields:
                        errors.append(f"fields.{path}.text_source.deduplicate_by inexistente: {field_name}")
            else:
                selectors = source.get("selectors")
                if selectors is None:
                    selectors = [source]
                if (
                    not isinstance(selectors, list)
                    or (not selectors and source.get("model_fallback") != "never")
                ):
                    errors.append(f"fields.{path}.text_source.selectors debe ser lista no vacía")
                    continue
                for index, selector in enumerate(selectors):
                    selector_path = f"fields.{path}.text_source.selectors[{index}]"
                    if not isinstance(selector, dict):
                        errors.append(f"{selector_path} debe ser objeto")
                        continue
                    compiled = validate_selector(selector, selector_path)
                    if compiled is None:
                        continue
                    if "value" not in selector:
                        group = selector.get("group")
                        selected = 1 if group is None and compiled.groups else (0 if group is None else group)
                        if isinstance(selected, str) and selected.isdigit():
                            selected = int(selected)
                        try:
                            compiled.groupindex.get(str(selected)) if isinstance(selected, str) else None
                            if isinstance(selected, str) and selected not in compiled.groupindex:
                                raise ValueError(f"grupo inexistente {selected}")
                            if isinstance(selected, int) and selected > compiled.groups:
                                raise ValueError(f"grupo inexistente {selected}")
                        except (TypeError, ValueError) as exc:
                            errors.append(f"{selector_path}.group: {exc}")

    walk(entry.field_tree or {})
    return errors
