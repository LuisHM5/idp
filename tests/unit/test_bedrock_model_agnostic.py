"""Tests for the model-agnostic bits of `pipeline/bedrock.py`.

Two behaviours are covered, both verified against live Bedrock before being
pinned here:

- per-stage model resolution (base model + optional per-stage override);
- graceful degradation when a model rejects `cachePoint`. Bedrock does *not*
  ignore an unsupported cache point — it fails the whole request, and reports
  it as `AccessDeniedException`, the same code as a genuinely missing model
  permission. Without the fallback the pipeline would only run on the subset
  of Bedrock models that support caching.
"""

import json
import sys
from pathlib import Path

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from botocore.exceptions import ClientError  # noqa: E402

from pipeline import bedrock  # noqa: E402

MODEL_ENV = (
    "BEDROCK_MODEL_ID",
    "BEDROCK_EXTRACTOR_MODEL_ID",
    "BEDROCK_SEGMENTER_MODEL_ID",
    "BEDROCK_RECOVERY_MODEL_ID",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in MODEL_ENV:
        monkeypatch.delenv(var, raising=False)
    bedrock._NO_CACHE_SUPPORT.clear()
    bedrock._MODEL_MAX_TOKENS.clear()
    yield
    bedrock._NO_CACHE_SUPPORT.clear()
    bedrock._MODEL_MAX_TOKENS.clear()


def _access_denied(message):
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": message}}, "Converse"
    )


CACHE_UNSUPPORTED = _access_denied(
    "You invoked an unsupported model or your request did not allow prompt caching."
)


class FakeClient:
    """Converse stub that fails the first N calls with a given error."""

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


# --- per-stage model resolution ---------------------------------------------


def test_stages_share_the_base_model_by_default(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    for stage in ("segmenter", "extractor", "recovery"):
        assert bedrock.resolve_model(stage) == "base-model"


def test_override_affects_only_its_own_stage(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    monkeypatch.setenv("BEDROCK_SEGMENTER_MODEL_ID", "other-model")
    assert bedrock.resolve_model("segmenter") == "other-model"
    assert bedrock.resolve_model("extractor") == "base-model"


def test_legacy_extractor_var_still_acts_as_base(monkeypatch):
    """Deployments predating per-stage config set only this variable."""
    monkeypatch.setenv("BEDROCK_EXTRACTOR_MODEL_ID", "legacy-model")
    assert bedrock.base_model_id() == "legacy-model"
    assert bedrock.resolve_model("segmenter") == "legacy-model"


def test_falls_back_to_a_known_model_with_no_config():
    assert bedrock.resolve_model("segmenter")  # never empty


def test_catalog_model_id_outranks_configured_env(monkeypatch):
    """A type's own `model_id` (catalogs/README.md) beats env-configured
    stage/base models — it's a deliberate per-type choice, not a fallback."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    monkeypatch.setenv("BEDROCK_EXTRACTOR_MODEL_ID", "stage-model")
    assert bedrock.resolve_model("extractor", "catalog-model") == "catalog-model"


def test_request_override_still_beats_catalog_model_id(monkeypatch):
    """The dev-only per-request override forces every stage regardless of
    what the type's catalog entry declares."""
    bedrock.set_model_override("forced-model")
    try:
        assert bedrock.resolve_model("extractor", "catalog-model") == "forced-model"
    finally:
        bedrock.set_model_override(None)


def test_no_catalog_model_id_falls_through_to_env_as_before(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    assert bedrock.resolve_model("extractor", None) == "base-model"


# --- caching degradation -----------------------------------------------------


def test_retries_without_cache_point_when_model_rejects_it(fake):
    client = fake(fail_times=1, error=CACHE_UNSUPPORTED)

    text, _ = bedrock.converse("hola", model_id="no-cache-model", cache_system="ctx")

    assert text == "OK"  # the call succeeds instead of propagating
    assert len(client.calls) == 2
    assert any("cachePoint" in b for b in client.calls[0]["system"])
    assert not any("cachePoint" in b for b in client.calls[1]["system"])
    # The static prefix itself is kept — only the cache marker is dropped.
    assert any(b.get("text") == "ctx" for b in client.calls[1]["system"])


def test_model_is_remembered_so_later_calls_skip_the_cache_point(fake):
    client = fake(fail_times=1, error=CACHE_UNSUPPORTED)
    bedrock.converse("uno", model_id="no-cache-model", cache_system="ctx")
    assert "no-cache-model" in bedrock._NO_CACHE_SUPPORT

    bedrock.converse("dos", model_id="no-cache-model", cache_system="ctx")

    # Three calls total: the rejected one, its retry, and the second request —
    # which must not have paid another rejection.
    assert len(client.calls) == 3
    assert not any("cachePoint" in b for b in client.calls[2]["system"])


def test_structured_calls_degrade_too(fake):
    fake(fail_times=1, error=CACHE_UNSUPPORTED)
    schema = {"type": "object", "properties": {"rfc": {"type": "string"}}}

    out, _ = bedrock.converse_structured(
        "hola",
        tool_schema=schema,
        tool_name="extract",
        tool_description="d",
        model_id="no-cache-model",
        cache_system="ctx",
    )
    assert out == {}  # stub returns text, not toolUse — the point is it returned


def test_a_real_access_denial_is_not_swallowed(fake):
    """Same error code as the caching rejection — must still propagate."""
    denied = _access_denied("You don't have access to the model with the specified model ID.")
    fake(fail_times=1, error=denied)

    with pytest.raises(ClientError):
        bedrock.converse("hola", model_id="forbidden-model", cache_system="ctx")

    assert "forbidden-model" not in bedrock._NO_CACHE_SUPPORT


def test_no_retry_when_the_request_carried_no_cache_point(fake):
    """Nothing to strip means nothing to retry — don't mask the error."""
    client = fake(fail_times=1, error=CACHE_UNSUPPORTED)

    with pytest.raises(ClientError):
        bedrock.converse("hola", model_id="some-model")  # no cache_system

    assert len(client.calls) == 1


def test_clamps_max_tokens_to_the_limit_the_model_reports(fake):
    """Output ceilings differ (10k on Nova, 16k+ on Claude) — a request sized
    for one model is rejected outright by another, failing the extraction."""
    too_high = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "The maximum tokens you requested exceeds the model limit of 10000.",
            }
        },
        "Converse",
    )
    client = fake(fail_times=1, error=too_high)

    bedrock.converse("hola", model_id="capped-model", max_tokens=32000)

    assert client.calls[0]["inferenceConfig"]["maxTokens"] == 32000
    assert client.calls[1]["inferenceConfig"]["maxTokens"] == 10000


def test_the_learned_limit_is_applied_upfront_on_later_calls(fake):
    too_high = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "The maximum tokens you requested exceeds the model limit of 10000.",
            }
        },
        "Converse",
    )
    client = fake(fail_times=1, error=too_high)
    bedrock.converse("uno", model_id="capped-model", max_tokens=32000)

    bedrock.converse("dos", model_id="capped-model", max_tokens=32000)

    # No second rejection: the third call already went out clamped.
    assert len(client.calls) == 3
    assert client.calls[2]["inferenceConfig"]["maxTokens"] == 10000


def test_a_smaller_request_is_left_alone(fake):
    bedrock._MODEL_MAX_TOKENS["capped-model"] = 10000
    client = fake()

    bedrock.converse("hola", model_id="capped-model", max_tokens=4096)

    assert client.calls[0]["inferenceConfig"]["maxTokens"] == 4096


def test_dump_writes_the_exchange_when_enabled(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(bedrock, "_DUMP_DIR", str(tmp_path))
    fake()

    bedrock.converse("hola", model_id="some.model-v1:0")

    dumps = list(tmp_path.glob("*.json"))
    assert len(dumps) == 1
    # Model id becomes the filename, so it must survive as a safe slug.
    assert dumps[0].name.startswith("some-model-v1-0-")
    payload = json.loads(dumps[0].read_text())
    assert payload["modelId"] == "some.model-v1:0"
    assert payload["text"] == "OK"
    assert payload["request"]["messages"][0]["content"][0]["text"] == "hola"


def test_dump_failure_never_breaks_the_call(fake, monkeypatch):
    monkeypatch.setattr(bedrock, "_DUMP_DIR", "/proc/cannot-write-here")
    fake()

    text, _ = bedrock.converse("hola", model_id="some-model")

    assert text == "OK"


def test_caching_is_preserved_for_models_that_support_it(fake):
    client = fake()  # never fails

    bedrock.converse("hola", model_id="caching-model", cache_system="ctx")

    assert len(client.calls) == 1
    assert any("cachePoint" in b for b in client.calls[0]["system"])
    assert not bedrock._NO_CACHE_SUPPORT
