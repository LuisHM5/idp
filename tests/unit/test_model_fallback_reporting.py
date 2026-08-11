"""The final result must say which stages switched models and how, not just
which model each stage ended up on: `tracking.modelFallbacks` (stage ->
{expectedModel, actualModel, retries, attempts}), promoted to the DynamoDB
`Tracking` column exactly like `tracking.modelIds` already is (indexing.py).

See `tests/unit/test_bedrock_model_agnostic.py` for the switching mechanics
themselves (`_invoke_with_fallback`); this file covers only that the outcome
is reported correctly.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import handler  # noqa: E402
from indexing import _TRACKING_FIELD_MAP, _tracking_attribute  # noqa: E402
from pipeline import bedrock  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in ("BEDROCK_MODEL_ID", "BEDROCK_EXTRACTOR_MODEL_ID", "FALLBACK_MODELS_TABLE"):
        monkeypatch.delenv(var, raising=False)
    bedrock.reset_used_models()
    yield
    bedrock.reset_used_models()


def _access_denied(message):
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": message}}, "Converse"
    )


class FakeClient:
    def __init__(self, fail_times=0, error=None):
        self.calls = []
        self._left = fail_times
        self._error = error

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._left > 0:
            self._left -= 1
            raise self._error
        return {
            "output": {"message": {"content": [{"text": "OK"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        c = FakeClient(**kwargs)
        monkeypatch.setattr(bedrock, "client", lambda *a, **k: c)
        return c

    return install


# --- bedrock.fallback_events() ------------------------------------------------


def test_empty_when_no_stage_ever_switched(fake, monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    fake()  # never fails

    bedrock.converse("hola", model_id="base-model", stage="extractor")

    assert bedrock.fallback_events() == {}


def test_populated_with_expected_vs_actual_and_retry_count(fake):
    denied = _access_denied("no access")
    fake(fail_times=1, error=denied)

    bedrock.converse("hola", model_id="requested-model", stage="extractor")

    events = bedrock.fallback_events()
    assert events["extractor"]["expectedModel"] == "requested-model"
    assert events["extractor"]["actualModel"] == "minimax.minimax-m2.5"
    assert events["extractor"]["retries"] == 1
    assert events["extractor"]["attempts"] == [
        {"model": "requested-model", "error": "AccessDeniedException"}
    ]


def test_retries_count_matches_the_number_of_switches(fake):
    denied = _access_denied("no access")
    fake(fail_times=2, error=denied)

    bedrock.converse("hola", model_id="requested-model", stage="extractor")

    event = bedrock.fallback_events()["extractor"]
    assert event["retries"] == 2
    assert [a["model"] for a in event["attempts"]] == ["requested-model", "minimax.minimax-m2.5"]
    assert event["actualModel"] == "moonshotai.kimi-k2.5"


def test_a_stage_that_never_switched_is_absent_even_if_others_did(fake):
    """Only the stage that actually switched is reported — same rule as
    `used_models()`: a stage present with no switch would be indistinguishable
    from one that genuinely never ran into trouble."""
    denied = _access_denied("no access")
    fake(fail_times=1, error=denied)
    bedrock.converse("hola", model_id="requested-model", stage="extractor")

    events = bedrock.fallback_events()
    assert "extractor" in events
    assert "segmenter" not in events
    assert "recovery" not in events


def test_no_event_recorded_when_the_call_ultimately_fails(fake):
    """A switch that never leads to success has nothing useful to report as
    'the model that ran' — there wasn't one."""
    denied = _access_denied("no access")
    fake(fail_times=99, error=denied)

    with pytest.raises(ClientError):
        bedrock.converse("hola", model_id="requested-model", stage="extractor")

    assert bedrock.fallback_events() == {}


def test_reset_clears_fallback_events(fake):
    denied = _access_denied("no access")
    fake(fail_times=1, error=denied)
    bedrock.converse("hola", model_id="requested-model", stage="extractor")
    assert bedrock.fallback_events() != {}

    bedrock.reset_used_models()  # what handler() calls per invocation

    assert bedrock.fallback_events() == {}


# --- envelope wiring ----------------------------------------------------------


def test_lands_in_tracking_when_passed(fake):
    denied = _access_denied("no access")
    fake(fail_times=1, error=denied)
    bedrock.converse("hola", model_id="requested-model", stage="extractor")

    result = handler.build_canonical_result(
        "doc-1", "INE", {}, [], "APROBADO",
        model_fallbacks=bedrock.fallback_events(),
    )

    assert result["tracking"]["modelFallbacks"]["extractor"]["actualModel"] == "minimax.minimax-m2.5"


def test_absent_rather_than_empty_when_nothing_switched():
    result = handler.build_canonical_result(
        "doc-1", "INE", {}, [], "APROBADO", model_fallbacks={}
    )
    assert "modelFallbacks" not in result["tracking"]


# --- DynamoDB indexed Tracking column ------------------------------------------


def test_promoted_to_the_indexed_tracking_column():
    assert _TRACKING_FIELD_MAP["modelFallbacks"] == "ModelFallbacks"

    attr = _tracking_attribute({
        "tracking": {
            "createdAt": "2026-01-01T00:00:00Z",
            "modelFallbacks": {
                "extractor": {
                    "expectedModel": "requested-model",
                    "actualModel": "minimax.minimax-m2.5",
                    "retries": 1,
                    "attempts": [{"model": "requested-model", "error": "AccessDeniedException"}],
                }
            },
        }
    })

    assert attr["ModelFallbacks"]["extractor"]["retries"] == 1
    assert attr["ModelFallbacks"]["extractor"]["actualModel"] == "minimax.minimax-m2.5"


def test_column_absent_when_tracking_has_no_fallbacks():
    attr = _tracking_attribute({"tracking": {"createdAt": "2026-01-01T00:00:00Z"}})
    assert "ModelFallbacks" not in attr
