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

# Manual classification via S3 key prefix: raw/{TYPE}/{uuid}/{file}.
# TYPE must be UPPER_SNAKE_CASE. The uuid segment (lowercase + hyphens)
# never matches, so auto-classification paths (raw/{uuid}/{file}) fall through
# to Bedrock as before.
_MANUAL_TYPE_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')

# Files above this size cannot go through Bedrock vision (a ~5.8MB/86-page
# PDF is ~200K tokens and exceeds the model's context window). Instead of
# failing the workflow, classification returns LARGE_DOC_FALLBACK_TYPE (the
# enrichment Lambda's expediente pipeline re-segments and classifies each
# page range itself) and extraction returns empty entities.
_MAX_BEDROCK_DOC_BYTES = int(os.environ.get('MAX_BEDROCK_DOC_BYTES', '3500000'))
_LARGE_DOC_FALLBACK_TYPE = os.environ.get(
    'LARGE_DOC_FALLBACK_TYPE', 'EXPEDIENTE_ADUANAL_MULTIDOCUMENTO'
)


def type_has_upstream_extraction(classification, prompt):
    """True when the processing prompt defines fields for this type.

    Customs/pedimento types are intentionally absent from the processing
    prompt (their extraction happens in the enrichment Lambda's expediente
    pipeline), so running the Bedrock extraction for them wastes a full
    vision call whose output gets discarded anyway.
    """
    return f"{classification}:" in prompt


def extract_manual_classification(key):
    """Return the manual document type encoded in the S3 key, or None.

    Expects keys shaped like `raw/{TYPE}/...`. Returns the TYPE segment when
    it matches UPPER_SNAKE_CASE; otherwise returns None so the caller falls
    back to Bedrock-based classification.
    """
    if not key:
        return None
    parts = key.split('/')
    if len(parts) < 3 or parts[0] != 'raw':
        return None
    candidate = parts[1]
    if _MANUAL_TYPE_RE.match(candidate):
        return candidate
    return None


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

    # Manual classification short-circuit: if the S3 key encodes the type
    # (raw/{TYPE}/...), skip the Bedrock call for the classification step
    # and return the type directly. Extraction still runs normally because
    # its prompt uses `classificationResult` which will be populated below.
    if invoke_type == 'classification' and content_type == 'file':
        content_location = event.get('content', {}).get('location')
        if content_location == 's3':
            manual_type = extract_manual_classification(
                event.get('content', {}).get('key', '')
            )
            if manual_type:
                tracer.put_annotation(key="classification_mode", value="manual")
                metrics.add_metric(
                    name="ManualClassificationBypass",
                    unit=MetricUnit.Count,
                    value=1,
                )
                return {"documentClassification": manual_type}

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
        # Extraction bypass: types without fields in the processing prompt
        # (customs/pedimento catalog) are extracted downstream by the
        # enrichment Lambda; skip the redundant Bedrock vision call.
        if invoke_type != 'classification' and not type_has_upstream_extraction(classification, prompt):
            tracer.put_annotation(key="extraction_mode", value="deferred_to_enrichment")
            metrics.add_metric(name="ExtractionBypass", unit=MetricUnit.Count, value=1)
            return {"documentClassification": classification, "result": {"entities": []}}
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

            # Large-document guard: files this big blow Bedrock's context
            # window ("prompt is too long"). Degrade gracefully instead of
            # failing the workflow — the enrichment pipeline handles the
            # heavy lifting for these.
            if os.path.getsize(local_path) > _MAX_BEDROCK_DOC_BYTES:
                tracer.put_annotation(key="large_doc_bypass", value="true")
                metrics.add_metric(name="LargeDocumentBypass", unit=MetricUnit.Count, value=1)
                if invoke_type == 'classification':
                    return {"documentClassification": _LARGE_DOC_FALLBACK_TYPE}
                classification = event.get('classificationResult', {}).get(
                    'documentClassification', _LARGE_DOC_FALLBACK_TYPE
                )
                return {"documentClassification": classification, "result": {"entities": []}}

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
