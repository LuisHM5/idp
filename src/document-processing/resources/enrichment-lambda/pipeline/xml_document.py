"""XML document orchestrator: XML bytes -> extracted, validated result.

Counterpart to `pipeline/expediente.py` for document types whose source is
already-structured XML (CFDI invoices, VUCEM acknowledgements, and any other
government/EDI XML) instead of a PDF/scan that needs an LLM to read. Since the
data is already machine-structured, extraction is a deterministic tree walk
driven by each field's `xpath` (a field-centric-DSL extra, see
catalogs/README.md) — no Bedrock call, no OCR, no segmentation.

A type opts into this path simply by declaring `xpath`/`item_xpath` on its
`fields` instead of (or alongside) `hint`; nothing here is coupled to any
specific TYPE_ID. `catalogs/README.md`'s convention is to name such types with
an `_XML` suffix so they're easy to spot in the `catalogs/` folder, but that
suffix is a naming convention only — `handler.py` decides whether to route a
document here by the uploaded file's extension, and this module itself only
cares whether the type's fields declare an `xpath`.

Only single, standalone documents are supported (no multi-document XML
"expediente" — an XML upload is always exactly one document, always with an
explicit `declared_type`; there is no vision-based auto-classification path
for raw XML). Cross-document rules therefore never fire here (no siblings);
if a business process ever needs to cross-check a native XML CFDI against
other documents inside a PDF expediente, that needs a future multi-file
bundling feature, out of scope for this module.
"""

from __future__ import annotations

import logging
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from document_catalog import FieldSpec, TypeRegistryEntry, get_type_entry
from validators import engine
from validators.engine import ValidationContext

from .derived import apply_derived_fields
from .liquidacion import apply_liquidacion_normalizada
from .result_common import build_tracking, checks_to_json, error_result

logger = logging.getLogger(__name__)

# Deterministic parse, not a guess: every value pulled straight off an
# attribute/element the schema's own namespace guarantees is there. Same
# spirit as qr_recovery's 0.99 (trust the source), but XML has no OCR/decode
# uncertainty at all, so this uses the ceiling.
_XML_CONFIDENCE = 1.0

# Tag used in `tracking.schema.model` for results that never called a model,
# so a reader doesn't mistake this for `used_models()`/`base_model_id()`
# reporting an LLM that actually ran.
_SOURCE_LABEL = "deterministic:xml"


def _split_attr(xpath: str) -> Tuple[str, Optional[str]]:
    """Split `xpath` into (element_path, attribute_name_or_None).

    `"@Attr"` addresses an attribute on the CURRENT context node itself
    (element_path "."); `"a/b/@Attr"` addresses an attribute on a descendant;
    a plain `"a/b"` (no `@`) means the descendant's own text content.
    """
    if xpath.startswith("@"):
        return ".", xpath[1:]
    if "/@" in xpath:
        path, attr = xpath.rsplit("/@", 1)
        return path, attr
    return xpath, None


def _resolve_one(node: ET.Element, xpath: str, ns: Dict[str, str]) -> Optional[str]:
    path, attr = _split_attr(xpath)
    target = node if path in (".", "") else node.find(path, ns)
    if target is None:
        return None
    if attr:
        return target.get(attr)
    text = (target.text or "").strip()
    return text or None


def _resolve_xpath(node: ET.Element, xpath: Any, ns: Dict[str, str]) -> Optional[str]:
    """Resolve a field's `xpath` (a single string, or a list tried in order —
    e.g. across minor schema-version differences); returns the first match."""
    for candidate in (xpath if isinstance(xpath, list) else [xpath]):
        value = _resolve_one(node, candidate, ns)
        if value is not None:
            return value
    return None


def _resolve_items(node: ET.Element, item_xpath: Any, ns: Dict[str, str]) -> List[ET.Element]:
    """Resolve an array field's `item_xpath` (a single path, or a list of
    paths whose matches are concatenated — a union, for tables split across
    sibling elements, e.g. Traslados + Retenciones rows on one CFDI concept).
    """
    items: List[ET.Element] = []
    for candidate in (item_xpath if isinstance(item_xpath, list) else [item_xpath]):
        items.extend(node.findall(candidate, ns))
    return items


def _coerce(raw: str, ftype: str) -> Any:
    """Cast a raw XML string to the field's declared type.

    Mirrors what `pipeline.text_extractor._parse_field_tree_response` hands
    `resolve_field_tree` for a Bedrock-sourced field: strings/dates stay
    strings, numbers/booleans become native Python values so downstream
    numeric rules (`custom_formula`, `sum_array`, ...) work unchanged.
    """
    raw = raw.strip()
    if ftype == "number":
        try:
            return float(raw)
        except ValueError:
            return raw
    if ftype == "boolean":
        return raw.lower() in ("true", "1", "si", "sí", "yes", "presente")
    return raw


def _extract_node(
    node: ET.Element, tree: Dict[str, FieldSpec], ns: Dict[str, str], prefix: str = ""
) -> Tuple[Dict[str, Any], Dict[str, float]]:
    """Recursively walk `tree` against `node`, resolving each field's `xpath`/
    `item_xpath`. Same nested entity-map shape `resolve_field_tree` expects
    from any extractor (see `text_extractor._parse_field_tree_response`)."""
    entity: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}
    for name, spec in tree.items():
        path = f"{prefix}{name}"
        if spec.computed:
            entity[name] = None
            continue
        if spec.type == "object":
            obj_xpath = spec.params.get("xpath")
            sub_node = node.find(obj_xpath, ns) if obj_xpath else node
            if sub_node is None:
                sub_entity: Dict[str, Any] = {}
                sub_conf: Dict[str, float] = {}
            else:
                sub_entity, sub_conf = _extract_node(sub_node, spec.fields, ns, path + ".")
            entity[name] = sub_entity
            confidences.update(sub_conf)
        elif spec.type == "array":
            item_xpath = spec.params.get("item_xpath")
            rows: List[Dict[str, Any]] = []
            if item_xpath and spec.item_fields:
                for item_node in _resolve_items(node, item_xpath, ns):
                    row_entity, _row_conf = _extract_node(item_node, spec.item_fields, ns)
                    rows.append(row_entity)
            entity[name] = rows
            if rows:
                confidences[path] = _XML_CONFIDENCE
        else:
            xpath = spec.params.get("xpath")
            raw = _resolve_xpath(node, xpath, ns) if xpath else None
            if not raw:
                entity[name] = None
                continue
            entity[name] = _coerce(raw, spec.type)
            confidences[path] = _XML_CONFIDENCE
    return entity, confidences


def extract_from_xml(
    root: ET.Element, type_entry: TypeRegistryEntry
) -> Tuple[Dict[str, Any], Dict[str, float]]:
    """Deterministically extract a catalog's `field_tree` from a parsed XML
    document root. Public so tests/other callers can exercise it directly."""
    return _extract_node(root, type_entry.field_tree, type_entry.namespaces or {})


def process_xml(
    xml_bytes: bytes,
    declared_type: Optional[str] = None,
    expected_values: Optional[Dict[str, str]] = None,
    source_name: str = "",
) -> Dict[str, Any]:
    """Process a single standalone XML document and return the full result,
    shaped exactly like `pipeline.expediente.process_pdf`'s return value (one
    entry in `documents`, same `tracking`/`status`/`crossDocumentChecks`
    envelope) so `handler.py`'s downstream assembly needs no branch on which
    pipeline produced it.
    """
    started = time.time()
    started_at = datetime.now(timezone.utc)
    processing_id = str(uuid.uuid4())
    expected_values = expected_values or {}

    def _fail(
        message: str, *, stage: Optional[str] = None, exception: Optional[BaseException] = None
    ) -> Dict[str, Any]:
        return error_result(
            processing_id, source_name, message,
            build_tracking(started_at, started, _SOURCE_LABEL),
            stage=stage, exception=exception, source_format="xml",
        )

    if not declared_type:
        return _fail(
            "XML documents require an explicit declared type "
            "(upload under raw/{TYPE}/{uuid}/file.xml) — there is no "
            "vision-based auto-classification for raw XML",
            stage="declared_type",
        )

    entry = get_type_entry(declared_type)
    if not entry.field_tree:
        return _fail(
            f"type '{declared_type}' has no field-centric schema (\"fields\") "
            "to drive XML extraction",
            stage="declared_type",
        )

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.exception("failed to parse XML for %s", declared_type)
        return _fail(f"invalid XML: {e}", stage="parse", exception=e)

    entity_map, confidences = extract_from_xml(root, entry)

    # Deterministic customs liquidation normalization, same convention as the
    # PDF pipeline: only fires when the type declares a LIQUIDACION_NORMALIZADA
    # computed field; a no-op for every other type (see catalogs/README.md).
    liq = apply_liquidacion_normalizada(entity_map, confidences, entry)
    # Config-driven derived fields (`derived` blocks in the catalog) — same
    # convention and same ordering as the PDF pipeline; a no-op for a type
    # that declares none. See catalogs/README.md.
    derived = apply_derived_fields(entity_map, confidences, entry)

    ctx = ValidationContext(expected_values=expected_values, sibling_documents=[], document_text="")
    resolved_tree, checks, _effective_confidences = engine.resolve_field_tree(
        entity_map, confidences, entry, ctx, default_source="XML_EXTRACTION",
        source_overrides=derived.sources, value_labels=derived.labels,
        default_confidence=_XML_CONFIDENCE,
    )
    status = engine.document_status_from_checks(checks)

    doc: Dict[str, Any] = {
        "documentType": declared_type,
        "documentSubtype": None,
        "pageStart": 1,
        "pageEnd": 1,
        "classificationConfidence": _XML_CONFIDENCE,
        "extractedEntities": resolved_tree,
        "fields": {},
        "checks": checks_to_json(checks),
        "status": status,
        "businessDestination": entry.business_destination or None,
        "confidence": _XML_CONFIDENCE,
        "fieldTreeShape": True,
        **({"liquidacionNormalizada": liq} if liq is not None else {}),
    }

    tracking = build_tracking(started_at, started, _SOURCE_LABEL, doc_types=[declared_type])
    return {
        "processingId": processing_id,
        "sourceFile": source_name,
        "processedAt": tracking["processingCompletedAt"],
        "processingSeconds": tracking["processingSeconds"],
        "processingMs": tracking["processingMs"],
        "tracking": tracking,
        "modelIds": {},
        # A "pages" breakdown (native/OCR) is PDF vocabulary that doesn't map
        # onto a single parsed XML document — sourceFormat lets any consumer
        # (console printers, the API) render/branch appropriately instead of
        # forcing PDF-shaped fields (nativePages, ocrPages, emptyPages) onto
        # something that was never paginated or scanned.
        "sourceFormat": "xml",
        "pages": {"totalPages": 1},
        "status": status,
        "reviewRequired": status in (engine.DOC_REVIEW, engine.DOC_PARTIAL),
        "documents": [doc],
        "crossDocumentChecks": [],
        "warnings": [],
    }
