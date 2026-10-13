"""Finalize direct S3 → Lambda invocations that exhausted async retries."""

from __future__ import annotations

import json
import os
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
    parts = [part for part in key.split("/") if part]
    return parts[-2] if len(parts) >= 2 else None


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    import boto3

    table = boto3.resource("dynamodb").Table(os.environ["DOCUMENT_TABLE"])
    events = boto3.client("events")
    now = datetime.now(timezone.utc).isoformat()
    finalized = []

    for record in _records(event):
        document_id = _document_id(record)
        if not document_id:
            continue
        response_context = event.get("responseContext") or {}
        reason = str(
            response_context.get("functionError")
            or response_context.get("condition")
            or "ASYNC_RETRIES_EXHAUSTED"
        )[:300]
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
