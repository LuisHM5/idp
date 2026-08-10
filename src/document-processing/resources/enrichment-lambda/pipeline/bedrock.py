"""Shared Bedrock Converse helper: retries, JSON parsing, usage tracking."""

from __future__ import annotations

import itertools
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

_FALLBACK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Env var holding the per-stage model override. A stage with no override falls
# back to the base model, so `BEDROCK_MODEL_ID` alone configures the whole
# pipeline and each stage can then be moved to a cheaper or stronger model
# independently — without touching code.
_STAGE_ENV = {
    "segmenter": "BEDROCK_SEGMENTER_MODEL_ID",
    "extractor": "BEDROCK_EXTRACTOR_MODEL_ID",
    "recovery": "BEDROCK_RECOVERY_MODEL_ID",
}


def base_model_id() -> str:
    """Model used by any stage that doesn't override it.

    `BEDROCK_EXTRACTOR_MODEL_ID` is honoured as the base for backwards
    compatibility: deployments made before per-stage config existed set only
    that variable, and it fed every call in this module.
    """
    return (
        os.environ.get("BEDROCK_MODEL_ID")
        or os.environ.get("BEDROCK_EXTRACTOR_MODEL_ID")
        or _FALLBACK_MODEL_ID
    )


# Stage -> model actually used during this invocation. Each stage calls
# `resolve_model` immediately before invoking, so this records which stages ran
# rather than which are merely configured — reporting a model for a stage that
# never executed would be as misleading as the "default" placeholder this
# replaced. Lambda containers are reused, so `reset_used_models()` must run at
# the start of each invocation.
_USED_MODELS: Dict[str, str] = {}


# Per-request model override. Only ever set when the deployment allows it
# (dev), so a request can try a model without a redeploy. Cleared per
# invocation alongside the used-model registry.
_MODEL_OVERRIDE: Optional[str] = None


def set_model_override(model_id: Optional[str]) -> None:
    """Force every stage of this invocation onto `model_id`."""
    global _MODEL_OVERRIDE
    _MODEL_OVERRIDE = model_id or None


def reset_used_models() -> None:
    global _MODEL_OVERRIDE
    _MODEL_OVERRIDE = None
    _USED_MODELS.clear()


def used_models() -> Dict[str, str]:
    return dict(_USED_MODELS)


def resolve_model(stage: str, catalog_model_id: Optional[str] = None) -> str:
    """Model for `stage`, falling back to the base model.

    `catalog_model_id` is a document type's own `model_id` (catalogs/README.md
    → "Entry de nivel de tipo"), when the caller has one in scope. It outranks
    the deployment's stage/base env config — a type that opted into a
    specific model did so on purpose — but still yields to `_MODEL_OVERRIDE`,
    the dev-only per-request override, since that's an explicit ad hoc
    request to force every stage onto one model regardless of type.

    Read at call time (not import time) so a test or a script can change the
    environment between calls.
    """
    env_var = _STAGE_ENV.get(stage)
    model = (
        _MODEL_OVERRIDE
        or catalog_model_id
        or (os.environ.get(env_var) if env_var else None)
        or base_model_id()
    )
    _USED_MODELS[stage] = model
    return model


_RETRYABLE = {"ThrottlingException", "ModelNotReadyException", "ServiceUnavailableException"}

# Models observed to reject `cachePoint`. Populated at runtime by the first
# rejection — see `_invoke_with_retry`.
_NO_CACHE_SUPPORT: set = set()

# Per-model output-token ceiling, learned from Bedrock's own rejection message.
# Ceilings differ widely (Nova caps at 10k where Claude allows 16k+), so a
# single MAX_OUTPUT_TOKENS constant can't fit every model.
_MODEL_MAX_TOKENS: Dict[str, int] = {}
_MAX_TOKENS_RE = re.compile(r"model limit of (\d+)")

# Set MODEL_RESPONSE_DUMP_DIR to write every request/response pair to disk, one
# JSON per call, for inspecting what a model actually returned — which field it
# invented, which it dropped, how it shaped the JSON. Off unless the env var is
# set, so it costs nothing in production.
_DUMP_DIR = os.environ.get("MODEL_RESPONSE_DUMP_DIR")
_dump_counter = itertools.count(1)


def _dump_exchange(kwargs: Dict[str, Any], response: Dict[str, Any]) -> None:
    if not _DUMP_DIR:
        return
    try:
        os.makedirs(_DUMP_DIR, exist_ok=True)
        model = kwargs.get("modelId", "unknown")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", model).strip("-")
        path = os.path.join(_DUMP_DIR, f"{slug}-{next(_dump_counter)}.json")
        output = response.get("output", {}).get("message", {}).get("content", [])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "modelId": model,
                    "stopReason": response.get("stopReason"),
                    "usage": response.get("usage", {}),
                    # What the model produced, split by shape: `toolUse.input`
                    # is already a dict (structured extraction); `text` is raw
                    # output that still needs parsing.
                    "toolUseInput": next(
                        (b["toolUse"].get("input") for b in output if "toolUse" in b), None
                    ),
                    "text": "\n".join(b.get("text", "") for b in output if "text" in b),
                    "request": kwargs,
                    "rawResponseContent": output,
                },
                fh,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
    except Exception as e:  # never let dumping break a real run
        logger.warning("no se pudo guardar el dump del modelo: %s", e)
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


def _strip_cache_points(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Same request without the `cachePoint` marker."""
    system = kwargs.get("system")
    if not system:
        return kwargs
    return {**kwargs, "system": [b for b in system if "cachePoint" not in b]}


def _clamp_max_tokens(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Cap `maxTokens` at the model's known ceiling, if we've learned one."""
    limit = _MODEL_MAX_TOKENS.get(kwargs.get("modelId", ""))
    cfg = kwargs.get("inferenceConfig") or {}
    if not limit or cfg.get("maxTokens", 0) <= limit:
        return kwargs
    return {**kwargs, "inferenceConfig": {**cfg, "maxTokens": limit}}


def _max_tokens_limit(e: ClientError) -> Optional[int]:
    """The model's output ceiling, when Bedrock rejected the request for it.

    Output ceilings vary by model (10k on Nova, 16k+ on Claude), so a request
    sized for one model is rejected outright by another — the whole extraction
    fails rather than returning a shorter answer. Bedrock names the limit in
    the message, so learn it from there instead of maintaining a table.
    """
    m = _MAX_TOKENS_RE.search(e.response.get("Error", {}).get("Message", ""))
    return int(m.group(1)) if m else None


def _is_cache_unsupported(e: ClientError) -> bool:
    """Whether this error is Bedrock refusing the `cachePoint` specifically.

    Matched on the message, not the error code: Bedrock reports this as
    `AccessDeniedException` — the same code as a genuinely missing model
    permission — so the code alone can't tell the two apart. The caller only
    consults this when the request actually carried a cachePoint, so a real
    access denial (whose message never mentions caching) still propagates.
    """
    return "prompt caching" in e.response.get("Error", {}).get("Message", "").lower()


def _invoke_with_retry(kwargs: Dict[str, Any], max_retries: int) -> Dict[str, Any]:
    # Bedrock does NOT ignore a `cachePoint` a model can't honour — it rejects
    # the whole request with a ValidationException. Verified against
    # qwen.qwen3-vl-235b-a22b and mistral-large-3, which support Converse and
    # tool use but not caching. Left unhandled, every extraction call would
    # fail on those models, so the pipeline would only ever run on the subset
    # of Bedrock models that support caching.
    #
    # Instead: drop the cachePoint and retry, remembering the model so later
    # calls skip it. Costs one rejected call per model per container; the only
    # loss is the cache discount, which that model never offered anyway.
    if kwargs.get("modelId") in _NO_CACHE_SUPPORT:
        kwargs = _strip_cache_points(kwargs)
    kwargs = _clamp_max_tokens(kwargs)

    delay = 2.0
    for attempt in range(max_retries + 1):
        try:
            response = client().converse(**kwargs)
            _dump_exchange(kwargs, response)
            return response
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE and attempt < max_retries:
                logger.warning("bedrock %s, retrying in %.1fs", code, delay)
                time.sleep(delay)
                delay *= 2
                continue
            limit = _max_tokens_limit(e)
            if limit is not None:
                model_id = kwargs.get("modelId", "")
                logger.warning(
                    "bedrock: %s caps output at %d tokens; clamping and retrying",
                    model_id,
                    limit,
                )
                _MODEL_MAX_TOKENS[model_id] = limit
                kwargs = _clamp_max_tokens(kwargs)
                continue
            if _is_cache_unsupported(e) and any(
                "cachePoint" in b for b in kwargs.get("system") or []
            ):
                model_id = kwargs.get("modelId", "")
                logger.warning(
                    "bedrock: %s does not support prompt caching; "
                    "retrying without it (no cache discount on this model)",
                    model_id,
                )
                _NO_CACHE_SUPPORT.add(model_id)
                kwargs = _strip_cache_points(kwargs)
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
    model_id = model_id or base_model_id()
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
    model_id = model_id or base_model_id()
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
