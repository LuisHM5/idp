import json
import os
import boto3
from urllib.parse import unquote_plus
import re
from aws_lambda_powertools import Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit

def _resolve_endpoint_url(*service_env_keys):
    for key in service_env_keys:
        endpoint = os.getenv(key)
        if endpoint:
            return endpoint
    return None


def _create_boto3_client(service_name, *service_env_keys):
    endpoint_url = _resolve_endpoint_url(*service_env_keys, 'AWS_ENDPOINT_URL')
    if endpoint_url:
        return boto3.client(service_name, endpoint_url=endpoint_url)
    return boto3.client(service_name)


sfn_client = _create_boto3_client('stepfunctions', 'AWS_ENDPOINT_URL_STEPFUNCTIONS')
s3_client = _create_boto3_client('s3', 'AWS_ENDPOINT_URL_S3')
ddb_client = _create_boto3_client('dynamodb', 'AWS_ENDPOINT_URL_DYNAMODB')
metrics = Metrics()
tracer = Tracer()


def _mark_queued(document_id, event_time):
    """Best-effort public lifecycle update; duplicate deliveries never regress it."""
    table_name = os.getenv('DOCUMENT_TABLE')
    if not table_name:
        return {}
    try:
        response = ddb_client.update_item(
            TableName=table_name,
            Key={'DocumentId': {'S': document_id}},
            UpdateExpression='SET #status = :queued, ProcessingStatusUpdatedAt = :updated',
            ConditionExpression=(
                'attribute_not_exists(#status) OR '
                '#status IN (:awaiting, :queued)'
            ),
            ExpressionAttributeNames={'#status': 'ProcessingStatus'},
            ExpressionAttributeValues={
                ':awaiting': {'S': 'AWAITING_UPLOAD'},
                ':queued': {'S': 'QUEUED'},
                ':updated': {'S': event_time},
            },
            ReturnValues='ALL_NEW',
        )
        return response.get('Attributes') or {}
    except ddb_client.exceptions.ConditionalCheckFailedException:
        # A duplicate S3/SQS delivery arrived after processing advanced.
        return {}
    except Exception as error:
        # Status reporting must never prevent the real workflow from starting.
        print(f'Could not mark {document_id} as QUEUED: {error}')
        return {}

@metrics.log_metrics
@tracer.capture_lambda_handler
def handler(event, context):
    tracer.put_annotation(key="invoke_type", value="sqsconsumer")
    metrics.add_dimension(name="invoke_type", value="sqsconsumer")
    print(f'SQS Consumer: Received event: {json.dumps(event, indent=2)}')

    results = []
    raw_prefix = os.getenv('RAW_PREFIX', 'raw/')

    for record in event['Records']:
        try:
            print(f'Processing SQS record: {record["messageId"]}')

            # Parse S3 event from SQS message body
            s3_event = json.loads(record['body'])
            print(f'Parsed S3 event: {json.dumps(s3_event, indent=2)}')

            # Skip S3 test events
            if "Event" in s3_event and s3_event["Event"] == 's3:TestEvent':
                print(f'Skipping S3 test event: {s3_event["Event"]}')
                continue

            if "Records" in s3_event:
                # Process each S3 record in the event
                for s3_record in s3_event['Records']:
                    event_name = s3_record['eventName']

                    bucket = s3_record['s3']['bucket']['name']
                    key = unquote_plus(s3_record['s3']['object']['key'])

                    # Skip folder creation events (keys ending with '/')
                    if key.endswith('/'):
                        print(f'Skipping folder creation event: s3://{bucket}/{key}')
                        continue

                    event_time = s3_record['eventTime']

                    print(f'Processing S3 object: s3://{bucket}/{key}')

                    # Everything upload-lambda attached to the object as S3
                    # metadata, read back in ONE HeadObject:
                    #
                    #   - `app-origin` — which app uploaded this, resolved from
                    #     the caller's x-api-key (see upload-lambda/index.mjs's
                    #     resolveApp);
                    #   - every other key — the caller's own `metadata` from
                    #     `POST /documents` (normalizeMetadata already lowercased
                    #     it and stripped the two reserved keys below), carried
                    #     onto the result envelope as `metadata` so
                    #     `GET /documents?meta.<key>=<value>` can filter on it.
                    #
                    # Read here, not in the enrichment Lambda, so both reach
                    # EVERY document regardless of pipeline branch: the
                    # enrichment Lambda only re-fetches the raw object (and its
                    # metadata) for the expediente flow or when re-extraction
                    # triggers, not on every standard-flow invocation.
                    # Best-effort and isolated from the record's own
                    # try/except below — a failure here (permissions,
                    # throttling, object already moved) must never trigger an
                    # SQS retry/DLQ, it just costs traceability on one document.
                    app_origin = None
                    upload_metadata = {}
                    try:
                        head = s3_client.head_object(Bucket=bucket, Key=key)
                        object_metadata = head.get('Metadata') or {}
                        app_origin = object_metadata.get('app-origin')
                        # `model-id` is an execution knob, not document context.
                        upload_metadata = {
                            k: v for k, v in object_metadata.items()
                            if k not in ('app-origin', 'model-id') and v not in (None, '')
                        }
                    except Exception as e:
                        print(f'Could not read upload metadata for s3://{bucket}/{key}: {e}')

                    # Manual classification (upload-lambda's `documentType` param)
                    # stores the object as raw/{TYPE}/{uuid}/{file} instead of the
                    # auto-classified raw/{uuid}/{file} — one extra path segment.
                    # The uuid segment immediately before the filename is always
                    # the id the Upload API returned to the caller, regardless of
                    # that optional TYPE folder, so anchor documentId on THAT
                    # segment ALONE — not on uuid+filename, and not uuid+timestamp
                    # either (a former version of this file appended a timestamp
                    # "for uniqueness", but the uuid the Upload API already handed
                    # back is unique per upload on its own — the timestamp only
                    # ever made documentId diverge from the id callers actually
                    # have, forcing GET /documents/{id} onto a full table Scan
                    # instead of a direct GetItem; see query-lambda/index.mjs's
                    # getById, which already tries an exact GetItem first).
                    # Without anchoring on the uuid AT ALL, a manually classified
                    # upload got a documentId prefixed by TYPE instead, and
                    # GET /documents/{id} could never find it even though the
                    # workflow completed and wrote EnrichmentResult.
                    key_no_prefix = key.replace(raw_prefix, '', 1)  # Remove raw/ prefix
                    key_parts = key_no_prefix.split('/')
                    uuid_segment = key_parts[-2] if len(key_parts) >= 2 else key_no_prefix.rsplit('.', 1)[0]
                    filename = key_parts[-1]  # bare original filename, e.g. "bad-pedimento.pdf"

                    document_id = re.sub(r'[^a-zA-Z0-9-]', '-', uuid_segment)
                    queued_item = _mark_queued(document_id, event_time)
                    requested_document_type = (
                        key_parts[-3] if len(key_parts) >= 3 else 'AUTO'
                    )
                    job_owner = (
                        app_origin
                        or (queued_item.get('JobOwner') or {}).get('S')
                        or 'dev-unrestricted'
                    )
                    registered_at = (
                        (queued_item.get('RegisteredAt') or {}).get('S')
                        or event_time
                    )
                    upload_expires_at = (
                        (queued_item.get('UploadExpiresAt') or {}).get('S')
                        or event_time
                    )

                    # Explicit file type alongside location/bucket/key/filename,
                    # so downstream consumers (enrichment Lambda, model-invoke
                    # Lambda) don't each independently re-derive it by parsing
                    # the key's extension. "" when the filename has none.
                    file_type = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

                    # Prepare Step Functions execution input
                    step_function_input = {
                        'documentId': document_id,
                        'contentType': 'file',
                        'content': {
                            'location': 's3',
                            'bucket': bucket,
                            'key': key,
                            'filename': filename,
                            'fileType': file_type
                        },
                        'eventTime': event_time,
                        'eventName': event_name,
                        'source': 'sqs-consumer',
                        'jobOwner': job_owner,
                        'requestedDocumentType': requested_document_type,
                        'registeredAt': registered_at,
                        'uploadExpiresAt': upload_expires_at,
                        **({'appOrigin': app_origin} if app_origin else {}),
                        **({'uploadMetadata': upload_metadata} if upload_metadata else {})
                    }

                    print(f'Starting Step Functions execution with input: {json.dumps(step_function_input, indent=2)}')

                    # Start Step Functions execution. Deterministic documentId
                    # means a genuinely duplicate S3/SQS delivery of the SAME
                    # object (SQS is at-least-once, not exactly-once — AWS
                    # documents this as rare but real) reuses this exact name:
                    # while the original execution is still running, Standard
                    # Step Functions' own StartExecution is idempotent for a
                    # matching name+input and just returns that execution
                    # (https://docs.aws.amazon.com/step-functions/latest/apireference/API_StartExecution.html) —
                    # no duplicate Bedrock/Textract billing. If the original
                    # already finished, the SAME call instead raises
                    # ExecutionAlreadyExists; caught below and treated as a
                    # no-op (not re-raised) so a late duplicate lands as a
                    # skip, not as five SQS retries ending in the DLQ.
                    execution_name = f'{document_id}-execution'[:80]  # AWS limit

                    try:
                        response = sfn_client.start_execution(
                            stateMachineArn=os.environ['STATE_MACHINE_ARN'],
                            input=json.dumps(step_function_input),
                            name=execution_name
                        )
                    except sfn_client.exceptions.ExecutionAlreadyExists:
                        metrics.add_metric(name="DuplicateExecutionSkipped", unit=MetricUnit.Count, value=1)
                        print(
                            f'Execution {execution_name} already exists for documentId '
                            f'{document_id} (duplicate S3/SQS delivery, or a manual re-upload '
                            f'to the same key) — already processed or in progress, skipping.'
                        )
                        results.append({
                            'documentId': document_id,
                            'executionArn': None,
                            's3Location': f's3://{bucket}/{key}',
                            'duplicate': True,
                        })
                        continue

                    print(f'Step Functions execution started: {response["executionArn"]}')

                    results.append({
                        'documentId': document_id,
                        'executionArn': response['executionArn'],
                        's3Location': f's3://{bucket}/{key}'
                    })
            else:
                print(f"Skipping")

        except Exception as error:
            metrics.add_metric(name="FailedToProcessed", unit=MetricUnit.Count, value=1)
            print(f'Error processing SQS record: {record["messageId"]} {str(error)}')

            # Re-raise error to trigger SQS retry mechanism
            # After max retries, message will go to Dead Letter Queue
            raise Exception(f'Failed to process SQS record {record["messageId"]}: {str(error)}')

    metrics.add_metric(name="SuccessfullyProcessed", unit=MetricUnit.Count, value=len(results))
    print(f'Successfully processed {len(results)} documents: {results}')

    return {
        'statusCode': 200,
        'processedCount': len(results),
        'results': results
    }
