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
    return {
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
        "processingStartedAt": started_at.isoformat(),
        "processingCompletedAt": now.isoformat(),
        "processingSeconds": round(time.time() - started_monotonic, 2),
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
) -> Dict[str, Any]:
    tracking = tracking or build_tracking(datetime.now(timezone.utc), time.time(), None)
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
