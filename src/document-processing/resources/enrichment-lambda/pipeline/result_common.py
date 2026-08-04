"""Result-envelope helpers shared by every document pipeline (PDF expediente,
XML), so tracking/provenance and error shapes stay identical regardless of
which extractor produced the document. Nothing here is PDF- or XML-specific.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from document_catalog import get_schema_info, get_type_entry
from validators import engine
from validators.engine import RuleCheck

from .bedrock import base_model_id, used_models

# Bump when extraction/validation logic changes in a way consumers should
# notice (recorded in every result's tracking block).
PIPELINE_VERSION = "1.0.0"


def build_tracking(
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
    # Single elapsed-time sample shared by both fields: two separate
    # time.time() calls (one per field) would drift apart by however long
    # the dict literal below takes to build, and `processingSeconds` rounded
    # to 2 decimals reads as a flat 0.0 for anything under ~5ms (the XML
    # pipeline routinely is) — `processingMs` keeps millisecond precision for
    # exactly that case, without dropping the existing seconds field anyone
    # already reads.
    elapsed = time.time() - started_monotonic
    return {
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
        "processingStartedAt": started_at.isoformat(),
        "processingCompletedAt": now.isoformat(),
        "processingSeconds": round(elapsed, 2),
        "processingMs": round(elapsed * 1000, 2),
        "schema": schema,
    }


def checks_to_json(checks: List[RuleCheck]) -> List[Dict[str, Any]]:
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


def error_result(
    processing_id: str,
    source_name: str,
    message: str,
    tracking: Optional[Dict[str, Any]] = None,
    *,
    stage: Optional[str] = None,
    exception: Optional[BaseException] = None,
    source_format: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the whole-document failure envelope (no document could even be
    produced — contrast with a per-document `error`/`status: PROCESSING_ERROR`
    inside `documents[]`, which `expediente.py::_extract_segment` sets when
    only ONE segment of a multi-document PDF fails).

    `warnings: [message]` is kept as the free-text summary every existing
    consumer already reads; `stage`/`exception`/`source_format` — all
    optional, so old call sites keep working unchanged — additionally build
    a structured `error` block so a caller can triage by `error.type`/
    `error.stage` instead of parsing prose (e.g. distinguishing "the file
    itself couldn't be read" from "extraction/validation blew up" without
    string-matching the message).
    """
    tracking = tracking or build_tracking(datetime.now(timezone.utc), time.time(), None)
    error_detail: Dict[str, Any] = {"message": message}
    if stage:
        error_detail["stage"] = stage
    if exception is not None:
        error_detail["type"] = type(exception).__name__
    if source_format:
        error_detail["sourceFormat"] = source_format
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
        **({"sourceFormat": source_format} if source_format else {}),
        "status": engine.DOC_ERROR,
        "reviewRequired": True,
        "documents": [],
        "crossDocumentChecks": [],
        "warnings": [message],
        "error": error_detail,
    }
