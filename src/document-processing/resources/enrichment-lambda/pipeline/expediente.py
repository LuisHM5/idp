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

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from document_catalog import (
    get_cross_document_rules,
    get_schema_info,
    get_type_entry,
)
from validators import engine
from validators.engine import RuleCheck, ValidationContext

from .liquidacion import build_liquidacion_normalizada
from .normalize import (
    find_pedimento_reference,
    normalize_pedimento,
    normalize_uuid,
    parse_amount,
    pedimento_consecutivo,
)
from .pdf_pages import PageText, load_pages, pages_summary
from .segmenter import Segment, segment_pages
from .text_extractor import extract_document

logger = logging.getLogger(__name__)

# Bump when extraction/validation logic changes in a way consumers should
# notice (recorded in every result's tracking block).
PIPELINE_VERSION = "1.0.0"


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
    schema: Dict[str, Any] = {
        "pipelineVersion": PIPELINE_VERSION,
        "model": model_id or "default",
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

    try:
        pages = load_pages(pdf_bytes)
    except Exception as e:
        logger.error("failed to read PDF: %s", e)
        return _fail(f"unreadable PDF: {e}")

    if not pages:
        return _fail("PDF has no pages")

    segments = segment_pages(pages, declared_type=declared_type, model_id=model_id)

    # ---- extraction pass (all documents first, so cross-doc rules see siblings)
    documents: List[Dict[str, Any]] = []
    for seg in segments:
        seg_pages = [p for p in pages if seg.page_start <= p.number <= seg.page_end]
        entry = get_type_entry(seg.doc_type)
        doc: Dict[str, Any] = {
            "documentType": seg.doc_type,
            "documentSubtype": seg.subtype,
            "pageStart": seg.page_start,
            "pageEnd": seg.page_end,
            "classificationConfidence": round(seg.confidence, 4),
        }
        if seg.doc_type == "OTRO" or not entry.extraction_fields:
            doc["status"] = engine.DOC_REVIEW if seg.doc_type == "OTRO" else engine.DOC_APPROVED
            doc["fields"] = {}
            doc["_entities"] = {}
            doc["_text"] = "\n".join(p.text for p in seg_pages)
            documents.append(doc)
            continue
        try:
            extracted = extract_document(seg_pages, entry, model_id=model_id)
            doc["_entities"] = extracted.entity_map
            doc["_confidences"] = extracted.confidences
            doc["_text"] = "\n".join(p.text for p in seg_pages)
            doc["extractionMeta"] = extracted.meta
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
        documents.append(doc)

    # ---- validation pass (with sibling context)
    siblings = [
        {"type": d["documentType"], "entities": d.get("_entities", {})}
        for d in documents
    ]
    for doc in documents:
        if doc.get("status") in (engine.DOC_ERROR,):
            continue
        entry = get_type_entry(doc["documentType"])
        if not entry.extraction_fields:
            continue
        ctx = ValidationContext(
            expected_values=expected_values,
            sibling_documents=[s for s in siblings if s["entities"] is not doc.get("_entities")],
            document_text=doc.get("_text", ""),
        )
        entity_map = doc.get("_entities", {})
        confidences = doc.get("_confidences", {})
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
        doc["extractedEntities"] = _public_entities(entities)

    statuses = [d.get("status") for d in documents if d.get("documentType") != "OTRO"]
    overall = _overall_status(statuses, cross_checks)

    tracking = _tracking(
        started_at, started, model_id,
        doc_types=[d["documentType"] for d in documents],
    )
    return {
        "processingId": processing_id,
        "sourceFile": source_name,
        "processedAt": tracking["processingCompletedAt"],
        "processingSeconds": tracking["processingSeconds"],
        "tracking": tracking,
        "pages": pages_summary(pages),
        "status": overall,
        "reviewRequired": overall in (engine.DOC_REVIEW, engine.DOC_PARTIAL),
        "documents": documents,
        "crossDocumentChecks": cross_checks,
        "warnings": [c["message"] for c in cross_checks if not c["valid"]],
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

    for name in ("PEDIMENTO_REFERENCE", "NUMERO_PEDIMENTO"):
        if name in entry.extraction_fields and entities.get(name) in (None, "", []):
            match = find_pedimento_reference(text)
            if match:
                entities[name] = match
                doc.setdefault("_confidences", {})[name] = 0.95
                recovered[name] = match

    # Derived, purely mechanical: the normalized 15-digit form
    if "NUMERO_PEDIMENTO_NORMALIZADO" in entry.extraction_fields and entities.get(
        "NUMERO_PEDIMENTO_NORMALIZADO"
    ) in (None, "", []):
        source = entities.get("NUMERO_PEDIMENTO") or entities.get("PEDIMENTO_REFERENCE")
        digits = normalize_pedimento(source) if source else None
        if digits and len(digits) == 15:
            entities["NUMERO_PEDIMENTO_NORMALIZADO"] = digits
            doc.setdefault("_confidences", {})["NUMERO_PEDIMENTO_NORMALIZADO"] = (
                doc.get("_confidences", {}).get("NUMERO_PEDIMENTO", 0.9)
            )
            recovered["NUMERO_PEDIMENTO_NORMALIZADO"] = digits

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
        entities = d.get("extractedEntities") or d.get("_entities") or {}
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
        entities = d.get("extractedEntities") or d.get("_entities") or {}
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
        entities = d.get("extractedEntities") or d.get("_entities") or {}
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
            str((d.get("extractedEntities") or {}).get("TOTAL")) for d in docs
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
        "status": engine.DOC_ERROR,
        "reviewRequired": True,
        "documents": [],
        "crossDocumentChecks": [],
        "warnings": [message],
    }
