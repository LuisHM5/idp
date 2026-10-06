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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from document_catalog import (
    get_cross_document_rules,
    get_type_entry,
    normalize_declared_types,
    resolve_declared_counts,
)
from validators import engine
from validators.engine import RuleCheck, ValidationContext

from .derived import SOURCE_DERIVED, apply_derived_fields, build_context
from .liquidacion import apply_liquidacion_normalizada
from .normalize import (
    digits_exact,
    find_pedimento_components,
    find_pedimento_reference,
    looks_like_pedimento_field,
    normalize_pedimento,
    normalize_uuid,
    normalize_year_2digit,
    parse_amount,
    pedimento_consecutivo,
)
from . import confidence as conf_policy
from . import page_locator, page_selector
from .pdf_pages import PageText, load_image_page, load_pages, pages_summary
from .bedrock import fallback_events, stage_degradations, used_models
from .pricing import merge_bedrock_usage
from .result_common import build_tracking, checks_to_json, error_result
from .segmenter import CONTAINER_TYPE, Segment, segment_pages
from .text_extractor import _chunk_pages as chunk_pages, extract_document

logger = logging.getLogger(__name__)

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

# Ceiling that applies INSTEAD of `_MAX_PAGES` when a type selects its own
# pages (see `_page_cap_for`). Deliberately far higher: this one is not a cost
# guard — the selector already bounds cost by only extracting relevant pages —
# it only stops a pathological upload (a mis-scanned 2,000-page bundle) from
# burning the whole 15-minute Lambda budget on page loading. A type can raise
# or lower it with `page_selection.max_source_pages`.
_MAX_PAGES_WITH_SELECTION = int(os.environ.get("MAX_PAGES_WITH_SELECTION", "500"))


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
    source_name: str = "",
) -> Dict[str, Any]:
    """Extract one segment into a document dict. Runs in a worker thread.

    Self-contained (reads only its own pages + catalog entry) so it can be mapped
    concurrently over all segments without shared mutable state. `reader` is a
    shared pypdf.PdfReader over the whole file (same sharing pattern
    pdf_pages.load_pages already uses across its OCR worker threads) — only
    read from when the segment's catalog declares a QR source. `source_name`
    is the upload's name, which only `derived` blocks reading `$FILE_NAME` use
    — it describes the FILE, so every segment cut from it sees the same one.
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
    # Cost gate: send only the pages that carry this type's fields to Bedrock.
    # Deliberately narrow — ONLY the extraction call sees the filtered pages:
    # `_text` (cross-document rules, reference recovery) and the QR search
    # keep the segment's full page set, because those are free and restricting
    # them could only lose information. Opt-in per type, fail-open when
    # nothing matches; see pipeline/page_selector.py.
    extract_pages = seg_pages
    selection = None
    if page_selector.enabled_for(entry):
        spec = page_selector.spec_for(entry)
        selection = page_selector.select(seg_pages, spec)
        # The free path gave up (no marker cleared the bar, or the selection
        # was too wide to trust) and the type says its size is variable, so
        # there is no fixed range to fall back on either. Spend one cheap call
        # on page digests rather than every page's full text on extraction.
        if selection.fail_open and page_locator.wants_locator(spec):
            located, locator_usage = page_locator.locate(seg_pages, entry, model_id)
            if locator_usage:
                doc["pageLocatorMeta"] = locator_usage
            if located is not None:
                selection = located
        if selection.filtered:
            extract_pages = page_selector.filter_pages(seg_pages, selection)
            logger.info(
                "page_selector: %s páginas %d-%d → extrayendo %s (%s)",
                seg.doc_type, seg.page_start, seg.page_end,
                selection.selected, selection.reason,
            )
        else:
            selection = None

    try:
        extracted = extract_document(extract_pages, entry, model_id=model_id)
        doc["_entities"] = extracted.entity_map
        doc["_confidences"] = extracted.confidences
        # Per-row confidences of array fields (see pipeline.confidence): the
        # liquidation totals below average the cells they sum, and the
        # validation pass gives each row cell its own number instead of the
        # whole array sharing the segment's OCR score.
        doc["_row_confidences"] = extracted.row_confidences
        doc["_text"] = "\n".join(p.text for p in seg_pages)
        doc["extractionMeta"] = extracted.meta
        if selection is not None:
            # Priced with the REAL usage Bedrock billed for the pages that
            # were sent, so `savings` reports what filtering actually avoided
            # instead of a guess made before the call. The counterfactual
            # chunk count is exact and free — `_chunk_pages` is pure text
            # math over page lengths, no model call — and it is what prices
            # the OUTPUT side, since every chunk emits one full schema JSON.
            doc["pageSelection"] = selection.to_json(
                extracted.meta.get("usage"),
                chunks_sent=int(extracted.meta.get("chunks") or 0),
                chunks_without_selection=len(chunk_pages(seg_pages)),
            )
        _recover_from_qr(doc, entry, seg, reader, seg_pages)
        _recover_references(doc, entry)
        # Deterministic customs liquidation normalization (config-driven):
        # DTA/IGI/IVA/PRV/IEPS_* from CUADRO_LIQUIDACION.CONCEPTOS + CONTRIBUCIONES.
        liq = apply_liquidacion_normalizada(
            doc["_entities"], doc["_confidences"], entry, doc["_row_confidences"]
        )
        if liq is not None:
            doc["liquidacionNormalizada"] = liq
        # Config-driven derived fields (`derived` blocks in the catalog): a value
        # decided by condition from fields already established, not read off the
        # page. After the liquidation build on purpose, so a rule can read either
        # the raw extraction or a normalized concept total.
        derived = apply_derived_fields(
            doc["_entities"], doc.setdefault("_confidences", {}), entry,
            build_context(source_name),
        )
        doc.setdefault("_recovered_sources", {}).update(derived.sources)
        if derived.labels:
            doc["_derived_labels"] = derived.labels
    except Exception as e:
        # Full traceback goes to CloudWatch (logger.exception, not just
        # logger.error's bare message) so a real crash is diagnosable without
        # reproducing it locally; the JSON result itself carries the
        # exception type alongside the truncated message for the same
        # "what actually broke" triage without needing to reproduce.
        logger.exception("extraction failed for %s pages %d-%d", seg.doc_type, seg.page_start, seg.page_end)
        if selection is not None:
            # No usage to price against, but WHICH pages were dropped is
            # exactly what you need to tell a bad selection from a bad model.
            doc["pageSelection"] = selection.to_json()
        doc["status"] = engine.DOC_ERROR
        doc["error"] = str(e)[:300]
        doc["errorType"] = type(e).__name__
        doc["_entities"] = {}
    return doc


def _truncate_pdf(pdf_bytes: bytes, max_pages: int) -> Tuple[bytes, Optional[int]]:
    """Cap a PDF to its first `max_pages` pages before any OCR/Bedrock work.

    Returns (bytes_to_process, original_page_count). `original_page_count` is
    None when no truncation was needed (page count <= max_pages) or the PDF
    can't be parsed here — `load_pages()`'s own error handling takes over in
    that case, unchanged. Never raises.

    Keeps a PREFIX of the pages, in order. The authenticity stage relies on
    that: it reads the ORIGINAL bytes for file integrity and indexes both
    files with the same page numbers (`authenticity.PageSource`).
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


def _page_cap_for(declared_types: Optional[Sequence[str]]) -> Tuple[int, bool]:
    """How many pages of the upload to keep, and whether selection is driving it.

    Truncation and page selection are the two cost guards, and they are
    MUTUALLY EXCLUSIVE — running both would defeat the point. `_truncate_pdf`
    runs before anything else, so with `MAX_PAGES=50` a selector would only
    ever get to choose among the first 50 pages of a 68-page upload: a
    pedimento sitting on page 63 would be dropped before the selector could
    even see it, which is exactly the outcome selection exists to prevent.

    So a type that selects its own pages does NOT get blind truncation. Its
    cost is bounded by how many pages actually carry its fields, and the only
    remaining cap is the much higher `_MAX_PAGES_WITH_SELECTION` guard against
    an upload big enough to exhaust the Lambda.

    Everything else keeps today's behaviour exactly: a type without
    `page_selection`, a type with `enabled: false`, `PAGE_SELECTION_ENABLED=false`,
    and — importantly — an upload with NO declared type, since which selector
    applies isn't known until the segmenter has classified the pages, and
    classifying them requires reading them first.

    With SEVERAL declared types the answer still has to be one number, decided
    before anything has been classified. It is ANY, not all: if even one of the
    declared types selects its own pages, truncating blindly reintroduces
    exactly the bug above — with `MAX_PAGES=50`, a pedimento on page 63 of a
    68-page upload is gone before its selector can see it. The cost of
    EXTRACTION stays bounded either way, because each segment is filtered by its
    own type's selector in `_extract_segment`; what grows is page loading and
    OCR, which `max_source_pages` is there to cap.
    """
    types = normalize_declared_types(declared_types)
    if not types:
        return _MAX_PAGES, False
    caps: List[int] = []
    for declared_type in types:
        try:
            entry = get_type_entry(declared_type)
        except Exception:  # unknown/unloadable type: behave exactly as before
            continue
        if not page_selector.enabled_for(entry):
            continue
        spec = page_selector.spec_for(entry) or {}
        caps.append(int(spec.get("max_source_pages", _MAX_PAGES_WITH_SELECTION)))
    if not caps:
        return _MAX_PAGES, False
    return max(caps), True


def _only_requested_segments(
    segments: List[Segment], requested_types: Sequence[str]
) -> List[Segment]:
    """Close a declared request at the pipeline boundary.

    The segmenter still uses `OTRO` internally to sanitize model output and to
    account for gaps, duplicate copies and explicit count limits. Once the
    caller named real document types, those audit-only ranges are not response
    documents and must not reach extraction/validation.

    With no real requested type (auto-classification, or the container
    directive by itself), keep the historical complete-coverage behaviour.
    """
    if not requested_types:
        return segments
    allowed = set(requested_types)
    kept = [segment for segment in segments if segment.doc_type in allowed]
    omitted = len(segments) - len(kept)
    if omitted:
        logger.info(
            "expediente: omitiendo %d segmento(s) fuera de los tipos solicitados %s",
            omitted,
            list(requested_types),
        )
    return kept


def _missing_requested_documents(
    documents: Sequence[Dict[str, Any]],
    requested_types: Sequence[str],
    declared_counts: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return one range-less review marker for every requested occurrence missing.

    Finite `:N` requests get enough markers to expose the exact deficit. `:all`
    has no target cardinality, so it gets one marker only when the type did not
    appear at all. `process_pdf` resolves request overrides, catalog defaults
    and the global one/many-types default before calling this helper.
    """
    if not requested_types:
        return []
    found: Dict[str, int] = {}
    for document in documents:
        doc_type = document.get("documentType")
        if doc_type in requested_types:
            found[doc_type] = found.get(doc_type, 0) + 1

    counts = declared_counts or {}
    missing: List[Dict[str, Any]] = []
    for doc_type in requested_types:
        default_count: Any = 1
        requested = counts.get(doc_type, default_count)
        present = found.get(doc_type, 0)
        if requested == "all":
            deficit = 1 if present == 0 else 0
        elif (
            isinstance(requested, int)
            and not isinstance(requested, bool)
            and requested >= 1
        ):
            deficit = max(requested - present, 0)
        else:
            # Metadata is best-effort and may come from an older/direct caller.
            # A malformed value must not crash the document; use the mode default.
            deficit = 1 if default_count == "all" and present == 0 else max(1 - present, 0)
        missing.extend(
            {
                "documentType": doc_type,
                "pageStart": None,
                "pageEnd": None,
                "status": engine.DOC_REVIEW,
                "confidence": None,
                "fields": {},
            }
            for _ in range(deficit)
        )
    return missing


def process_pdf(
    pdf_bytes: bytes,
    declared_type: Optional[str] = None,
    expected_values: Optional[Dict[str, str]] = None,
    model_id: Optional[str] = None,
    source_name: str = "",
    source_format: str = "pdf",
    declared_types: Optional[Sequence[str]] = None,
    declared_counts: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Process a (possibly multi-document) PDF — or, when `source_format` is
    an image type ("jpg"/"jpeg"/"png"), a single standalone photographed/
    scanned document such as an ID card — and return the full result.

    `declared_types` is the FULL list of types the uploader declared; pass it
    when there is more than one, so the file is segmented and each document
    classified among exactly those (see `segmenter.segment_pages`). With one
    type it is equivalent to `declared_type`, which every existing caller keeps
    using unchanged.

    `context` is what the caller knows ABOUT the upload rather than what is in
    it — `{"documentId", "metadata" (the uploader's own key/values), "appOrigin"}`.
    Optional and generic: today only the authenticity stage reads it (a catalog
    names the metadata key that identifies whose references to compare against,
    e.g. `metadata.proveedor`). A local run simply passes none.
    """
    types = normalize_declared_types(declared_types, declared_type)
    declared_counts = resolve_declared_counts(types, declared_counts)
    started = time.time()
    started_at = datetime.now(timezone.utc)
    processing_id = str(uuid.uuid4())
    expected_values = expected_values or {}
    is_image = source_format in ("jpg", "jpeg", "png")

    def _fail(
        message: str, *, stage: Optional[str] = None, exception: Optional[BaseException] = None
    ) -> Dict[str, Any]:
        return error_result(
            processing_id, source_name, message,
            build_tracking(started_at, started, model_id),
            stage=stage, exception=exception, source_format=source_format,
        )

    # Cronómetro por etapa. Sin esto, "tardó 56 segundos" no se puede desglosar
    # en OCR / segmentación / extracción / validación, y cualquier optimización
    # se evalúa a ciegas. Sale en `tracking.timings` del envelope.
    timings: Dict[str, float] = {}
    stage_started = time.time()

    def _mark(stage: str) -> None:
        nonlocal stage_started
        now = time.time()
        timings[stage] = round((now - stage_started) * 1000, 1)
        stage_started = now

    original_page_count: Optional[int] = None
    page_cap = _MAX_PAGES
    selection_drives_cap = False
    qr_reader = None

    original_bytes = pdf_bytes
    if is_image:
        # No pypdf involved at all: a raw JPEG/PNG has no PDF container for
        # pypdf to parse (that mismatch is exactly what used to surface as
        # "unreadable PDF: Stream has ended unexpectedly" for an image
        # upload) — Textract reads the image bytes directly instead.
        try:
            pages = load_image_page(pdf_bytes)
        except Exception as e:
            logger.exception("failed to read %s image for OCR", source_format)
            return _fail(f"unreadable {source_format} image: {e}", stage="page_load", exception=e)
    else:
        page_cap, selection_drives_cap = _page_cap_for(types)
        # The upload AS RECEIVED, kept for the authenticity stage: truncation
        # rewrites the file and erases the edit history its integrity layer
        # reads (see `authenticity.PageSource`).
        original_bytes = pdf_bytes
        pdf_bytes, original_page_count = _truncate_pdf(pdf_bytes, page_cap)
        try:
            pages = load_pages(pdf_bytes)
        except Exception as e:
            logger.exception("failed to read PDF")
            return _fail(f"unreadable PDF: {e}", stage="page_load", exception=e)

    if not pages:
        return _fail(f"{source_format} document has no pages", stage="page_load")
    _mark("pageLoad")

    segments, segmentation_usage = segment_pages(
        pages, model_id=model_id, declared_types=types, declared_counts=declared_counts
    )
    requested_types = [doc_type for doc_type in types if doc_type != CONTAINER_TYPE]
    segments = _only_requested_segments(segments, requested_types)
    _mark("segmentation")

    if not is_image:
        # Shared reader for QR-recovery rasterization only (see qr_recovery.py) —
        # same reader-shared-across-threads pattern pdf_pages.load_pages already
        # uses for its own Textract OCR workers. None when the PDF can't be
        # re-read here; `_recover_from_qr` no-ops in that case. Images skip this
        # entirely: there is no PDF page for qr_recovery.py's pypdfium2-based
        # rasterization to work from, so a type with `qr_source` fields simply
        # leaves them unfilled for an image-sourced document — a real gap only
        # if a future catalog type needs a QR read off a photographed document.
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
            pool.map(
                lambda seg: _extract_segment(seg, pages, model_id, qr_reader, source_name),
                segments,
            )
        )
    _mark("extraction")

    # ---- physical-authenticity pass (pipeline/authenticity.py). Its own pass,
    # NOT one more enricher inside `_extract_segment`: that chain shares a
    # single try/except, so a failure in here would turn a perfectly extracted
    # document into a PROCESSING_ERROR. It also needs the extracted fields
    # (the issuer the stamp must name, the employee+date that identify the
    # letter), and deserves its own line in `tracking.timings`. A type without
    # an `authenticity` block costs one dict lookup.
    _run_authenticity(documents, pdf_bytes, qr_reader, is_image, context, original_bytes)
    _mark("authenticity")

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
            resolved_tree, checks, effective_confidences = engine.resolve_field_tree(
                entity_map, confidences, entry, ctx,
                source_overrides=doc.get("_recovered_sources"),
                value_labels=doc.get("_derived_labels"),
                row_confidences=doc.get("_row_confidences"),
                # How well the SOURCE was read, for values the extractor
                # attached no number to — see the discard rule in
                # `resolve_field_tree` / `pipeline.confidence`.
                default_confidence=(doc.get("extractionMeta") or {}).get("segmentConfidence"),
            )
            doc["extractedEntities"] = resolved_tree
            doc["fields"] = {}
            doc["checks"] = checks_to_json(checks)
            doc["status"] = engine.document_status_from_checks(checks)
            doc["businessDestination"] = entry.business_destination or None
            # Average the post-capping (trust-aware) confidences, not the raw
            # model×OCR ones — a field demoted by a failed check should pull
            # the document-level number down too, not just its own node.
            # `effective_confidences` deliberately keeps None for a field with
            # no value: an absent optional field reports `confidence: 0.0` in
            # the output (never null — see pipeline.confidence), but averaging
            # those zeros in would sink the document's own number for fields
            # that were never on the page to begin with.
            confidence_summary = conf_policy.summarize(effective_confidences.values())
            doc["confidence"] = confidence_summary.mean or 0.0
            if confidence_summary.shared is not None:
                doc["fieldConfidence"] = confidence_summary.shared
            # Persisted (not popped) — handler.py's envelope assembly needs to
            # know, per document, whether `extractedEntities` is already the
            # full recursive field-centric-DSL shape (pass through as-is) or
            # the legacy raw-values view (needs its own compacting/filtering).
            doc["fieldTreeShape"] = True
        else:
            checks = engine.run_rules(entity_map, entry, ctx)
            resolved = engine.resolve_fields(entity_map, confidences, checks, entry)
            doc["fields"] = resolved
            doc["checks"] = checks_to_json(checks)
            doc["status"] = engine.document_status(resolved, checks, entry)
            doc["businessDestination"] = entry.business_destination or None
            confidence_summary = conf_policy.summarize(
                f["confidence"] for f in resolved.values()
                if f.get("confidence") is not None
                and f.get("status") not in (engine.NOT_FOUND, engine.NOT_APPLICABLE)
            )
            doc["confidence"] = confidence_summary.mean or 0.0
            if confidence_summary.shared is not None:
                doc["fieldConfidence"] = confidence_summary.shared

    _mark("validation")

    # ---- expediente-level cross-document rules
    cross_checks = _run_cross_document_rules(documents, expected_values)

    # ---- assemble
    for doc in documents:
        doc.pop("_text", None)
        doc.pop("_confidences", None)
        doc.pop("_row_confidences", None)
        # Already consumed as `source_overrides` when the field tree resolved;
        # it is internal bookkeeping, not part of the emitted document.
        doc.pop("_recovered_sources", None)
        doc.pop("_derived_labels", None)
        entities = doc.pop("_entities", {})
        if "fields" not in doc:
            doc["fields"] = {}
        if not doc.get("fieldTreeShape"):
            doc["extractedEntities"] = _public_entities(entities)

    found_documents = list(documents)
    missing_documents = _missing_requested_documents(
        found_documents, requested_types, declared_counts
    )
    documents.extend(missing_documents)

    statuses = [d.get("status") for d in documents if d.get("documentType") != "OTRO"]
    overall = _overall_status(statuses, cross_checks)

    tracking = build_tracking(
        started_at, started, model_id,
        doc_types=[d["documentType"] for d in found_documents],
        timings=timings,
    )
    warnings = [c["message"] for c in cross_checks if not c["valid"]]
    truncated = original_page_count is not None
    if truncated:
        # Con selección activa este tope es la salvaguarda contra un archivo
        # capaz de agotar la Lambda, no un límite de costo — el costo ya lo
        # acota el selector — así que el mensaje dice otra cosa.
        warnings.append(
            f"Documento truncado a {page_cap} de {original_page_count} páginas "
            + (
                "por el tope de procesamiento del tipo (`page_selection.max_source_pages`); "
                if selection_drives_cap
                else "para acotar costo de procesamiento; "
            )
            + "el PDF original completo permanece sin modificar en S3 — "
            "reprocesar manualmente si se necesitan las páginas restantes."
        )
    segmentation_degraded = stage_degradations().get("segmenter") or []
    for degradation in segmentation_degraded:
        # Solo lo que terminó rendido (marcado OTRO) amerita alarmar al
        # usuario final: lo que el reintento sí recuperó ("recovered_on_retry")
        # queda visible en tracking para observabilidad, pero no es un
        # problema que el usuario tenga que revisar.
        if degradation.get("resolution") != "marked_otro":
            continue
        warnings.append(
            f"Clasificación de páginas {degradation.get('pageStart')}-{degradation.get('pageEnd')} "
            f"degradada tras agotar reintentos ({degradation.get('reason')}); "
            "marcada como OTRO y sujeta a revisión manual."
        )
    # Bedrock usage for cost tracking (documentacion/COSTOS.md /
    # cost_audit.py): segmentation comes back from segment_pages() itself;
    # extraction is summed across every segment's extractionMeta.usage (each
    # segment is an independent Converse call, possibly several when chunked)
    # so the whole document's token spend is available in one place instead
    # of scattered per-document.
    extraction_usage = merge_bedrock_usage(
        *(d.get("extractionMeta", {}).get("usage") for d in documents)
    )
    # The AI page locator only runs when the free marker path gave up on a
    # type whose size is variable — a REAL billed call, unlike the estimated
    # savings in `pageSelection`, so it gets its own usage line and its own
    # cost service.
    locator_usage = merge_bedrock_usage(*(d.get("pageLocatorMeta", {}).get("usage") for d in documents))
    locator_model = next(
        (d["pageLocatorMeta"].get("modelId") for d in documents if d.get("pageLocatorMeta")), None
    )
    # Same for the authenticity stage's vision calls: real, billed, and only
    # made for types that declare the stage — its own usage line and service.
    authenticity_usage = merge_bedrock_usage(*(d.get("authenticityMeta", {}).get("usage") for d in documents))
    authenticity_model = next(
        (d["authenticityMeta"].get("modelId") for d in documents if d.get("authenticityMeta")), None
    )
    # Image embeddings of the reference retrieval: billed per image.
    embedding_images = sum(int(d.get("embeddingMeta", {}).get("images") or 0) for d in documents)
    embedding_model = next(
        (d["embeddingMeta"].get("modelId") for d in documents if d.get("embeddingMeta")), None
    )
    usage_summary = {
        "segmentation": segmentation_usage or None,
        "extraction": {"modelId": used_models().get("extractor"), "usage": extraction_usage},
        "pageSelection": _page_selection_summary(documents),
        **({"pageLocator": {"modelId": locator_model, "usage": locator_usage}} if locator_usage else {}),
        **({"authenticity": {"modelId": authenticity_model, "usage": authenticity_usage}}
           if authenticity_usage else {}),
        **({"embeddings": {"modelId": embedding_model, "images": embedding_images}}
           if embedding_images else {}),
    }
    return {
        "processingId": processing_id,
        "sourceFile": source_name,
        "processedAt": tracking["processingCompletedAt"],
        "processingSeconds": tracking["processingSeconds"],
        "processingMs": tracking["processingMs"],
        "tracking": tracking,
        # Model per phase that actually ran. The handler merges in the
        # classification/processing phases, which run in a different Lambda.
        "modelIds": used_models(),
        # Only phases that actually switched models mid-call — see
        # pipeline/bedrock.py::fallback_events.
        "modelFallbacks": fallback_events(),
        # Page ranges the segmenter had to settle for a best-effort result on
        # (empty/truncated model output, even after its own retry) — see
        # pipeline/bedrock.py::stage_degradations and
        # pipeline/segmenter.py::_classify_pages. `[]`, not absent, to match
        # this dict's other always-present keys; handler.py/indexing.py only
        # promote it into tracking/DynamoDB when non-empty.
        "segmentationDegraded": segmentation_degraded,
        "usage": usage_summary,
        "sourceFormat": source_format,
        "pages": pages_summary(pages),
        "status": overall,
        "reviewRequired": (
            bool(missing_documents) or truncated
            or overall in (engine.DOC_REVIEW, engine.DOC_PARTIAL)
            # A degraded-and-marked-OTRO page range can leave `overall`
            # looking fine (e.g. the rest of the expediente extracted
            # cleanly) while a whole range never got classified at all —
            # that always needs a human look, independent of what the other
            # documents' own status says.
            or any(d.get("resolution") == "marked_otro" for d in segmentation_degraded)
        ),
        "documents": documents,
        "crossDocumentChecks": cross_checks,
        "warnings": warnings,
        **({"truncated": {"originalPages": original_page_count, "processedPages": page_cap}} if truncated else {}),
    }


def _run_authenticity(
    documents: List[Dict[str, Any]],
    file_bytes: bytes,
    reader: Optional[Any],
    is_image: bool,
    context: Optional[Dict[str, Any]],
    original_bytes: Optional[bytes] = None,
) -> None:
    """Run the authenticity/integrity stage on every document whose type
    declares it. Detected by catalog SHAPE (`authenticity.applies_to`), never
    by TYPE_ID. Documents are independent, so they run concurrently; each one
    is isolated — the stage itself never raises, and the guard here covers the
    import too (a missing imaging wheel must cost a review, not the upload).

    `original_bytes` is the upload before `_truncate_pdf`; `file_bytes` is
    what the rest of the pipeline processed. They differ only when the page
    cap kicked in, and the stage needs both (see `authenticity.PageSource`)."""
    try:
        from . import authenticity
    except Exception as e:  # pragma: no cover - only a broken deployment
        logger.error("authenticity stage unavailable: %s", e)
        return

    targets = []
    for doc in documents:
        if doc.get("status") == engine.DOC_ERROR or doc.get("documentType") == "OTRO":
            continue
        entry = get_type_entry(doc["documentType"])
        if authenticity.applies_to(entry):
            targets.append((doc, entry))
    if not targets:
        return

    source = authenticity.source_for(file_bytes, reader, is_image, raw_bytes=original_bytes)

    def _one(target) -> None:
        doc, entry = target
        try:
            authenticity.run(doc, entry, source, context)
        except Exception:  # belt and braces: `run` already guards itself
            logger.exception("authenticity stage crashed for %s", doc.get("documentType"))

    with ThreadPoolExecutor(max_workers=min(_EXTRACT_WORKERS, len(targets)) or 1) as pool:
        list(pool.map(_one, targets))


def _page_selection_summary(documents: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Whole-document roll-up of what page selection avoided, or None when it
    never filtered anything (no key rather than a row of zeroes).

    `billedInputTokens` is exact — Bedrock reported it. Everything prefixed
    `estimated` is derived from those billed tokens and the character counts
    of the pages that were dropped, because the cheaper call is the only one
    that was ever made; see `page_selector.Selection.savings`.
    """
    blocks = [d["pageSelection"] for d in documents if d.get("pageSelection")]
    if not blocks:
        return None

    savings = [b.get("savings") or {} for b in blocks]
    total = {
        "documentsFiltered": len(blocks),
        "pagesConsidered": sum(b.get("pagesConsidered", 0) for b in blocks),
        "pagesExtracted": sum(len(b.get("selected") or []) for b in blocks),
        "pagesSkipped": sum(len(b.get("skipped") or []) for b in blocks),
        # Characters and chunks are the two things the token estimates are
        # derived FROM, so they roll up alongside them: without them a stored
        # record can't be re-checked later, only trusted.
        "charsSent": sum(s.get("charsSent", 0) for s in savings),
        "charsSkipped": sum(s.get("charsSkipped", 0) for s in savings),
        "chunksSent": sum(s.get("chunksSent", 0) for s in savings),
        "estimatedChunksWithoutSelection": sum(
            s.get("estimatedChunksWithoutSelection", 0) for s in savings
        ),
    }
    billed_in = sum(s.get("billedInputTokens", 0) for s in savings)
    billed_out = sum(s.get("billedOutputTokens", 0) for s in savings)
    saved_in = sum(s.get("estimatedInputTokensSaved", 0) for s in savings)
    saved_out = sum(s.get("estimatedOutputTokensSaved", 0) for s in savings)
    total["billedInputTokens"] = billed_in
    total["billedOutputTokens"] = billed_out
    if saved_in:
        total["estimatedInputTokensSaved"] = saved_in
        total["estimatedInputTokensWithoutSelection"] = billed_in + saved_in
        total["estimatedInputSavingPct"] = round(saved_in / (billed_in + saved_in) * 100, 1)
    if saved_out:
        total["estimatedOutputTokensSaved"] = saved_out
        total["estimatedOutputTokensWithoutSelection"] = billed_out + saved_out
    return total


def _public_entities(entity_map: Dict[str, Any]) -> Dict[str, Any]:
    """Compact entity view (original extracted values only)."""
    return {k: v for k, v in entity_map.items() if v not in (None, "", [])}


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
        _mark_source(doc, decoded_text_field, _SOURCE_QR)

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
        _mark_source(doc, validity_field, _SOURCE_QR)

    if not result["fields"]:
        return

    recovered = doc.setdefault("recoveredFields", {})
    for name, value in result["fields"].items():
        if not _has_field(entry, name):
            continue
        entities[name] = value
        confidences[name] = confidence
        recovered[name] = value
        _mark_source(doc, name, _SOURCE_QR)


# Canonical name of each pedimento-number role, used when a catalog does not
# say otherwise. A type can rename any of them by declaring `pedimento_role`
# on the field itself (same field-centric convention as `qr_source` /
# `concept_aliases`): a catalog whose fields carry the ERP's own column names
# writes `"No_Pedimento": {..., "pedimento_role": ["numero", "normalizado"]}`
# and keeps every deterministic recovery below — which used to be wired to
# these literal names, so renaming a field silently turned it off.
# Labels for `extractedEntities[...]["source"]` on a value the pipeline
# recovered itself. Neither is model output, and the difference matters when
# reading a result: QR_RECOVERY is read off the printed code (deterministic,
# confidence 0.99); DERIVED is computed from values already established
# (concatenating the 4 components, or a regex over the page text).
_SOURCE_QR = "QR_RECOVERY"
# Defined in pipeline.derived (the config-driven derived-field evaluator uses
# the same label) so the two never drift apart.
_SOURCE_DERIVED = SOURCE_DERIVED


def _mark_source(doc: Dict[str, Any], field: str, label: str) -> None:
    """Record where a recovered field's value came from, so the output can
    report it instead of defaulting to MODEL_EXTRACTION."""
    doc.setdefault("_recovered_sources", {})[field] = label


_PEDIMENTO_ROLE_DEFAULTS = {
    "referencia": "PEDIMENTO_REFERENCE",
    "numero": "NUMERO_PEDIMENTO",
    "normalizado": "NUMERO_PEDIMENTO_NORMALIZADO",
    "anio": "ANIO_PEDIMENTO",
    "aduana": "ADUANA_DESPACHO",
    "patente": "PATENTE",
    "consecutivo": "NUMERO_CONSECUTIVO",
}


def _pedimento_field_names(entry) -> Dict[str, str]:
    """Role -> field name for this type. Defaults to the canonical names, so
    every existing catalog behaves exactly as before; a field declaring
    `pedimento_role` (a role string, or a list when one column plays several
    roles — the ERP keeps a single 15-digit column) overrides its role."""
    names = dict(_PEDIMENTO_ROLE_DEFAULTS)
    for field_name, spec in (getattr(entry, "field_tree", None) or {}).items():
        declared = (getattr(spec, "params", {}) or {}).get("pedimento_role")
        if not declared:
            continue
        for role in (declared if isinstance(declared, list) else [declared]):
            role = str(role).strip().lower()
            if role in names:
                names[role] = field_name
            else:
                logger.warning("unknown pedimento_role %r on field %s", role, field_name)
    return names


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
    names = _pedimento_field_names(entry)
    f_ref, f_num = names["referencia"], names["numero"]
    f_norm = names["normalizado"]

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
    if _has_field(entry, f_ref) and entities.get(f_ref) in (None, "", []):
        match = find_pedimento_reference(text)
        if match:
            entities[f_ref] = match
            doc.setdefault("_confidences", {})[f_ref] = 0.95
            recovered[f_ref] = match
            _mark_source(doc, f_ref, _SOURCE_DERIVED)

    if _has_field(entry, f_num) and not digits_exact(entities.get(f_num), 15):
        match = find_pedimento_reference(text)
        digits = digits_exact(match, 15) if match else None
        if digits:
            entities[f_num] = digits
            doc.setdefault("_confidences", {})[f_num] = 0.95
            recovered[f_num] = digits
            _mark_source(doc, f_num, _SOURCE_DERIVED)

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
            (names["anio"], anio_c, 2),
            (names["aduana"], aduana_c, 2),
            (names["patente"], patente_c, 4),
            (names["consecutivo"], consecutivo_c, 7),
        ):
            if _has_field(entry, name) and not digits_exact(entities.get(name), expected_len):
                entities[name] = comp_value
                doc.setdefault("_confidences", {})[name] = 0.9
                recovered[name] = comp_value
                _mark_source(doc, name, _SOURCE_DERIVED)

    # ANIO_PEDIMENTO must be 2 digits; models sometimes expand it to 4
    # ("2026" instead of "26") despite the catalog hint saying otherwise —
    # collapsing it loses no information (same digits) so it's safe to fix
    # deterministically rather than leave it failing its own regex rule.
    if _has_field(entry, names["anio"]):
        anio_raw = entities.get(names["anio"])
        fixed_anio = normalize_year_2digit(anio_raw) if anio_raw not in (None, "", []) else None
        if fixed_anio and fixed_anio != str(anio_raw).strip():
            entities[names["anio"]] = fixed_anio
            recovered[names["anio"]] = fixed_anio
            _mark_source(doc, names["anio"],
                         doc.get("_recovered_sources", {}).get(names["anio"], _SOURCE_DERIVED))

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
    if _has_field(entry, f_norm):
        anio = digits_exact(entities.get(names["anio"]), 2)
        aduana = digits_exact(entities.get(names["aduana"]), 2)
        patente = digits_exact(entities.get(names["patente"]), 4)
        consecutivo = digits_exact(entities.get(names["consecutivo"]), 7)
        computed = f"{anio}{aduana}{patente}{consecutivo}" if all((anio, aduana, patente, consecutivo)) else None

        current = entities.get(f_norm)
        if computed:
            # Concatenating four QR-proven components yields a QR-grade value;
            # anything less certain came from the page text and stays DERIVED.
            from_qr = all(
                doc.get("_recovered_sources", {}).get(names[r]) == _SOURCE_QR
                for r in ("anio", "aduana", "patente", "consecutivo")
            )
            computed_confidence = doc.get("_confidences", {}).get(names["consecutivo"], 0.9)
            if computed != current:
                entities[f_norm] = computed
                recovered[f_norm] = computed
            # The confidence and the source are refreshed even when the value
            # was ALREADY right: an earlier, weaker pass (the regex over the
            # page text, 0.95/DERIVED) may have written the very same digits
            # first, and reporting that instead of the QR's 0.99 understates
            # evidence the pipeline actually has. Never lowers an existing
            # number — only the better one wins.
            if computed_confidence >= doc.get("_confidences", {}).get(f_norm, 0.0):
                doc.setdefault("_confidences", {})[f_norm] = computed_confidence
                _mark_source(doc, f_norm, _SOURCE_QR if from_qr else _SOURCE_DERIVED)
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
            source = entities.get(f_num) or entities.get(f_ref)
            digits = normalize_pedimento(source) if source else None
            if digits and len(digits) == 15:
                entities[f_norm] = digits
                doc.setdefault("_confidences", {})[f_norm] = (
                    doc.get("_confidences", {}).get(f_num, 0.9)
                )
                recovered[f_norm] = digits
                _mark_source(doc, f_norm, _SOURCE_DERIVED)

    # NUMERO_PEDIMENTO_NORMALIZADO at >=0.99 confidence means it's QR-sourced
    # (see qr_recovery's confidence, typically 0.99) — fully trusted, not a
    # model guess. Copy it onto NUMERO_PEDIMENTO too: the model sometimes
    # leaves that field holding only a fragment of what's printed (e.g. just
    # the 7-digit consecutivo "6001630" instead of the full 15-digit
    # reference), even when NORMALIZADO itself is the complete, correct
    # value — no reason to trust the model's own NUMERO_PEDIMENTO over data
    # already confirmed by the QR.
    if _has_field(entry, f_num) and f_num != f_norm:
        normalizado_confidence = doc.get("_confidences", {}).get(f_norm)
        normalizado_value = digits_exact(entities.get(f_norm), 15)
        if (
            normalizado_confidence is not None
            and normalizado_confidence >= 0.99
            and normalizado_value
            and entities.get(f_num) != normalizado_value
        ):
            entities[f_num] = normalizado_value
            doc.setdefault("_confidences", {})[f_num] = normalizado_confidence
            recovered[f_num] = normalizado_value
            _mark_source(doc, f_num, doc.get("_recovered_sources", {}).get(f_norm, _SOURCE_DERIVED))

    if recovered:
        # Merge, don't replace: `_recover_from_qr` ran first and already
        # recorded what it read off the QR (patente, consecutivo, año,
        # aduana). Assigning here dropped those from the audit trail, leaving
        # a result that showed only the fields THIS pass touched.
        doc.setdefault("recoveredFields", {}).update(recovered)


# ------------------------------------------------------- cross-document rules


def _pedimento_ref_value(entities: Dict[str, Any]) -> Optional[str]:
    """The pedimento reference carried by an already-processed document.

    Tries the canonical field names first, then any field whose NAME reads as
    a pedimento number (`looks_like_pedimento_field`) — a type that renamed
    the column via `pedimento_role` (e.g. the ERP's `No_Pedimento`) has no
    canonical name left to match, and would otherwise drop out of
    same_pedimento silently.
    """
    ref = _first_value(entities, _PEDIMENTO_REF_FIELDS)
    if ref:
        return ref
    for name, value in (entities or {}).items():
        if looks_like_pedimento_field(name) and value not in (None, "", []):
            return str(value)
    return None


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
        ref = _pedimento_ref_value(entities)
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
    # Respeta `documents` como el resto de las reglas cruzadas. Sin este
    # filtro corría sobre lo que hubiera en el archivo: dos facturas
    # extranjeras del mismo proveedor daban "importer RFC consistent across
    # documents" — comparar un dato contra sí mismo y reportar que coincide.
    involved = _docs_of(documents, rule["documents"]) if rule.get("documents") else documents
    if len(involved) < 2 and not expected:
        # Sin expectativa configurada, esta regla solo puede medir
        # CONSISTENCIA, y la consistencia de un solo documento no existe.
        return {"rule": "expected_importer_rfc", "valid": True, "skipped": True, "severity": "warning",
                "message": "fewer than two documents carry an importer RFC; consistency check skipped",
                "details": {}}
    found: Dict[str, List[str]] = {}
    weak: Dict[str, List[str]] = {}
    for d in involved:
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
