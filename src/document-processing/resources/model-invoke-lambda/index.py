"""Classification / extraction step, driven by the Bedrock Converse API.

Replaces the blueprint's own `default-bedrock-invoke` Lambda, which we used to
overwrite inside `node_modules` with a patch. Two reasons this lives here now:

1. **Any Bedrock model.** The blueprint's version calls `invoke_model` with an
   Anthropic-native body (`anthropic_version`, `{"type": "image"}` content
   blocks), so only Anthropic models work. Converse is Bedrock's
   model-agnostic surface: the same request shape serves every model Bedrock
   hosts, so the model becomes a config value instead of a code change.
2. **No patching.** The stack now subclasses the blueprint construct and
   points its classification/processing steps here (see
   `multi-model-document-processing.ts`), which is a supported extension
   point instead of a file overwrite that a dependency bump would silently
   revert.

The business logic (manual-classification bypass, extraction bypass for types
extracted downstream, the large-document guard, chunk context) is carried over
unchanged from that patch — only the model layer differs.
"""

import json
import os
import re
import time

import boto3
from aws_lambda_powertools import Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from botocore.config import Config
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=300, retries={"max_attempts": 2}),
)
metrics = Metrics()
tracer = Tracer()

# Manual classification via S3 key prefix: raw/{TYPE}/{uuid}/{file}.
# TYPE must be UPPER_SNAKE_CASE. The uuid segment (lowercase + hyphens)
# never matches, so auto-classification paths (raw/{uuid}/{file}) fall through
# to the model as before.
_MANUAL_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# Files above this size blow the model's context window. Instead of failing the
# workflow, classification returns LARGE_DOC_FALLBACK_TYPE (the enrichment
# Lambda's expediente pipeline re-segments and classifies each page range
# itself) and extraction returns empty entities.
#
# The right value depends on the configured model's context window, so it is an
# env var rather than a constant: ~3.5MB suits a ~200K-token window (a
# 5.8MB/86-page PDF is ~200K tokens). Retune it when pointing this step at a
# model with a different window. `MAX_BEDROCK_DOC_BYTES` is the name the
# patched Lambda used and is still honoured.
_MAX_DOC_BYTES = int(
    os.environ.get("MAX_MODEL_DOC_BYTES")
    or os.environ.get("MAX_BEDROCK_DOC_BYTES")
    or "3500000"
)
_LARGE_DOC_FALLBACK_TYPE = os.environ.get(
    "LARGE_DOC_FALLBACK_TYPE", "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"
)

# Bedrock exception codes worth a retry. The blueprint's version had none and
# leaned on Step Functions to retry the whole task, which re-downloads the
# document and re-bills the tokens already spent.
_RETRYABLE = {"ThrottlingException", "ModelNotReadyException", "ServiceUnavailableException"}

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# Converse names its media formats by extension, without the dot.
_IMAGE_FORMATS = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}
_DOC_FORMATS = {"pdf": "pdf", "csv": "csv", "doc": "doc", "docx": "docx", "txt": "txt", "md": "md"}

# Converse rejects document names outside this set (no dots, no underscores),
# which is stricter than S3 key naming — hence the sanitising below.
_DOC_NAME_ALLOWED_RE = re.compile(r"[^a-zA-Z0-9\s\-()\[\]]")

# Forcing a tool call constrains the model to a schema structurally, instead of
# asking it to write JSON and hoping. Off by default so switching to this
# Lambda is a like-for-like swap that can be verified on its own; turn it on
# when a model returns prose around its JSON.
_USE_STRUCTURED = os.environ.get("MODEL_INVOKE_STRUCTURED", "").lower() in ("1", "true", "yes")

_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {"documentClassification": {"type": "string"}},
    "required": ["documentClassification"],
}

_PROCESSING_SCHEMA = {
    "type": "object",
    "properties": {
        "documentClassification": {"type": "string"},
        "result": {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["type", "value"],
                    },
                }
            },
            "required": ["entities"],
        },
    },
    "required": ["documentClassification", "result"],
}


def type_has_upstream_extraction(classification, prompt):
    """True when the processing prompt defines fields for this type.

    Customs/pedimento types are intentionally absent from the processing
    prompt (their extraction happens in the enrichment Lambda's expediente
    pipeline), so running the extraction here wastes a full call whose output
    gets discarded anyway.
    """
    return f"{classification}:" in prompt


def extract_manual_classification(key):
    """Return the manual document type encoded in the S3 key, or None.

    Expects keys shaped like `raw/{TYPE}/...`. Returns the TYPE segment when
    it matches UPPER_SNAKE_CASE; otherwise returns None so the caller falls
    back to model-based classification.
    """
    if not key:
        return None
    parts = key.split("/")
    if len(parts) < 3 or parts[0] != "raw":
        return None
    candidate = parts[1]
    if _MANUAL_TYPE_RE.match(candidate):
        return candidate
    return None


def parse_chunk_metadata(event):
    if "chunkMetadata" in event:
        return event["chunkMetadata"]
    if "chunk" in event:
        chunk = event["chunk"]
        return {
            "chunkIndex": chunk.get("chunkIndex", event.get("chunkIndex", 0)),
            "totalChunks": event.get("totalChunks", 1),
            "startPage": chunk.get("startPage", 0),
            "endPage": chunk.get("endPage", 0),
            "pageCount": chunk.get("pageCount", 0),
            "estimatedTokens": chunk.get("estimatedTokens", 0),
            "overlapPages": chunk.get("overlapPages", 0),
        }
    return None


def build_chunk_context_prompt(chunk_metadata):
    if not chunk_metadata:
        return ""
    chunk_index = chunk_metadata.get("chunkIndex", 0)
    total_chunks = chunk_metadata.get("totalChunks", 1)
    start_page = chunk_metadata.get("startPage", 0)
    end_page = chunk_metadata.get("endPage", 0)
    overlap_pages = chunk_metadata.get("overlapPages", 0)
    context_parts = [
        f"You are analyzing chunk {chunk_index + 1} of {total_chunks} from pages {start_page + 1} to {end_page + 1}."
    ]
    if overlap_pages > 0 and chunk_index > 0:
        context_parts.append(
            f"Note: This chunk includes {overlap_pages} overlapping pages from the previous chunk for context."
        )
    return "\n".join(context_parts) + "\n\n"


def _document_name(key):
    """Converse only accepts alphanumerics, spaces, hyphens, parens and
    brackets in a document name — an S3 basename usually has a dot at least."""
    base = key.split("/")[-1].rsplit(".", 1)[0]
    # Collapse runs of illegal characters and trim the separators they leave
    # behind, so a name like `___.pdf` becomes "document" rather than "---".
    cleaned = _DOC_NAME_ALLOWED_RE.sub("-", base).strip(" -")
    return cleaned or "document"


def _media_block(ext, key, payload):
    """Wrap raw file bytes in the Converse content block for its media type.

    Converse takes raw bytes; the Anthropic-native API this replaced took a
    base64 string, so passing base64 through here would be read as file
    content rather than decoded.
    """
    if ext in _IMAGE_FORMATS:
        return {"image": {"format": _IMAGE_FORMATS[ext], "source": {"bytes": payload}}}
    return {
        "document": {
            "format": _DOC_FORMATS[ext],
            "name": _document_name(key),
            "source": {"bytes": payload},
        }
    }


def _converse_with_retry(kwargs, max_retries=4):
    delay = 2.0
    for attempt in range(max_retries + 1):
        try:
            return bedrock.converse(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE and attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise AssertionError("unreachable")  # loop always returns or raises


def extract_json_from_response(response):
    """Pull the step's JSON payload out of a Converse response.

    Converse normalises every model's output to flat `{"text": ...}` /
    `{"toolUse": ...}` blocks, so unlike the Anthropic-native shape there are
    no provider-specific block types to skip.
    """
    content = response.get("output", {}).get("message", {}).get("content", [])

    for block in content:
        if "toolUse" in block:
            return block["toolUse"].get("input") or {}

    text = "\n".join(b.get("text", "") for b in content if "text" in b).strip()
    if not text:
        raise ValueError(f"Empty response from model. Content blocks: {json.dumps(content)[:500]}")

    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"Could not parse JSON from response: {text[:300]}")
    return json.loads(text[start : end + 1])


@metrics.log_metrics
@tracer.capture_lambda_handler
def handler(event, context):
    invoke_type = os.environ["INVOKE_TYPE"]
    tracer.put_annotation(key="invoke_type", value=invoke_type)
    tracer.put_annotation(key="documentId", value=event["documentId"])
    metrics.add_dimension(name="invoke_type", value=invoke_type)
    content_type = event["contentType"]

    # Manual classification short-circuit: if the S3 key encodes the type
    # (raw/{TYPE}/...), skip the model call for the classification step and
    # return the type directly. Extraction still runs normally because its
    # prompt uses `classificationResult` which will be populated below.
    if invoke_type == "classification" and content_type == "file":
        content_location = event.get("content", {}).get("location")
        if content_location == "s3":
            manual_type = extract_manual_classification(
                event.get("content", {}).get("key", "")
            )
            if manual_type:
                tracer.put_annotation(key="classification_mode", value="manual")
                metrics.add_metric(
                    name="ManualClassificationBypass",
                    unit=MetricUnit.Count,
                    value=1,
                )
                return {"documentClassification": manual_type, "modelId": None}

    chunk_metadata = parse_chunk_metadata(event)
    if chunk_metadata:
        tracer.put_annotation(key="chunkIndex", value=str(chunk_metadata.get("chunkIndex", 0)))
        tracer.put_annotation(key="totalChunks", value=str(chunk_metadata.get("totalChunks", 1)))
        metrics.add_dimension(name="is_chunked", value="true")
    else:
        metrics.add_dimension(name="is_chunked", value="false")

    prompt = os.environ["PROMPT"]
    if "classificationResult" in event:
        classification = event["classificationResult"]["documentClassification"]
        # Extraction bypass: types without fields in the processing prompt
        # (customs/pedimento catalog) are extracted downstream by the
        # enrichment Lambda; skip the redundant model call.
        if invoke_type != "classification" and not type_has_upstream_extraction(classification, prompt):
            tracer.put_annotation(key="extraction_mode", value="deferred_to_enrichment")
            metrics.add_metric(name="ExtractionBypass", unit=MetricUnit.Count, value=1)
            return {
                "documentClassification": classification,
                "result": {"entities": []},
                "modelId": None,
            }
        prompt = prompt.replace("[ACTUAL_CLASSIFICATION]", classification)

    chunk_context = build_chunk_context_prompt(chunk_metadata)
    if chunk_context:
        prompt = chunk_context + prompt

    content = [{"text": prompt}]
    if content_type == "file":
        content_location = event["content"]["location"]
        if content_location == "s3":
            if chunk_metadata and "bucket" in chunk_metadata and "key" in chunk_metadata:
                bucket = chunk_metadata["bucket"]
                key = chunk_metadata["key"]
            else:
                bucket = event["content"]["bucket"]
                key = event["content"]["key"]

            ext = key.lower().split(".")[-1]
            if ext not in _IMAGE_FORMATS and ext not in _DOC_FORMATS:
                raise ValueError(f"Unsupported file type: {ext}")

            local_path = f"/tmp/{key.split('/')[-1]}"
            s3.download_file(bucket, key, local_path)

            # Large-document guard: degrade gracefully instead of failing the
            # workflow — the enrichment pipeline handles these.
            if os.path.getsize(local_path) > _MAX_DOC_BYTES:
                tracer.put_annotation(key="large_doc_bypass", value="true")
                metrics.add_metric(name="LargeDocumentBypass", unit=MetricUnit.Count, value=1)
                if invoke_type == "classification":
                    return {
                        "documentClassification": _LARGE_DOC_FALLBACK_TYPE,
                        "modelId": None,
                    }
                classification = event.get("classificationResult", {}).get(
                    "documentClassification", _LARGE_DOC_FALLBACK_TYPE
                )
                return {
                    "documentClassification": classification,
                    "result": {"entities": []},
                    "modelId": None,
                }

            with open(local_path, "rb") as f:
                content.append(_media_block(ext, key, f.read()))

    elif content_type == "data":
        content.append({"text": event["content"]["data"]})

    max_tokens = int(os.getenv("INVOKE_MAX_TOKENS", "4096"))
    kwargs = {
        "modelId": os.environ["MODEL_ID"],
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
    }
    if _USE_STRUCTURED:
        schema = _CLASSIFICATION_SCHEMA if invoke_type == "classification" else _PROCESSING_SCHEMA
        kwargs["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": "return_result",
                        "description": "Return the result for this document step.",
                        "inputSchema": {"json": schema},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": "return_result"}},
        }

    response = _converse_with_retry(kwargs)

    metrics.add_metric(name="SuccessfulInvocation", unit=MetricUnit.Count, value=1)
    return extract_json_from_response(response)
