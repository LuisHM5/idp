"""Shared Bedrock Converse helper: retries, JSON parsing, usage tracking."""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import threading
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from document_catalog import TypeRegistryEntry

from .model_fallback import get_fallback_models, get_max_fallback_retries

logger = logging.getLogger(__name__)

_FALLBACK_MODEL_ID = "us.anthropic.claude-haiku-5-5"

# Env var holding the per-stage model override. A stage with no override falls
# back to the base model, so `BEDROCK_MODEL_ID` alone configures the whole
# pipeline and each stage can then be moved to a cheaper or stronger model
# independently — without touching code.
_STAGE_ENV = {
    "segmenter": "BEDROCK_SEGMENTER_MODEL_ID",
    "extractor": "BEDROCK_EXTRACTOR_MODEL_ID",
    "recovery": "BEDROCK_RECOVERY_MODEL_ID",
    "vision": "BEDROCK_VISION_MODEL_ID",
}

# Stages that send IMAGES. Every other stage is text-only by design (the
# expediente pipeline never shows a page to a model), which is why the base
# model and the fallback list are free to be text-only models — and why a
# multimodal stage must never resolve to either: handing an image to a
# text-only model is a ValidationException at best and a confident answer
# about a page it never saw at worst. See `_model_candidates`.
_MULTIMODAL_STAGES = frozenset({"vision"})
# Vision-capable default for those stages when nothing pins one.
_MULTIMODAL_DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-5-5"


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


# Stage -> fallback summary, populated by `_invoke_with_fallback` ONLY when
# it actually switched models for that stage AND the switched-to model went
# on to succeed (a stage that never had to switch — the common case — is
# simply absent here, same "only report what happened" rule `_USED_MODELS`
# already follows). Surfaced in the result envelope as `tracking.modelFallbacks`
# — see `pipeline/result_common.py::build_tracking` and `handler.py`. Cleared
# per invocation alongside `_USED_MODELS` (Lambda containers are reused).
_FALLBACK_EVENTS: Dict[str, Dict[str, Any]] = {}


# Stage -> list of caller-level degradations this invocation, populated by
# `record_stage_degraded`. Distinct from `_FALLBACK_EVENTS` in two ways: (1)
# it records a CALLER's own retry loop (e.g. segmenter.py re-issuing a whole
# new `converse()` call on a different model after a 200-OK-but-empty/
# truncated response) rather than a swap `_invoke_with_fallback` made inside
# a single call after a `ClientError`; (2) it accumulates, because a caller
# that classifies a document in windows (segmenter.py's page-windowing) can
# degrade more than once per invocation, each independent of the others —
# a dict keyed by stage, like `_FALLBACK_EVENTS`, would silently drop all but
# the last. A degradation that the caller's own retry then RECOVERED from is
# still recorded (with its own `resolution`), unlike `_FALLBACK_EVENTS` which
# only ever reports success. Cleared per invocation alongside `_USED_MODELS`;
# writes happen from worker threads (windowed segmentation), so guarded by a
# lock the same way `_PROMPT_LOG` is.
_STAGE_DEGRADATIONS: Dict[str, List[Dict[str, Any]]] = {}
_STAGE_DEGRADATIONS_LOCK = threading.Lock()


# Every prompt/response pair sent to Bedrock this invocation, in call order —
# one entry per `converse`/`converse_structured` call, appended by
# `_record_prompt`. Extraction runs several calls concurrently (per-segment
# and per-chunk thread pools — `pipeline/expediente.py::_EXTRACT_WORKERS`,
# `pipeline/text_extractor.py::_CHUNK_WORKERS`), so appends go through
# `_PROMPT_LOG_LOCK`. Consumed by `scripts/procesar-docs.py` to dump the
# exact input/output prompts next to its usual per-document result; cleared
# per invocation alongside `_USED_MODELS`/`_FALLBACK_EVENTS`.
_PROMPT_LOG: List[Dict[str, Any]] = []
_PROMPT_LOG_LOCK = threading.Lock()


def set_model_override(model_id: Optional[str]) -> None:
    """Force every stage of this invocation onto `model_id`."""
    global _MODEL_OVERRIDE
    _MODEL_OVERRIDE = model_id or None


def reset_used_models() -> None:
    global _MODEL_OVERRIDE
    _MODEL_OVERRIDE = None
    _USED_MODELS.clear()
    _FALLBACK_EVENTS.clear()
    with _PROMPT_LOG_LOCK:
        _PROMPT_LOG.clear()
    with _STAGE_DEGRADATIONS_LOCK:
        _STAGE_DEGRADATIONS.clear()


def used_models() -> Dict[str, str]:
    return dict(_USED_MODELS)


def prompt_log() -> List[Dict[str, Any]]:
    """Snapshot of `_PROMPT_LOG`: every Bedrock call recorded so far this
    invocation, each `{"stage", "requestedModelId", "usedModelId", "input",
    "output"}` — see `_record_prompt`."""
    with _PROMPT_LOG_LOCK:
        return [dict(entry) for entry in _PROMPT_LOG]


def _record_prompt(
    stage: Optional[str],
    requested_model_id: str,
    used_model_id: str,
    input_payload: Dict[str, Any],
    output_payload: Dict[str, Any],
) -> None:
    entry = {
        "stage": stage,
        "requestedModelId": requested_model_id,
        "usedModelId": used_model_id,
        "input": input_payload,
        "output": output_payload,
    }
    with _PROMPT_LOG_LOCK:
        _PROMPT_LOG.append(entry)


def fallback_events() -> Dict[str, Dict[str, Any]]:
    """Stage -> `{"expectedModel", "actualModel", "retries", "attempts"}` for
    every stage where a model-unavailable fallback (`_invoke_with_fallback`)
    actually switched models this invocation:

      - `expectedModel` — what `resolve_model()` picked before the call ran.
      - `actualModel`   — what really served the request (== `used_models()[stage]`).
      - `retries`       — how many switches it took (`len(attempts)`).
      - `attempts`      — `[{"model", "error"}, ...]`, one entry per model that
        failed along the way, in the order they were tried.
    """
    return {stage: dict(event) for stage, event in _FALLBACK_EVENTS.items()}


def record_stage_degraded(stage: str, detail: Dict[str, Any]) -> None:
    """Record that `stage` had to settle for a best-effort result over PART
    of this invocation — e.g. `segmenter.py` giving up on a page range after
    exhausting its own retry budget and labelling it OTRO. See
    `_STAGE_DEGRADATIONS` for how this differs from `_FALLBACK_EVENTS`.
    Thread-safe: windowed segmentation calls this from a `ThreadPoolExecutor`.
    """
    with _STAGE_DEGRADATIONS_LOCK:
        _STAGE_DEGRADATIONS.setdefault(stage, []).append(dict(detail))


def stage_degradations() -> Dict[str, List[Dict[str, Any]]]:
    """Stage -> list of degradations recorded this invocation via
    `record_stage_degraded`. A stage absent here never degraded — same
    "only report what happened" rule as `fallback_events()`."""
    with _STAGE_DEGRADATIONS_LOCK:
        return {stage: list(events) for stage, events in _STAGE_DEGRADATIONS.items() if events}


def note_used_model(stage: str, model_id: str) -> None:
    """Correct `_USED_MODELS[stage]` after the fact for a retry the CALLER
    made itself (a fresh `converse()` call on a different model, not a swap
    `_invoke_with_fallback` performed inside one call after a `ClientError`
    — that case already updates `_USED_MODELS` on its own). Same purpose as
    that internal correction, for the one path it can't see."""
    _USED_MODELS[stage] = model_id


def _model_candidates(
    stage: str, type_entry: Optional[TypeRegistryEntry]
) -> List[Tuple[str, Optional[str]]]:
    """Every model-id source for `stage`, highest priority first.

    THE single place this order is defined — `resolve_model` and
    `explain_model_resolution` both just walk this list, so the two can never
    disagree about which override wins:

      1. request override — `_MODEL_OVERRIDE`, set by `set_model_override`
         (dev-only, `ALLOW_MODEL_OVERRIDE` upload metadata — see
         `handler.py::_apply_model_override`). An explicit "try this model
         right now" for the whole invocation, so nothing outranks it —
         not even a type's own catalog config.
      2. catalog model_ids[stage] — `type_entry.model_ids`, the per-STAGE
         override on this document type's catalog entry (catalogs/README.md).
         Lets a type pin e.g. only its "recovery" phase without touching
         "extractor".
      3. catalog model_id — `type_entry.model_id`, the type's blanket
         override, used when it has no more specific entry in `model_ids`
         for this stage.
      4. stage env var — `BEDROCK_<STAGE>_MODEL_ID` (`_STAGE_ENV`), the
         deployment-wide override for this one stage, same for every type.
      5. segmenter's own default — ONLY for `stage == "segmenter"`: the last
         (best-verified/most-compatible, by `model_fallback`'s own design)
         entry of `get_fallback_models()`, instead of the shared base model.
         Segmentation is a different task shape from extraction/recovery —
         open-ended multi-document JSON with disambiguation rules, not a
         schema-constrained tool call — and the base model in this
         deployment (`minimax.minimax-m2.5`, chosen for extraction's cost)
         has verified real trouble with it: it reliably exhausts its whole
         output budget (8K, confirmed on its Bedrock model card) without
         emitting any answer text on exactly this shape, recovered only by
         `pipeline/segmenter.py::_classify_pages`'s own retry — at the cost
         of a wasted call every time. `extractor`/`recovery` are untouched:
         they keep falling through to the base model, where minimax has a
         track record of working (documentacion/COSTOS.md). Still only a
         DEFAULT — every layer above it (including a plain
         `BEDROCK_SEGMENTER_MODEL_ID`) wins over it exactly as before.
      6. base model — `base_model_id()`: `BEDROCK_MODEL_ID`, or the hardcoded
         fallback. Always non-empty, so resolution can never come up short.

    A stage in `_MULTIMODAL_STAGES` (it sends images) walks a shorter chain
    instead — catalog `model_ids[stage]`, its stage env var, then
    `_MULTIMODAL_DEFAULT_MODEL_ID` — so it can never land on a text-only model.
    """
    env_var = _STAGE_ENV.get(stage)
    if stage in _MULTIMODAL_STAGES:
        # A deliberately SHORTER chain: only sources that were chosen knowing
        # the stage sends images. The request override, the type's blanket
        # `model_id` and the base model are all picked for text extraction
        # (this deployment's base is a text-only model) and are skipped.
        return [
            ("catalog model_ids[stage]", type_entry.model_ids.get(stage) if type_entry else None),
            ("stage env var", os.environ.get(env_var) if env_var else None),
            ("multimodal default", _MULTIMODAL_DEFAULT_MODEL_ID),
        ]
    candidates: List[Tuple[str, Optional[str]]] = [
        ("request override", _MODEL_OVERRIDE),
        ("catalog model_ids[stage]", type_entry.model_ids.get(stage) if type_entry else None),
        ("catalog model_id", type_entry.model_id if type_entry else None),
        ("stage env var", os.environ.get(env_var) if env_var else None),
    ]
    # Only `segmenter` gets its own default layer, between the stage env var
    # and the shared base model — see this function's docstring, candidate 5.
    # Omitted entirely (not present-with-None) for every other stage, so
    # `explain_model_resolution`'s candidate list stays exactly what it was
    # for extractor/recovery.
    if stage == "segmenter":
        candidates.append(("segmenter default", _segmenter_default_model()))
    candidates.append(("base model", base_model_id()))
    return candidates


def _segmenter_default_model() -> Optional[str]:
    """The most reliable known model, for the segmenter stage's own default
    — see candidate 5 in `_model_candidates`'s docstring for why this stage
    doesn't just fall through to the shared base model like the others.
    `None` (falls through to the base model like before) when the fallback
    list is empty, which never happens with the hardcoded
    `model_fallback.DEFAULT_FALLBACK_MODELS` but is defensive against a
    misconfigured/empty DynamoDB override table."""
    models = get_fallback_models()
    return models[-1] if models else None


def resolve_model(stage: str, type_entry: Optional[TypeRegistryEntry] = None) -> str:
    """Model used for `stage`, honouring every override layer.

    `type_entry` is the document type currently being processed, when the
    caller has one in scope (extraction and Textract+Bedrock recovery both
    run per-type; segmentation doesn't, so it always resolves with
    `type_entry=None` and only the request/env/base layers apply). See
    `_model_candidates` for the full priority order.

    Read at call time (not import time) so a test or a script can change the
    environment between calls.
    """
    for _source, model in _model_candidates(stage, type_entry):
        if model:
            _USED_MODELS[stage] = model
            return model
    raise AssertionError("base model candidate must never be empty")  # pragma: no cover


def explain_model_resolution(
    stage: str, type_entry: Optional[TypeRegistryEntry] = None
) -> Dict[str, Any]:
    """Read-only introspection: every candidate for `stage` in priority
    order, plus which one wins — without recording into `_USED_MODELS` (use
    `resolve_model` for the real call). Meant for a debug log line or a CLI
    check when it's unclear which override layer produced a given model,
    since `_model_candidates` alone doesn't say which source "wins".
    """
    candidates = _model_candidates(stage, type_entry)
    resolved = next((model for _source, model in candidates if model), None)
    return {"stage": stage, "resolved": resolved, "candidates": candidates}


_RETRYABLE = {
    "ThrottlingException",
    "ModelNotReadyException",
    "ServiceUnavailableException",
    # Bedrock's generic 500 — AWS's own general guidance treats it as
    # transient, so it gets the same same-model backoff as the three above
    # before `_MODEL_UNAVAILABLE` below considers switching models.
    "InternalServerException",
}

# Models observed to reject `cachePoint`. Populated at runtime by the first
# rejection — see `_invoke_with_retry`.
_NO_CACHE_SUPPORT: set = set()

# Per-model output-token ceiling, verified against each model's own Bedrock
# model card (not inferred), seeded in `_MODEL_MAX_TOKENS` at import time so
# the very first request already respects a ceiling that's public knowledge
# instead of spending one wasted rejected call to learn it reactively (see
# `_max_tokens_limit` below for the general, any-model learning path this
# complements — most models have no entry here and rely on that instead).
_KNOWN_MODEL_MAX_TOKENS: Dict[str, int] = {
    # "Max output tokens: 8K" — https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-minimax-minimax-m2-5.html
    "minimax.minimax-m2.5": 8192,
}

# Per-model output-token ceiling actually enforced by `_clamp_max_tokens`:
# `_KNOWN_MODEL_MAX_TOKENS`'s entries up front, plus whatever gets LEARNED at
# runtime from Bedrock's own rejection message (see `_max_tokens_limit`) the
# first time some OTHER model's request asks for too much — ceilings differ
# widely (Nova caps at 10k where Claude allows 16k+), so a single constant
# can't fit every model. Never cleared by `reset_used_models()`, same as
# `_NO_CACHE_SUPPORT`: what a model's real ceiling is doesn't change between
# invocations in the same container, seeded or learned.
_MODEL_MAX_TOKENS: Dict[str, int] = dict(_KNOWN_MODEL_MAX_TOKENS)
_MAX_TOKENS_RE = re.compile(r"model limit of (\d+)")

# Set MODEL_RESPONSE_DUMP_DIR to write every request/response pair to disk, one
# JSON per call, for inspecting what a model actually returned — which field it
# invented, which it dropped, how it shaped the JSON. Off unless the env var is
# set, so it costs nothing in production.
_DUMP_DIR = os.environ.get("MODEL_RESPONSE_DUMP_DIR")
_dump_counter = itertools.count(1)


def _image_digest(image: Dict[str, Any]) -> Dict[str, Any]:
    """What a prompt log keeps of an image: enough to identify it, never the
    bytes (they are not JSON, and a page scan would dwarf the rest of the log)."""
    data = image.get("bytes") or b""
    return {
        "label": image.get("label"),
        "format": image.get("format") or "jpeg",
        "sha256": hashlib.sha256(data).hexdigest(),
        "byteLength": len(data),
    }


def _without_image_bytes(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """The request with every image block reduced to its digest, so it can be
    JSON-dumped. Text-only requests come back untouched."""
    messages = []
    for message in kwargs.get("messages") or []:
        blocks = []
        for block in message.get("content") or []:
            if "image" in block:
                source = (block["image"].get("source") or {})
                blocks.append({"image": _image_digest(
                    {"format": block["image"].get("format"), "bytes": source.get("bytes")}
                )})
            else:
                blocks.append(block)
        messages.append({**message, "content": blocks})
    return {**kwargs, "messages": messages}


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
                    "request": _without_image_bytes(kwargs),
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


# Model families with Anthropic's newer request surface (Claude Haiku 5.5):
#   - `temperature` other than 1 / `topP` other than its default → 400, so the
#     `temperature: 0` every call here sends must be dropped;
#   - an assistant prefill turn → 400;
#   - adaptive thinking is ON by default at effort `medium`, and the only
#     lever is `output_config.effort` (low|medium|high|xhigh|max).
# All three are `ValidationException`s, which never trigger a model switch
# (see `_MODEL_UNAVAILABLE`), so the request has to be right before it's sent.
# Matched by substring so the `us.`/`global.` inference profiles and the bare
# id all count. Applied per attempted model in `_invoke_with_retry`, so a
# fallback between this family and MiniMax/Kimi/GLM reshapes the request for
# whichever model actually receives it.
_EFFORT_MODEL_MARKERS = ("claude-haiku-5-5",)
# Extraction is schema-constrained, so `low` keeps latency and output tokens
# down; forced `toolChoice` calls skip thinking anyway. Override per
# deployment (`BEDROCK_EFFORT`), per stage (`BEDROCK_<STAGE>_EFFORT`) or per
# type (catalog `model_effort`) — see `effort_for`.
_DEFAULT_EFFORT = "low"
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def takes_effort(model_id: Optional[str]) -> bool:
    """Whether `model_id` uses the effort/no-sampling/no-prefill request shape."""
    return any(marker in (model_id or "") for marker in _EFFORT_MODEL_MARKERS)


def effort_for(stage: Optional[str] = None, type_entry: Optional[TypeRegistryEntry] = None) -> str:
    """Effort level for a call: catalog `model_effort` > `BEDROCK_<STAGE>_EFFORT`
    > `BEDROCK_EFFORT` > `_DEFAULT_EFFORT`. Only models in
    `_EFFORT_MODEL_MARKERS` ever receive it. An unknown value falls back to
    the default instead of sending Bedrock a request it would reject."""
    candidates = [
        getattr(type_entry, "model_effort", None) if type_entry else None,
        os.environ.get(f"BEDROCK_{stage.upper()}_EFFORT") if stage else None,
        os.environ.get("BEDROCK_EFFORT"),
    ]
    for value in candidates:
        level = str(value or "").strip().lower()
        if level in _EFFORT_LEVELS:
            return level
        if level:
            logger.warning("bedrock: effort %r no reconocido; usando %s", value, _DEFAULT_EFFORT)
            break
    return _DEFAULT_EFFORT


def _adapt_request(kwargs: Dict[str, Any], effort: Optional[str] = None) -> Dict[str, Any]:
    """The request reshaped for its `modelId` — see `_EFFORT_MODEL_MARKERS`.
    Any other model gets `kwargs` back untouched. Pure: callers keep the
    canonical request so a later fallback to another family starts from it."""
    if not takes_effort(kwargs.get("modelId")):
        return kwargs
    cfg = {k: v for k, v in (kwargs.get("inferenceConfig") or {}).items() if k not in ("temperature", "topP")}
    messages = list(kwargs.get("messages") or [])
    if messages and messages[-1].get("role") == "assistant":
        messages = messages[:-1]
    extra = dict(kwargs.get("additionalModelRequestFields") or {})
    extra["output_config"] = {**(extra.get("output_config") or {}), "effort": effort or effort_for()}
    return {**kwargs, "inferenceConfig": cfg, "messages": messages, "additionalModelRequestFields": extra}


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


def _invoke_with_retry(
    kwargs: Dict[str, Any], max_retries: int, effort: Optional[str] = None
) -> Dict[str, Any]:
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
    kwargs = _adapt_request(kwargs, effort)
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


# Bedrock error codes that mean the MODEL is the problem (down, not granted
# to this account/region, doesn't exist, or broke mid-response) rather than
# "retry me" or "your request needs adjusting". `_RETRYABLE` above already
# retries the SAME model for its three transient codes; once that budget is
# exhausted (or immediately, for the codes `_invoke_with_retry` never retries
# at all) this set decides whether to switch to the next model in
# `model_fallback.get_fallback_models()` instead of failing the whole call.
_MODEL_UNAVAILABLE = _RETRYABLE | {
    "ModelErrorException",
    "ModelTimeoutException",
    "ModelNotSupportedException",
    "ResourceNotFoundException",
    # The model (or the account) is over its own quota for this specific
    # model — retrying the SAME model won't help (quotas don't refill in
    # seconds), but a different model likely has separate headroom.
    "ServiceQuotaExceededException",
    # NOT the cache-related AccessDeniedException — `_invoke_with_retry`
    # already recognizes and swallows that one internally (strips the
    # cachePoint, retries the same model; see `_is_cache_unsupported`). Any
    # AccessDeniedException that reaches here is a genuine one — the model
    # isn't enabled for this account/region — which is exactly "not
    # available" in the sense this fallback exists for.
    "AccessDeniedException",
}
# Deliberately NOT here: ValidationException (a malformed/unsupported
# REQUEST — e.g. bad schema, forced tool-use the model doesn't support — is
# a caller-side problem a different model won't fix, and conflating it with
# a provider outage would mask real bugs) — the max-tokens-ceiling case is
# already special-cased above in `_invoke_with_retry` before this is ever
# consulted.


def _invoke_with_fallback(
    kwargs: Dict[str, Any],
    max_retries: int,
    stage: Optional[str] = None,
    max_fallback_retries: Optional[int] = None,
    fallback_models: Optional[List[str]] = None,
    effort: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """`_invoke_with_retry`, plus: when the model itself is the problem
    (`_MODEL_UNAVAILABLE` — never a request-shape issue like
    `ValidationException`, see the note above that set), switch to the next
    model in `model_fallback.get_fallback_models()` and retry the whole
    request there — up to `max_fallback_retries` switches (default
    `model_fallback.get_max_fallback_retries()`).

    Returns `(response, model_id_actually_used)`: the two can differ from the
    requested `kwargs["modelId"]` after a switch, so callers (`converse`,
    `converse_structured`) report and cost the model that really served the
    request, not just the one they asked for.

    `stage`, when given, keeps two registries in sync with what actually
    happened (both cleared per invocation by `reset_used_models()`):
      - `_USED_MODELS` — `resolve_model` already records the PRE-call intent
        there; this corrects it after the fact if a switch happened, so
        `used_models()` / `tracking.modelIds` / cost audit reflect reality.
      - `_FALLBACK_EVENTS` — set only if a switch actually happened AND the
        call went on to succeed, with the full expected-vs-actual model and
        retry count for `tracking.modelFallbacks` (see `fallback_events()`).
    """
    # `fallback_models`, when given, REPLACES the shared list for this call —
    # `[]` meaning "never switch". A multimodal stage passes its own, since the
    # shared list holds text-only models (see `_MULTIMODAL_STAGES`).
    switch_pool = get_fallback_models() if fallback_models is None else list(fallback_models)
    requested_model = kwargs.get("modelId")
    budget = get_max_fallback_retries() if max_fallback_retries is None else max_fallback_retries
    total_budget = budget
    tried = {requested_model}
    attempts: List[Dict[str, str]] = []
    current = kwargs
    while True:
        try:
            response = _invoke_with_retry(current, max_retries, effort)
            if stage and attempts:
                _FALLBACK_EVENTS[stage] = {
                    "expectedModel": requested_model,
                    "actualModel": current["modelId"],
                    "retries": len(attempts),
                    "attempts": list(attempts),
                }
            return response, current["modelId"]
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            next_model = (
                next((m for m in switch_pool if m not in tried), None)
                if code in _MODEL_UNAVAILABLE and budget > 0
                else None
            )
            if next_model is None:
                if attempts:
                    logger.error(
                        "bedrock: stage=%s giving up after %d fallback switch(es) starting "
                        "from %s (last error on %s: %s)",
                        stage or "-", len(attempts), requested_model, current.get("modelId"), code,
                    )
                raise
            attempts.append({"model": current.get("modelId"), "error": code})
            logger.warning(
                "bedrock: stage=%s %s unavailable (%s); switching to %s "
                "(switch %d/%d, originally requested %s)",
                stage or "-", current.get("modelId"), code, next_model,
                len(attempts), total_budget, requested_model,
            )
            tried.add(next_model)
            current = {**current, "modelId": next_model}
            budget -= 1
            if stage:
                _USED_MODELS[stage] = next_model


def converse(
    prompt: str,
    model_id: Optional[str] = None,
    max_tokens: int = 8192,
    system: Optional[str] = None,
    cache_system: Optional[str] = None,
    max_retries: int = 4,
    prefill: Optional[str] = None,
    stage: Optional[str] = None,
    max_fallback_retries: Optional[int] = None,
    effort: Optional[str] = None,
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
    there instead of choosing its own response shape. Models that reject
    prefill (`takes_effort`) get the request without it; their answer is
    only prefixed with `prefill` when it doesn't already start with it.

    `effort` — see `effort_for`; ignored by models outside
    `_EFFORT_MODEL_MARKERS`. Defaults to `effort_for(stage)`.

    `stage` ("segmenter"/"extractor"/"recovery") lets a model-unavailable
    fallback (see `_invoke_with_fallback`) correct `_USED_MODELS` for that
    stage when it switches models mid-call; omit it for ad hoc calls that
    don't correspond to one of the pipeline's own stages. `max_fallback_retries`
    overrides `model_fallback.get_max_fallback_retries()` for this call only
    (mainly for tests/scripts); leave it unset to use the configured budget.

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

    response, used_model_id = _invoke_with_fallback(
        kwargs, max_retries, stage=stage, max_fallback_retries=max_fallback_retries,
        effort=effort or effort_for(stage),
    )

    output = response.get("output", {}).get("message", {})
    # Only `text` blocks: a thinking model's `reasoningContent` blocks are skipped.
    text = "\n".join(b.get("text", "") for b in output.get("content", []) if "text" in b).strip()
    if prefill and not (takes_effort(used_model_id) and text.startswith(prefill)):
        text = prefill + text
    meta = {
        "modelId": used_model_id,
        "stopReason": response.get("stopReason"),
        "usage": response.get("usage", {}),
    }
    _record_prompt(
        stage,
        model_id,
        used_model_id,
        input_payload={
            "system": system,
            "cacheSystem": cache_system,
            "prompt": prompt,
            "prefill": prefill,
            "maxTokens": max_tokens,
        },
        output_payload={"text": text, "stopReason": meta["stopReason"], "usage": meta["usage"]},
    )
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
    stage: Optional[str] = None,
    max_fallback_retries: Optional[int] = None,
    images: Optional[List[Dict[str, Any]]] = None,
    fallback_models: Optional[List[str]] = None,
    effort: Optional[str] = None,
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

    `stage`, `effort` — see `converse()`'s docstring; same purpose here.
    Forced `toolChoice` is kept for every model: Claude Haiku 5.5 accepts it
    and answers with the tool call directly, skipping thinking.

    `images` — optional `[{"label", "format" ("jpeg"|"png"), "bytes"}]` sent
    BEFORE the prompt, each preceded by its label as a text block so the
    prompt can refer to them by name. Only a multimodal stage passes them, and
    it must pass `fallback_models` too (its own vision-capable list, or `[]`):
    the shared fallback list holds text-only models.
    """
    model_id = model_id or base_model_id()
    content: List[Dict[str, Any]] = []
    for image in images or []:
        content.append({"text": str(image.get("label") or "imagen")})
        content.append({"image": {"format": image.get("format") or "jpeg",
                                  "source": {"bytes": image["bytes"]}}})
    content.append({"text": prompt})
    kwargs: Dict[str, Any] = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": content}],
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

    response, used_model_id = _invoke_with_fallback(
        kwargs, max_retries, stage=stage, max_fallback_retries=max_fallback_retries,
        fallback_models=fallback_models, effort=effort or effort_for(stage),
    )

    output = response.get("output", {}).get("message", {})
    tool_input: Dict[str, Any] = {}
    for block in output.get("content", []):
        if "toolUse" in block:
            tool_input = block["toolUse"].get("input") or {}
            break
    meta = {
        "modelId": used_model_id,
        "stopReason": response.get("stopReason"),
        "usage": response.get("usage", {}),
    }
    _record_prompt(
        stage,
        model_id,
        used_model_id,
        input_payload={
            "system": system,
            "cacheSystem": cache_system,
            "prompt": prompt,
            **({"images": [_image_digest(i) for i in images]} if images else {}),
            "maxTokens": max_tokens,
            "toolName": tool_name,
            "toolDescription": tool_description,
            "toolSchema": tool_schema,
        },
        output_payload={"toolInput": tool_input, "stopReason": meta["stopReason"], "usage": meta["usage"]},
    )
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
