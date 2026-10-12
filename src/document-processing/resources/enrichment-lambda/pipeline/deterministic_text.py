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


def _transform(value: Any, transform: Any) -> Any:
    if value is None or transform in (None, "", "strip"):
        return value
    if transform == "digits":
        digits = re.sub(r"\D", "", str(value))
        return digits or None
    if transform == "upper":
        return str(value).upper()
    raise ValueError(f"transform no permitido: {transform}")


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
    value = _transform(_group(match, selector.get("group")), selector.get("transform"))
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
    window = _region(_window(pages, source.get("pages")), source)
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
                    group_name = groups.get(field_name)
                    if group_name is None:
                        row[field_name] = None
                        continue
                    try:
                        row[field_name] = _clean(match.group(group_name))
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
    aggregate = min(accepted_confidences, default=0.0)
    return rows, round(aggregate, 4), row_confidences


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
    result.entity_map = _extract_tree(pages, entry.field_tree or {}, result)
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
    prefix: str = "",
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
            children = unresolved_tree(spec.fields, resolved_paths, fallback, path + ".")
            if children:
                unresolved[name] = replace(spec, fields=children)
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
