"""Ordered Bedrock model fallback: which model to try next when the one in
use turns out to be unavailable or broken, and how many times to switch
before giving up.

Two independent knobs, both hot-reloadable without a redeploy:
  - the ORDER (which models, in which priority) — DynamoDB table named by
    `FALLBACK_MODELS_TABLE`, falling back to `DEFAULT_FALLBACK_MODELS` when
    the table is unset, empty, or unreachable, so this feature never becomes
    a hard dependency on DynamoDB being up.
  - the BUDGET (how many model switches a single request may make) —
    `MODEL_FALLBACK_MAX_RETRIES` env var, default `DEFAULT_MAX_FALLBACK_RETRIES`.

See `pipeline/bedrock.py::_invoke_with_fallback` for where this actually gets
consulted, and `catalogs/README.md` -> "Entry de nivel de tipo" for how this
relates to a document type's own `model_id`/`model_ids`: two independent
concerns — the catalog decides what to try FIRST, this module decides what to
try NEXT after a failure. A type's catalog override is never treated as a
fallback candidate and never appears in this list.
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

# Seed order AND the ultimate fallback-of-the-fallback when the DynamoDB
# table is unset, empty, or unreachable. Haiku last as the
# best-verified-compatible safety net — before reordering this, check the
# candidate offers what the calling stage needs (Converse, forced tool use
# for the extractor, prompt caching) per AGENTS.md's model notes; an
# incompatible model fails differently (a bad/empty extraction) rather than
# with a retryable error, so it won't get skipped by this mechanism.
DEFAULT_FALLBACK_MODELS: List[str] = [
    "minimax.minimax-m2.5",
    "moonshotai.kimi-k2.5",
    "zai.glm-5",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
]

DEFAULT_MAX_FALLBACK_RETRIES = 4

_FALLBACK_CACHE: Optional[List[str]] = None
_FALLBACK_CACHE_LOADED_AT: Optional[float] = None


def _load_from_dynamodb() -> List[str]:
    """Table schema:
      PK: `Rank` (N) — ascending priority, lowest tried first.
      `ModelId` (S) — the Bedrock model id or inference-profile ARN.
      `Enabled` (BOOL, optional, default true) — lets an entry be paused
        without deleting/renumbering the rest of the table.

    Returns [] (never raises) on any failure — the table not existing yet or
    a transient DynamoDB error must not take down the pipeline's own error
    handling; `get_fallback_models()` falls back to the hardcoded default.
    """
    table_name = os.environ.get("FALLBACK_MODELS_TABLE")
    if not table_name:
        return []
    try:
        import aws_clients  # deferred: keep this module importable without boto3 in unit tests

        table = aws_clients.table(table_name)
        response = table.scan()
        items = list(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))

        ordered = sorted(
            (item for item in items if item.get("Enabled", True) and item.get("ModelId")),
            key=lambda item: item.get("Rank", 0),
        )
        return [str(item["ModelId"]) for item in ordered]
    except Exception as e:
        logger.error("model_fallback: failed to load %s: %s", table_name, e)
        return []


def get_fallback_models() -> List[str]:
    """Ordered fallback list, highest priority (tried first) at index 0.

    Cached per Lambda container after the first call; use
    `reload_fallback_models()` after editing the table to pick up changes
    without a redeploy (mirrors `document_catalog.reload_catalog`).
    """
    global _FALLBACK_CACHE, _FALLBACK_CACHE_LOADED_AT
    try:
        ttl = max(
            0.0,
            float(os.environ.get("FALLBACK_MODELS_CACHE_TTL_SECONDS", "30")),
        )
    except ValueError:
        ttl = 30.0
    now = time.monotonic()
    if _FALLBACK_CACHE is not None and (
        # Keep the same injection seam as document_catalog: unit tests and
        # local tools may install an explicit in-memory value without a
        # timestamp and expect it to remain authoritative.
        _FALLBACK_CACHE_LOADED_AT is None
        or ttl <= 0
        or now - _FALLBACK_CACHE_LOADED_AT < ttl
    ):
        return _FALLBACK_CACHE
    models = _load_from_dynamodb() or list(DEFAULT_FALLBACK_MODELS)
    _FALLBACK_CACHE = models
    _FALLBACK_CACHE_LOADED_AT = now
    return models


def reload_fallback_models() -> None:
    """Force the next `get_fallback_models()` call to re-read DynamoDB."""
    global _FALLBACK_CACHE, _FALLBACK_CACHE_LOADED_AT
    _FALLBACK_CACHE = None
    _FALLBACK_CACHE_LOADED_AT = None


def get_max_fallback_retries() -> int:
    """How many model SWITCHES a single request may make before giving up.

    Read at call time (not import time) so a test or a script can change the
    environment between calls. Independent of the `max_retries` param on
    `converse()`/`converse_structured()`, which governs same-model retries
    (throttling, transient unavailability) before a switch is even
    considered — see `pipeline/bedrock.py::_invoke_with_fallback`.
    """
    raw = os.environ.get("MODEL_FALLBACK_MAX_RETRIES")
    if not raw:
        return DEFAULT_MAX_FALLBACK_RETRIES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "model_fallback: invalid MODEL_FALLBACK_MAX_RETRIES=%r, using default %d",
            raw,
            DEFAULT_MAX_FALLBACK_RETRIES,
        )
        return DEFAULT_MAX_FALLBACK_RETRIES
