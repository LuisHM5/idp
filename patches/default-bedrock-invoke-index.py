import json
import os
import re
import boto3
import base64
from aws_lambda_powertools import Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit

s3 = boto3.client('s3')
bedrock = boto3.client('bedrock-runtime')
metrics = Metrics()
tracer = Tracer()


def parse_chunk_metadata(event):
    if 'chunkMetadata' in event:
        return event['chunkMetadata']
    if 'chunk' in event:
        chunk = event['chunk']
        return {
            'chunkIndex': chunk.get('chunkIndex', event.get('chunkIndex', 0)),
            'totalChunks': event.get('totalChunks', 1),
            'startPage': chunk.get('startPage', 0),
            'endPage': chunk.get('endPage', 0),
            'pageCount': chunk.get('pageCount', 0),
            'estimatedTokens': chunk.get('estimatedTokens', 0),
            'overlapPages': chunk.get('overlapPages', 0),
        }
    return None


def build_chunk_context_prompt(chunk_metadata):
    if not chunk_metadata:
        return ""
    chunk_index = chunk_metadata.get('chunkIndex', 0)
    total_chunks = chunk_metadata.get('totalChunks', 1)
    start_page = chunk_metadata.get('startPage', 0)
    end_page = chunk_metadata.get('endPage', 0)
    overlap_pages = chunk_metadata.get('overlapPages', 0)
    context_parts = [
        f"You are analyzing chunk {chunk_index + 1} of {total_chunks} from pages {start_page + 1} to {end_page + 1}."
    ]
    if overlap_pages > 0 and chunk_index > 0:
        context_parts.append(
            f"Note: This chunk includes {overlap_pages} overlapping pages from the previous chunk for context."
        )
    return "\n".join(context_parts) + "\n\n"


def extract_json_from_response(response_body):
    """
    Robustly extract JSON from Bedrock response.
    Handles: plain text JSON, markdown-wrapped JSON, thinking blocks, empty text.
    """
    parsed = json.loads(response_body)
    content_blocks = parsed.get("content", [])

    # Find the text block (skip thinking blocks)
    text = ""
    for block in content_blocks:
        if block.get("type") == "text":
            text = block.get("text", "")
            break

    if not text or not text.strip():
        # If no text block found, try the first block regardless of type
        for block in content_blocks:
            if "text" in block:
                text = block["text"]
                break

    if not text or not text.strip():
        raise ValueError(f"Empty response from Bedrock. Content blocks: {json.dumps(content_blocks)[:500]}")

    # Strip markdown code fences if present
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove ```json or ``` at start and ``` at end
        stripped = re.sub(r'^```(?:json)?\s*\n?', '', stripped)
        stripped = re.sub(r'\n?```\s*$', '', stripped)

    # Try to parse as JSON
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', stripped)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Could not parse JSON from response: {stripped[:300]}")


@metrics.log_metrics
@tracer.capture_lambda_handler
def handler(event, context):
    invoke_type = os.environ["INVOKE_TYPE"]
    tracer.put_annotation(key="invoke_type", value=invoke_type)
    tracer.put_annotation(key="documentId", value=event["documentId"])
    metrics.add_dimension(name="invoke_type", value=invoke_type)
    content_type = event["contentType"]

    chunk_metadata = parse_chunk_metadata(event)
    if chunk_metadata:
        tracer.put_annotation(key="chunkIndex", value=str(chunk_metadata.get('chunkIndex', 0)))
        tracer.put_annotation(key="totalChunks", value=str(chunk_metadata.get('totalChunks', 1)))
        metrics.add_dimension(name="is_chunked", value="true")
    else:
        metrics.add_dimension(name="is_chunked", value="false")

    prompt = os.environ['PROMPT']
    if 'classificationResult' in event:
        classification = event['classificationResult']['documentClassification']
        prompt = prompt.replace("[ACTUAL_CLASSIFICATION]", classification)

    chunk_context = build_chunk_context_prompt(chunk_metadata)
    if chunk_context:
        prompt = chunk_context + prompt

    content = [{'type': 'text', 'text': prompt}]
    if content_type == 'file':
        content_location = event['content']['location']
        if content_location == 's3':
            if chunk_metadata and 'bucket' in chunk_metadata and 'key' in chunk_metadata:
                bucket = chunk_metadata['bucket']
                key = chunk_metadata['key']
            else:
                bucket = event['content']['bucket']
                key = event['content']['key']

            ext = key.lower().split('.')[-1]
            if ext not in ['jpg', 'jpeg', 'png', 'pdf']:
                raise ValueError(f"Unsupported file type: {ext}")

            media_type = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'pdf': 'application/pdf'}[ext]
            local_path = f"/tmp/{key.split('/')[-1]}"
            s3.download_file(bucket, key, local_path)

            with open(local_path, 'rb') as f:
                file_data = base64.b64encode(f.read()).decode('utf-8')

            if ext == 'pdf':
                content.append({'type': 'document', 'source': {'type': 'base64', 'media_type': media_type, 'data': file_data}})
            else:
                content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': file_data}})

    elif content_type == 'data':
        content.append({'type': 'text', 'text': event['content']['data']})

    # Invoke Bedrock
    max_tokens = int(os.getenv('INVOKE_MAX_TOKENS', '4096'))
    response = bedrock.invoke_model(
        modelId=os.environ['MODEL_ID'],
        body=json.dumps({
            'anthropic_version': 'bedrock-2023-05-31',
            'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': content}]
        })
    )

    response_payload = response['body'].read()
    metrics.add_metric(name="SuccessfulInvocation", unit=MetricUnit.Count, value=1)
    return extract_json_from_response(response_payload)
