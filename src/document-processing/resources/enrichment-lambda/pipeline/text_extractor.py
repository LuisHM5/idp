"""Catalog-driven structured extraction over page text (Bedrock).

Takes the text of a document segment (native or OCR) and the catalog entry
for its type, and asks the model to fill the catalog schema:
- scalar `extraction_fields` (with `extraction_hints` as guidance)
- `repeatable_fields` as arrays of objects (ALL rows, not just the first)

Long documents are chunked by pages; scalars merge with first-non-null-wins
(higher confidence preferred) and arrays are concatenated in page order.

Anti-hallucination contract enforced in the prompt:
- value null when the data is not on the document (never invented)
- values verbatim as printed (normalization happens deterministically later)
- per-field confidence 0-1 reported by the model, then combined with the
  OCR confidence of the source pages.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from document_catalog import TypeRegistryEntry

from .bedrock import converse_structured
from .pdf_pages import PageText

logger = logging.getLogger(__name__)

_MAX_CHUNK_CHARS = 28_000
# us.anthropic.claude-haiku-4-5's real Bedrock output ceiling is 64,000
# tokens (verified against the live Converse API — a request above that
# value is rejected outright). The old 8,192 cap was self-imposed and far
# below that: for schemas with many repeatable-field rows (e.g.
# PEDIMENTO_ADUANAL's PARTIDAS/CUADRO_LIQUIDACION), a row-dense chunk's
# output regularly exceeded 8,192, truncating mid-JSON and forcing the
# recursive split-in-half retry below to fire repeatedly. Raising the cap
# costs nothing by itself (billed by tokens actually generated, not the
# ceiling) and avoids paying for truncated, unusable output followed by a
# retry. Leaves headroom under the hard 64,000 limit.
_MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
# Chunks of the same document are extracted concurrently: each is an
# independent Bedrock call, so a multi-chunk document runs in parallel
# instead of serially. Bounded (not equal to EXTRACT_WORKERS in
# expediente.py) so a multi-segment expediente's segment-level pool times
# this pool without multiplying concurrent Bedrock calls too far; boto3
# clients are thread-safe (pipeline.bedrock.client() is an lru_cache
# singleton, already relied on by the segment-level pool).
_CHUNK_WORKERS = int(os.environ.get("CHUNK_WORKERS", "2"))


@dataclass
class ExtractedDoc:
    entity_map: Dict[str, Any]           # scalars: str|None; repeatables: list[dict]
    confidences: Dict[str, float]        # per-field combined confidence 0-1
    meta: Dict[str, Any] = field(default_factory=dict)


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
        model_id=model_id, max_tokens=_MAX_OUTPUT_TOKENS,
        cache_system=cache_prefix if cache else None,
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
        for item in items:
            if item not in existing:  # drop exact duplicates across chunks
                existing.append(item)
    return merged


def extract_document(
    pages: List[PageText],
    type_entry: TypeRegistryEntry,
    model_id: Optional[str] = None,
) -> ExtractedDoc:
    """Extract the catalog schema from a document's pages."""
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


def _segment_confidence(pages: List[PageText]) -> float:
    """OCR-quality factor for the segment: mean page confidence."""
    if not pages:
        return 0.0
    usable = [p.confidence for p in pages if p.source != "empty"]
    if not usable:
        return 0.0
    return sum(usable) / len(usable)
