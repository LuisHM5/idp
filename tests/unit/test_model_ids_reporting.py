"""The response stored in DynamoDB must name the model used by each phase.

Phases run in two different Lambdas: classification and processing in the
model-invoke Lambda (which reports its model back through the Step Functions
payload), and the expediente phases here. Both have to end up in one map, and
a phase that did not run must not appear — reporting a configured-but-unused
model is the same class of lie as the `"default"` placeholder this replaced.
"""

import sys
from pathlib import Path

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import handler  # noqa: E402
from pipeline import bedrock  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in ("BEDROCK_MODEL_ID", "BEDROCK_EXTRACTOR_MODEL_ID",
                "BEDROCK_SEGMENTER_MODEL_ID", "BEDROCK_RECOVERY_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    bedrock.reset_used_models()
    yield
    bedrock.reset_used_models()


def test_reports_only_phases_that_actually_ran(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    bedrock.resolve_model("segmenter")  # simulates the segmenter running
    bedrock.resolve_model("extractor")

    ids = handler._model_ids({})

    assert ids == {"segmenter": "base-model", "extractor": "base-model"}
    assert "recovery" not in ids  # never invoked → must not be reported


def test_merges_the_phases_from_the_other_lambda(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    bedrock.resolve_model("extractor")

    ids = handler._model_ids(
        {
            "classificationResult": {"documentClassification": "X", "modelId": "vision-model"},
            "processingResult": {"result": {}, "modelId": "vision-model"},
        }
    )

    assert ids == {
        "classification": "vision-model",
        "processing": "vision-model",
        "extractor": "base-model",
    }


def test_a_bypassed_phase_reports_no_model():
    """Manual classification and the large-doc guard answer without a model."""
    ids = handler._model_ids(
        {"classificationResult": {"documentClassification": "PEDIMENTO_ADUANAL", "modelId": None}}
    )
    assert "classification" not in ids


def test_each_phase_can_run_a_different_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    monkeypatch.setenv("BEDROCK_SEGMENTER_MODEL_ID", "cheap-model")
    bedrock.resolve_model("segmenter")
    bedrock.resolve_model("extractor")

    ids = handler._model_ids({"classificationResult": {"modelId": "vision-model"}})

    assert ids == {
        "classification": "vision-model",
        "segmenter": "cheap-model",
        "extractor": "base-model",
    }


def test_model_ids_lands_in_the_stored_response(monkeypatch):
    """`build_canonical_result` is what the standard flow persists."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    bedrock.resolve_model("recovery")

    result = handler.build_canonical_result(
        "doc-1", "INE", {}, [], "APROBADO",
        model_ids=handler._model_ids({"classificationResult": {"modelId": "vision-model"}}),
    )

    assert result["modelIds"] == {"classification": "vision-model", "recovery": "base-model"}


def test_absent_rather_than_empty_when_nothing_ran():
    """No phase ran (e.g. an early handler error) — don't emit a misleading {}."""
    result = handler.build_canonical_result("doc-1", "INE", {}, [], "APROBADO")
    assert "modelIds" not in result


def test_registry_is_reset_between_invocations(monkeypatch):
    """Warm Lambda containers reuse the process; phases must not leak across
    documents."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    bedrock.resolve_model("segmenter")
    assert "segmenter" in bedrock.used_models()

    bedrock.reset_used_models()

    assert bedrock.used_models() == {}
