import json
import os
import boto3
from urllib.parse import unquote_plus
import time
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
metrics = Metrics()
tracer = Tracer()

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

                    # Generate unique document ID from S3 key and timestamp
                    timestamp = int(time.time() * 1000)

                    # Manual classification (upload-lambda's `documentType` param)
                    # stores the object as raw/{TYPE}/{uuid}/{file} instead of the
                    # auto-classified raw/{uuid}/{file} — one extra path segment.
                    # The uuid segment immediately before the filename is always
                    # the id the Upload API returned to the caller, regardless of
                    # that optional TYPE folder, so anchor documentId on THAT
                    # segment ALONE — not on uuid+filename. Without anchoring on
                    # the uuid, a manually classified upload got a documentId
                    # prefixed by TYPE instead of by the uuid, and GET /documents/{id}'s
                    # prefix lookup could never find it even though the workflow
                    # completed and wrote EnrichmentResult. Concatenating the
                    # filename on top of that (the previous shape here) was a
                    # separate, smaller wart: it made documentId noisy/harder to
                    # read for no benefit — uniqueness already comes from the uuid
                    # + timestamp, the filename adds nothing but clutter — and
                    # forced `filename` (used verbatim by the blueprint's own
                    # raw->processed/failed move, see queued-s3-adapter.js
                    # createSuccessChain/createFailedChain) into the same
                    # "uuid/original.ext" shape, so the archived object ended up
                    # double-nested as processed/{documentId}/{uuid}/original.ext
                    # instead of the expected processed/{documentId}/original.ext.
                    key_no_prefix = key.replace(raw_prefix, '', 1)  # Remove raw/ prefix
                    key_parts = key_no_prefix.split('/')
                    uuid_segment = key_parts[-2] if len(key_parts) >= 2 else key_no_prefix.rsplit('.', 1)[0]
                    filename = key_parts[-1]  # bare original filename, e.g. "bad-pedimento.pdf"

                    document_id = re.sub(r'[^a-zA-Z0-9-]', '-', uuid_segment) + '-' + str(timestamp)

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
                        'source': 'sqs-consumer'
                    }

                    print(f'Starting Step Functions execution with input: {json.dumps(step_function_input, indent=2)}')

                    # Start Step Functions execution
                    execution_name = f'{document_id}-execution'[:80]  # AWS limit

                    response = sfn_client.start_execution(
                        stateMachineArn=os.environ['STATE_MACHINE_ARN'],
                        input=json.dumps(step_function_input),
                        name=execution_name
                    )

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
