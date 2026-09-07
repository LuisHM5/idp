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
import threading
from pathlib import Path

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from botocore.exceptions import ClientError  # noqa: E402

from document_catalog import TypeRegistryEntry  # noqa: E402
from pipeline import bedrock, model_fallback  # noqa: E402


def _type_entry(model_id=None, model_ids=None):
    return TypeRegistryEntry(
        type_id="TEST_TYPE",
        extraction_fields=[],
        required_fields=[],
        model_id=model_id,
        model_ids=model_ids or {},
    )

MODEL_ENV = (
    "BEDROCK_MODEL_ID",
    "BEDROCK_EXTRACTOR_MODEL_ID",
    "BEDROCK_SEGMENTER_MODEL_ID",
    "BEDROCK_RECOVERY_MODEL_ID",
    "FALLBACK_MODELS_TABLE",
    "MODEL_FALLBACK_MAX_RETRIES",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in MODEL_ENV:
        monkeypatch.delenv(var, raising=False)
    bedrock._NO_CACHE_SUPPORT.clear()
    bedrock._MODEL_MAX_TOKENS.clear()
    bedrock.reset_used_models()
    model_fallback.reload_fallback_models()
    yield
    bedrock._NO_CACHE_SUPPORT.clear()
    bedrock._MODEL_MAX_TOKENS.clear()
    bedrock.reset_used_models()
    model_fallback.reload_fallback_models()


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


def test_extractor_and_recovery_share_the_base_model_by_default(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    for stage in ("extractor", "recovery"):
        assert bedrock.resolve_model(stage) == "base-model"


def test_segmenter_defaults_to_the_most_reliable_fallback_model_not_the_base_one(monkeypatch):
    """Unlike extractor/recovery, segmenter's own default (absent any
    override) is the LAST entry of `model_fallback.get_fallback_models()` —
    not the shared base model. See `_model_candidates`'s docstring: this
    stage's task shape (open-ended multi-document JSON) has verified real
    trouble with the base model this deployment picked for cost
    (`minimax.minimax-m2.5`), so its own default should not inherit that
    choice blindly."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    # bedrock.py did `from .model_fallback import get_fallback_models` — a
    # direct name binding into ITS namespace, so patching `model_fallback`'s
    # own attribute would not reach it; patch the bound name instead.
    monkeypatch.setattr(bedrock, "get_fallback_models", lambda: ["a", "b", "most-reliable"])
    assert bedrock.resolve_model("segmenter") == "most-reliable"
    # extractor/recovery are untouched by this — still the shared base model.
    assert bedrock.resolve_model("extractor") == "base-model"


def test_override_affects_only_its_own_stage(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    monkeypatch.setenv("BEDROCK_SEGMENTER_MODEL_ID", "other-model")
    assert bedrock.resolve_model("segmenter") == "other-model"
    assert bedrock.resolve_model("extractor") == "base-model"


def test_legacy_extractor_var_still_acts_as_base(monkeypatch):
    """Deployments predating per-stage config set only this variable."""
    monkeypatch.setenv("BEDROCK_EXTRACTOR_MODEL_ID", "legacy-model")
    assert bedrock.base_model_id() == "legacy-model"
    assert bedrock.resolve_model("extractor") == "legacy-model"
    # segmenter still resolves to its OWN default (the most reliable
    # fallback model), not the legacy base — same as with BEDROCK_MODEL_ID.
    assert bedrock.resolve_model("segmenter") == model_fallback.get_fallback_models()[-1]


def test_falls_back_to_a_known_model_with_no_config():
    assert bedrock.resolve_model("segmenter")  # never empty


def test_catalog_model_id_outranks_configured_env(monkeypatch):
    """A type's own `model_id` (catalogs/README.md) beats env-configured
    stage/base models — it's a deliberate per-type choice, not a fallback."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    monkeypatch.setenv("BEDROCK_EXTRACTOR_MODEL_ID", "stage-model")
    entry = _type_entry(model_id="catalog-model")
    assert bedrock.resolve_model("extractor", entry) == "catalog-model"


def test_catalog_model_ids_stage_entry_outranks_catalog_model_id(monkeypatch):
    """`model_ids[stage]` pins one phase; `model_id` is the type's blanket
    fallback for every OTHER phase it didn't pin."""
    entry = _type_entry(model_id="catalog-general", model_ids={"recovery": "catalog-recovery"})
    assert bedrock.resolve_model("recovery", entry) == "catalog-recovery"
    assert bedrock.resolve_model("extractor", entry) == "catalog-general"


def test_request_override_still_beats_catalog_model_ids(monkeypatch):
    """The dev-only per-request override forces every stage regardless of
    what the type's catalog entry declares, even a per-stage pin."""
    entry = _type_entry(model_ids={"extractor": "catalog-extractor"})
    bedrock.set_model_override("forced-model")
    try:
        assert bedrock.resolve_model("extractor", entry) == "forced-model"
    finally:
        bedrock.set_model_override(None)


def test_no_catalog_override_falls_through_to_env_as_before(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    assert bedrock.resolve_model("extractor", None) == "base-model"
    assert bedrock.resolve_model("extractor", _type_entry()) == "base-model"


def test_explain_model_resolution_reports_every_candidate_and_the_winner(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    monkeypatch.setenv("BEDROCK_EXTRACTOR_MODEL_ID", "stage-model")
    entry = _type_entry(model_id="catalog-general", model_ids={"extractor": "catalog-extractor"})

    report = bedrock.explain_model_resolution("extractor", entry)

    assert report["resolved"] == "catalog-extractor"
    labels = [label for label, _ in report["candidates"]]
    assert labels == [
        "request override",
        "catalog model_ids[stage]",
        "catalog model_id",
        "stage env var",
        "base model",
    ]
    assert dict(report["candidates"])["stage env var"] == "stage-model"
    # Read-only: doesn't feed `used_models()` the way `resolve_model` does.
    bedrock.reset_used_models()
    bedrock.explain_model_resolution("extractor", entry)
    assert bedrock.used_models() == {}


def test_explain_model_resolution_for_segmenter_includes_its_own_default_layer(monkeypatch):
    """Only `segmenter` gets the extra "segmenter default" candidate,
    between the stage env var and the base model — never present (not even
    with a `None` value) for any other stage, which is what
    `test_explain_model_resolution_reports_every_candidate_and_the_winner`
    just pinned above."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "base-model")
    monkeypatch.setattr(bedrock, "get_fallback_models", lambda: ["a", "most-reliable"])

    report = bedrock.explain_model_resolution("segmenter")

    assert report["resolved"] == "most-reliable"
    labels = [label for label, _ in report["candidates"]]
    assert labels == [
        "request override",
        "catalog model_ids[stage]",
        "catalog model_id",
        "stage env var",
        "segmenter default",
        "base model",
    ]
    assert dict(report["candidates"])["segmenter default"] == "most-reliable"


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


def test_a_real_access_denial_is_not_treated_as_a_cache_issue(fake):
    """Same error code as the caching rejection, but a different message —
    must not be swallowed as a cachePoint retry on the SAME model. It's still
    handled, just one layer up: see the model-fallback tests below."""
    denied = _access_denied("You don't have access to the model with the specified model ID.")
    kwargs = {
        "modelId": "forbidden-model",
        "messages": [{"role": "user", "content": [{"text": "hola"}]}],
        "inferenceConfig": {"maxTokens": 8192, "temperature": 0},
        "system": [{"text": "ctx"}, {"cachePoint": {"type": "default"}}],
    }
    fake(fail_times=99, error=denied)

    with pytest.raises(ClientError):
        bedrock._invoke_with_retry(kwargs, max_retries=4)

    assert "forbidden-model" not in bedrock._NO_CACHE_SUPPORT


def test_no_retry_when_the_request_carried_no_cache_point(fake):
    """Nothing to strip means nothing to retry (same-model) — don't mask the
    error at that layer. Uses `_invoke_with_retry` directly to isolate this
    from the model-fallback layer above it (tested separately below)."""
    client = fake(fail_times=1, error=CACHE_UNSUPPORTED)
    kwargs = {
        "modelId": "some-model",
        "messages": [{"role": "user", "content": [{"text": "hola"}]}],
        "inferenceConfig": {"maxTokens": 8192, "temperature": 0},
    }

    with pytest.raises(ClientError):
        bedrock._invoke_with_retry(kwargs, max_retries=4)  # no cache_system

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


def test_known_model_max_tokens_has_minimax_seeded_with_its_real_bedrock_ceiling():
    """Verified against MiniMax M2.5's own Bedrock model card ("Max output
    tokens: 8K"), not inferred — see the constant's own comment for the
    source. This is the RECIPE (`_KNOWN_MODEL_MAX_TOKENS`), not the mutable
    per-container cache (`_MODEL_MAX_TOKENS`, which other tests in this file
    freely `.clear()` for isolation) — checking the recipe here is what
    survives that clearing."""
    assert bedrock._KNOWN_MODEL_MAX_TOKENS["minimax.minimax-m2.5"] == 8192


def test_a_known_model_is_clamped_on_the_very_first_call_no_rejection_needed(fake):
    """On a genuinely fresh container `_MODEL_MAX_TOKENS` starts seeded from
    `_KNOWN_MODEL_MAX_TOKENS` (this file's autouse fixture clears it for
    isolation between tests, so re-seed it here the same way module import
    does, to prove the FIRST call already respects it — no wasted rejected
    call like `test_clamps_max_tokens_to_the_limit_the_model_reports`)."""
    bedrock._MODEL_MAX_TOKENS.update(bedrock._KNOWN_MODEL_MAX_TOKENS)
    client = fake()

    bedrock.converse("hola", model_id="minimax.minimax-m2.5", max_tokens=32000)

    assert len(client.calls) == 1  # no rejection round-trip
    assert client.calls[0]["inferenceConfig"]["maxTokens"] == 8192


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


# --- model-unavailable fallback ----------------------------------------------
#
# `model_fallback.DEFAULT_FALLBACK_MODELS` (no FALLBACK_MODELS_TABLE set in
# these tests) is ["minimax.minimax-m2.5", "moonshotai.kimi-k2.5",
# "zai.glm-5", "us.anthropic.claude-haiku-4-5-20251001-v1:0"], and the
# default budget is 4 switches — one per entry.


def test_model_unavailable_falls_back_to_the_next_configured_model(fake):
    """A genuine AccessDeniedException (model not granted — NOT the caching
    heuristic, which `_invoke_with_retry` already handles on the same model)
    now means 'try the next model' instead of failing the whole call."""
    denied = _access_denied("You don't have access to the model with the specified model ID.")
    client = fake(fail_times=1, error=denied)

    text, meta = bedrock.converse("hola", model_id="forbidden-model")

    assert text == "OK"
    assert meta["modelId"] == "minimax.minimax-m2.5"  # first configured fallback
    assert len(client.calls) == 2
    assert client.calls[0]["modelId"] == "forbidden-model"
    assert client.calls[1]["modelId"] == "minimax.minimax-m2.5"


def test_fallback_keeps_switching_past_the_first_candidate(fake):
    """Two unavailable models in a row: falls all the way to the third."""
    denied = _access_denied("no access")
    client = fake(fail_times=2, error=denied)

    text, meta = bedrock.converse("hola", model_id="forbidden-model")

    assert text == "OK"
    assert meta["modelId"] == "moonshotai.kimi-k2.5"
    assert [c["modelId"] for c in client.calls] == [
        "forbidden-model",
        "minimax.minimax-m2.5",
        "moonshotai.kimi-k2.5",
    ]


def test_fallback_corrects_used_models_for_the_given_stage(fake):
    """`stage` keeps `_USED_MODELS` (and therefore `used_models()` /
    tracking.modelIds / cost audit) in sync with a mid-call switch — without
    it, cost tracking would blame the originally-resolved model instead of
    the one that actually served the request."""
    bedrock.resolve_model("extractor")  # simulates the pre-call resolution
    denied = _access_denied("no access")
    fake(fail_times=1, error=denied)

    bedrock.converse("hola", model_id="forbidden-model", stage="extractor")

    assert bedrock.used_models()["extractor"] == "minimax.minimax-m2.5"


def test_fallback_exhausts_the_configured_list_then_raises(fake):
    """Every candidate unavailable, including the last resort — must still
    surface as a real error instead of silently returning nothing."""
    denied = _access_denied("no access")
    client = fake(fail_times=99, error=denied)  # never recovers

    with pytest.raises(ClientError):
        bedrock.converse("hola", model_id="forbidden-model")

    # original + all 4 configured fallbacks, none repeated
    assert len(client.calls) == 5
    assert len({c["modelId"] for c in client.calls}) == 5


def test_zero_fallback_budget_propagates_immediately(fake):
    """`max_fallback_retries=0` disables the switch entirely — the original
    error propagates on the first failure, same as before this feature
    existed."""
    denied = _access_denied("no access")
    client = fake(fail_times=1, error=denied)

    with pytest.raises(ClientError):
        bedrock.converse("hola", model_id="forbidden-model", max_fallback_retries=0)

    assert len(client.calls) == 1


def test_env_configured_max_fallback_retries_limits_the_switches(fake, monkeypatch):
    monkeypatch.setenv("MODEL_FALLBACK_MAX_RETRIES", "1")
    denied = _access_denied("no access")
    client = fake(fail_times=99, error=denied)

    with pytest.raises(ClientError):
        bedrock.converse("hola", model_id="forbidden-model")

    # original + exactly 1 switch
    assert len(client.calls) == 2
    assert client.calls[1]["modelId"] == "minimax.minimax-m2.5"


def test_a_retryable_error_still_exhausts_same_model_retries_before_switching(fake, monkeypatch):
    """`ThrottlingException` is in both `_RETRYABLE` (same-model backoff) and
    `_MODEL_UNAVAILABLE` (cross-model fallback) — the same-model retries run
    first; only once THOSE are exhausted does a switch happen."""
    monkeypatch.setattr(bedrock.time, "sleep", lambda _seconds: None)
    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")
    client = fake(fail_times=2, error=throttled)

    text, meta = bedrock.converse("hola", model_id="slow-model", max_retries=1)

    # max_retries=1 same-model retry isn't enough (2 failures needed to clear it)
    # so it must have switched models rather than exhausting more same-model
    # attempts.
    assert text == "OK"
    assert meta["modelId"] == "minimax.minimax-m2.5"
    assert [c["modelId"] for c in client.calls] == ["slow-model", "slow-model", "minimax.minimax-m2.5"]


def test_a_validation_error_is_not_treated_as_model_unavailable(fake):
    """A generic `ValidationException` unrelated to max-tokens (the only
    message `_invoke_with_retry` special-cases) is a caller/request problem,
    not a provider outage — must propagate, not trigger a model switch."""
    bad_request = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "malformed input"}}, "Converse"
    )
    client = fake(fail_times=99, error=bad_request)

    with pytest.raises(ClientError):
        bedrock.converse("hola", model_id="some-model")

    assert len(client.calls) == 1


def test_internal_server_error_exhausts_same_model_retries_then_switches(fake, monkeypatch):
    """`InternalServerException` is AWS's generic transient 500 — retry the
    same model a couple of times (like throttling) before concluding the
    model itself is the problem."""
    monkeypatch.setattr(bedrock.time, "sleep", lambda _seconds: None)
    broken = ClientError(
        {"Error": {"Code": "InternalServerException", "Message": "internal error"}}, "Converse"
    )
    client = fake(fail_times=2, error=broken)

    text, meta = bedrock.converse("hola", model_id="flaky-model", max_retries=1)

    assert text == "OK"
    assert meta["modelId"] == "minimax.minimax-m2.5"
    assert [c["modelId"] for c in client.calls] == ["flaky-model", "flaky-model", "minimax.minimax-m2.5"]


def test_quota_exceeded_switches_immediately_without_same_model_retries(fake):
    """A quota error on THIS model won't clear itself within a backoff
    window — no point burning same-model retries before switching."""
    quota = ClientError(
        {"Error": {"Code": "ServiceQuotaExceededException", "Message": "quota exceeded"}}, "Converse"
    )
    client = fake(fail_times=1, error=quota)

    text, meta = bedrock.converse("hola", model_id="over-quota-model")

    assert text == "OK"
    assert meta["modelId"] == "minimax.minimax-m2.5"
    assert [c["modelId"] for c in client.calls] == ["over-quota-model", "minimax.minimax-m2.5"]


# --- fallback logging ---------------------------------------------------------


def test_switch_is_logged_with_stage_and_switch_count(fake, caplog):
    denied = _access_denied("no access")
    fake(fail_times=1, error=denied)

    with caplog.at_level("WARNING", logger="pipeline.bedrock"):
        bedrock.converse("hola", model_id="forbidden-model", stage="extractor")

    [record] = [r for r in caplog.records if "switching to" in r.getMessage()]
    message = record.getMessage()
    assert "stage=extractor" in message
    assert "forbidden-model" in message
    assert "AccessDeniedException" in message
    assert "minimax.minimax-m2.5" in message
    assert "switch 1/4" in message  # default budget is 4


def test_exhaustion_is_logged_at_error_level_after_at_least_one_switch(fake, caplog):
    denied = _access_denied("no access")
    fake(fail_times=99, error=denied)

    with caplog.at_level("WARNING", logger="pipeline.bedrock"):
        with pytest.raises(ClientError):
            bedrock.converse("hola", model_id="forbidden-model", stage="recovery")

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    assert "stage=recovery" in message
    assert "giving up" in message
    assert "forbidden-model" in message


def test_no_exhaustion_log_when_the_very_first_call_is_not_fallback_worthy(fake, caplog):
    """A plain single-model failure (nothing fallback-related ever happened)
    must not be logged as if a fallback had been attempted."""
    bad_request = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "malformed input"}}, "Converse"
    )
    fake(fail_times=99, error=bad_request)

    with caplog.at_level("WARNING", logger="pipeline.bedrock"):
        with pytest.raises(ClientError):
            bedrock.converse("hola", model_id="some-model", stage="extractor")

    assert not [r for r in caplog.records if r.levelname == "ERROR"]


# --- bedrock.record_stage_degraded() / stage_degradations() / note_used_model() ---
#
# Distinct from fallback_events(): a CALLER's own retry loop (segmenter.py
# re-issuing a fresh converse() call after a 200-OK-but-empty/truncated
# response), not a swap _invoke_with_fallback made inside one call after a
# ClientError. See pipeline/bedrock.py's docstring on _STAGE_DEGRADATIONS for
# why this accumulates in a list instead of overwriting like _FALLBACK_EVENTS.


def test_stage_degradations_starts_empty():
    assert bedrock.stage_degradations() == {}


def test_record_stage_degraded_accumulates_in_a_list():
    """Unlike fallback_events(), which overwrites, a stage can degrade more
    than once in one invocation (windowed segmentation calls the model once
    per window) — every call must survive, not just the last."""
    bedrock.record_stage_degraded("segmenter", {"pageStart": 1, "pageEnd": 10, "reason": "empty_response"})
    bedrock.record_stage_degraded("segmenter", {"pageStart": 11, "pageEnd": 20, "reason": "output_truncated"})

    events = bedrock.stage_degradations()["segmenter"]
    assert len(events) == 2
    assert [e["pageStart"] for e in events] == [1, 11]


def test_a_stage_that_never_degraded_is_absent():
    bedrock.record_stage_degraded("segmenter", {"pageStart": 1, "pageEnd": 1})
    assert "extractor" not in bedrock.stage_degradations()


def test_reset_clears_stage_degradations():
    bedrock.record_stage_degraded("segmenter", {"pageStart": 1, "pageEnd": 1})
    assert bedrock.stage_degradations() != {}

    bedrock.reset_used_models()

    assert bedrock.stage_degradations() == {}


def test_note_used_model_corrects_used_models_without_resolve_model():
    bedrock._USED_MODELS["segmenter"] = "original-model"
    bedrock.note_used_model("segmenter", "retry-model")
    assert bedrock.used_models()["segmenter"] == "retry-model"


def test_record_stage_degraded_is_thread_safe():
    """Windowed segmentation records from a ThreadPoolExecutor — every
    concurrent write must land, none silently lost to a racing append."""
    def _record(i):
        bedrock.record_stage_degraded("segmenter", {"pageStart": i, "pageEnd": i})

    threads = [threading.Thread(target=_record, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(bedrock.stage_degradations()["segmenter"]) == 50
