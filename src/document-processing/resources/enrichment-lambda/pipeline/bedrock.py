"""Shared Bedrock Converse helper: retries, JSON parsing, usage tracking."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = os.environ.get(
    "BEDROCK_EXTRACTOR_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

_RETRYABLE = {"ThrottlingException", "ModelNotReadyException", "ServiceUnavailableException"}
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@lru_cache(maxsize=1)
def client(region: Optional[str] = None):
    region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(read_timeout=300, retries={"max_attempts": 2}),
    )


def converse(
    prompt: str,
    model_id: Optional[str] = None,
    max_tokens: int = 8192,
    system: Optional[str] = None,
    cache_system: Optional[str] = None,
    max_retries: int = 4,
) -> Tuple[str, Dict[str, Any]]:
    """Single-turn Converse call. Returns (text, meta) where meta carries
    usage tokens and stopReason for cost tracking.

    `cache_system` is a large, STATIC prefix (instructions + schema) marked with
    a Bedrock `cachePoint`, so repeated calls (chunks of a document, or several
    documents of the same type) reuse it at the discounted cache-read rate
    instead of re-billing it as fresh input. Content is identical, so extraction
    quality is unchanged. Below the model's cache minimum Bedrock simply skips
    caching (no error)."""
    model_id = model_id or DEFAULT_MODEL_ID
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    kwargs: Dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
    }
    system_blocks = []
    if system:
        system_blocks.append({"text": system})
    if cache_system:
        system_blocks.append({"text": cache_system})
        system_blocks.append({"cachePoint": {"type": "default"}})
    if system_blocks:
        kwargs["system"] = system_blocks

    delay = 2.0
    for attempt in range(max_retries + 1):
        try:
            response = client().converse(**kwargs)
            break
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE and attempt < max_retries:
                logger.warning("bedrock %s, retrying in %.1fs", code, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise

    output = response.get("output", {}).get("message", {})
    text = "\n".join(b.get("text", "") for b in output.get("content", []) if "text" in b).strip()
    meta = {
        "modelId": model_id,
        "stopReason": response.get("stopReason"),
        "usage": response.get("usage", {}),
    }
    return text, meta


def parse_json_response(raw_text: str) -> Dict[str, Any]:
    """Extract the first JSON object from a model response, tolerating fences
    and leading/trailing prose."""
    text = raw_text.strip()
    if not text:
        raise ValueError("empty model response")
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model response")
    return json.loads(text[start : end + 1])
