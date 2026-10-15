"""Catalog-driven structured extraction over page text (Bedrock).

Takes the text of a document segment (native or OCR) and the catalog entry
for its type, and asks the model to fill the catalog schema:
- scalar `extraction_fields` (with `extraction_hints` as guidance)
- `repeatable_fields` as arrays of objects (ALL rows, not just the first)

Long documents are chunked by pages; scalars merge with first-non-null-wins
(higher confidence preferred) and arrays are concatenated in page order.

Anti-hallucination contract enforced in the prompt:
- absent keys when the data is not on the document (never invented)
- values verbatim as printed (normalization happens deterministically later)
- per-field confidence 0-1 reported by the model, then combined with the
  OCR confidence of the source pages.
"""

from __future__ import annotations

import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Set, Tuple

from document_catalog import FieldSpec, TypeRegistryEntry

from . import type_check
from .bedrock import converse_structured, effort_for, resolve_model
from .confidence import row_key
from .pdf_pages import PageText
from .row_common import fill_row_common_fields

logger = logging.getLogger(__name__)

_MAX_CHUNK_CHARS = 28_000
# Claude Haiku 5.5's Bedrock output ceiling is 128K tokens (Haiku 4.5 had
# 64,000, verified against the live Converse API — a request above the
# ceiling is rejected outright; `bedrock._clamp_max_tokens` lowers it for
# models with a smaller one, e.g. MiniMax's 8K). With adaptive thinking the
# thinking tokens also count against this cap. The old 8,192 cap was self-imposed and far
# below that: for schemas with many repeatable-field rows (e.g.
# PEDIMENTO_ADUANAL's PARTIDAS/CUADRO_LIQUIDACION), a row-dense chunk's
# output regularly exceeded 8,192, truncating mid-JSON and forcing the
# recursive split-in-half retry below to fire repeatedly. Raising the cap
# costs nothing by itself (billed by tokens actually generated, not the
# ceiling) and avoids paying for truncated, unusable output followed by a
# retry. Well under the model ceiling.
_MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
# Chunks of the same document are extracted concurrently: each is an
# independent Bedrock call, so a multi-chunk document runs in parallel
# instead of serially. Bounded (not equal to EXTRACT_WORKERS in
# expediente.py) so a multi-segment expediente's segment-level pool times
# this pool without multiplying concurrent Bedrock calls too far; boto3
# clients are thread-safe (pipeline.bedrock.client() is an lru_cache
# singleton, already relied on by the segment-level pool).
_CHUNK_WORKERS = int(os.environ.get("CHUNK_WORKERS", "2"))
# A large field-centric schema with repeatable rows is harder for slower
# models to finish reliably in one tool call. Above this static-schema size,
# split non-repeatable fields and arrays into two independent calls. They use
# the same bounded pool as page chunks, so one-page documents run both calls
# concurrently without multiplying the existing concurrency ceiling.
_FIELD_TREE_SPLIT_SCHEMA_CHARS = int(os.environ.get("FIELD_TREE_SPLIT_SCHEMA_CHARS", "10000"))

# Scope of the legacy call that re-asks only the branches a compact response
# got wrong. Like `arrays`, it does not repeat the type check: the same
# response already answered it.
_RETRY_SCOPE = "retry"
_NO_TYPE_CHECK_SCOPES = frozenset({"arrays", _RETRY_SCOPE})

AI_RESPONSE_COMPACT = "compact"
AI_RESPONSE_LEGACY = "legacy"
_AI_RESPONSE_FORMATS = {AI_RESPONSE_COMPACT, AI_RESPONSE_LEGACY}


class CompactResponseError(ValueError):
    """The compact tool payload cannot be decoded without guessing data."""

    def __init__(self, message: str, usage: Optional[Dict[str, int]] = None):
        super().__init__(message)
        self.usage = dict(usage or {})


def resolve_ai_response_format(
    type_entry: TypeRegistryEntry, requested: Optional[str] = None
) -> Dict[str, str]:
    """Resolve request -> catalog -> environment -> compact.

    The caller already authorizes request overrides.  Keeping resolution here
    makes local runs and direct unit calls obey the same catalog/global policy.
    """
    request_value = str(requested or "").strip().lower()
    if request_value in _AI_RESPONSE_FORMATS:
        return {"selected": request_value, "source": "api", "requested": request_value}
    catalog_value = str(getattr(type_entry, "ai_response_format", None) or "").strip().lower()
    if catalog_value in _AI_RESPONSE_FORMATS:
        return {"selected": catalog_value, "source": "catalog", "requested": "inherit"}
    env_value = str(os.environ.get("AI_RESPONSE_FORMAT") or "").strip().lower()
    if env_value in _AI_RESPONSE_FORMATS:
        return {"selected": env_value, "source": "environment", "requested": "inherit"}
    return {"selected": AI_RESPONSE_COMPACT, "source": "default", "requested": "inherit"}


@dataclass
class ExtractedDoc:
    entity_map: Dict[str, Any]           # scalars: str|None; repeatables: list[dict]
    confidences: Dict[str, float]        # per-field combined confidence 0-1
    meta: Dict[str, Any] = field(default_factory=dict)
    # Per-ROW confidences of array fields: {row_key(row): {SUBCAMPO: conf}}.
    # Keyed by row content (pipeline.confidence.row_key), not by index, so it
    # survives the chunk merge below. Consumers: `pipeline.liquidacion` (a
    # total inherits the mean of the cells it summed) and
    # `validators.engine.resolve_field_tree` (each row cell reports its own
    # number instead of the whole array sharing the segment's OCR score).
    row_confidences: Dict[str, Dict[str, float]] = field(default_factory=dict)


def _schema_block(type_entry: TypeRegistryEntry) -> str:
    scalar_lines = []
    for name in type_entry.extraction_fields:
        if name in type_entry.repeatable_fields:
            continue
        hint = type_entry.extraction_hints.get(name, "")
        req = " (OBLIGATORIO)" if name in type_entry.required_fields else ""
        scalar_lines.append(f"- {name}{req}{': ' + hint if hint else ''}")
    array_lines = []
    for name, subfields in type_entry.repeatable_fields.items():
        hint = type_entry.extraction_hints.get(name, "")
        array_lines.append(f"- {name} [{', '.join(subfields)}]{': ' + hint if hint else ''}")
    block = "CAMPOS ESCALARES:\n" + "\n".join(scalar_lines)
    if array_lines:
        block += "\n\nCAMPOS REPETIBLES (extrae TODAS las filas/renglones, no solo el primero):\n" + "\n".join(array_lines)
    return block


def _pages_block(pages: List[PageText]) -> str:
    parts = []
    for p in pages:
        parts.append(f"===== PAGINA {p.number} ({p.source}) =====\n{p.text}")
    return "\n\n".join(parts)


def _build_prompts(
    type_entry: TypeRegistryEntry, pages: List[PageText], arrays_only: bool
) -> Tuple[str, str]:
    """Return (cache_prefix, user_text).

    `cache_prefix` is the large STATIC part (type header + task + schema +
    instructions), identical across chunks and across documents of the same
    type, so it is marked with a Bedrock cachePoint. `user_text` is the variable
    per-call page text. Splitting them lets prompt caching discount the repeated
    prefix without changing what the model sees."""
    task = (
        "Extrae UNICAMENTE los campos repetibles (arrays) del schema."
        if arrays_only
        else "Extrae los campos del schema."
    )
    cache_prefix = (
        f"Documento clasificado como {type_entry.type_id}. {type_entry.description}\n\n"
        f"{task}\n\n{_schema_block(type_entry)}\n\n"
        "CONTEXTO ADUANAL: las referencias de pedimento aparecen en formas como "
        "'26 07 3435 6041248' (completo AA-ADUANA-PATENTE-CONSECUTIVO), "
        "'P0823 6001187' (patente+consecutivo), '-999-9999-6041248', o solo el "
        "consecutivo de 7 digitos junto a etiquetas REF/PED/Pedimento/Observaciones.\n"
        "CONTEXTO CFDI: EMISOR es quien factura (bloque 'Emisor', normalmente junto "
        "al logo con su regimen fiscal); RECEPTOR es el cliente (bloque 'Receptor' o "
        "'Cliente'). Un RFC tiene 12-13 caracteres alfanumericos (ej. OFU910626UQ0); "
        "NUNCA uses folios, series, codigos postales o numeros de cliente como RFC. "
        "El texto puede venir en columnas desordenadas: asocia cada RFC al bloque "
        "Emisor/Receptor al que pertenece, no al mas cercano en el texto plano. "
        "NUNCA uses como EMISOR_RFC el RFC del Proveedor Autorizado de Certificacion "
        "(PAC) ni RFCs que aparezcan dentro de la cadena original '||1.1|...||' o de "
        "los sellos digitales. Para campos como RFC_IMPORTADOR / IMPORTADOR_RFC: si "
        "el documento no identifica explicitamente al importador, usa null; el RFC "
        "del beneficiario de una transferencia, del banco o de la cuenta destino NO "
        "es el importador.\n\n"
        "REGLAS ESTRICTAS:\n"
        "- NUNCA inventes un valor. Si el dato no aparece en el texto, usa null.\n"
        "- Copia los valores tal cual aparecen impresos (montos con comas, fechas en su formato original).\n"
        "- No calcules ni derives valores que no esten impresos.\n"
        "- En los arrays extrae TODAS las filas visibles en estas paginas; una fila por objeto.\n"
        "- Reporta confidence 0.0-1.0 por campo: 1.0 = el dato es textual e inequivoco; "
        "baja el valor si el OCR se ve corrupto o hay ambiguedad.\n\n"
        "FORMATO DE RESPUESTA (solo JSON, sin markdown):\n"
        "{\"fields\": {\"CAMPO\": {\"value\": \"...\"|null, \"confidence\": 0.95}},\n"
        " \"arrays\": {\"CAMPO\": [{\"SUBCAMPO\": \"...\"|null}]}}"
    )
    user_text = "TEXTO DEL DOCUMENTO:\n\n" + _pages_block(pages)
    return cache_prefix, user_text


def _tool_schema(type_entry: TypeRegistryEntry, arrays_only: bool) -> Dict[str, Any]:
    """JSON schema for the extraction tool's input, mirroring the catalog
    exactly (same field/subfield names as `_schema_block`'s prose listing).

    Passed as `converse_structured`'s `tool_schema` so Bedrock's tool-calling
    validates the model's response against it — the model can only emit keys
    that exist here, so it can't rename or restructure fields the way it can
    when merely asked (in prose) to produce JSON in a given shape. See
    `converse_structured` for why the prose-only instruction isn't enough for
    a schema this size.
    """
    field_props: Dict[str, Any] = {}
    if not arrays_only:
        for name in type_entry.extraction_fields:
            if name in type_entry.repeatable_fields:
                continue
            field_props[name] = {
                "type": "object",
                "properties": {
                    "value": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
            }
    array_props: Dict[str, Any] = {}
    for name, subfields in type_entry.repeatable_fields.items():
        array_props[name] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {sf: {"type": ["string", "null"]} for sf in subfields},
            },
        }
    return {
        "type": "object",
        "properties": {
            "fields": {"type": "object", "properties": field_props},
            "arrays": {"type": "object", "properties": array_props},
        },
        "required": ["fields", "arrays"],
    }


def _chunk_pages(pages: List[PageText], max_chars: int = _MAX_CHUNK_CHARS) -> List[List[PageText]]:
    chunks: List[List[PageText]] = []
    current: List[PageText] = []
    size = 0
    for p in pages:
        page_len = len(p.text) + 40
        if current and size + page_len > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(p)
        size += page_len
    if current:
        chunks.append(current)
    return chunks


def _call_model(
    type_entry: TypeRegistryEntry,
    pages: List[PageText],
    arrays_only: bool,
    model_id: Optional[str],
    cache: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, int]]:
    """One extraction call. On max_tokens overflow, split the pages and merge.

    Returns (fields, arrays, usage) — usage is returned rather than
    accumulated into a shared dict so this function (and its recursion) can
    be called concurrently from multiple worker threads without shared
    mutable state; callers merge usage themselves via `_merge_usage`.

    `cache` marks the static prefix with a Bedrock cachePoint. Only enable it
    when the prefix will be reused (multi-chunk document or overflow split):
    caching a prefix used just once costs ~1.25x (net loss); reused it saves
    ~90% on the prefix. The recursive overflow split guarantees reuse, so it
    always caches.

    Uses `converse_structured` (Bedrock tool-calling), not free-text JSON:
    verified against a real call that with a schema this large (e.g.
    PEDIMENTO_ADUANAL's 136 scalar fields + 47 repeatable groups) the model
    silently invents its own field names/shape instead of following the
    prose instruction — even with a "{" prefill forcing valid JSON, the JSON
    just isn't the requested schema. Tool-calling constrains the response to
    `tool_schema`, so the model structurally cannot rename or restructure
    fields."""
    cache_prefix, user_text = _build_prompts(type_entry, pages, arrays_only)
    schema = _tool_schema(type_entry, arrays_only)
    parsed, meta = converse_structured(
        user_text,
        tool_schema=schema,
        tool_name="extract_document_fields",
        tool_description=f"Extrae los campos de un documento {type_entry.type_id} segun el schema exacto.",
        model_id=model_id or resolve_model("extractor", type_entry),
        effort=effort_for("extractor", type_entry), max_tokens=_MAX_OUTPUT_TOKENS,
        # The prefix (schema + hints + strict rules) goes on EVERY call;
        # `cache` only decides whether it's also marked as cacheable. It used
        # to travel exclusively as `cache_system`, so a document that fit in
        # ONE chunk was extracted with no hints and no rules at all — the model
        # saw the raw page text plus bare field names, and filled them by
        # guessing what the names meant (the acuse "COVE2689JP013 / 35079805"
        # landed in `Factura`, the pesos value in `Valor_ComercialDol`).
        system=None if cache else cache_prefix,
        cache_system=cache_prefix if cache else None,
        stage="extractor",
    )
    usage: Dict[str, int] = {
        k: v for k, v in (meta.get("usage") or {}).items() if isinstance(v, int)
    }

    if meta.get("stopReason") == "max_tokens" and len(pages) > 1:
        mid = len(pages) // 2
        # Splitting means ≥2 calls share this prefix → caching now pays off.
        # Kept sequential (not a nested pool): the caller's ThreadPoolExecutor
        # already bounds total in-flight Bedrock calls to _CHUNK_WORKERS, and
        # a nested pool here would multiply concurrency past that bound.
        left_f, left_a, left_u = _call_model(type_entry, pages[:mid], arrays_only, model_id, cache=True)
        right_f, right_a, right_u = _call_model(type_entry, pages[mid:], arrays_only, model_id, cache=True)
        return (
            _merge_fields(left_f, right_f),
            _merge_arrays(left_a, right_a),
            _merge_usage(_merge_usage(usage, left_u), right_u),
        )

    if not parsed:
        logger.error("extractor: empty tool response for %s", type_entry.type_id)
    fields = parsed.get("fields") or {}
    arrays = parsed.get("arrays") or {}
    return (
        fields if isinstance(fields, dict) else {},
        arrays if isinstance(arrays, dict) else {},
        usage,
    )


def _merge_usage(base: Dict[str, int], extra: Dict[str, int]) -> Dict[str, int]:
    merged = dict(base)
    for k, v in extra.items():
        merged[k] = merged.get(k, 0) + v
    return merged


def _merge_fields(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for name, slot in extra.items():
        value = slot.get("value") if isinstance(slot, dict) else slot
        current = merged.get(name)
        current_value = current.get("value") if isinstance(current, dict) else current
        if current_value in (None, "") and value not in (None, ""):
            merged[name] = slot
    return merged


def _merge_arrays(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    merged = {k: list(v) for k, v in base.items() if isinstance(v, list)}
    for name, items in extra.items():
        if not isinstance(items, list):
            continue
        existing = merged.setdefault(name, [])
        seen = {row_key(item) if isinstance(item, dict) else json.dumps(item, sort_keys=True, default=str) for item in existing}
        for item in items:
            identity = row_key(item) if isinstance(item, dict) else json.dumps(item, sort_keys=True, default=str)
            if identity not in seen:  # O(1) average, not a growing list scan
                existing.append(item)
                seen.add(identity)
    return merged


_JSON_TYPE_MAP = {"string": "string", "number": "number", "boolean": "boolean", "date": "string"}


def _field_tree_schema_lines(tree: Dict[str, FieldSpec], indent: int = 0) -> List[str]:
    lines: List[str] = []
    pad = "  " * indent
    for name, spec in tree.items():
        if spec.computed:
            continue
        req = " (OBLIGATORIO)" if spec.required else ""
        hint = f": {spec.hint}" if spec.hint else ""
        if spec.type == "object":
            lines.append(f"{pad}- {name}{req} [objeto]{hint}")
            lines.extend(_field_tree_schema_lines(spec.fields, indent + 1))
        elif spec.type == "array":
            lines.append(f"{pad}- {name}{req} [lista, extrae TODAS las filas visibles]{hint}")
            if spec.item_fields:
                lines.extend(_field_tree_schema_lines(spec.item_fields, indent + 1))
        else:
            lines.append(f"{pad}- {name}{req}{hint}")
    return lines


def _field_tree_schema_block(field_tree: Dict[str, FieldSpec]) -> str:
    return "CAMPOS:\n" + "\n".join(_field_tree_schema_lines(field_tree))


def _partition_field_tree(
    tree: Dict[str, FieldSpec],
) -> Tuple[Dict[str, FieldSpec], Dict[str, FieldSpec]]:
    """Split a schema into non-repeatable and repeatable branches.

    Objects are retained in both branches when necessary so nested arrays such
    as ``CUADRO_LIQUIDACION.CONCEPTOS`` keep their original path. The split is
    based exclusively on DSL shape, never on a document type or field name.
    """
    non_arrays: Dict[str, FieldSpec] = {}
    arrays: Dict[str, FieldSpec] = {}
    for name, spec in tree.items():
        if spec.computed:
            continue
        if spec.type == "array":
            arrays[name] = spec
        elif spec.type == "object":
            child_non_arrays, child_arrays = _partition_field_tree(spec.fields)
            if child_non_arrays:
                non_arrays[name] = replace(spec, fields=child_non_arrays)
            if child_arrays:
                arrays[name] = replace(spec, fields=child_arrays)
        else:
            non_arrays[name] = spec
    return non_arrays, arrays


def _field_tree_call_partitions(
    tree: Dict[str, FieldSpec],
) -> List[Tuple[str, Dict[str, FieldSpec]]]:
    """Return one full-schema call or two bounded calls for a large schema."""
    non_arrays, arrays = _partition_field_tree(tree)
    should_split = (
        bool(non_arrays)
        and bool(arrays)
        and len(_field_tree_schema_block(tree)) >= _FIELD_TREE_SPLIT_SCHEMA_CHARS
    )
    if should_split:
        return [("non_arrays", non_arrays), ("arrays", arrays)]
    return [("all", tree)]


# The key a row uses to report how well IT was read (see `_field_tree_tool_props`).
ROW_CONFIDENCE_KEY = "confidence"


def _field_tree_tool_props(tree: Dict[str, FieldSpec], in_row: bool = False) -> Dict[str, Any]:
    """Recursive tool-schema properties from a field_tree — real support for
    arbitrarily nested objects/arrays, which the legacy `_tool_schema` (flat
    scalars + one level of arrays) cannot express. `computed` fields (e.g.
    LIQUIDACION_NORMALIZADA) are omitted: a deterministic post-extraction step
    fills them, so asking the model for a value that gets overwritten anyway
    would just be wasted output tokens.

    `in_row` marks the cells of an array row: they are asked for as bare
    values plus ONE `confidence` for the whole row, instead of the
    `{value, confidence}` pair every top-level scalar uses. Measured on a real
    139-page expediente, the per-CELL confidence was 44% of everything the
    model emitted (16,662 output tokens -> ~9,465 without it) — and output is
    both the slow half of a Bedrock call and the expensive one (5x input).
    A row is the unit that gets read well or badly anyway: cells inherit
    their row's number (`_parse_field_tree_response`)."""
    props: Dict[str, Any] = {}
    for name, spec in tree.items():
        if spec.computed:
            continue
        if spec.type == "object":
            props[name] = {
                "type": "object",
                "properties": _field_tree_tool_props(spec.fields, in_row),
            }
        elif spec.type == "array":
            if spec.item_fields:
                item_schema = {
                    "type": "object",
                    "properties": {
                        **_field_tree_tool_props(spec.item_fields, in_row=True),
                        ROW_CONFIDENCE_KEY: {"type": "number"},
                    },
                }
            else:
                item_schema = {"type": _JSON_TYPE_MAP.get(spec.item_type or "string", "string")}
            props[name] = {"type": "array", "items": item_schema}
        else:
            json_type = _JSON_TYPE_MAP.get(spec.type, "string")
            if in_row:
                props[name] = {"type": [json_type, "null"]}
            else:
                props[name] = {
                    "type": "object",
                    "properties": {"value": {"type": [json_type, "null"]}, "confidence": {"type": "number"}},
                }
    return props


def _field_tree_tool_schema(field_tree: Dict[str, FieldSpec], scope: str = "all") -> Dict[str, Any]:
    props = _field_tree_tool_props(field_tree)
    # La verificación de tipo viaja en el MISMO tool call (pipeline/type_check):
    # ~30 tokens de salida en vez de una llamada aparte. Solo la pide la
    # llamada de escalares — la partición `arrays` ve el mismo texto y
    # contestaría lo mismo, y un chunk debe responder una sola vez.
    if scope not in _NO_TYPE_CHECK_SCOPES:
        props = {type_check.SIGNAL_KEY: type_check.tool_property(), **props}
    return {"type": "object", "properties": props}


def _compact_manifest(
    tree: Dict[str, FieldSpec], prefix: str = ""
) -> Tuple[Dict[str, FieldSpec], Dict[str, FieldSpec]]:
    """Flat scalar/array indexes used by both compact schema and parser."""
    scalars: Dict[str, FieldSpec] = {}
    tables: Dict[str, FieldSpec] = {}
    for name, spec in tree.items():
        if spec.computed:
            continue
        path = f"{prefix}.{name}" if prefix else name
        if spec.type == "object":
            child_scalars, child_tables = _compact_manifest(spec.fields, path)
            scalars.update(child_scalars)
            tables.update(child_tables)
        elif spec.type == "array":
            tables[path] = spec
        else:
            scalars[path] = spec
    return scalars, tables


def _compact_scalar_pair_schema(spec: FieldSpec) -> Dict[str, Any]:
    value_type = _JSON_TYPE_MAP.get(spec.type, "string")
    return {
        "type": "array",
        "items": {"type": [value_type, "number", "boolean", "null"]},
        "minItems": 2,
        "maxItems": 2,
        "description": "[valor, confianza_0_a_1]",
    }


def _compact_table_schema(spec: FieldSpec) -> Dict[str, Any]:
    # Rows are positional and are validated against the catalog by Python.
    # Keeping this generic avoids repeating every column name in every row;
    # the static prompt below states their exact order. The fixed length is
    # what tells the model an empty cell is still a cell: without it a blank
    # last column was simply dropped and the row could not be decoded.
    width = len([name for name, child in spec.item_fields.items() if not child.computed]) + 1 if spec.item_fields else 2
    return {
        "type": "array",
        "items": {"type": "array", "items": {}, "minItems": width, "maxItems": width},
    }


def _compact_tool_schema(field_tree: Dict[str, FieldSpec], scope: str = "all") -> Dict[str, Any]:
    scalars, tables = _compact_manifest(field_tree)
    props: Dict[str, Any] = {
        "s": {
            "type": "object",
            "properties": {path: _compact_scalar_pair_schema(spec) for path, spec in scalars.items()},
            "additionalProperties": False,
        },
        "t": {
            "type": "object",
            "properties": {path: _compact_table_schema(spec) for path, spec in tables.items()},
            "additionalProperties": False,
        },
    }
    if scope not in _NO_TYPE_CHECK_SCOPES:
        props["d"] = {
            "type": "array",
            "items": {},
            "minItems": 2,
            "maxItems": 3,
            "description": "[corresponde, confianza, descripcion_opcional]",
        }
    return {
        "type": "object",
        "properties": props,
        "required": ["s", "t"],
        "additionalProperties": False,
    }


def _compact_column_description(spec: FieldSpec) -> str:
    if spec.type == "array":
        if spec.item_fields:
            columns = [
                f"{name}{'<' + _compact_column_description(child) + '>' if child.type in {'array', 'object'} else ''}"
                for name, child in spec.item_fields.items() if not child.computed
            ]
            return "filas(" + ", ".join(columns) + ", confidence)"
        return "filas(valor, confidence)"
    if spec.type == "object":
        return "objeto(" + ", ".join(
            f"{name}{'<' + _compact_column_description(child) + '>' if child.type in {'array', 'object'} else ''}"
            for name, child in spec.fields.items() if not child.computed
        ) + ")"
    return spec.type


def _build_compact_field_tree_prompts(
    type_entry: TypeRegistryEntry,
    pages: List[PageText],
    field_tree: Dict[str, FieldSpec],
    scope: str,
) -> Tuple[str, str]:
    base, user_text = _build_field_tree_prompts(type_entry, pages, field_tree, scope)
    if scope not in _NO_TYPE_CHECK_SCOPES:
        base = base.replace(
            type_check.PROMPT_RULE,
            "- Antes de extraer decide si el texto ES el documento declarado. "
            "En d responde [corresponde, confianza, descripcion]: false SOLO para "
            "un documento de otra naturaleza; una variante del mismo documento sí "
            "corresponde. Extrae los campos de todos modos.\n",
        )
    # The shared prompt ends with the legacy nested-output instructions.
    # Keeping both shapes in one prompt makes the model choose unpredictably;
    # compact replaces that final section while retaining all extraction hints
    # and anti-hallucination rules above it.
    base = base.split("\n\nFORMATO DE RESPUESTA", 1)[0]
    scalars, tables = _compact_manifest(field_tree)
    scalar_lines = "\n".join(f"- {path}: [valor, confidence]" for path in scalars)
    table_lines = "\n".join(
        f"- {path}: {_compact_column_description(spec)}" for path, spec in tables.items()
    )
    compact = (
        "\n\nPROTOCOLO COMPACTO OBLIGATORIO:\n"
        "- Responde solamente con las llaves s, t y, cuando el schema la admita, d.\n"
        "- s es un objeto disperso: omite campos no encontrados; cada valor es [valor, confidence].\n"
        "- t es un objeto de tablas: cada fila usa EXACTAMENTE el orden indicado y termina en confidence.\n"
        "- Cada fila lleva TODAS sus celdas: una celda vacía se escribe null, nunca se omite.\n"
        "- Para una lista anidada, su celda contiene sus filas posicionales con la misma regla.\n"
        "- No repitas nombres de columnas dentro de cada fila y no emitas nulls para escalares ausentes.\n"
        "- d, si se solicita, es [corresponde, confianza, descripcion_opcional].\n"
        f"ESCALARES:\n{scalar_lines or '(ninguno)'}\n"
        f"TABLAS:\n{table_lines or '(ninguna)'}"
    )
    return base + compact, user_text


def _build_field_tree_prompts(
    type_entry: TypeRegistryEntry,
    pages: List[PageText],
    field_tree: Optional[Dict[str, FieldSpec]] = None,
    scope: str = "all",
) -> Tuple[str, str]:
    selected_tree = type_entry.field_tree if field_tree is None else field_tree
    task = {
        "non_arrays": "Extrae UNICAMENTE los campos escalares y objetos no repetibles del schema.",
        "arrays": "Extrae UNICAMENTE los campos tipo lista del schema.",
        "retry": "Extrae UNICAMENTE estos campos del schema; los demás ya se obtuvieron.",
    }.get(scope, "Extrae los campos del schema.")
    # "DECLARADO", no "clasificado": en la ruta con tipo declarado nadie
    # clasificó nada — quien subió el archivo dijo qué era, y la regla de
    # abajo le pide al modelo confirmarlo en vez de darlo por hecho.
    cache_prefix = (
        f"Documento DECLARADO como {type_entry.type_id}. {type_entry.description}\n\n"
        f"{task}\n\n"
        f"{_field_tree_schema_block(selected_tree)}\n\n"
        "REGLAS ESTRICTAS:\n"
        + (type_check.PROMPT_RULE if scope not in _NO_TYPE_CHECK_SCOPES else "") +
        "- NUNCA inventes un valor. Si un dato no aparece en el texto, OMITE esa llave por completo; "
        "no emitas null ni objetos vacios como relleno.\n"
        "- Copia los valores tal cual aparecen impresos (montos con comas, fechas en su formato original).\n"
        "- No calcules ni derives valores que no esten impresos.\n"
        "- En los campos tipo lista extrae TODAS las filas visibles en estas paginas; una fila por objeto.\n"
        "- Reporta confidence 0.0-1.0 por campo escalar: 1.0 = el dato es textual e inequivoco; "
        "baja el valor si el OCR se ve corrupto o hay ambiguedad.\n"
        "- En las filas de una lista NO pongas confidence en cada celda: una sola confidence "
        "por fila, que describe que tan bien se leyo ESA fila completa.\n\n"
        "FORMATO DE RESPUESTA (solo JSON, sin markdown), usando exclusivamente llaves presentes en el schema: "
        "escalares encontrados como {\"value\": ..., \"confidence\": 0.95}, objetos con sus subcampos anidados, "
        "listas como arreglo de objetos cuyos subcampos son el valor directo (sin {value,...}) "
        "mas una sola \"confidence\" para toda la fila. Omite campos, subcampos, objetos y listas sin datos."
    )
    user_text = "TEXTO DEL DOCUMENTO:\n\n" + _pages_block(pages)
    return cache_prefix, user_text


def _coerce_container(node: Any, expect: type) -> Any:
    """A list/dict the model answered as a JSON STRING, parsed back.

    Even with a tool schema declaring `"type": "array"`, a model occasionally
    serializes the argument instead of emitting it. Measured on a real
    2-invoice PDF: the same model, on the same run, returned CONCEPTOS as a
    real array for one segment and, for the other, as a string holding the
    array followed by a leaked tag for the NEXT argument —

        '[{"DESCRIPCION": ...}]\n,<parameter name="MONEDA">{"value": "USD"}'

    — so the whole array was silently dropped and that invoice came back with
    zero line items, failing its own `minimum_items` rule.

    `raw_decode` is what makes it recoverable: it reads the JSON value at the
    START of the string and reports where it ended, so the leaked trailing
    fragment is ignored rather than poisoning the parse. The spilled argument
    itself stays lost — it is the model's mistake, not something to guess at.

    Nothing here invents data: only a string whose leading value really is the
    expected container is accepted. Anything else comes back untouched for the
    caller's existing `isinstance` check to reject exactly as before.
    """
    if isinstance(node, expect) or not isinstance(node, str):
        return node
    text = node.strip()
    if not text:
        return node
    try:
        parsed, _end = json.JSONDecoder().raw_decode(text)
    except (ValueError, TypeError):
        # One more recoverable shape, measured with MiniMax M2.5 on a scanned
        # W-9: the object arrived as a string whose JSON was complete except
        # for its LAST closing brace. Appending the closers it still owes adds
        # no data; a string cut anywhere else is not touched.
        closed = _close_unbalanced(text)
        if closed is None:
            return node
        try:
            parsed, _end = json.JSONDecoder().raw_decode(closed)
        except (ValueError, TypeError):
            return node
    return parsed if isinstance(parsed, expect) else node


def _close_unbalanced(text: str) -> Optional[str]:
    """`text` plus the `}`/`]` it still owes, or None when that is not the
    only thing wrong with it: a bracket closed out of order, a cut inside a
    string, or nothing left open."""
    owed: List[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            owed.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not owed or owed.pop() != ch:
                return None
    if in_string or not owed:
        return None
    return text + "".join(reversed(owed))


def _unwrap_container(node: Any, expect: type, own_keys: Any = ()) -> Any:
    """An object/array the model wrapped in the SCALAR envelope, unwrapped.

    The response format asks for `{"value": ..., "confidence": ...}` around
    scalars only, with objects carrying their subfields directly. Some models
    apply the envelope to containers too. Measured with MiniMax M2.5 on a real
    Dutch business-register extract:

        "DOMICILIO_FISCAL": {"value": {"CODIGO_POSTAL": {...}, ...}, "confidence": 1.0}

    — the parser then looked for CODIGO_POSTAL next to `value`, found nothing,
    and the whole address the model HAD read came back empty (while the object
    itself still counted as present).

    Only that exact shape is accepted: nothing but `value`/`confidence` at this
    level, `value` holding the expected container, and `value` not being one of
    the object's own subfield names (`own_keys`) — so a legitimate object is
    never mistaken for an envelope. The envelope's own confidence is dropped:
    the subfields carry theirs.
    """
    node = _coerce_container(node, expect)
    if (
        isinstance(node, dict)
        and "value" in node
        and "value" not in own_keys
        and set(node) <= {"value", "confidence"}
    ):
        inner = _coerce_container(node["value"], expect)
        if isinstance(inner, expect):
            return inner
    return node


def _parse_field_tree_response(
    raw: Any, tree: Dict[str, FieldSpec], seg_conf: float, prefix: str = "",
    in_row: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, float], Dict[str, Dict[str, float]]]:
    """Recursively turn one tool-call response (or one array row) into a
    nested plain-value entity dict, a flat {dotted.path: confidence} map, and
    the per-row confidences of every array field ({row_key: {campo: conf}}).
    Mirrors the scalar-handling of the legacy loop in `extract_document`
    (str/None coercion, confidence clamped and combined with OCR confidence).

    The model reports a confidence per CELL of an array row (the tool schema
    asks for `{value, confidence}` inside `item_fields` too). Those numbers
    used to be parsed and thrown away, so every cell of every row ended up
    reporting the array's segment-level OCR score — and a total summed from
    those rows had no confidence at all. They are kept here, keyed by row
    content so the chunk merge can't invalidate them."""
    entity: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}
    row_confidences: Dict[str, Dict[str, float]] = {}
    if not isinstance(raw, dict):
        raw = {}
    for name, spec in tree.items():
        path = f"{prefix}{name}"
        if spec.computed:
            entity[name] = None
            continue
        node = raw.get(name)
        if spec.type == "object":
            sub_entity, sub_conf, sub_rows = _parse_field_tree_response(
                _unwrap_container(node, dict, spec.fields), spec.fields, seg_conf, path + ".", in_row
            )
            entity[name] = sub_entity
            confidences.update(sub_conf)
            row_confidences.update(sub_rows)
        elif spec.type == "array":
            node = _unwrap_container(node, list)
            items = node if isinstance(node, list) else []
            if spec.item_fields:
                rows = []
                for item in items:
                    row_entity, row_conf, nested_rows = _parse_field_tree_response(
                        item, spec.item_fields, seg_conf, in_row=True
                    )
                    rows.append(row_entity)
                    row_confidences.update(nested_rows)
                    # The row reports ONE confidence for itself (see
                    # `_field_tree_tool_props`); every cell of that row
                    # inherits it, which is the shape `resolve_field_tree`
                    # already consumes. A row that reported none keeps
                    # whatever its cells said individually — older responses,
                    # and any model that answers with the per-cell shape
                    # anyway, still work unchanged.
                    declared = item.get(ROW_CONFIDENCE_KEY) if isinstance(item, dict) else None
                    inherited = _row_confidence(declared, seg_conf)
                    if inherited is not None:
                        row_conf = {name: inherited for name in row_entity}
                    if row_conf:
                        row_confidences[row_key(row_entity)] = row_conf
                entity[name] = rows
            else:
                entity[name] = [i for i in items if i not in (None, "")]
            if entity[name]:
                confidences[path] = round(seg_conf, 4)
        else:
            if isinstance(node, dict):
                value, llm_conf = node.get("value"), node.get("confidence")
            else:
                value, llm_conf = node, None
            if value in (None, "", "null"):
                entity[name] = None
                continue
            entity[name] = str(value).strip() if spec.type in ("string", "date") else value
            if llm_conf is None and in_row:
                # A cell inside a row is asked for as a bare value: its row
                # reports ONE confidence for all of them (see
                # `_field_tree_tool_props`). Defaulting to 0.85 here would
                # fabricate a per-cell number nobody measured AND outrank the
                # row's own. Recording nothing lets `resolve_field_tree` fall
                # back to the segment's real OCR number.
                continue
            try:
                llm_conf = float(llm_conf) if llm_conf is not None else 0.85
            except (TypeError, ValueError):
                llm_conf = 0.85
            if not math.isfinite(llm_conf):
                llm_conf = 0.85
            confidences[path] = round(max(0.0, min(1.0, llm_conf)) * seg_conf, 4)
    return entity, confidences, row_confidences


def _row_confidence(declared: Any, seg_conf: float) -> Optional[float]:
    """One row's self-reported confidence, combined with how well the page was
    read — the same product `_parse_field_tree_response` applies to a scalar's
    own number. Returns None when the row reported nothing usable, so the
    caller leaves the per-cell numbers alone instead of inventing one."""
    if declared is None:
        return None
    try:
        value = float(declared)
    except (TypeError, ValueError):
        return None
    # NaN/inf would survive the clamp below as a fabricated number: every
    # comparison against NaN is False, so `max(0.0, min(1.0, nan))` returns
    # 1.0 — a perfect confidence nobody reported.
    if not math.isfinite(value):
        return None
    return round(max(0.0, min(1.0, value)) * seg_conf, 4)


def _compact_confidence(value: Any, seg_conf: float, where: str) -> float:
    confidence = _row_confidence(value, seg_conf)
    if confidence is None:
        raise CompactResponseError(f"confidence inválida en {where}")
    return confidence


def _compact_scalar_value(value: Any, spec: FieldSpec, where: str) -> Any:
    if value is None:
        return None
    if spec.type in ("string", "date"):
        if isinstance(value, (dict, list)):
            raise CompactResponseError(f"valor no escalar en {where}")
        return str(value).strip()
    if spec.type == "number":
        if isinstance(value, bool) or isinstance(value, (dict, list)):
            raise CompactResponseError(f"número inválido en {where}")
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                raise CompactResponseError(f"número no finito en {where}")
            return value
        try:
            number = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            raise CompactResponseError(f"número inválido en {where}") from None
        if not math.isfinite(number):
            raise CompactResponseError(f"número no finito en {where}")
        return number
    if spec.type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "si", "sí"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        raise CompactResponseError(f"boolean inválido en {where}")
    return value


def _set_compact_path(target: Dict[str, Any], path: str, value: Any) -> None:
    node = target
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        if not isinstance(child, dict):
            raise CompactResponseError(f"colisión de ruta en {path}")
        node = child
    if parts[-1] in node and node[parts[-1]] != value:
        raise CompactResponseError(f"valor duplicado contradictorio en {path}")
    node[parts[-1]] = value


def _decode_compact_object(value: Any, spec: FieldSpec, seg_conf: float, where: str) -> Dict[str, Any]:
    fields = [(name, child) for name, child in spec.fields.items() if not child.computed]
    if not isinstance(value, list) or len(value) != len(fields):
        raise CompactResponseError(f"objeto posicional inválido en {where}")
    decoded: Dict[str, Any] = {}
    for index, (name, child) in enumerate(fields):
        cell = value[index]
        if cell is None:
            continue
        decoded[name] = _decode_compact_cell(cell, child, seg_conf, f"{where}.{name}")
    return decoded


def _decode_compact_cell(value: Any, spec: FieldSpec, seg_conf: float, where: str) -> Any:
    if spec.type == "array":
        rows, _aggregate, _row_conf = _decode_compact_array(value, spec, seg_conf, where)
        return rows
    if spec.type == "object":
        return _decode_compact_object(value, spec, seg_conf, where)
    return _compact_scalar_value(value, spec, where)


def _is_confidence(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0
    )


def _repair_compact_row(
    raw_row: Any, columns: List[Tuple[str, FieldSpec]], where: str
) -> List[Any]:
    """A positional row with every cell, or `CompactResponseError`.

    Models drop EMPTY trailing cells despite the protocol: a constancia row
    whose last column (FECHA_FIN) was blank came back with 7 cells instead of
    8, and the whole response was thrown away and paid again in legacy. When
    the row ends in a confidence and only optional, non-numeric trailing
    columns are missing, those columns were empty: pad them with null. A
    missing numeric column would make "is the last number the confidence or
    the cell?" a guess, so that, and any longer/shorter row, stays an error.
    """
    expected = len(columns) + 1
    if not isinstance(raw_row, list):
        raise CompactResponseError(f"fila desalineada en {where}: esperaba {expected} celdas")
    if len(raw_row) == expected:
        return raw_row
    missing = expected - len(raw_row)
    tail = columns[len(columns) - missing:] if 0 < missing < expected else []
    if (
        tail
        and raw_row
        and _is_confidence(raw_row[-1])
        and all(not spec.required and spec.type not in ("number", "array", "object") for _name, spec in tail)
    ):
        return list(raw_row[:-1]) + [None] * missing + [raw_row[-1]]
    raise CompactResponseError(f"fila desalineada en {where}: esperaba {expected} celdas")


def _decode_compact_array(
    raw: Any, spec: FieldSpec, seg_conf: float, path: str
) -> Tuple[List[Any], Optional[float], Dict[str, Dict[str, float]]]:
    if not isinstance(raw, list):
        raise CompactResponseError(f"tabla inválida en {path}")
    rows: List[Any] = []
    row_confidences: Dict[str, Dict[str, float]] = {}
    confidences: List[float] = []
    if spec.item_fields:
        columns = [(name, child) for name, child in spec.item_fields.items() if not child.computed]
        expected = len(columns) + 1
        for row_index, raw_row in enumerate(raw):
            where = f"{path}[{row_index}]"
            raw_row = _repair_compact_row(raw_row, columns, where)
            confidence = _compact_confidence(raw_row[-1], seg_conf, where)
            row: Dict[str, Any] = {}
            for index, (name, child) in enumerate(columns):
                cell = raw_row[index]
                if cell is None:
                    continue
                if child.type == "array":
                    nested, _nested_aggregate, nested_conf = _decode_compact_array(
                        cell, child, seg_conf, f"{where}.{name}"
                    )
                    row[name] = nested
                    row_confidences.update(nested_conf)
                else:
                    row[name] = _decode_compact_cell(cell, child, seg_conf, f"{where}.{name}")
            rows.append(row)
            confidences.append(confidence)
            if row:
                row_confidences[row_key(row)] = {name: confidence for name in row}
    else:
        item_spec = FieldSpec(name="$item", type=spec.item_type or "string")
        for row_index, raw_row in enumerate(raw):
            where = f"{path}[{row_index}]"
            if not isinstance(raw_row, list) or len(raw_row) != 2:
                raise CompactResponseError(f"fila escalar inválida en {where}")
            value = _compact_scalar_value(raw_row[0], item_spec, where)
            if value is None:
                continue
            rows.append(value)
            confidences.append(_compact_confidence(raw_row[1], seg_conf, where))
    aggregate = min(confidences) if confidences else None
    return rows, aggregate, row_confidences


_TRUE_WORDS = frozenset({"true", "si", "sí", "yes", "corresponde"})
_FALSE_WORDS = frozenset({"false", "no", "no corresponde"})


def _compact_type_signal(raw: Any, type_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, list) and len(raw) in {2, 3} and isinstance(raw[0], str):
        # Measured (MiniMax, constancia 2026-10-14): `d` came back as
        # ["CONSTANCIA_SITUACION_FISCAL_PORTAL", 1.0, "..."] — the declared
        # type's name where `true` was expected. That, and a yes/no word, are
        # unambiguous; any other string is not read as a verdict.
        word = raw[0].strip().lower()
        if word in _TRUE_WORDS or (type_id and word == type_id.strip().lower()):
            raw = [True, *raw[1:]]
        elif word in _FALSE_WORDS:
            raw = [False, *raw[1:]]
    if not isinstance(raw, list) or len(raw) not in {2, 3} or not isinstance(raw[0], bool):
        raise CompactResponseError("señal d inválida")
    try:
        confidence = float(raw[1])
    except (TypeError, ValueError):
        raise CompactResponseError("confianza inválida en d") from None
    if not math.isfinite(confidence):
        raise CompactResponseError("confianza no finita en d")
    return {
        "corresponde": raw[0],
        "confianza": max(0.0, min(1.0, confidence)),
        "descripcion": str(raw[2]).strip() if len(raw) == 3 and raw[2] not in (None, "") else None,
    }


# Same default `_parse_field_tree_response` gives a scalar that reported no
# confidence of its own.
_DEFAULT_SCALAR_CONFIDENCE = 0.85


def _compact_pair(pair: Any, spec: FieldSpec, where: str) -> Tuple[Any, Any]:
    """(value, declared confidence) of one `s` entry, tolerating the shapes a
    model emits for an EMPTY or unscored field instead of omitting it.

    `null`, `[]`, `[null]`, `[null, c]` and `""` mean "not found" — exactly
    what the protocol asks the model to express by omission. `[v]` and a bare
    scalar are a value without its confidence. Measured: one blank
    `NOMBRE_LOCALIDAD` used to discard a whole constancia response, and MiniMax
    writes a blank field as `[0]` — a confidence with no value. In a text or
    date field a lone number in [0, 1] is that, never the value; in a numeric
    field it could be either, so that branch is re-asked instead of guessed.
    """
    if pair is None or pair == "":
        return None, None
    if isinstance(pair, list):
        if len(pair) == 2:
            return pair[0], pair[1]
        if len(pair) == 1:
            if _is_confidence(pair[0]):
                if spec.type in ("string", "date"):
                    return None, None
                if spec.type == "number":
                    raise CompactResponseError(f"par escalar ambiguo en {where}")
            return pair[0], None
        if not pair:
            return None, None
        raise CompactResponseError(f"par escalar inválido en {where}")
    if isinstance(pair, dict):
        if set(pair) <= {"value", "confidence"} and "value" in pair:
            return pair.get("value"), pair.get("confidence")
        raise CompactResponseError(f"par escalar inválido en {where}")
    return pair, None


def _parse_compact_response(
    raw: Any, tree: Dict[str, FieldSpec], seg_conf: float, type_id: Optional[str] = None,
) -> Tuple[
    Dict[str, Any], Dict[str, float], Dict[str, Dict[str, float]], Optional[Dict[str, Any]], Dict[str, str]
]:
    """Decode a compact payload branch by branch.

    The fifth element maps each path that could not be decoded without
    guessing to its reason. Those branches are left out of the entity so the
    caller can ask for ONLY them again, instead of discarding and repaying the
    whole response. Problems with the payload as a whole (missing containers,
    paths the schema never declared) still raise `CompactResponseError`: they
    mean the model did not follow the protocol at all.
    """
    if not isinstance(raw, dict) or not ({"s", "t"} <= set(raw)):
        raise CompactResponseError("faltan contenedores compactos s/t")
    if set(raw) - {"s", "t", "d"}:
        raise CompactResponseError("llaves desconocidas en payload compacto")
    scalar_raw, table_raw = raw.get("s"), raw.get("t")
    if not isinstance(scalar_raw, dict) or not isinstance(table_raw, dict):
        raise CompactResponseError("s/t deben ser objetos")
    scalar_specs, table_specs = _compact_manifest(tree)
    unknown = (set(scalar_raw) - set(scalar_specs)) | (set(table_raw) - set(table_specs))
    known = (set(scalar_raw) & set(scalar_specs)) | (set(table_raw) & set(table_specs))
    if unknown and not known:
        raise CompactResponseError(f"rutas desconocidas: {', '.join(sorted(unknown))}")
    if unknown:
        # Measured: in a hybrid call the model also returned a field the
        # mapper had already read (it is printed on the page) and that alone
        # discarded the whole response. Extra paths are dropped; they never
        # overwrite anything.
        logger.warning("compact response: ignoring paths not requested: %s", sorted(unknown))
        scalar_raw = {path: pair for path, pair in scalar_raw.items() if path in scalar_specs}
        table_raw = {path: rows for path, rows in table_raw.items() if path in table_specs}

    entity: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}
    row_confidences: Dict[str, Dict[str, float]] = {}
    broken: Dict[str, str] = {}
    for path, pair in scalar_raw.items():
        try:
            raw_value, declared = _compact_pair(pair, scalar_specs[path], path)
            value = _compact_scalar_value(raw_value, scalar_specs[path], path)
            if value is None or value == "":
                continue
            confidence = _compact_confidence(
                _DEFAULT_SCALAR_CONFIDENCE if declared is None else declared, seg_conf, path
            )
            _set_compact_path(entity, path, value)
        except CompactResponseError as exc:
            broken[path] = str(exc)
            continue
        confidences[path] = confidence
    for path, raw_rows in table_raw.items():
        try:
            rows, aggregate, nested_conf = _decode_compact_array(raw_rows, table_specs[path], seg_conf, path)
            _set_compact_path(entity, path, rows)
        except CompactResponseError as exc:
            broken[path] = str(exc)
            continue
        if rows and aggregate is not None:
            confidences[path] = round(aggregate, 4)
        row_confidences.update(nested_conf)
    try:
        signal = _compact_type_signal(raw.get("d"), type_id)
    except CompactResponseError as exc:
        # An unreadable verdict is an unanswered one: the type check already
        # fails open on None (`type_check.merge_signals`). Repaying the whole
        # extraction to re-ask a yes/no would cost far more than it protects.
        logger.warning("compact type signal unreadable (%s): %r", exc, raw.get("d"))
        signal = None
    return entity, confidences, row_confidences, signal, broken


def _subtree_for_paths(tree: Dict[str, FieldSpec], paths: Set[str], prefix: str = "") -> Dict[str, FieldSpec]:
    """The part of `tree` that holds exactly `paths` (dotted, as in `s`/`t`)."""
    selected: Dict[str, FieldSpec] = {}
    for name, spec in tree.items():
        if spec.computed:
            continue
        path = f"{prefix}{name}"
        if path in paths:
            selected[name] = spec
        elif spec.type == "object":
            children = _subtree_for_paths(spec.fields, paths, path + ".")
            if children:
                selected[name] = replace(spec, fields=children)
    return selected


def _merge_nested(target: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(target)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested(merged[key], value)
        elif value is not None and value != [] and value != {}:
            merged[key] = value
        else:
            merged.setdefault(key, value)
    return merged


def _value_at_path(entity: Dict[str, Any], path: str) -> Any:
    value: Any = entity
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _legacy_counterfactual_node(
    tree: Dict[str, FieldSpec], entity: Dict[str, Any], confidences: Dict[str, float],
    row_confidences: Dict[str, Dict[str, float]], prefix: str = "", in_row: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for name, spec in tree.items():
        if spec.computed:
            continue
        path = f"{prefix}.{name}" if prefix else name
        value = entity.get(name) if isinstance(entity, dict) else None
        if value in (None, "", []):
            continue
        if spec.type == "object":
            nested = _legacy_counterfactual_node(
                spec.fields, value if isinstance(value, dict) else {}, confidences,
                row_confidences, path, in_row,
            )
            if nested:
                payload[name] = nested
        elif spec.type == "array":
            if spec.item_fields:
                rows = []
                for row in value if isinstance(value, list) else []:
                    if not isinstance(row, dict):
                        continue
                    encoded = _legacy_counterfactual_node(
                        spec.item_fields, row, confidences, row_confidences, "", True
                    )
                    row_values = row_confidences.get(row_key(row)) or {}
                    if row_values:
                        encoded[ROW_CONFIDENCE_KEY] = min(row_values.values())
                    rows.append(encoded)
                payload[name] = rows
            else:
                payload[name] = value
        elif in_row:
            payload[name] = value
        else:
            payload[name] = {"value": value, "confidence": confidences.get(path, 0.0)}
    return payload


def _compact_savings_estimate(
    *, usage: Dict[str, int], compact_raw: Dict[str, Any], tree: Dict[str, FieldSpec],
    entity: Dict[str, Any], confidences: Dict[str, float],
    row_confidences: Dict[str, Dict[str, float]], signal: Optional[Dict[str, Any]],
    compact_prefix: str, legacy_prefix: str, user_text: str,
    compact_schema: Dict[str, Any], legacy_schema: Dict[str, Any],
) -> Dict[str, Any]:
    compact_chars = max(1, len(compact_prefix) + len(user_text) + len(json.dumps(compact_schema, default=str)))
    legacy_chars = len(legacy_prefix) + len(user_text) + len(json.dumps(legacy_schema, default=str))
    input_tokens = int(usage.get("inputTokens") or 0)
    output_tokens = int(usage.get("outputTokens") or 0)
    estimated_legacy_input = round(input_tokens * legacy_chars / compact_chars) if input_tokens else 0

    legacy_payload = _legacy_counterfactual_node(tree, entity, confidences, row_confidences)
    if signal:
        legacy_payload[type_check.SIGNAL_KEY] = signal
    compact_output_chars = max(1, len(json.dumps(compact_raw, ensure_ascii=False, default=str)))
    legacy_output_chars = len(json.dumps(legacy_payload, ensure_ascii=False, default=str))
    estimated_legacy_output = round(output_tokens * legacy_output_chars / compact_output_chars) if output_tokens else 0
    return {
        "estimated": True,
        "method": "serialized_counterfactual_v1",
        "billedInputTokens": input_tokens,
        "billedOutputTokens": output_tokens,
        "estimatedLegacyInputTokens": estimated_legacy_input,
        "estimatedLegacyOutputTokens": estimated_legacy_output,
        "estimatedInputTokensSaved": estimated_legacy_input - input_tokens,
        "estimatedOutputTokensSaved": estimated_legacy_output - output_tokens,
    }


def _merge_format_savings(
    left_audit: Dict[str, Any], right_audit: Dict[str, Any],
    initial_usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    blocks = [
        audit.get("tokenSavings") for audit in (left_audit, right_audit)
        if isinstance(audit.get("tokenSavings"), dict)
    ]
    initial_usage = initial_usage or {}
    billed_input = int(initial_usage.get("inputTokens") or 0) + sum(
        int(block.get("billedInputTokens") or 0) for block in blocks
    )
    billed_output = int(initial_usage.get("outputTokens") or 0) + sum(
        int(block.get("billedOutputTokens") or 0) for block in blocks
    )
    return {
        "estimated": True,
        "method": "serialized_counterfactual_v1",
        "billedInputTokens": billed_input,
        "billedOutputTokens": billed_output,
        "estimatedLegacyInputTokens": sum(int(block.get("estimatedLegacyInputTokens") or 0) for block in blocks),
        "estimatedLegacyOutputTokens": sum(int(block.get("estimatedLegacyOutputTokens") or 0) for block in blocks),
        "estimatedInputTokensSaved": sum(int(block.get("estimatedInputTokensSaved") or 0) for block in blocks) - int(initial_usage.get("inputTokens") or 0),
        "estimatedOutputTokensSaved": sum(int(block.get("estimatedOutputTokensSaved") or 0) for block in blocks) - int(initial_usage.get("outputTokens") or 0),
    }


def _merge_field_tree_entities(
    base: Dict[str, Any], extra: Dict[str, Any], tree: Dict[str, FieldSpec]
) -> Dict[str, Any]:
    """Merge two chunks' nested entity dicts: scalars first-non-null-wins,
    arrays concatenate+dedup, objects recurse — the nested-tree equivalent of
    `_merge_fields`/`_merge_arrays` combined."""
    merged = dict(base or {})
    for name, spec in tree.items():
        b, e = merged.get(name), (extra or {}).get(name)
        if spec.type == "object":
            merged[name] = _merge_field_tree_entities(b or {}, e or {}, spec.fields)
        elif spec.type == "array":
            combined = list(b) if isinstance(b, list) else []
            seen: Set[str] = {
                row_key(item) if isinstance(item, dict) else json.dumps(item, sort_keys=True, default=str)
                for item in combined
            }
            for item in (e if isinstance(e, list) else []):
                identity = row_key(item) if isinstance(item, dict) else json.dumps(item, sort_keys=True, default=str)
                if identity not in seen:
                    combined.append(item)
                    seen.add(identity)
            merged[name] = combined
        else:
            if b in (None, "") and e not in (None, ""):
                merged[name] = e
            elif name not in merged:
                merged[name] = b
    return merged


def _call_model_field_tree_legacy(
    type_entry: TypeRegistryEntry,
    pages: List[PageText],
    model_id: Optional[str],
    cache: bool = False,
    field_tree: Optional[Dict[str, FieldSpec]] = None,
    scope: str = "all",
) -> Tuple[
    Dict[str, Any], Dict[str, float], Dict[str, Dict[str, float]], Dict[str, int], Optional[Dict[str, Any]], Dict[str, Any]
]:
    """Field-tree equivalent of `_call_model` — same overflow split-in-half
    retry on `max_tokens`, same caching rule, but a genuinely nested response
    shape (objects/arrays nest for real, not via a flat dotted-key convention).

    The fifth element is the type-check signal (`pipeline/type_check`) the
    model answered in this same call, or None when it did not answer one."""
    selected_tree = type_entry.field_tree if field_tree is None else field_tree
    cache_prefix, user_text = _build_field_tree_prompts(type_entry, pages, selected_tree, scope)
    schema = _field_tree_tool_schema(selected_tree, scope)
    parsed, meta = converse_structured(
        user_text,
        tool_schema=schema,
        tool_name="extract_document_fields",
        tool_description=f"Extrae los campos de un documento {type_entry.type_id} segun el schema exacto.",
        model_id=model_id or resolve_model("extractor", type_entry),
        effort=effort_for("extractor", type_entry), max_tokens=_MAX_OUTPUT_TOKENS,
        # The prefix (schema + hints + strict rules) goes on EVERY call;
        # `cache` only decides whether it's also marked as cacheable. It used
        # to travel exclusively as `cache_system`, so a document that fit in
        # ONE chunk was extracted with no hints and no rules at all — the model
        # saw the raw page text plus bare field names, and filled them by
        # guessing what the names meant (the acuse "COVE2689JP013 / 35079805"
        # landed in `Factura`, the pesos value in `Valor_ComercialDol`).
        system=None if cache else cache_prefix,
        cache_system=cache_prefix if cache else None,
        stage="extractor",
    )
    usage: Dict[str, int] = {k: v for k, v in (meta.get("usage") or {}).items() if isinstance(v, int)}

    if meta.get("stopReason") == "max_tokens" and len(pages) > 1:
        mid = len(pages) // 2
        left_e, left_c, left_r, left_u, left_s, left_a = _call_model_field_tree_legacy(
            type_entry, pages[:mid], model_id, cache=True, field_tree=selected_tree, scope=scope
        )
        right_e, right_c, right_r, right_u, right_s, right_a = _call_model_field_tree_legacy(
            type_entry, pages[mid:], model_id, cache=True, field_tree=selected_tree, scope=scope
        )
        merged_entity = _merge_field_tree_entities(left_e, right_e, selected_tree)
        merged_conf = {**left_c, **right_c}
        # Row confidences are keyed by row CONTENT, so the two halves merge
        # with a plain dict update no matter how the rows themselves were
        # concatenated or deduped above.
        merged_rows = {**left_r, **right_r}
        return (
            merged_entity, merged_conf, merged_rows,
            _merge_usage(_merge_usage(usage, left_u), right_u),
            type_check.merge_signals([left_s, right_s]),
            {
                "compactCalls": 0,
                "legacyCalls": 1 + int(left_a.get("legacyCalls") or 0) + int(right_a.get("legacyCalls") or 0),
                "effective": "legacy",
                "fallbackExecuted": False,
                "tokenSavings": {
                    "estimated": True,
                    "method": "legacy_baseline_v1",
                    "billedInputTokens": int(usage.get("inputTokens") or 0) + int(left_u.get("inputTokens") or 0) + int(right_u.get("inputTokens") or 0),
                    "billedOutputTokens": int(usage.get("outputTokens") or 0) + int(left_u.get("outputTokens") or 0) + int(right_u.get("outputTokens") or 0),
                    "estimatedLegacyInputTokens": int(usage.get("inputTokens") or 0) + int(left_u.get("inputTokens") or 0) + int(right_u.get("inputTokens") or 0),
                    "estimatedLegacyOutputTokens": int(usage.get("outputTokens") or 0) + int(left_u.get("outputTokens") or 0) + int(right_u.get("outputTokens") or 0),
                    "estimatedInputTokensSaved": 0,
                    "estimatedOutputTokensSaved": 0,
                },
            },
        )

    seg_conf = _segment_confidence(pages)
    # Read BEFORE the tree parse, which walks only the catalog's keys and
    # would drop this one on the floor.
    signal = type_check.read_signal(parsed)
    entity, confidences, row_confidences = _parse_field_tree_response(
        parsed or {}, selected_tree, seg_conf
    )
    return entity, confidences, row_confidences, usage, signal, {
        "compactCalls": 0,
        "legacyCalls": 1,
        "effective": "legacy",
        "fallbackExecuted": False,
        "tokenSavings": {
            "estimated": True,
            "method": "legacy_baseline_v1",
            "billedInputTokens": int(usage.get("inputTokens") or 0),
            "billedOutputTokens": int(usage.get("outputTokens") or 0),
            "estimatedLegacyInputTokens": int(usage.get("inputTokens") or 0),
            "estimatedLegacyOutputTokens": int(usage.get("outputTokens") or 0),
            "estimatedInputTokensSaved": 0,
            "estimatedOutputTokensSaved": 0,
        },
    }


def _call_model_field_tree_compact(
    type_entry: TypeRegistryEntry,
    pages: List[PageText],
    model_id: Optional[str],
    cache: bool = False,
    field_tree: Optional[Dict[str, FieldSpec]] = None,
    scope: str = "all",
) -> Tuple[
    Dict[str, Any], Dict[str, float], Dict[str, Dict[str, float]], Dict[str, int], Optional[Dict[str, Any]], Dict[str, Any]
]:
    selected_tree = type_entry.field_tree if field_tree is None else field_tree
    cache_prefix, user_text = _build_compact_field_tree_prompts(
        type_entry, pages, selected_tree, scope
    )
    parsed, meta = converse_structured(
        user_text,
        tool_schema=_compact_tool_schema(selected_tree, scope),
        tool_name="extract_document_fields_compact",
        tool_description=(
            f"Extrae {type_entry.type_id} en pares escalares y filas posicionales compactas."
        ),
        model_id=model_id or resolve_model("extractor", type_entry),
        effort=effort_for("extractor", type_entry),
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=None if cache else cache_prefix,
        cache_system=cache_prefix if cache else None,
        stage="extractor",
    )
    usage: Dict[str, int] = {
        k: v for k, v in (meta.get("usage") or {}).items() if isinstance(v, int)
    }
    if meta.get("stopReason") == "max_tokens" and len(pages) > 1:
        mid = len(pages) // 2
        left = _call_model_field_tree_compact(
            type_entry, pages[:mid], model_id, True, selected_tree, scope
        )
        right = _call_model_field_tree_compact(
            type_entry, pages[mid:], model_id, True, selected_tree, scope
        )
        merged_entity = _merge_field_tree_entities(left[0], right[0], selected_tree)
        return (
            merged_entity,
            {**left[1], **right[1]},
            {**left[2], **right[2]},
            _merge_usage(_merge_usage(usage, left[3]), right[3]),
            type_check.merge_signals([left[4], right[4]]),
            {
                "compactCalls": 1 + int(left[5].get("compactCalls") or 0) + int(right[5].get("compactCalls") or 0),
                "legacyCalls": int(left[5].get("legacyCalls") or 0) + int(right[5].get("legacyCalls") or 0),
                "effective": "mixed" if left[5].get("effective") != right[5].get("effective") else left[5].get("effective", "compact"),
                "fallbackExecuted": bool(left[5].get("fallbackExecuted") or right[5].get("fallbackExecuted")),
                "tokenSavings": _merge_format_savings(left[5], right[5], usage),
            },
        )

    seg_conf = _segment_confidence(pages)
    # Compatibility bridge for models that ignore the new tool schema but do
    # return a valid old response. It avoids paying a duplicate call while the
    # strict parser still rejects arbitrary or partially compact structures.
    if isinstance(parsed, dict) and not ({"s", "t"} <= set(parsed)) and any(
        key in parsed for key in selected_tree
    ):
        signal = type_check.read_signal(parsed)
        entity, confidences, row_confidences = _parse_field_tree_response(
            parsed, selected_tree, seg_conf
        )
        return entity, confidences, row_confidences, usage, signal, {
            "compactCalls": 1,
            "legacyCalls": 0,
            "effective": "legacy",
            "fallbackExecuted": False,
            "compatibilityResponse": True,
        }
    try:
        entity, confidences, row_confidences, signal, broken = _parse_compact_response(
            parsed, selected_tree, seg_conf, type_entry.type_id
        )
    except CompactResponseError as exc:
        exc.usage = usage
        raise
    repair_audit: Dict[str, Any] = {}
    if broken:
        # Only the branches that could not be decoded go back to the model, in
        # the legacy shape; everything else from this response is kept. Before,
        # one blank scalar or one short row re-ran the WHOLE partition.
        retry_tree = _subtree_for_paths(selected_tree, set(broken))
        logger.warning(
            "compact response partially invalid for %s/%s; retrying legacy for %s: %s",
            type_entry.type_id, scope, sorted(broken), "; ".join(broken.values()),
        )
        if retry_tree:
            r_entity, r_conf, r_rows, r_usage, _r_signal, _r_audit = _call_model_field_tree_legacy(
                type_entry, pages, model_id, cache, retry_tree, _RETRY_SCOPE
            )
            entity = _merge_nested(entity, r_entity)
            confidences.update(r_conf)
            row_confidences.update(r_rows)
            compact_usage = usage
            usage = _merge_usage(usage, r_usage)
            repair_audit = {
                "compactCalls": 1,
                "legacyCalls": 1,
                "effective": "mixed",
                "fallbackExecuted": True,
                "fallbackReason": "; ".join(broken.values())[:240],
                "retriedPaths": sorted(broken),
                "tokenSavings": {
                    "estimated": True,
                    "method": "partial_fallback_v1",
                    "billedInputTokens": int(usage.get("inputTokens") or 0),
                    "billedOutputTokens": int(usage.get("outputTokens") or 0),
                    "estimatedLegacyInputTokens": int(compact_usage.get("inputTokens") or 0),
                    "estimatedLegacyOutputTokens": int(compact_usage.get("outputTokens") or 0),
                    "estimatedInputTokensSaved": -int(r_usage.get("inputTokens") or 0),
                    "estimatedOutputTokensSaved": -int(r_usage.get("outputTokens") or 0),
                },
            }
    if repair_audit:
        return entity, confidences, row_confidences, usage, signal, repair_audit
    legacy_prefix, _legacy_user = _build_field_tree_prompts(
        type_entry, pages, selected_tree, scope
    )
    savings = _compact_savings_estimate(
        usage=usage,
        compact_raw=parsed,
        tree=selected_tree,
        entity=entity,
        confidences=confidences,
        row_confidences=row_confidences,
        signal=signal,
        compact_prefix=cache_prefix,
        legacy_prefix=legacy_prefix,
        user_text=user_text,
        compact_schema=_compact_tool_schema(selected_tree, scope),
        legacy_schema=_field_tree_tool_schema(selected_tree, scope),
    )
    return entity, confidences, row_confidences, usage, signal, {
        "compactCalls": 1,
        "legacyCalls": 0,
        "effective": "compact",
        "fallbackExecuted": False,
        "tokenSavings": savings,
    }


def _call_model_field_tree(
    type_entry: TypeRegistryEntry,
    pages: List[PageText],
    model_id: Optional[str],
    cache: bool = False,
    field_tree: Optional[Dict[str, FieldSpec]] = None,
    scope: str = "all",
    response_format: str = AI_RESPONSE_COMPACT,
) -> Tuple[
    Dict[str, Any], Dict[str, float], Dict[str, Dict[str, float]], Dict[str, int], Optional[Dict[str, Any]], Dict[str, Any]
]:
    if response_format == AI_RESPONSE_LEGACY:
        return _call_model_field_tree_legacy(
            type_entry, pages, model_id, cache, field_tree, scope
        )
    try:
        return _call_model_field_tree_compact(
            type_entry, pages, model_id, cache, field_tree, scope
        )
    except CompactResponseError as exc:
        logger.warning(
            "compact response invalid for %s/%s; retrying legacy once: %s",
            type_entry.type_id, scope, exc,
        )
        compact_usage = dict(exc.usage)
        legacy = _call_model_field_tree_legacy(
            type_entry, pages, model_id, cache, field_tree, scope
        )
        audit = dict(legacy[5])
        audit.update({
            "compactCalls": 1,
            "effective": "legacy",
            "fallbackExecuted": True,
            "fallbackReason": str(exc)[:240],
            "tokenSavings": {
                "estimated": True,
                "method": "fallback_vs_legacy_v1",
                "billedInputTokens": int(compact_usage.get("inputTokens") or 0) + int(legacy[3].get("inputTokens") or 0),
                "billedOutputTokens": int(compact_usage.get("outputTokens") or 0) + int(legacy[3].get("outputTokens") or 0),
                "estimatedLegacyInputTokens": int(legacy[3].get("inputTokens") or 0),
                "estimatedLegacyOutputTokens": int(legacy[3].get("outputTokens") or 0),
                "estimatedInputTokensSaved": -int(compact_usage.get("inputTokens") or 0),
                "estimatedOutputTokensSaved": -int(compact_usage.get("outputTokens") or 0),
            },
        })
        return legacy[0], legacy[1], legacy[2], _merge_usage(compact_usage, legacy[3]), legacy[4], audit


def _extract_document_field_tree(
    pages: List[PageText], type_entry: TypeRegistryEntry, model_id: Optional[str] = None,
    field_tree: Optional[Dict[str, FieldSpec]] = None,
    ai_response_format: Optional[str] = None,
) -> ExtractedDoc:
    selected_tree = type_entry.field_tree if field_tree is None else field_tree
    selected_tree = selected_tree or {}
    chunks = _chunk_pages(pages)
    partitions = _field_tree_call_partitions(selected_tree)
    format_resolution = resolve_ai_response_format(type_entry, ai_response_format)
    use_cache = len(chunks) > 1
    calls = [(chunk, scope, tree) for chunk in chunks for scope, tree in partitions]
    workers = min(_CHUNK_WORKERS, len(calls)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda call: _call_model_field_tree(
                type_entry,
                call[0],
                model_id=model_id,
                cache=use_cache,
                field_tree=call[2],
                scope=call[1],
                response_format=format_resolution["selected"],
            ),
            calls,
        ))

    usage: Dict[str, int] = {}
    entity_map: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}
    row_confidences: Dict[str, Dict[str, float]] = {}
    signals: List[Optional[Dict[str, Any]]] = []
    format_audits: List[Dict[str, Any]] = []
    for entity, conf, row_conf, chunk_usage, signal, response_audit in results:
        entity_map = _merge_field_tree_entities(entity_map, entity, selected_tree)
        confidences.update(conf)
        row_confidences.update(row_conf)
        usage = _merge_usage(usage, chunk_usage)
        signals.append(signal)
        format_audits.append(response_audit)

    fill_row_common_fields(entity_map, selected_tree, pages, row_confidences)

    seg_conf = _segment_confidence(pages)
    effective_values = {str(item.get("effective") or "") for item in format_audits}
    effective_values.discard("")
    effective = next(iter(effective_values)) if len(effective_values) == 1 else "mixed"
    response_audit = {
        "requested": format_resolution["requested"],
        "source": format_resolution["source"],
        "selected": format_resolution["selected"],
        "effective": effective or format_resolution["selected"],
        "fallbackExecuted": any(bool(item.get("fallbackExecuted")) for item in format_audits),
        "compactCalls": sum(int(item.get("compactCalls") or 0) for item in format_audits),
        "legacyCalls": sum(int(item.get("legacyCalls") or 0) for item in format_audits),
    }
    fallback_reasons = [str(item["fallbackReason"]) for item in format_audits if item.get("fallbackReason")]
    if fallback_reasons:
        response_audit["fallbackReasons"] = fallback_reasons
    retried = sorted({path for item in format_audits for path in item.get("retriedPaths") or []})
    if retried:
        response_audit["retriedPaths"] = retried
    saving_blocks = [
        item.get("tokenSavings") for item in format_audits
        if isinstance(item.get("tokenSavings"), dict)
    ]
    if saving_blocks:
        response_audit["tokenSavings"] = {
            "estimated": True,
            "method": "serialized_counterfactual_v1",
            "billedInputTokens": int(usage.get("inputTokens") or 0),
            "billedOutputTokens": int(usage.get("outputTokens") or 0),
            "estimatedLegacyInputTokens": sum(int(block.get("estimatedLegacyInputTokens") or 0) for block in saving_blocks),
            "estimatedLegacyOutputTokens": sum(int(block.get("estimatedLegacyOutputTokens") or 0) for block in saving_blocks),
            "estimatedInputTokensSaved": sum(int(block.get("estimatedInputTokensSaved") or 0) for block in saving_blocks),
            "estimatedOutputTokensSaved": sum(int(block.get("estimatedOutputTokensSaved") or 0) for block in saving_blocks),
        }
    return ExtractedDoc(
        entity_map=entity_map,
        confidences=confidences,
        row_confidences=row_confidences,
        meta={
            "chunks": len(chunks),
            "schemaPartitions": len(partitions),
            "pages": [p.number for p in pages],
            "usage": usage,
            "segmentConfidence": round(seg_conf, 4),
            # One answer per document, fail-open across chunks — see
            # type_check.merge_signals. None when no call answered it.
            "typeSignal": type_check.merge_signals(signals),
            "aiResponseFormat": response_audit,
        },
    )


def extract_document(
    pages: List[PageText],
    type_entry: TypeRegistryEntry,
    model_id: Optional[str] = None,
    field_tree: Optional[Dict[str, FieldSpec]] = None,
    ai_response_format: Optional[str] = None,
) -> ExtractedDoc:
    """Extract the catalog schema from a document's pages."""
    if type_entry.field_tree:
        return _extract_document_field_tree(
            pages, type_entry, model_id, field_tree, ai_response_format
        )

    chunks = _chunk_pages(pages)

    # Cache the static prefix only when it will be reused: >1 chunk means the
    # chunk pool reuses it (first call to write, rest read at ~10%). A
    # single-chunk document makes one call, where caching would be a net loss.
    use_cache = len(chunks) > 1

    # Chunks are independent Bedrock calls (each reads only its own chunk +
    # catalog entry) mapped concurrently, mirroring expediente.py's
    # segment-level pool. pool.map() preserves chunk order, so results merge
    # in the same order as the old sequential loop — only *when* each call
    # runs changes, not the first-non-null-wins merge semantics below.
    workers = min(_CHUNK_WORKERS, len(chunks)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda chunk: _call_model(type_entry, chunk, arrays_only=False, model_id=model_id, cache=use_cache),
            chunks,
        ))

    usage: Dict[str, int] = {}
    all_fields: Dict[str, Any] = {}
    all_arrays: Dict[str, Any] = {}
    for fields, arrays, chunk_usage in results:
        all_fields = _merge_fields(all_fields, fields)
        all_arrays = _merge_arrays(all_arrays, arrays)
        usage = _merge_usage(usage, chunk_usage)

    seg_conf = _segment_confidence(pages)
    entity_map: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}

    for name in type_entry.extraction_fields:
        if name in type_entry.repeatable_fields:
            items = all_arrays.get(name) or []
            entity_map[name] = [i for i in items if isinstance(i, dict)]
            if entity_map[name]:
                confidences[name] = round(seg_conf, 4)
            continue
        slot = all_fields.get(name)
        if isinstance(slot, dict):
            value = slot.get("value")
            llm_conf = slot.get("confidence")
        else:
            value, llm_conf = slot, None
        if value in (None, "", "null"):
            entity_map[name] = None
            continue
        entity_map[name] = str(value).strip()
        try:
            llm_conf = float(llm_conf) if llm_conf is not None else 0.85
        except (TypeError, ValueError):
            llm_conf = 0.85
        confidences[name] = round(max(0.0, min(1.0, llm_conf)) * seg_conf, 4)

    # Nested/dotted repeatables (e.g. CUADRO_LIQUIDACION.CONCEPTOS,
    # PARTIDAS.CONTRIBUCIONES_PARTIDA) are declared only in repeatable_fields,
    # not extraction_fields, so the scalar loop above never materializes them.
    # Bring them through here so their rows are not silently dropped.
    for name in type_entry.repeatable_fields:
        if name in entity_map:
            continue
        items = all_arrays.get(name) or []
        entity_map[name] = [i for i in items if isinstance(i, dict)]
        if entity_map[name]:
            confidences[name] = round(seg_conf, 4)

    return ExtractedDoc(
        entity_map=entity_map,
        confidences=confidences,
        meta={
            "chunks": len(chunks),
            "pages": [p.number for p in pages],
            "usage": usage,
            "segmentConfidence": round(seg_conf, 4),
        },
    )


_ESTIMATED_OUTPUT_CHARS_PER_TOKEN = 4.0


def _request_chars_for_tree(
    pages: List[PageText], type_entry: TypeRegistryEntry, tree: Dict[str, FieldSpec]
) -> int:
    """Comparable request size for a field-tree extraction.

    This deliberately counts only payload text controlled by this pipeline:
    prompt, page text and tool schema. Bedrock's hidden protocol overhead is
    recovered later from the real billed input-token density when a partial
    call actually ran.
    """
    if not tree:
        return 0
    normalized_pages = [
        page
        if hasattr(page, "source")
        else PageText(
            int(getattr(page, "number", index)),
            str(getattr(page, "text", "") or ""),
            "unknown",
            float(getattr(page, "confidence", 1.0)),
        )
        for index, page in enumerate(pages, 1)
    ]
    total = 0
    description = (
        f"Extrae los campos de un documento {type_entry.type_id} segun el schema exacto."
    )
    for chunk in _chunk_pages(normalized_pages):
        for scope, partition in _field_tree_call_partitions(tree):
            system, user = _build_field_tree_prompts(
                type_entry, chunk, partition, scope
            )
            schema = json.dumps(
                _field_tree_tool_schema(partition, scope),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            total += len(system) + len(user) + len(schema) + len(description)
    return total


def _removed_tree(
    complete: Dict[str, FieldSpec], requested: Dict[str, FieldSpec]
) -> Dict[str, FieldSpec]:
    """Branches omitted from the partial model schema."""
    removed: Dict[str, FieldSpec] = {}
    for name, spec in complete.items():
        if spec.computed:
            continue
        requested_spec = requested.get(name)
        if requested_spec is None:
            removed[name] = spec
        elif spec.type == "object" and requested_spec.type == "object":
            children = _removed_tree(spec.fields, requested_spec.fields)
            if children:
                removed[name] = replace(spec, fields=children)
    return removed


def _response_proxy(
    tree: Dict[str, FieldSpec], entities: Dict[str, Any], in_row: bool = False
) -> Dict[str, Any]:
    """Model-shaped JSON for deterministic values omitted from the call.

    It is not presented as billed usage. Its serialized size is only the
    auditable basis for the estimated output saving below.
    """
    proxy: Dict[str, Any] = {}
    for name, spec in tree.items():
        value = entities.get(name)
        if value is None:
            continue
        if spec.type == "object":
            nested = _response_proxy(
                spec.fields, value if isinstance(value, dict) else {}, in_row
            )
            if nested:
                proxy[name] = nested
        elif spec.type == "array" and isinstance(value, list):
            if spec.item_fields:
                rows = []
                for row in value:
                    if not isinstance(row, dict):
                        continue
                    rendered = _response_proxy(spec.item_fields, row, in_row=True)
                    if rendered:
                        rendered[ROW_CONFIDENCE_KEY] = 0.95
                        rows.append(rendered)
                if rows:
                    proxy[name] = rows
            elif value:
                proxy[name] = value
        elif in_row:
            proxy[name] = value
        else:
            proxy[name] = {"value": value, "confidence": 0.95}
    return proxy


def estimate_deterministic_token_savings(
    pages: List[PageText],
    type_entry: TypeRegistryEntry,
    requested_tree: Dict[str, FieldSpec],
    deterministic_entities: Dict[str, Any],
    usage: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Estimate tokens avoided by sending only unresolved schema branches.

    Billed tokens remain the raw Bedrock numbers. Input savings use the real
    call's tokens-per-controlled-character density whenever a partial call
    ran; a fully deterministic call uses the explicit 4 chars/token fallback.
    Output savings serialize the values code already produced in the exact
    response shape the model would have emitted and apply the same documented
    4 chars/token approximation. This avoids pretending an unmade full-schema
    call has exact billing data.
    """
    usage = usage or {}
    billed_input = int(usage.get("inputTokens") or 0)
    billed_output = int(usage.get("outputTokens") or 0)
    complete_tree = type_entry.field_tree or {}
    actual_chars = _request_chars_for_tree(pages, type_entry, requested_tree)
    full_chars = _request_chars_for_tree(pages, type_entry, complete_tree)
    saved_input_chars = max(0, full_chars - actual_chars)

    if billed_input > 0 and actual_chars > 0:
        input_tokens_per_char = billed_input / actual_chars
        input_method = "real_partial_call_density"
    else:
        input_tokens_per_char = 1.0 / _ESTIMATED_OUTPUT_CHARS_PER_TOKEN
        input_method = "four_chars_per_token"
    saved_input = int(round(saved_input_chars * input_tokens_per_char))

    removed = _removed_tree(complete_tree, requested_tree)
    output_proxy = _response_proxy(removed, deterministic_entities)
    output_chars = len(json.dumps(output_proxy, ensure_ascii=False, separators=(",", ":"))) if output_proxy else 0
    saved_output = int(round(output_chars / _ESTIMATED_OUTPUT_CHARS_PER_TOKEN))

    without_input = billed_input + saved_input
    without_output = billed_output + saved_output
    return {
        "estimated": True,
        "method": "partial_schema_counterfactual_v1",
        "inputMethod": input_method,
        "outputMethod": "deterministic_response_at_four_chars_per_token",
        "billedInputTokens": billed_input,
        "billedOutputTokens": billed_output,
        "estimatedInputTokensSaved": saved_input,
        "estimatedOutputTokensSaved": saved_output,
        "estimatedInputTokensWithoutDeterministic": without_input,
        "estimatedOutputTokensWithoutDeterministic": without_output,
        "estimatedInputSavingPct": round(saved_input / without_input * 100, 1) if without_input else 0.0,
        "estimatedOutputSavingPct": round(saved_output / without_output * 100, 1) if without_output else 0.0,
        "requestCharsSent": actual_chars,
        "estimatedRequestCharsWithoutDeterministic": full_chars,
        "deterministicOutputProxyChars": output_chars,
    }


def _segment_confidence(pages: List[PageText]) -> float:
    """OCR-quality factor for the segment: mean page confidence."""
    if not pages:
        return 0.0
    usable = [p.confidence for p in pages if p.source != "empty"]
    if not usable:
        return 0.0
    return sum(usable) / len(usable)
