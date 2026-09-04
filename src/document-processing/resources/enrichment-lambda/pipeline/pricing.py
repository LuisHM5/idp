"""Real AWS pricing for per-document cost tracking.

Every rate here traces back to a live source, not a guess: Bedrock/Textract
were verified against actual Cost Explorer spend (token-for-token, to the
cent — see `documentacion/COSTOS.md` section 1), and Lambda came straight from
the AWS Pricing API. There is no live pricing lookup at runtime (that would
add a network call + IAM permission to every invocation just to fetch a
number that only changes when AWS reprices something) — if this pipeline ever
runs outside us-east-1, these numbers stop being accurate and need re-pulling.

`PRICING_VERSION` is stamped onto every cost record this module prices, so a
future repricing doesn't silently make old records incomparable to new ones.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Se persiste por documento (`cost_audit` -> columna `PricingVersion`, visible
# en `pnpm costos`), así que es lo que permite saber CON QUÉ TARIFAS se calculó
# un registro viejo. Súbela cuando cambie cualquier número de las tablas de
# abajo — incluido agregar un modelo, porque eso cambia el costo reportado de
# los documentos que ya corrieron con él.
PRICING_VERSION = "2026-09-04"

# $ per MILLION tokens, Bedrock Converse, on-demand STANDARD tier, us-east-1.
# An unrecognized model falls back to the Haiku 4.5 rates with
# `approximatePricing: true` on the returned breakdown, so a model that isn't
# listed here never mis-prices silently.
#
# ⚠️ Every model this pipeline can actually run has to be listed, not just the
# configured one: `pipeline.model_fallback.DEFAULT_FALLBACK_MODELS` switches
# models on its own when one is unavailable. Until 2026-09-04 this table held
# ONLY Haiku while MiniMax was first in that list — so every cost record was
# computed at Haiku's rates whatever actually ran, over-reporting a real
# 31-page expediente as $0.143 when it cost $0.070. If you add a model to the
# fallback list or to BEDROCK_*_MODEL_ID, add it here in the same change.
#
# Rates below verified 2026-09-04 against the AWS Price List API, us-east-1,
# `service_tier: standard`, all effectiveDate 2026-08-01.
#
# ⚠️ **Los modelos NO viven todos bajo el mismo service code**, y esto cuesta
# media hora de confusión si no está escrito:
#
#   AmazonBedrock         -> MiniMax, Kimi, GLM (terceros). De Anthropic solo
#                            tiene los LEGACY (Claude 2.0/2.1/Instant/3 Haiku/
#                            3 Sonnet) y, para ésos, únicamente SKUs de INPUT:
#                            preguntar por output de Anthropic aquí no devuelve
#                            nada, y parece que el precio no existiera.
#   AmazonBedrockService  -> los modelos ACTUALES de Anthropic (Claude Haiku 4.5,
#                            Sonnet 4.5, Sonnet 4), con input, output y los dos
#                            de caché. No tiene ninguno de los de terceros.
#
# O sea que un script que refresque tarifas tiene que consultar LOS DOS. Además,
# en `AmazonBedrockService` el `inferenceType` viene vacío: lo que distingue
# input de output es el `usagetype`
# (`USE1-anthropic.claude-haiku-4-5-mantle-{input,output}-tokens-standard`).
#
# Nota sobre `cacheWrite`: Haiku publica DOS tarifas de escritura de caché,
# $1.375/M (`mantle-cache-write-tokens-standard`, TTL default de 5 min) y
# $2.20/M (`...-1h-standard`, TTL de una hora). Aquí va la de 5 min porque es la
# que el pipeline usa — `bedrock.py` manda `{"cachePoint": {"type": "default"}}`
# y nunca `"1h"`. Si alguna vez se cambia ese TTL, esta tarifa se queda corta un
# 60%.
#
# `cacheRead`/`cacheWrite` for the three non-Anthropic models: Bedrock lists NO
# cache SKU for them and none supports prompt caching (verified, see
# models_pricing.md), so the pipeline never sends cache tokens and these should
# stay at 0. They are priced at the input rate rather than 0.0 on purpose — if
# cache tokens ever do show up, the number over-reports instead of vanishing.
_BEDROCK_PRICING: Dict[str, Dict[str, float]] = {
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": 1.10,
        "output": 5.50,
        "cacheRead": 0.11,
        "cacheWrite": 1.375,
    },
    # USE1-minimax.minimax-m2.5-mantle-{input,output}-tokens-standard
    "minimax.minimax-m2.5": {
        "input": 0.30,
        "output": 1.20,
        "cacheRead": 0.30,
        "cacheWrite": 0.30,
    },
    # USE1-moonshotai.kimi-k2.5-mantle-{input,output}-tokens-standard
    "moonshotai.kimi-k2.5": {
        "input": 0.60,
        "output": 3.00,
        "cacheRead": 0.60,
        "cacheWrite": 0.60,
    },
    # USE1-zai.glm5-mantle-{input,output}-tokens-standard. Note the SKU spells
    # it `glm5` while the Bedrock model id is `zai.glm-5`; the key here is the
    # MODEL ID, which is what `bedrock_cost` is called with.
    "zai.glm-5": {
        "input": 1.00,
        "output": 3.20,
        "cacheRead": 1.00,
        "cacheWrite": 1.00,
    },
}
_DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Textract DetectDocumentText (Sync), per page — verified exact against Cost
# Explorer (COSTOS.md section 1).
_TEXTRACT_SYNC_PAGE = 0.0015

# AWS Lambda, us-east-1 on-demand, Tier-1 (AWS Pricing API, pulled 2026-08-05
# — this pipeline's volume never reaches Tier 2/3 pricing breakpoints).
_LAMBDA_GB_SECOND = 0.0000166667
_LAMBDA_REQUEST = 0.0000002


def bedrock_cost(usage: Optional[Dict[str, Any]], model_id: Optional[str]) -> Dict[str, Any]:
    """$ breakdown for one Bedrock Converse `usage` dict.

    `usage` is the raw Converse `response["usage"]` shape (`inputTokens`/
    `outputTokens`/`cacheReadInputTokens`/`cacheWriteInputTokens` — the cache
    fields are absent whenever caching wasn't used for that call). Always
    returns zeroed numeric fields (never None) so callers can unconditionally
    add several of these into a running total.
    """
    usage = usage or {}
    rates = _BEDROCK_PRICING.get(model_id or "", _BEDROCK_PRICING[_DEFAULT_BEDROCK_MODEL])
    input_tok = int(usage.get("inputTokens", 0) or 0)
    output_tok = int(usage.get("outputTokens", 0) or 0)
    cache_read = int(usage.get("cacheReadInputTokens", 0) or 0)
    cache_write = int(usage.get("cacheWriteInputTokens", 0) or 0)
    # Only flag "approximate" when it's actually informative: a $0 line item
    # (no call made — bypassed step, or a segment that never ran) is exact
    # regardless of which rate table it would have used.
    has_usage = input_tok or output_tok or cache_read or cache_write
    approximate = has_usage and (model_id or "") not in _BEDROCK_PRICING
    cost = (
        input_tok / 1_000_000 * rates["input"]
        + output_tok / 1_000_000 * rates["output"]
        + cache_read / 1_000_000 * rates["cacheRead"]
        + cache_write / 1_000_000 * rates["cacheWrite"]
    )
    return {
        "modelId": model_id,
        "inputTokens": input_tok,
        "outputTokens": output_tok,
        "cacheReadTokens": cache_read,
        "cacheWriteTokens": cache_write,
        # The $/M-token rates actually applied, so a caller (e.g. a saved
        # per-document summary) can show "this is what input/output tokens
        # cost at the time this ran" without re-deriving it from modelId —
        # useful once PRICING_VERSION moves on and the live table no longer
        # matches what an old record was billed at.
        "inputRatePerMTokUsd": rates["input"],
        "outputRatePerMTokUsd": rates["output"],
        "cacheReadRatePerMTokUsd": rates["cacheRead"],
        "cacheWriteRatePerMTokUsd": rates["cacheWrite"],
        "pricingVersion": PRICING_VERSION,
        "costUsd": round(cost, 6),
        **({"approximatePricing": True} if approximate else {}),
    }


def textract_cost(pages: int) -> Dict[str, Any]:
    """$ breakdown for `pages` synchronous Textract `DetectDocumentText` calls."""
    pages = max(0, int(pages or 0))
    return {
        "pages": pages,
        "ratePerPageUsd": _TEXTRACT_SYNC_PAGE,
        "pricingVersion": PRICING_VERSION,
        "costUsd": round(pages * _TEXTRACT_SYNC_PAGE, 6),
    }


def lambda_cost(duration_ms: float, memory_mb: int) -> Dict[str, Any]:
    """Approximate Lambda compute cost for one invocation.

    `duration_ms` should be the caller's best estimate of billed duration.
    This pipeline uses the processing-phase wall clock (`tracking.processingMs`)
    as that estimate, which slightly undercounts the real billed duration (it
    excludes the S3 download that precedes it and the DynamoDB/S3 writes that
    follow) — the only duration measurable from inside the invocation before
    it has finished. `memory_mb` should come from the Lambda-provided
    `AWS_LAMBDA_FUNCTION_MEMORY_SIZE` env var. Always marked `approximate` in
    the result so a reader never mistakes it for a billed-duration figure.
    """
    duration_ms = max(0.0, float(duration_ms or 0.0))
    memory_mb = max(0, int(memory_mb or 0))
    gb_seconds = (memory_mb / 1024.0) * (duration_ms / 1000.0)
    cost = gb_seconds * _LAMBDA_GB_SECOND + _LAMBDA_REQUEST
    return {
        "durationMs": round(duration_ms, 2),
        "memoryMb": memory_mb,
        "costUsd": round(cost, 6),
        "approximate": True,
    }


def merge_bedrock_usage(*usages: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Sum several raw Converse `usage` dicts into one (int fields only)."""
    merged: Dict[str, int] = {}
    for usage in usages:
        for k, v in (usage or {}).items():
            if isinstance(v, int):
                merged[k] = merged.get(k, 0) + v
    return merged
