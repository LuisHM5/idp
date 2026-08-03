"""Expediente orchestrator: PDF bytes → segmented, extracted, validated result.

Flow:
1. pdf_pages.load_pages       — native text + Textract OCR per page
2. segmenter.segment_pages    — typed page ranges (skipped for single docs)
3. text_extractor per segment — catalog schema extraction (scalars + arrays)
4. validators.engine per doc  — catalog rules with sibling-document context
5. cross-document rules       — same_pedimento, expected_importer_rfc,
                                unique_cfdi_uuid, expense_reconciliation,
                                pedimento_value_reconciliation

The result never presents uncertain data as valid: every field carries its
extracted value, deterministic normalization, confidence and a status; the
document and expediente statuses aggregate them conservatively.
"""

from __future__ import annotations

import io
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from document_catalog import (
    get_cross_document_rules,
    get_schema_info,
    get_type_entry,
)
from validators import engine
from validators.engine import RuleCheck, ValidationContext

from .liquidacion import build_liquidacion_normalizada
from .normalize import (
    digits_exact,
    find_pedimento_components,
    find_pedimento_reference,
    normalize_pedimento,
    normalize_uuid,
    normalize_year_2digit,
    parse_amount,
    pedimento_consecutivo,
)
from .pdf_pages import PageText, load_pages, pages_summary
from .bedrock import base_model_id, used_models
from .segmenter import Segment, segment_pages
from .text_extractor import extract_document

logger = logging.getLogger(__name__)

# Bump when extraction/validation logic changes in a way consumers should
# notice (recorded in every result's tracking block).
PIPELINE_VERSION = "1.0.0"

# Segments are extracted concurrently: each is an independent Bedrock call, so a
# 17-document expediente runs in parallel instead of serially. Bounded to respect
# Bedrock rate limits; boto3 clients are thread-safe.
_EXTRACT_WORKERS = int(os.environ.get("EXTRACT_WORKERS", "4"))

# Cost guardrail: an unbounded page count means unbounded Textract OCR calls
# and Bedrock extraction chunks before anything can fail or time out — a
# single oversized upload (accidental or malicious) can burn a full 15-minute
# Lambda invocation's worth of billed Textract+Bedrock usage. Cap it before
# any of that work starts; the untouched original always stays exactly where
# it was uploaded (S3 raw/... -> processed/failed/... via the workflow) since
# this only truncates the in-memory copy used for extraction, so nothing is
# lost — just flagged for a human to reprocess in full if the extra pages
# matter.
_MAX_PAGES = int(os.environ.get("MAX_PAGES", "50"))


def _tracking(
    started_at: datetime,
    started_monotonic: float,
    model_id: Optional[str],
    doc_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Provenance for a result: created/updated, processing window, schema/version.

    `createdAt` and `updatedAt` mark this result record's lifecycle; they start
    equal and consumers bump `updatedAt` whenever they mutate the result.

    `typeVersions` reports the declared `schema_version` of each document type
    that actually processed the file (including DynamoDB hot-reloaded overrides),
    so a change to a type in DynamoDB is reflected here without a redeploy.
    """
    now = datetime.now(timezone.utc)
    # Report the models that actually ran, not the caller's argument: it is
    # normally None (each stage resolves its own model from the environment),
    # which used to be reported as the literal "default" — useless for telling
    # two models' results apart when comparing them.
    ran = used_models()
    schema: Dict[str, Any] = {
        "pipelineVersion": PIPELINE_VERSION,
        "model": model_id or ran.get("extractor") or base_model_id(),
        **get_schema_info(),
    }
    type_versions = {}
    for dt in sorted(set(doc_types or [])):
        entry = get_type_entry(dt)
        if getattr(entry, "schema_version", None):
            type_versions[dt] = entry.schema_version
    if type_versions:
        schema["typeVersions"] = type_versions
    return {
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
        "processingStartedAt": started_at.isoformat(),
        "processingCompletedAt": now.isoformat(),
        "processingSeconds": round(time.time() - started_monotonic, 2),
        "schema": schema,
    }

# Fields (by doc type) that carry the pedimento reference for same_pedimento
_PEDIMENTO_REF_FIELDS = (
    "NUMERO_PEDIMENTO_NORMALIZADO",
    "NUMERO_PEDIMENTO",
    "PEDIMENTO_REFERENCE",
)
# RFC_PARA_CONSULTA (VUCEM) is deliberately excluded: it names whoever may
# consult the eDocument (often customs-broker staff), not the importer.
_IMPORTER_RFC_FIELDS = (
    "RFC_IMPORTADOR_EXPORTADOR",
    "RECEPTOR_RFC",
    "IMPORTADOR_RFC",
    "RFC_IMPORTADOR",
)


def _extract_segment(
    seg: Segment,
    pages: List[PageText],
    model_id: Optional[str],
    reader: Optional[Any] = None,
) -> Dict[str, Any]:
    """Extract one segment into a document dict. Runs in a worker thread.

    Self-contained (reads only its own pages + catalog entry) so it can be mapped
    concurrently over all segments without shared mutable state. `reader` is a
    shared pypdf.PdfReader over the whole file (same sharing pattern
    pdf_pages.load_pages already uses across its OCR worker threads) — only
    read from when the segment's catalog declares a QR source.
    """
    seg_pages = [p for p in pages if seg.page_start <= p.number <= seg.page_end]
    entry = get_type_entry(seg.doc_type)
    doc: Dict[str, Any] = {
        "documentType": seg.doc_type,
        "documentSubtype": seg.subtype,
        "pageStart": seg.page_start,
        "pageEnd": seg.page_end,
        "classificationConfidence": round(seg.confidence, 4),
    }
    if seg.doc_type == "OTRO" or not (entry.extraction_fields or entry.field_tree):
        doc["status"] = engine.DOC_REVIEW if seg.doc_type == "OTRO" else engine.DOC_APPROVED
        doc["fields"] = {}
        doc["_entities"] = {}
        doc["_text"] = "\n".join(p.text for p in seg_pages)
        return doc
    try:
        extracted = extract_document(seg_pages, entry, model_id=model_id)
        doc["_entities"] = extracted.entity_map
        doc["_confidences"] = extracted.confidences
        doc["_text"] = "\n".join(p.text for p in seg_pages)
        doc["extractionMeta"] = extracted.meta
        _recover_from_qr(doc, entry, seg, reader, seg_pages)
        _recover_references(doc, entry)
        # Deterministic customs liquidation normalization (config-driven):
        # DTA/IGI/IVA/PRV/IEPS_* from CUADRO_LIQUIDACION.CONCEPTOS + CONTRIBUCIONES.
        liq = build_liquidacion_normalizada(doc["_entities"], entry)
        if liq is not None:
            doc["_entities"]["LIQUIDACION_NORMALIZADA"] = liq
            doc["liquidacionNormalizada"] = liq
    except Exception as e:
        logger.error("extraction failed for %s pages %d-%d: %s", seg.doc_type, seg.page_start, seg.page_end, e)
        doc["status"] = engine.DOC_ERROR
        doc["error"] = str(e)[:300]
        doc["_entities"] = {}
    return doc


def _truncate_pdf(pdf_bytes: bytes, max_pages: int) -> Tuple[bytes, Optional[int]]:
    """Cap a PDF to its first `max_pages` pages before any OCR/Bedrock work.

    Returns (bytes_to_process, original_page_count). `original_page_count` is
    None when no truncation was needed (page count <= max_pages) or the PDF
    can't be parsed here — `load_pages()`'s own error handling takes over in
    that case, unchanged. Never raises.
    """
    from pypdf import PdfReader, PdfWriter  # deferred: pulls pypdf

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total = len(reader.pages)
    except Exception as e:
        logger.warning("could not read page count for truncation check: %s", e)
        return pdf_bytes, None

    if total <= max_pages:
        return pdf_bytes, None

    writer = PdfWriter()
    for i in range(max_pages):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    logger.warning("truncating PDF from %d to %d pages (MAX_PAGES)", total, max_pages)
    return buf.getvalue(), total


def process_pdf(
    pdf_bytes: bytes,
    declared_type: Optional[str] = None,
    expected_values: Optional[Dict[str, str]] = None,
    model_id: Optional[str] = None,
    source_name: str = "",
) -> Dict[str, Any]:
    """Process a (possibly multi-document) PDF and return the full result."""
    started = time.time()
    started_at = datetime.now(timezone.utc)
    processing_id = str(uuid.uuid4())
    expected_values = expected_values or {}

    def _fail(message: str) -> Dict[str, Any]:
        return _error_result(processing_id, source_name, message,
                             _tracking(started_at, started, model_id))

    pdf_bytes, original_page_count = _truncate_pdf(pdf_bytes, _MAX_PAGES)

    try:
        pages = load_pages(pdf_bytes)
    except Exception as e:
        logger.error("failed to read PDF: %s", e)
        return _fail(f"unreadable PDF: {e}")

    if not pages:
        return _fail("PDF has no pages")

    segments = segment_pages(pages, declared_type=declared_type, model_id=model_id)

    # Shared reader for QR-recovery rasterization only (see qr_recovery.py) —
    # same reader-shared-across-threads pattern pdf_pages.load_pages already
    # uses for its own Textract OCR workers. None when the PDF can't be
    # re-read here; `_recover_from_qr` no-ops in that case.
    try:
        from pypdf import PdfReader as _PdfReader

        qr_reader = _PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        logger.warning("could not open PDF for QR recovery: %s", e)
        qr_reader = None

    # ---- extraction pass (all documents first, so cross-doc rules see siblings).
    # Segments are extracted concurrently (each is an independent Bedrock call);
    # ThreadPoolExecutor.map preserves segment order, so `documents` stays in
    # page order for the downstream validation and cross-document passes.
    workers = min(_EXTRACT_WORKERS, len(segments)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        documents: List[Dict[str, Any]] = list(
            pool.map(lambda seg: _extract_segment(seg, pages, model_id, qr_reader), segments)
        )

    # ---- validation pass (with sibling context)
    siblings = [
        {"type": d["documentType"], "entities": d.get("_entities", {})}
        for d in documents
    ]
    for doc in documents:
        if doc.get("status") in (engine.DOC_ERROR,):
            continue
        entry = get_type_entry(doc["documentType"])
        if not entry.extraction_fields and not entry.field_tree:
            continue
        ctx = ValidationContext(
            expected_values=expected_values,
            sibling_documents=[s for s in siblings if s["entities"] is not doc.get("_entities")],
            document_text=doc.get("_text", ""),
        )
        entity_map = doc.get("_entities", {})
        confidences = doc.get("_confidences", {})
        if entry.field_tree:
            # Field-centric DSL (schema_version 4.x): one recursive structure
            # IS the public result — no separate _public_entities/resolve_fields
            # split (see catalog-dsl-v4-migration memory for why the legacy
            # trio existed and why arrays/objects were unrepresentable there).
            # QR presence/validity is also a first-class catalog field: any
            # boolean with `qr_source: "valid"` can carry a `boolean_true`
            # rule, so normal rule dispatch handles it without a TYPE_ID or
            # output-field special case here.
            resolved_tree, checks = engine.resolve_field_tree(entity_map, confidences, entry, ctx)
            doc["extractedEntities"] = resolved_tree
            doc["fields"] = {}
            doc["checks"] = _checks_json(checks)
            doc["status"] = engine.document_status_from_checks(checks)
            doc["businessDestination"] = entry.business_destination or None
            conf_values = [v for v in confidences.values() if v is not None]
            doc["confidence"] = round(sum(conf_values) / len(conf_values), 4) if conf_values else None
            # Persisted (not popped) — handler.py's envelope assembly needs to
            # know, per document, whether `extractedEntities` is already the
            # full recursive field-centric-DSL shape (pass through as-is) or
            # the legacy raw-values view (needs its own compacting/filtering).
            doc["fieldTreeShape"] = True
        else:
            checks = engine.run_rules(entity_map, entry, ctx)
            resolved = engine.resolve_fields(entity_map, confidences, checks, entry)
            doc["fields"] = resolved
            doc["checks"] = _checks_json(checks)
            doc["status"] = engine.document_status(resolved, checks, entry)
            doc["businessDestination"] = entry.business_destination or None
            conf_values = [f["confidence"] for f in resolved.values() if f.get("confidence") is not None]
            doc["confidence"] = round(sum(conf_values) / len(conf_values), 4) if conf_values else None

    # ---- expediente-level cross-document rules
    cross_checks = _run_cross_document_rules(documents, expected_values)

    # ---- assemble
    for doc in documents:
        doc.pop("_text", None)
        doc.pop("_confidences", None)
        entities = doc.pop("_entities", {})
        if "fields" not in doc:
            doc["fields"] = {}
        if not doc.get("fieldTreeShape"):
            doc["extractedEntities"] = _public_entities(entities)

    statuses = [d.get("status") for d in documents if d.get("documentType") != "OTRO"]
    overall = _overall_status(statuses, cross_checks)

    tracking = _tracking(
        started_at, started, model_id,
        doc_types=[d["documentType"] for d in documents],
    )
    warnings = [c["message"] for c in cross_checks if not c["valid"]]
    truncated = original_page_count is not None
    if truncated:
        warnings.append(
            f"Documento truncado a {_MAX_PAGES} de {original_page_count} páginas "
            "para acotar costo de procesamiento; el PDF original completo "
            "permanece sin modificar en S3 — reprocesar manualmente si se "
            "necesitan las páginas restantes."
        )
    return {
        "processingId": processing_id,
        "sourceFile": source_name,
        "processedAt": tracking["processingCompletedAt"],
        "processingSeconds": tracking["processingSeconds"],
        "tracking": tracking,
        # Model per phase that actually ran. The handler merges in the
        # classification/processing phases, which run in a different Lambda.
        "modelIds": used_models(),
        "pages": pages_summary(pages),
        "status": overall,
        "reviewRequired": truncated or overall in (engine.DOC_REVIEW, engine.DOC_PARTIAL),
        "documents": documents,
        "crossDocumentChecks": cross_checks,
        "warnings": warnings,
        **({"truncated": {"originalPages": original_page_count, "processedPages": _MAX_PAGES}} if truncated else {}),
    }


def _public_entities(entity_map: Dict[str, Any]) -> Dict[str, Any]:
    """Compact entity view (original extracted values only)."""
    return {k: v for k, v in entity_map.items() if v not in (None, "", [])}


def _checks_json(checks: List[RuleCheck]) -> List[Dict[str, Any]]:
    return [
        {
            "field": c.field,
            "rule": c.rule,
            "valid": c.valid,
            "severity": c.severity,
            "skipped": c.skipped,
            "message": c.message,
            **({"details": c.details} if c.details else {}),
        }
        for c in checks
    ]


def _overall_status(statuses: List[str], cross_checks: List[Dict[str, Any]]) -> str:
    if not statuses:
        return engine.DOC_REVIEW
    cross_failed = any(not c["valid"] and c.get("severity") == "error" and not c.get("skipped") for c in cross_checks)
    if any(s == engine.DOC_ERROR for s in statuses):
        return engine.DOC_ERROR if all(s == engine.DOC_ERROR for s in statuses) else engine.DOC_PARTIAL
    if any(s == engine.DOC_REJECTED for s in statuses) or cross_failed:
        return engine.DOC_PARTIAL if any(s == engine.DOC_APPROVED for s in statuses) else engine.DOC_REJECTED
    if any(s in (engine.DOC_PARTIAL, engine.DOC_REVIEW) for s in statuses):
        return engine.DOC_REVIEW
    return engine.DOC_APPROVED


def _has_field(entry, name: str) -> bool:
    """True when `name` is a declared top-level field, under either DSL —
    legacy `extraction_fields` (list) or field-centric `field_tree` (dict)."""
    return name in entry.extraction_fields or bool(entry.field_tree and name in entry.field_tree)


def _qr_spec_from_fields(entry) -> Dict[str, Any]:
    """Merge field-level ``qr_source`` declarations into ``qr_recovery``.

    New field-centric catalogs declare QR output where the field itself is
    defined. The older top-level ``fields``/``decoded_text_field``/
    ``validity_field`` shape remains supported so deployed catalogs such as
    PEDIMENTO_ADUANAL do not need a coordinated migration.
    """
    spec = dict(entry.qr_recovery or {})
    mappings = dict(spec.get("fields") or {})
    validity_requires = None

    for name, field_spec in (entry.field_tree or {}).items():
        params = getattr(field_spec, "params", {}) or {}
        source = params.get("qr_source")
        if isinstance(source, dict):
            mappings[name] = source
        elif source in ("payload", "decoded_text"):
            spec["decoded_text_field"] = name
        elif source == "valid":
            spec["validity_field"] = name
            validity_requires = params.get("qr_requires")

    if mappings:
        spec["fields"] = mappings
    if spec.get("validity_field") and not spec.get("required_fields"):
        spec["required_fields"] = validity_requires or list(mappings)
    return spec


def _recover_from_qr(doc: Dict[str, Any], entry, seg: Segment, reader: Optional[Any], seg_pages: List[PageText]) -> None:
    """Deterministic recovery from an embedded QR code (e.g. a pedimento or
    SAT fiscal certificate), when the type declares a field with `qr_source`
    or the legacy top-level `qr_recovery` mapping. Field mappings and output
    names are catalog-driven, not coupled to a specific document type.

    Page selection is text-anchored, not position-assumed: `qr_recovery.
    extract_qr_recovery` looks for the catalog's declared `page_marker`
    (the DEPOSITO REFERENCIADO / LINEA DE CAPTURA box's heading) across
    `seg_pages` and decodes from THAT page — the real pedimento layout puts
    the QR there, on whichever page NUM. PEDIMENTO is printed on, which isn't
    reliably the segment's first page (`seg.page_start` is still tried as a
    fallback when no marker page is found or it doesn't decode).

    Runs BEFORE `_recover_references` so the regex-based recovery there only
    fires for whatever the QR didn't provide (its own `not digits_exact(...)`
    guard already skips fields the QR left in a valid, exact-length state).

    Unlike that regex recovery, a QR-sourced value overrides the model's own
    value unconditionally, not just when empty or the wrong length — trusting
    data the SAT itself embedded over anything OCR/LLM-derived is the whole
    point of this recovery source, so it always wins.
    """
    if reader is None:
        return
    from .qr_recovery import extract_qr_recovery

    qr_spec = _qr_spec_from_fields(entry)
    if not qr_spec:
        return
    result = extract_qr_recovery(seg_pages, seg.page_start, reader, qr_spec)
    # No leading underscore: unlike _entities/_confidences/_text (popped
    # before the result is returned, see the "assemble" pass in process_pdf),
    # this is meant to survive into the full result for audit — same
    # convention as recoveredFields/checks/extractionMeta.
    required_qr_fields = qr_spec.get("required_fields") or []
    qr_valid = (
        all(name in result["fields"] for name in required_qr_fields)
        if required_qr_fields
        else (
            bool(result["fields"])
            if qr_spec.get("fields")
            else bool(result["qrPresent"])
        )
    )
    doc["qrRecovery"] = {
        "qrPresent": result["qrPresent"],
        "valid": qr_valid,
        "decodedText": result["decodedText"],
        "fieldsRecovered": sorted(result["fields"]),
        "markerPageFound": result["markerPageFound"],
        "pageChecked": result["pageChecked"],
    }

    entities = doc.setdefault("_entities", {})
    confidences = doc.setdefault("_confidences", {})
    confidence = float(qr_spec.get("confidence", 0.99))

    # The raw QR payload itself, exposed as a regular (computed) catalog
    # field when the type declares one — e.g. QR_PEDIMENTO or QR_CONSTANCIA —
    # so the literal data read from the QR is visible in extractedEntities,
    # not just this internal audit dict. Populated whenever a QR decoded at
    # all, even if its params didn't map onto any of the fields below.
    decoded_text_field = str(
        qr_spec.get("decoded_text_field") or "QR_PEDIMENTO"
    )
    if result["decodedText"] and _has_field(entry, decoded_text_field):
        entities[decoded_text_field] = result["decodedText"]
        confidences[decoded_text_field] = confidence

    # Formal "the expected QR and its required fields are present" signal —
    # a required boolean field (catalog validation: `boolean_true`) rather
    # than just the best-effort audit dict above, so a missing/incomplete QR
    # surfaces as a real validation failure.
    validity_field = str(
        qr_spec.get("validity_field") or "DEPOSITO_REFERENCIADO_QR_VALIDO"
    )
    if _has_field(entry, validity_field):
        entities[validity_field] = qr_valid
        confidences[validity_field] = confidence

    if not result["fields"]:
        return

    recovered = doc.setdefault("recoveredFields", {})
    for name, value in result["fields"].items():
        if not _has_field(entry, name):
            continue
        entities[name] = value
        confidences[name] = confidence
        recovered[name] = value


def _recover_references(doc: Dict[str, Any], entry) -> None:
    """Deterministic post-extraction recovery for pedimento references.

    When the LLM leaves PEDIMENTO_REFERENCE / NUMERO_PEDIMENTO(_NORMALIZADO)
    empty but the segment text contains an unambiguous pedimento pattern,
    fill it from the regex match. The recovery is recorded in
    `recoveredFields` (field → matched text) so it is auditable and clearly
    distinguished from model output.
    """
    entities = doc.get("_entities") or {}
    text = doc.get("_text", "")
    recovered: Dict[str, str] = {}

    # PEDIMENTO_REFERENCE legitimately takes several shapes on other document
    # types (find_pedimento_reference itself falls back to shorter
    # patente+consecutivo or bare-consecutivo forms), so it's only filled
    # when the model left it empty. NUMERO_PEDIMENTO is different: this
    # catalog's own validation requires exactly 15 digits, but a real,
    # observed model failure mode grabs the WRONG same-page field — the
    # pedimento form prints both "NUM. PEDIMENTO: 26 07 0823 6001630" in the
    # header AND a separate "PEDIMENTO: 6001630" (bare consecutivo only) in
    # the PAGO ELECTRÓNICO box, and the model sometimes reads the second one
    # into this field. That's a non-empty but wrong-length value, so it needs
    # the same "not digits_exact" override the 4 atomic fields already get
    # below, not just an empty-value fill.
    if _has_field(entry, "PEDIMENTO_REFERENCE") and entities.get("PEDIMENTO_REFERENCE") in (None, "", []):
        match = find_pedimento_reference(text)
        if match:
            entities["PEDIMENTO_REFERENCE"] = match
            doc.setdefault("_confidences", {})["PEDIMENTO_REFERENCE"] = 0.95
            recovered["PEDIMENTO_REFERENCE"] = match

    if _has_field(entry, "NUMERO_PEDIMENTO") and not digits_exact(entities.get("NUMERO_PEDIMENTO"), 15):
        match = find_pedimento_reference(text)
        digits = digits_exact(match, 15) if match else None
        if digits:
            entities["NUMERO_PEDIMENTO"] = digits
            doc.setdefault("_confidences", {})["NUMERO_PEDIMENTO"] = 0.95
            recovered["NUMERO_PEDIMENTO"] = digits

    # Fill any of the 4 atomic pedimento-number components the model left
    # empty OR extracted with the wrong digit count, by splitting the full
    # "AA ADUANA PATENTE CONSECUTIVO" reference (the same 15-digit pattern
    # used for PEDIMENTO_REFERENCE/NUMERO_PEDIMENTO recovery above) when it
    # appears in the segment text — e.g. ADUANA_DESPACHO regularly comes back
    # empty even when the full reference containing that same 2-digit group
    # was extracted elsewhere, and NUMERO_CONSECUTIVO has been observed
    # missing a trailing digit (7 expected, model wrote 6) even though the
    # full reference in the text has all 7. Each regex group's width is
    # fixed by `_PED_FULL_RE` itself, so `comp_value` is always exactly the
    # expected length when found — safe to override a wrong-length value,
    # never just an empty one.
    components = find_pedimento_components(text)
    if components:
        anio_c, aduana_c, patente_c, consecutivo_c = components
        for name, comp_value, expected_len in (
            ("ANIO_PEDIMENTO", anio_c, 2),
            ("ADUANA_DESPACHO", aduana_c, 2),
            ("PATENTE", patente_c, 4),
            ("NUMERO_CONSECUTIVO", consecutivo_c, 7),
        ):
            if _has_field(entry, name) and not digits_exact(entities.get(name), expected_len):
                entities[name] = comp_value
                doc.setdefault("_confidences", {})[name] = 0.9
                recovered[name] = comp_value

    # ANIO_PEDIMENTO must be 2 digits; models sometimes expand it to 4
    # ("2026" instead of "26") despite the catalog hint saying otherwise —
    # collapsing it loses no information (same digits) so it's safe to fix
    # deterministically rather than leave it failing its own regex rule.
    if _has_field(entry, "ANIO_PEDIMENTO"):
        anio_raw = entities.get("ANIO_PEDIMENTO")
        fixed_anio = normalize_year_2digit(anio_raw) if anio_raw not in (None, "", []) else None
        if fixed_anio and fixed_anio != str(anio_raw).strip():
            entities["ANIO_PEDIMENTO"] = fixed_anio
            recovered["ANIO_PEDIMENTO"] = fixed_anio

    # Derived, purely mechanical: the normalized 15-digit form. Prefer
    # rebuilding it from the 4 atomic fields the model already extracted
    # separately (ANIO_PEDIMENTO+ADUANA_DESPACHO+PATENTE+NUMERO_CONSECUTIVO)
    # over trusting whatever the model wrote directly into
    # NUMERO_PEDIMENTO_NORMALIZADO — the catalog asks the model to build
    # that value itself via string concatenation, which is exactly the kind
    # of task free-text generation is unreliable at (verified: a real run
    # returned 14 digits instead of 15, dropping the trailing 0 of
    # NUMERO_CONSECUTIVO, even though NUMERO_CONSECUTIVO itself was
    # extracted correctly as its own field). Concatenating in code cannot
    # drop or duplicate a digit the way regenerating the string can, so this
    # overrides the model's value whenever all 4 parts are present and each
    # already has its expected digit count.
    if _has_field(entry, "NUMERO_PEDIMENTO_NORMALIZADO"):
        anio = digits_exact(entities.get("ANIO_PEDIMENTO"), 2)
        aduana = digits_exact(entities.get("ADUANA_DESPACHO"), 2)
        patente = digits_exact(entities.get("PATENTE"), 4)
        consecutivo = digits_exact(entities.get("NUMERO_CONSECUTIVO"), 7)
        computed = f"{anio}{aduana}{patente}{consecutivo}" if all((anio, aduana, patente, consecutivo)) else None

        current = entities.get("NUMERO_PEDIMENTO_NORMALIZADO")
        if computed and computed != current:
            entities["NUMERO_PEDIMENTO_NORMALIZADO"] = computed
            doc.setdefault("_confidences", {})["NUMERO_PEDIMENTO_NORMALIZADO"] = (
                doc.get("_confidences", {}).get("NUMERO_CONSECUTIVO", 0.9)
            )
            recovered["NUMERO_PEDIMENTO_NORMALIZADO"] = computed
        elif not computed and not digits_exact(current, 15):
            # Fallback for when the atomic fields aren't all usable. Also
            # fires when the model's OWN concatenation attempt is present but
            # invalid (wrong digit count) — not just when it's empty: a real
            # run left NUMERO_CONSECUTIVO one digit short (so `computed` above
            # is None) AND the model's direct NUMERO_PEDIMENTO_NORMALIZADO
            # guess was also short, while NUMERO_PEDIMENTO itself (a
            # different field) had the correct, complete 15-digit reference
            # with spaces. The previous `current in (None, "", [])` guard
            # skipped this recovery entirely whenever the model wrote
            # *anything* here, even a wrong value — only overwrite when the
            # fallback source verifiably yields exactly 15 digits, so a bad
            # guess is never replaced by an equally unverified one.
            source = entities.get("NUMERO_PEDIMENTO") or entities.get("PEDIMENTO_REFERENCE")
            digits = normalize_pedimento(source) if source else None
            if digits and len(digits) == 15:
                entities["NUMERO_PEDIMENTO_NORMALIZADO"] = digits
                doc.setdefault("_confidences", {})["NUMERO_PEDIMENTO_NORMALIZADO"] = (
                    doc.get("_confidences", {}).get("NUMERO_PEDIMENTO", 0.9)
                )
                recovered["NUMERO_PEDIMENTO_NORMALIZADO"] = digits

    # NUMERO_PEDIMENTO_NORMALIZADO at >=0.99 confidence means it's QR-sourced
    # (see qr_recovery's confidence, typically 0.99) — fully trusted, not a
    # model guess. Copy it onto NUMERO_PEDIMENTO too: the model sometimes
    # leaves that field holding only a fragment of what's printed (e.g. just
    # the 7-digit consecutivo "6001630" instead of the full 15-digit
    # reference), even when NORMALIZADO itself is the complete, correct
    # value — no reason to trust the model's own NUMERO_PEDIMENTO over data
    # already confirmed by the QR.
    if _has_field(entry, "NUMERO_PEDIMENTO"):
        normalizado_confidence = doc.get("_confidences", {}).get("NUMERO_PEDIMENTO_NORMALIZADO")
        normalizado_value = digits_exact(entities.get("NUMERO_PEDIMENTO_NORMALIZADO"), 15)
        if (
            normalizado_confidence is not None
            and normalizado_confidence >= 0.99
            and normalizado_value
            and entities.get("NUMERO_PEDIMENTO") != normalizado_value
        ):
            entities["NUMERO_PEDIMENTO"] = normalizado_value
            doc.setdefault("_confidences", {})["NUMERO_PEDIMENTO"] = normalizado_confidence
            recovered["NUMERO_PEDIMENTO"] = normalizado_value

    if recovered:
        doc["recoveredFields"] = recovered


# ------------------------------------------------------- cross-document rules


def _first_value(entities: Dict[str, Any], names: tuple) -> Optional[str]:
    for n in names:
        v = entities.get(n)
        if v not in (None, "", []):
            return str(v)
    return None


def _run_cross_document_rules(
    documents: List[Dict[str, Any]], expected_values: Dict[str, str]
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    for rule in get_cross_document_rules():
        rt = rule.get("rule_type", "")
        try:
            if rt == "same_pedimento":
                checks.append(_cross_same_pedimento(rule, documents))
            elif rt == "expected_importer_rfc":
                checks.append(_cross_importer_rfc(rule, documents, expected_values))
            elif rt == "unique_cfdi_uuid":
                checks.append(_cross_unique_uuid(rule, documents))
            elif rt == "expense_reconciliation":
                checks.append(_cross_expense_reconciliation(rule, documents))
            elif rt == "pedimento_value_reconciliation":
                checks.append(_cross_pedimento_values(rule, documents))
            else:
                checks.append({"rule": rt, "valid": True, "skipped": True,
                               "severity": "warning", "message": f"unknown cross rule '{rt}' skipped"})
        except Exception as e:
            checks.append({"rule": rt, "valid": True, "skipped": True, "severity": "warning",
                           "message": f"cross rule error: {e}"})
    return checks


def _docs_of(documents: List[Dict[str, Any]], types: List[str]) -> List[Dict[str, Any]]:
    return [d for d in documents if d.get("documentType") in types]


def _cross_same_pedimento(rule: Dict[str, Any], documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    involved = _docs_of(documents, rule.get("documents", []))
    refs: Dict[str, List[str]] = {}
    for d in involved:
        entities = d.get("_entities") or d.get("extractedEntities") or {}
        ref = _first_value(entities, _PEDIMENTO_REF_FIELDS)
        cons = pedimento_consecutivo(ref) if ref else None
        if cons:
            refs.setdefault(cons, []).append(d["documentType"])
    if len(refs) <= 1:
        found = next(iter(refs), None)
        return {"rule": "same_pedimento", "valid": True, "skipped": not refs, "severity": "error",
                "message": f"all documents reference pedimento consecutivo {found}" if found
                else "no pedimento references found to compare",
                "details": refs}
    return {"rule": "same_pedimento", "valid": False, "severity": "error", "skipped": False,
            "message": rule.get("message", "documents reference different pedimentos"),
            "details": refs}


# SAT generic RFCs (foreign/public entities) — placeholders, not a different importer
_GENERIC_RFCS = {"XEXX010101000", "XAXX010101000"}


# Documents whose RFC fields routinely carry counterparty RFCs (bank,
# beneficiary): a mismatch there is weak evidence — review, not rejection.
_WEAK_IMPORTER_EVIDENCE_TYPES = {"COMPROBANTE_PAGO_ADUANAL"}


def _cross_importer_rfc(
    rule: Dict[str, Any], documents: List[Dict[str, Any]], expected_values: Dict[str, str]
) -> Dict[str, Any]:
    expected = expected_values.get("IMPORTER_RFC")
    found: Dict[str, List[str]] = {}
    weak: Dict[str, List[str]] = {}
    for d in documents:
        entities = d.get("_entities") or d.get("extractedEntities") or {}
        rfc = _first_value(entities, _IMPORTER_RFC_FIELDS)
        if rfc and rfc.strip().upper() not in _GENERIC_RFCS:
            bucket = weak if d["documentType"] in _WEAK_IMPORTER_EVIDENCE_TYPES else found
            bucket.setdefault(rfc.strip().upper(), []).append(d["documentType"])
    if expected:
        exp = expected.strip().upper()
        bad = {k: v for k, v in found.items() if k != exp}
        weak_bad = {k: v for k, v in weak.items() if k != exp}
        if bad:
            return {"rule": "expected_importer_rfc", "valid": False, "severity": "error", "skipped": False,
                    "message": rule.get("message", "documents belong to a different importer"),
                    "details": {"expected": expected, "found": found, "weakEvidence": weak}}
        if weak_bad:
            return {"rule": "expected_importer_rfc", "valid": False, "severity": "warning", "skipped": False,
                    "message": "payment receipts carry a different RFC (likely counterparty) — review",
                    "details": {"expected": expected, "found": found, "weakEvidence": weak}}
        return {"rule": "expected_importer_rfc", "valid": True, "severity": "error", "skipped": False,
                "message": "importer RFC matches app configuration",
                "details": {"expected": expected, "found": found}}
    # No app expectation: at least require internal consistency
    consistent = len(found) <= 1
    return {"rule": "expected_importer_rfc", "valid": consistent, "severity": "warning", "skipped": False,
            "message": "importer RFC consistent across documents" if consistent
            else "multiple importer RFCs found across documents",
            "details": {"found": found}}


def _cross_unique_uuid(rule: Dict[str, Any], documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    involved = _docs_of(documents, rule.get("documents", []))
    seen: Dict[str, List[Dict[str, Any]]] = {}
    for d in involved:
        entities = d.get("_entities") or d.get("extractedEntities") or {}
        u = normalize_uuid(entities.get("UUID"))
        if u:
            seen.setdefault(u, []).append(d)
    dupes = {u: docs for u, docs in seen.items() if len(docs) > 1}
    # Same UUID + same type + same total = the same CFDI included twice as a
    # copy (scan + digital print is common). That is a review note, not a
    # double-billed expense.
    copies, conflicts = {}, {}
    for u, docs in dupes.items():
        types = {d["documentType"] for d in docs}
        totals = {
            str(_first_value(
                d.get("_entities") or d.get("extractedEntities") or {},
                ("TOTAL", "TOTAL_CFDI"),
            ))
            for d in docs
        }
        if len(types) == 1 and len(totals) == 1:
            copies[u] = len(docs)
        else:
            conflicts[u] = len(docs)
    if conflicts:
        return {"rule": "unique_cfdi_uuid", "valid": False, "severity": "error", "skipped": False,
                "message": rule.get("message", "duplicated CFDI in expediente"),
                "details": {"duplicates": conflicts, "copies": copies}}
    if copies:
        return {"rule": "unique_cfdi_uuid", "valid": True, "severity": "warning", "skipped": False,
                "message": f"same CFDI appears more than once as identical copies: {copies}",
                "details": {"copies": copies}}
    return {"rule": "unique_cfdi_uuid", "valid": True, "severity": "error", "skipped": not seen,
            "message": "no duplicated CFDIs", "details": {}}


def _cross_expense_reconciliation(rule: Dict[str, Any], documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    summaries = _docs_of(documents, [rule.get("summary_document", "")])
    details = _docs_of(documents, rule.get("detail_documents", []))
    if not summaries or not details:
        return {"rule": "expense_reconciliation", "valid": True, "skipped": True, "severity": "warning",
                "message": "summary or detail documents missing; reconciliation skipped"}
    tolerance = abs(float(rule.get("tolerance", 0.02)))
    matches, unmatched = [], []
    summary_entities = summaries[0].get("extractedEntities") or {}
    resumen = summary_entities.get("GASTOS_RESUMEN") or summary_entities.get("GASTOS_CUENTA_TERCEROS") or []
    resumen_amounts = [parse_amount((r or {}).get("IMPORTE")) for r in resumen if isinstance(r, dict)]
    resumen_amounts = [a for a in resumen_amounts if a is not None]
    for d in details:
        entities = d.get("extractedEntities") or {}
        total = parse_amount(entities.get("TOTAL") or entities.get("IMPORTE_TOTAL"))
        if total is None:
            continue
        hit = next((a for a in resumen_amounts if abs(a - total) <= max(tolerance, 0.02 * a)), None)
        (matches if hit is not None else unmatched).append(
            {"documentType": d["documentType"], "total": total})
    # Detail docs not matching any summary line are review-worthy, not rejections
    return {"rule": "expense_reconciliation", "valid": not unmatched, "severity": "warning", "skipped": False,
            "message": "third-party expenses match the expense account summary" if not unmatched
            else rule.get("message", "some expenses do not match the summary"),
            "details": {"matched": matches, "unmatched": unmatched}}


def _cross_pedimento_values(rule: Dict[str, Any], documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    pedimentos = _docs_of(documents, [rule.get("pedimento_document", "PEDIMENTO_ADUANAL")])
    invoices = _docs_of(documents, [rule.get("foreign_invoice_document", "FACTURA_PROVEEDOR_EXTRANJERO")])
    if not pedimentos or not invoices:
        return {"rule": "pedimento_value_reconciliation", "valid": True, "skipped": True, "severity": "warning",
                "message": "pedimento or foreign invoice missing; value reconciliation skipped"}
    ped = pedimentos[0].get("extractedEntities") or {}
    val_dolares = parse_amount(ped.get("VALOR_DOLARES"))
    inv_totals = [parse_amount((i.get("extractedEntities") or {}).get("TOTAL")) for i in invoices]
    inv_total = sum(t for t in inv_totals if t is not None)
    if val_dolares is None or inv_total == 0:
        return {"rule": "pedimento_value_reconciliation", "valid": True, "skipped": True, "severity": "warning",
                "message": "amounts unavailable; value reconciliation skipped"}
    # Invoice may exclude/include freight; allow 5% slack before flagging
    ok = abs(val_dolares - inv_total) <= max(0.05 * val_dolares, 1.0)
    return {"rule": "pedimento_value_reconciliation", "valid": ok, "severity": "warning", "skipped": False,
            "message": "pedimento USD value consistent with foreign invoices" if ok
            else rule.get("message", "pedimento value does not match foreign invoices"),
            "details": {"pedimentoValorDolares": val_dolares, "foreignInvoicesTotal": inv_total}}


def _error_result(
    processing_id: str,
    source_name: str,
    message: str,
    tracking: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tracking = tracking or _tracking(datetime.now(timezone.utc), time.time(), None)
    return {
        "processingId": processing_id,
        "sourceFile": source_name,
        "processedAt": tracking["processingCompletedAt"],
        "processingSeconds": tracking["processingSeconds"],
        "tracking": tracking,
        # Model per phase that actually ran. The handler merges in the
        # classification/processing phases, which run in a different Lambda.
        "modelIds": used_models(),
        "status": engine.DOC_ERROR,
        "reviewRequired": True,
        "documents": [],
        "crossDocumentChecks": [],
        "warnings": [message],
    }
