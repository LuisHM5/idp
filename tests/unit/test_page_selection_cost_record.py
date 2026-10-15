"""What page selection saved must survive all the way into the cost table.

The numbers are useless if they only exist in the in-memory result: the point
of persisting them is to answer "how much did selection actually save us"
weeks later, from `pnpm costos`. So this guards the whole chain — segment ->
document roll-up -> cost breakdown -> DynamoDB item — and specifically the two
ways it has silently broken before:

- a field getting dropped in the per-document roll-up (chars/chunks did);
- a float sneaking into the item, which boto3's resource API rejects at write
  time, i.e. in production rather than here.
"""

import sys
from decimal import Decimal
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import cost_audit  # noqa: E402
from pipeline.expediente import _page_selection_summary  # noqa: E402

# One filtered document, shaped exactly like `Selection.to_json()` produces.
DOC = {
    "documentType": "PEDIMENTO_ADUANAL",
    "pageSelection": {
        "pagesConsidered": 86,
        "selected": list(range(4, 20)),
        "skipped": [n for n in range(1, 87) if not 4 <= n <= 19],
        "failOpen": False,
        "reason": "16/86 páginas con score>=3",
        "scores": [],
        "savings": {
            "charsSent": 50109,
            "charsSkipped": 105096,
            "billedInputTokens": 18719,
            "billedOutputTokens": 1800,
            "chunksSent": 2,
            "estimatedChunksWithoutSelection": 6,
            "estimatedInputTokensSaved": 39260,
            "estimatedInputTokensWithoutSelection": 57979,
            "estimatedInputSavingPct": 67.7,
            "estimatedOutputTokensSaved": 3600,
            "estimatedOutputTokensWithoutSelection": 5400,
        },
    },
}


def _breakdown(documents):
    # Envelope v2: `usage`/`pages` viven en la raíz, ya no dentro de `expediente`.
    result = {
        "documentId": "doc-1",
        "usage": {
            "extraction": {
                "modelId": "us.anthropic.claude-haiku-5-5",
                "usage": {"inputTokens": 18719, "outputTokens": 1800},
            },
            "pageSelection": _page_selection_summary(documents),
        },
        "pages": {"totalPages": 86, "nativePages": 67, "ocrPages": 19, "emptyPages": 0},
    }
    return cost_audit.build_cost_breakdown(result, {}, 12000.0)


def test_every_savings_field_survives_the_roll_up():
    """Nothing measured per segment may be lost on the way to the record —
    chars and chunks especially, since they're what the token estimates are
    DERIVED from and the only way to re-check a stored number later."""
    summary = _page_selection_summary([DOC])
    for name in (
        "charsSent", "charsSkipped", "chunksSent", "estimatedChunksWithoutSelection",
        "billedInputTokens", "billedOutputTokens",
        "estimatedInputTokensSaved", "estimatedOutputTokensSaved",
        "estimatedInputSavingPct",
    ):
        assert name in summary, f"{name} se perdió en el agregado"
    assert summary["pagesSkipped"] == 70
    assert summary["pagesExtracted"] == 16


def test_two_filtered_documents_add_up():
    summary = _page_selection_summary([DOC, DOC])
    assert summary["documentsFiltered"] == 2
    assert summary["charsSkipped"] == 105096 * 2
    assert summary["estimatedInputTokensSaved"] == 39260 * 2


def test_absent_rather_than_zeroed_when_nothing_was_filtered():
    assert _page_selection_summary([{"documentType": "FACTURA"}]) is None
    assert "pageSelection" not in _breakdown([{"documentType": "FACTURA"}])


def test_cost_avoided_prices_output_too():
    """Output is billed 5x input, so pricing only the input saving would
    understate what selection avoided by most of its value."""
    breakdown = _breakdown([DOC])
    avoided = breakdown["pageSelection"]["estimatedCostAvoidedUsd"]
    input_only = 39260 / 1_000_000 * 0.11  # Claude Haiku 5.5, la tarifa por defecto
    assert avoided > input_only * 1.4


def test_savings_stay_out_of_the_billed_total():
    """`estimatedCostAvoidedUsd` prices a call nobody made. If it ever leaked
    into totalCostUsd the cost table would overstate real spend."""
    breakdown = _breakdown([DOC])
    billed = sum(s["costUsd"] for s in breakdown["services"].values())
    assert breakdown["totalCostUsd"] == round(billed, 6)
    assert "pageSelection" not in breakdown["services"]


def test_the_item_is_writable_by_boto3():
    """boto3's resource API rejects a raw float ANYWHERE in the item, and it
    does so at write time — so catch it here instead of in CloudWatch."""
    from boto3.dynamodb.types import TypeSerializer

    breakdown = _breakdown([DOC])
    item = cost_audit._decimalize(breakdown["pageSelection"])

    def floats(node, path=""):
        if isinstance(node, float):
            return [path]
        if isinstance(node, dict):
            return [p for k, v in node.items() for p in floats(v, f"{path}.{k}")]
        if isinstance(node, list):
            return [p for i, v in enumerate(node) for p in floats(v, f"{path}[{i}]")]
        return []

    assert floats(item) == []
    assert TypeSerializer().serialize(item)["M"]["charsSkipped"] == {"N": "105096"}
    assert isinstance(item["estimatedInputSavingPct"], Decimal)


def test_extraction_strategy_and_deterministic_savings_are_audited_separately():
    result = {
        "documentId": "cert-1",
        "aiExecuted": False,
        "aiUsage": {"executed": False, "stages": []},
        "documents": [{
            "index": 1,
            "documentType": "CERTIFICADO_POLIZA",
            "extractionMode": {"requested": "catalog", "effective": "hybrid"},
            "deterministicExtraction": {
                "strategy": "deterministic",
                "resolvedFieldCount": 14,
                "fallbackFields": [],
                "profile": {"id": "GMM", "status": "matched"},
                "tokenSavings": {
                    "estimatedInputTokensSaved": 5000,
                    "estimatedOutputTokensSaved": 900,
                    "counterfactualModelId": "minimax.minimax-m2.5",
                },
            },
        }],
        "usage": {"extraction": {"modelId": None, "usage": {}}},
        "pages": {"totalPages": 5, "nativePages": 5, "ocrPages": 0, "emptyPages": 0},
    }

    breakdown = cost_audit.build_cost_breakdown(result, {}, 2500.0)
    audit = breakdown["extractionAudit"]

    assert audit["AiExecuted"] is False
    assert audit["Documents"][0]["Profile"] == "GMM"
    assert audit["Documents"][0]["Strategy"] == "deterministic"
    assert audit["DeterministicSavings"]["EstimatedInputTokensSaved"] == 5000
    assert audit["DeterministicSavings"]["EstimatedCostAvoidedUsd"] > 0
    assert breakdown["totalCostUsd"] == round(
        sum(service["costUsd"] for service in breakdown["services"].values()), 6
    )


def test_extraction_audit_is_written_to_the_dedicated_cost_table(monkeypatch):
    import boto3

    captured = {}

    class Table:
        def put_item(self, *, Item):
            captured["item"] = Item

    class Dynamo:
        def Table(self, name):
            captured["table"] = name
            return Table()

    monkeypatch.setattr(cost_audit, "_COST_TABLE", "document-validation-costs-dev")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    result = {
        "documentId": "cert-2",
        "documentClassification": "CERTIFICADO_POLIZA",
        "aiExecuted": False,
        "aiUsage": {"stages": []},
        "aiResponseSavings": {
            "estimatedInputTokensSaved": 10,
            "estimatedOutputTokensSaved": 20,
        },
        "documents": [{
            "index": 1,
            "documentType": "CERTIFICADO_POLIZA",
            "extractionMode": {"requested": "catalog", "effective": "hybrid"},
            "aiResponseFormat": {
                "selected": "compact", "effective": "not_executed",
                "source": "default", "fallbackExecuted": False,
                "compactCalls": 0, "legacyCalls": 0,
            },
            "deterministicExtraction": {
                "strategy": "deterministic",
                "resolvedFieldCount": 12,
                "fallbackFields": [],
                "profile": {"id": "SV", "status": "matched"},
                "tokenSavings": {"counterfactualModelId": "minimax.minimax-m2.5"},
            },
        }],
        "usage": {},
        "pages": {"totalPages": 3, "nativePages": 3, "ocrPages": 0, "emptyPages": 0},
    }

    item = cost_audit.write_cost_record(
        result, {"pipelineRoute": "enrichment-only"}, 1200.0
    )

    assert captured["table"] == "document-validation-costs-dev"
    assert captured["item"] is item
    assert item["ExtractionAudit"]["AiExecuted"] is False
    assert item["ExtractionAudit"]["Documents"][0]["Profile"] == "SV"
    assert item["ExtractionAudit"]["Documents"][0]["AiResponseSelected"] == "compact"
    assert item["ExtractionAudit"]["AiResponseSavings"]["EstimatedCostAvoidedUsd"] >= 0
    assert item["Services"]["BedrockExtraction"]["costUsd"] == Decimal("0.0")
    assert item["PipelineRoute"] == "enrichment-only"
