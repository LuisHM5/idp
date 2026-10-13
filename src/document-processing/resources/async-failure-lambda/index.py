"""Finalize direct S3 → Lambda invocations that exhausted async retries."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable
from urllib.parse import unquote_plus


def _records(event: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    payload = event.get("requestPayload") or event
    if isinstance(payload, dict):
        yield from payload.get("Records") or []


def _document_id(record: Dict[str, Any]) -> str | None:
    try:
        key = unquote_plus(record["s3"]["object"]["key"])
    except (KeyError, TypeError):
        return None
    # Same layout and id normalization as the enrichment handler's S3 adapter,
    # so both always address the same DynamoDB row.
    parts = [part for part in key.split("/") if part]
    if key.endswith("/") or len(parts) < 3 or parts[0] not in {"source", "raw"}:
        return None
    return re.sub(r"[^a-zA-Z0-9-]", "-", parts[-2])


def _reason(event: Dict[str, Any]) -> str:
    """Why Lambda gave up: the function's own error when it reported one,
    otherwise the delivery condition (RetriesExhausted, EventAgeExceeded)."""
    payload = event.get("responsePayload")
    message = payload.get("errorMessage") if isinstance(payload, dict) else None
    condition = (event.get("requestContext") or {}).get("condition")
    function_error = (event.get("responseContext") or {}).get("functionError")
    parts = [str(part) for part in (condition, function_error, message) if part]
    return (": ".join(parts) or "ASYNC_RETRIES_EXHAUSTED")[:300]


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    import boto3

    table = boto3.resource("dynamodb").Table(os.environ["DOCUMENT_TABLE"])
    events = boto3.client("events")
    now = datetime.now(timezone.utc).isoformat()
    finalized = []
    reason = _reason(event)

    for record in _records(event):
        document_id = _document_id(record)
        if not document_id:
            continue
        try:
            table.update_item(
                Key={"DocumentId": document_id},
                UpdateExpression=(
                    "SET #workflow = :workflow, ProcessingStatus = :status, "
                    "ProcessingStatusUpdatedAt = :updated, ProcessingCompletedAt = :updated, "
                    "ProcessingError = :reason REMOVE ProcessingLeaseUntil"
                ),
                # A delayed duplicate/failure destination must never turn an
                # already-complete document back into FAILED.
                ConditionExpression=(
                    "attribute_not_exists(#workflow) OR #workflow <> :complete"
                ),
                ExpressionAttributeNames={"#workflow": "WorkflowStatus"},
                ExpressionAttributeValues={
                    ":workflow": "enrichment-failure",
                    ":complete": "complete",
                    ":status": "FAILED",
                    ":updated": now,
                    ":reason": reason,
                },
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                continue
            raise
        events.put_events(
            Entries=[
                {
                    "EventBusName": os.environ["EVENT_BUS_NAME"],
                    "Source": os.environ.get(
                        "EVENT_SOURCE", "document-validation-pipeline"
                    ),
                    "DetailType": "document-processing-failed",
                    "Detail": json.dumps(
                        {"documentId": document_id, "reason": reason}
                    ),
                }
            ]
        )
        finalized.append(document_id)

    return {"finalized": finalized}
