"""S3/SQS ingress owned by this project.

The handler deliberately has no third-party runtime dependency: boto3 ships in
Lambda.  One S3 notification becomes one idempotently named Standard Step
Functions execution, preserving the public document id registered by the API.
"""

from datetime import datetime, timezone
import json
import os
import re
from urllib.parse import unquote_plus

import boto3

from routing import route_from_catalog_item


def _client(service: str):
    endpoint = os.getenv(f"AWS_ENDPOINT_URL_{service.upper()}") or os.getenv("AWS_ENDPOINT_URL")
    return boto3.client(service, endpoint_url=endpoint) if endpoint else boto3.client(service)


sfn = _client("stepfunctions")
s3 = _client("s3")
ddb = _client("dynamodb")


def _emit_metrics(successes: int, failures: int, duplicates: int):
    """Emit the former Powertools metrics using native CloudWatch EMF."""
    namespace = os.getenv("POWERTOOLS_METRICS_NAMESPACE")
    if not namespace:
        return
    print(json.dumps({
        "_aws": {
            "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": namespace,
                "Dimensions": [["invoke_type", "service"]],
                "Metrics": [
                    {"Name": "SuccessfullyProcessed", "Unit": "Count"},
                    {"Name": "FailedToProcessed", "Unit": "Count"},
                    {"Name": "DuplicateExecutionSkipped", "Unit": "Count"},
                ],
            }],
        },
        "invoke_type": "sqsconsumer",
        "service": os.getenv("POWERTOOLS_SERVICE_NAME", "extraction-workflow"),
        "SuccessfullyProcessed": successes,
        "FailedToProcessed": failures,
        "DuplicateExecutionSkipped": duplicates,
    }))


def _mark_queued(document_id: str, event_time: str):
    table = os.getenv("DOCUMENT_TABLE")
    if not table:
        return {}
    try:
        response = ddb.update_item(
            TableName=table,
            Key={"DocumentId": {"S": document_id}},
            UpdateExpression=(
                "SET #status = :queued, ProcessingStatusUpdatedAt = :updated, "
                "UploadedAt = if_not_exists(UploadedAt, :updated)"
            ),
            ConditionExpression=(
                "attribute_not_exists(#status) OR #status IN (:awaiting, :queued)"
            ),
            ExpressionAttributeNames={"#status": "ProcessingStatus"},
            ExpressionAttributeValues={
                ":awaiting": {"S": "AWAITING_UPLOAD"},
                ":queued": {"S": "QUEUED"},
                ":updated": {"S": event_time},
            },
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes") or {}
    except ddb.exceptions.ConditionalCheckFailedException:
        return {}
    except Exception as error:  # lifecycle reporting must not block processing
        print(f"Could not mark {document_id} as QUEUED: {error}")
        return {}


def _pipeline_route(requested_type: str) -> str:
    """Select the shortcut only when the live catalog proves it is safe."""
    table = os.getenv("CATALOG_TABLE")
    if not table or not requested_type or requested_type == "AUTO" or "," in requested_type:
        return "standard"
    try:
        item = ddb.get_item(
            TableName=table,
            Key={"type_id": {"S": requested_type}},
            ProjectionExpression="#enabled, #fields, #pipeline",
            ExpressionAttributeNames={
                "#enabled": "enabled",
                "#fields": "fields",
                "#pipeline": "pipeline",
            },
            ConsistentRead=True,
        ).get("Item") or {}
        return route_from_catalog_item(item)
    except Exception as error:
        print(f"Could not resolve catalog route for {requested_type}: {error}")
    return "standard"


def _metadata(bucket: str, key: str):
    try:
        values = (s3.head_object(Bucket=bucket, Key=key).get("Metadata") or {})
        app_origin = values.get("app-origin")
        public = {
            k: v
            for k, v in values.items()
            if k not in ("app-origin", "model-id", "extraction-mode", "ai-response-format")
            and v not in (None, "")
        }
        return app_origin, public
    except Exception as error:
        print(f"Could not read upload metadata for s3://{bucket}/{key}: {error}")
        return None, {}


def _process_s3_record(record: dict, consumer_started_at: str):
    bucket = record["s3"]["bucket"]["name"]
    key = unquote_plus(record["s3"]["object"]["key"])
    if key.endswith("/"):
        return None

    raw_prefix = os.getenv("RAW_PREFIX", "raw/")
    relative = key[len(raw_prefix):] if key.startswith(raw_prefix) else key
    parts = relative.split("/")
    uuid_segment = parts[-2] if len(parts) >= 2 else relative.rsplit(".", 1)[0]
    filename = parts[-1]
    document_id = re.sub(r"[^a-zA-Z0-9-]", "-", uuid_segment)
    requested_type = parts[-3] if len(parts) >= 3 else "AUTO"
    event_time = record["eventTime"]

    queued = _mark_queued(document_id, event_time)
    app_origin, upload_metadata = _metadata(bucket, key)
    job_owner = app_origin or (queued.get("JobOwner") or {}).get("S") or "dev-unrestricted"
    registered_at = (queued.get("RegisteredAt") or {}).get("S") or event_time
    upload_expires_at = (queued.get("UploadExpiresAt") or {}).get("S") or event_time
    file_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    workflow_requested_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "documentId": document_id,
        "contentType": "file",
        "content": {
            "location": "s3",
            "bucket": bucket,
            "key": key,
            "filename": filename,
            "fileType": file_type,
        },
        "eventTime": event_time,
        "eventName": record["eventName"],
        "source": "sqs-consumer",
        "jobOwner": job_owner,
        "requestedDocumentType": requested_type,
        "pipelineRoute": _pipeline_route(requested_type),
        "consumerStartedAt": consumer_started_at,
        "workflowRequestedAt": workflow_requested_at,
        "registeredAt": registered_at,
        "uploadExpiresAt": upload_expires_at,
        **({"appOrigin": app_origin} if app_origin else {}),
        **({"uploadMetadata": upload_metadata} if upload_metadata else {}),
    }

    name = f"{document_id}-execution"[:80]
    try:
        response = sfn.start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            input=json.dumps(payload),
            name=name,
        )
        execution_arn = response["executionArn"]
    except sfn.exceptions.ExecutionAlreadyExists:
        print(f"Execution {name} already exists; duplicate delivery skipped")
        execution_arn = None

    return {
        "documentId": document_id,
        "executionArn": execution_arn,
        "s3Location": f"s3://{bucket}/{key}",
        **({"duplicate": True} if execution_arn is None else {}),
    }


def handler(event, _context):
    started = datetime.now(timezone.utc).isoformat()
    results = []
    failures = []
    for sqs_record in event.get("Records", []):
        try:
            body = json.loads(sqs_record["body"])
            if body.get("Event") == "s3:TestEvent":
                continue
            for s3_record in body.get("Records", []):
                result = _process_s3_record(s3_record, started)
                if result:
                    results.append(result)
        except Exception as error:
            print(f"Failed SQS record {sqs_record.get('messageId')}: {error}")
            # Partial batch response prevents successful messages in the same
            # batch from being retried and potentially starting duplicate jobs.
            failures.append({"itemIdentifier": sqs_record["messageId"]})

    duplicates = sum(1 for result in results if result.get("duplicate"))
    _emit_metrics(len(results), len(failures), duplicates)
    print(json.dumps({"processed": results, "failures": failures}))
    return {"batchItemFailures": failures}
