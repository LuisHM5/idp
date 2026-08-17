"""Per-document AWS cost audit trail.

Every document processed writes one item to a dedicated DynamoDB table
(`COST_TABLE`, separate from the tracking table `DOCUMENT_TABLE` — this is an
audit/finance concern, not a document-query concern) with a full $ breakdown
per AWS service actually used: Bedrock (segmentation, extraction, and the
upstream classification/processing steps that run in the model-invoke
Lambda), Textract OCR, and an approximate Lambda compute cost. See
`pipeline/pricing.py` for the rates and `documentacion/COSTOS.md` for how they
were verified against real Cost Explorer spend.

Not tracked per-document, by design: DynamoDB/S3/SQS/KMS/CloudWatch/API
Gateway. None of those are attributable to a single invocation from inside
the Lambda itself (no per-call token/page count the way Bedrock/Textract
give you), and COSTOS.md's measured data shows they are collectively noise
next to Bedrock/Textract at this project's volume (~$3.38 over two weeks vs.
~$18 of Bedrock+Textract in the same window) — computing a fake per-document
split of that would be manufactured precision, not a measurement.

The write is best-effort: an audit failure must never fail validation, same
contract as `indexing.py`.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from indexing import display_name_for
from pipeline.pricing import PRICING_VERSION, bedrock_cost, lambda_cost, textract_cost

logger = logging.getLogger(__name__)

_COST_TABLE = os.environ.get("COST_TABLE")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimalize(value: Any) -> Any:
    """Recursively convert every float to Decimal — see indexing.py's twin
    of this function for why (boto3's DynamoDB resource API rejects floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


def _upstream_bedrock_cost(event: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Cost of a blueprint-side step (classification/processing) that ran in
    the model-invoke Lambda, from the usage it now forwards through the Step
    Functions payload. Zero-cost (not absent) when the step was bypassed
    (manual classification, extraction deferred to the enrichment pipeline,
    large-document guard) — those return `usage: {}` themselves."""
    phase = event.get(key)
    if not isinstance(phase, dict):
        phase = {}
    return bedrock_cost(phase.get("usage"), phase.get("modelId"))


def _expediente_pages(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pages = (result.get("expediente") or {}).get("pages")
    if not isinstance(pages, dict) or not pages.get("totalPages"):
        return None
    return {
        "Total": int(pages.get("totalPages") or 0),
        "Native": int(pages.get("nativePages") or 0),
        "Ocr": int(pages.get("ocrPages") or 0),
        "Empty": int(pages.get("emptyPages") or 0),
    }


def build_cost_breakdown(
    result: Dict[str, Any], event: Dict[str, Any], duration_ms: float
) -> Dict[str, Any]:
    """Full per-service $ breakdown for one enrichment Lambda invocation."""
    usage = (result.get("expediente") or {}).get("usage") or {}
    segmentation = usage.get("segmentation") or {}
    extraction = usage.get("extraction") or {}
    pages = _expediente_pages(result)
    ocr_pages = pages["Ocr"] if pages else 0

    services = {
        "BedrockClassificationUpstream": _upstream_bedrock_cost(event, "classificationResult"),
        "BedrockProcessingUpstream": _upstream_bedrock_cost(event, "processingResult"),
        "BedrockSegmentation": bedrock_cost(segmentation.get("usage"), segmentation.get("modelId")),
        "BedrockExtraction": bedrock_cost(extraction.get("usage"), extraction.get("modelId")),
        "TextractOcr": textract_cost(ocr_pages),
        "LambdaCompute": lambda_cost(
            duration_ms, os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
        ),
    }
    total = round(sum(s["costUsd"] for s in services.values()), 6)
    # Every Bedrock line item (the only services with token counts) summed
    # across the four phases — so "how many tokens did this document cost in
    # total" doesn't require a caller to know which services carry tokens.
    total_input_tokens = sum(s.get("inputTokens", 0) for s in services.values())
    total_output_tokens = sum(s.get("outputTokens", 0) for s in services.values())

    breakdown: Dict[str, Any] = {
        "services": services,
        "totalCostUsd": total,
        "totalInputTokens": total_input_tokens,
        "totalOutputTokens": total_output_tokens,
        "pricingVersion": PRICING_VERSION,
    }
    if pages:
        breakdown["pages"] = pages
        if pages["Total"] > 0:
            breakdown["costPerPageUsd"] = round(total / pages["Total"], 6)
    return breakdown


def write_cost_record(
    result: Dict[str, Any],
    event: Dict[str, Any],
    duration_ms: float,
    source_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist the cost breakdown for this invocation. Best-effort — returns
    the item written, or None when there is nothing to write (no table
    configured, no document id) or the write failed."""
    document_id = result.get("documentId")
    if not _COST_TABLE or not document_id or document_id == "unknown":
        return None

    breakdown = build_cost_breakdown(result, event, duration_ms)
    classification = str(result.get("documentClassification") or "OTRO").strip().upper() or "OTRO"
    source = source_key or result.get("sourceFile") or ""

    item: Dict[str, Any] = {
        "DocumentId": document_id,
        "DocumentType": classification,
        "DocumentTypeName": display_name_for(classification),
        "SourceFile": str(source).rsplit("/", 1)[-1],
        "ProcessedAt": str(result.get("processedAt") or _now()),
        "CreatedAt": _now(),
        "Services": {
            name: _decimalize(svc) for name, svc in breakdown["services"].items()
        },
        "TotalCostUsd": Decimal(str(breakdown["totalCostUsd"])),
        "TotalInputTokens": breakdown["totalInputTokens"],
        "TotalOutputTokens": breakdown["totalOutputTokens"],
        "PricingVersion": breakdown["pricingVersion"],
    }
    if event.get("appOrigin"):
        item["AppOrigin"] = str(event["appOrigin"])
    if "pages" in breakdown:
        item["Pages"] = breakdown["pages"]
    if "costPerPageUsd" in breakdown:
        item["CostPerPageUsd"] = Decimal(str(breakdown["costPerPageUsd"]))

    try:
        import boto3  # deferred: the Lambda runtime provides it

        boto3.resource("dynamodb").Table(_COST_TABLE).put_item(Item=item)
        return item
    except Exception as e:  # noqa: BLE001 - cost audit must never fail validation
        logger.error("failed to write cost record for %s: %s", document_id, e)
        return None
