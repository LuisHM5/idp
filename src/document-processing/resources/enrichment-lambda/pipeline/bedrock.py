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


def _system_blocks(system: Optional[str], cache_system: Optional[str]) -> list:
    blocks = []
    if system:
        blocks.append({"text": system})
    if cache_system:
        blocks.append({"text": cache_system})
        blocks.append({"cachePoint": {"type": "default"}})
    return blocks


def _invoke_with_retry(kwargs: Dict[str, Any], max_retries: int) -> Dict[str, Any]:
    delay = 2.0
    for attempt in range(max_retries + 1):
        try:
            return client().converse(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE and attempt < max_retries:
                logger.warning("bedrock %s, retrying in %.1fs", code, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise AssertionError("unreachable")  # loop always returns or raises


def converse(
    prompt: str,
    model_id: Optional[str] = None,
    max_tokens: int = 8192,
    system: Optional[str] = None,
    cache_system: Optional[str] = None,
    max_retries: int = 4,
    prefill: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Single-turn Converse call. Returns (text, meta) where meta carries
    usage tokens and stopReason for cost tracking.

    `cache_system` is a large, STATIC prefix (instructions + schema) marked with
    a Bedrock `cachePoint`, so repeated calls (chunks of a document, or several
    documents of the same type) reuse it at the discounted cache-read rate
    instead of re-billing it as fresh input. Content is identical, so extraction
    quality is unchanged. Below the model's cache minimum Bedrock simply skips
    caching (no error).

    `prefill` seeds an assistant turn (e.g. "{") so the model continues from
    there instead of choosing its own response shape.

    For schemas too large for the model to follow reliably as prose
    instructions (many extraction fields), prefer `converse_structured()`
    below — it enforces the shape structurally instead of hoping the model
    complies with a text instruction."""
    model_id = model_id or DEFAULT_MODEL_ID
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    if prefill:
        messages.append({"role": "assistant", "content": [{"text": prefill}]})
    kwargs: Dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
    }
    blocks = _system_blocks(system, cache_system)
    if blocks:
        kwargs["system"] = blocks

    response = _invoke_with_retry(kwargs, max_retries)

    output = response.get("output", {}).get("message", {})
    text = "\n".join(b.get("text", "") for b in output.get("content", []) if "text" in b).strip()
    if prefill:
        text = prefill + text
    meta = {
        "modelId": model_id,
        "stopReason": response.get("stopReason"),
        "usage": response.get("usage", {}),
    }
    return text, meta


def converse_structured(
    prompt: str,
    tool_schema: Dict[str, Any],
    tool_name: str,
    tool_description: str,
    model_id: Optional[str] = None,
    max_tokens: int = 8192,
    system: Optional[str] = None,
    cache_system: Optional[str] = None,
    max_retries: int = 4,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Single-turn Converse call constrained to a tool's JSON schema.

    `converse()` asks the model to *write* JSON as free text — reliable for
    small schemas, but verified unreliable for a catalog with 100+ extraction
    fields: the model can silently drift into its own field names (English,
    nested differently) or a human-readable summary instead of the exact
    schema, even with an explicit format instruction and a "{" prefill. This
    routes the same request through Bedrock's native tool-calling instead:
    the model can only respond with a `toolUse` block whose `input` is
    validated against `tool_schema`, so it structurally cannot rename or
    restructure the fields. Returns (parsed_input, meta) — no JSON parsing
    needed, botocore already gives back a Python dict.
    """
    model_id = model_id or DEFAULT_MODEL_ID
    kwargs: Dict[str, Any] = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool_name,
                        "description": tool_description,
                        "inputSchema": {"json": tool_schema},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": tool_name}},
        },
    }
    blocks = _system_blocks(system, cache_system)
    if blocks:
        kwargs["system"] = blocks

    response = _invoke_with_retry(kwargs, max_retries)

    output = response.get("output", {}).get("message", {})
    tool_input: Dict[str, Any] = {}
    for block in output.get("content", []):
        if "toolUse" in block:
            tool_input = block["toolUse"].get("input") or {}
            break
    meta = {
        "modelId": model_id,
        "stopReason": response.get("stopReason"),
        "usage": response.get("usage", {}),
    }
    return tool_input, meta


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
