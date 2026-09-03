"""Project terminal EventBridge events onto the stable public job lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Dict


_STATUS_BY_DETAIL_TYPE = {
    "document-processed-successful": "FINISHED",
    "document-processing-failed": "FAILED",
}


def status_for_event(event: Dict[str, Any]) -> str | None:
    return _STATUS_BY_DETAIL_TYPE.get(event.get("detail-type"))


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    status = status_for_event(event)
    document_id = (event.get("detail") or {}).get("documentId")
    if not status or not document_id:
        return {"updated": False, "reason": "unsupported-event"}

    updated_at = event.get("time") or datetime.now(timezone.utc).isoformat()

    # Deferred so status_for_event stays AWS-free and unit-testable.
    import boto3

    boto3.resource("dynamodb").Table(os.environ["DOCUMENT_TABLE"]).update_item(
        Key={"DocumentId": document_id},
        UpdateExpression=(
            "SET ProcessingStatus = :status, "
            "ProcessingStatusUpdatedAt = :updated, ProcessingCompletedAt = :updated"
        ),
        ExpressionAttributeValues={":status": status, ":updated": updated_at},
        ConditionExpression="attribute_exists(DocumentId)",
    )
    return {"updated": True, "documentId": document_id, "processingStatus": status}

