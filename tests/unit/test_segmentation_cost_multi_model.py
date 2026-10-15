"""Windowed segmentation (or a caller-level retry on a degraded response —
see `pipeline/segmenter.py::_classify_pages`/`_classify_windowed`) can leave
`usage.segmentation` with calls spread across more than one Bedrock model in
the SAME invocation. `pipeline/segmenter.py::_aggregate_usage` then emits
`perModel` alongside the usual single `modelId`/`usage` shape, and
`cost_audit.py::_segmentation_cost` must price each part at ITS OWN model's
rate — the exact mistake `documentacion/COSTOS.md` already documents once,
from a different cause (a single-`modelId` assumption meeting a
`model_fallback` switch it didn't know about): every MiniMax-run got costed
at Haiku's rate until 2026-09-04 (see `tests/unit/test_pricing_coverage.py`).
"""

import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import cost_audit  # noqa: E402
from pipeline import pricing  # noqa: E402

MINIMAX = "minimax.minimax-m2.5"
HAIKU = "us.anthropic.claude-haiku-5-5"


def _breakdown(segmentation):
    result = {
        "usage": {"segmentation": segmentation},
        "pages": {"totalPages": 1, "nativePages": 0, "ocrPages": 1, "emptyPages": 0},
    }
    return cost_audit.build_cost_breakdown(result, {}, 1000.0)


def test_sin_perModel_se_comporta_igual_que_antes():
    """Regresión: el caso común (un solo modelo) no cambia de forma."""
    breakdown = _breakdown({
        "modelId": MINIMAX,
        "usage": {"inputTokens": 1000, "outputTokens": 500},
    })
    expected = pricing.bedrock_cost({"inputTokens": 1000, "outputTokens": 500}, MINIMAX)
    assert breakdown["services"]["BedrockSegmentation"] == expected


def test_con_perModel_cada_parte_se_cobra_a_su_propia_tarifa():
    segmentation = {
        "modelId": f"{MINIMAX},{HAIKU}",
        "usage": {"inputTokens": 3000, "outputTokens": 1500},
        "perModel": [
            {"modelId": MINIMAX, "usage": {"inputTokens": 2000, "outputTokens": 1000}},
            {"modelId": HAIKU, "usage": {"inputTokens": 1000, "outputTokens": 500}},
        ],
    }
    breakdown = _breakdown(segmentation)
    cost = breakdown["services"]["BedrockSegmentation"]

    minimax_cost = pricing.bedrock_cost({"inputTokens": 2000, "outputTokens": 1000}, MINIMAX)
    haiku_cost = pricing.bedrock_cost({"inputTokens": 1000, "outputTokens": 500}, HAIKU)

    assert cost["costUsd"] == round(minimax_cost["costUsd"] + haiku_cost["costUsd"], 6)
    # La tarifa de un modelo NUNCA se aplica a los tokens del otro: si la
    # implementación regresara a `bedrock_cost(usage_total, modelId_combinado)`,
    # el modelo combinado ("minimax...,haiku...") no estaría en la tabla de
    # tarifas y el resultado saldría marcado approximate con la tarifa
    # default — deliberadamente distinto de este total exacto.
    assert cost["costUsd"] != pricing.bedrock_cost(segmentation["usage"], segmentation["modelId"])["costUsd"]
    assert cost["inputTokens"] == 3000
    assert cost["outputTokens"] == 1500
    assert len(cost["perModel"]) == 2


def test_perModel_marca_approximate_si_alguna_parte_no_tiene_tarifa_propia():
    segmentation = {
        "modelId": f"{MINIMAX},otro.modelo-sin-tarifa",
        "usage": {"inputTokens": 100, "outputTokens": 50},
        "perModel": [
            {"modelId": MINIMAX, "usage": {"inputTokens": 80, "outputTokens": 40}},
            {"modelId": "otro.modelo-sin-tarifa", "usage": {"inputTokens": 20, "outputTokens": 10}},
        ],
    }
    cost = _breakdown(segmentation)["services"]["BedrockSegmentation"]
    assert cost.get("approximatePricing") is True
