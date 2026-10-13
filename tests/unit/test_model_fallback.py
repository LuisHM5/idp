"""Tests for `pipeline/model_fallback.py`: the ordered fallback model list
(DynamoDB-backed, hot-reloadable) and the switch budget that
`pipeline/bedrock.py::_invoke_with_fallback` consults on a model-unavailable
error — see `tests/unit/test_bedrock_model_agnostic.py` for that side.
"""

import sys
from pathlib import Path

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline import model_fallback  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("FALLBACK_MODELS_TABLE", raising=False)
    monkeypatch.delenv("MODEL_FALLBACK_MAX_RETRIES", raising=False)
    model_fallback.reload_fallback_models()
    yield
    model_fallback.reload_fallback_models()


class FakeTable:
    def __init__(self, items):
        self._items = items

    def scan(self, **kwargs):
        return {"Items": self._items}


class FakeDynamoResource:
    def __init__(self, items):
        self._items = items

    def Table(self, name):
        return FakeTable(self._items)


def _install_table(monkeypatch, items):
    import boto3

    monkeypatch.setattr(boto3, "resource", lambda service: FakeDynamoResource(items))


# --- ordered fallback list -----------------------------------------------


def test_default_list_when_no_table_configured():
    assert model_fallback.get_fallback_models() == model_fallback.DEFAULT_FALLBACK_MODELS


def test_default_list_is_the_four_models_given_in_order():
    assert model_fallback.DEFAULT_FALLBACK_MODELS == [
        "minimax.minimax-m2.5",
        "moonshotai.kimi-k2.5",
        "zai.glm-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    ]


def test_loads_ordered_list_from_dynamodb(monkeypatch):
    monkeypatch.setenv("FALLBACK_MODELS_TABLE", "fallback-models")
    _install_table(
        monkeypatch,
        [
            {"Rank": 2, "ModelId": "second"},
            {"Rank": 1, "ModelId": "first"},
            {"Rank": 3, "ModelId": "third"},
        ],
    )
    model_fallback.reload_fallback_models()

    assert model_fallback.get_fallback_models() == ["first", "second", "third"]


def test_disabled_entries_are_skipped(monkeypatch):
    monkeypatch.setenv("FALLBACK_MODELS_TABLE", "fallback-models")
    _install_table(
        monkeypatch,
        [
            {"Rank": 1, "ModelId": "first", "Enabled": False},
            {"Rank": 2, "ModelId": "second"},
        ],
    )
    model_fallback.reload_fallback_models()

    assert model_fallback.get_fallback_models() == ["second"]


def test_empty_table_falls_back_to_the_default_list(monkeypatch):
    monkeypatch.setenv("FALLBACK_MODELS_TABLE", "fallback-models")
    _install_table(monkeypatch, [])
    model_fallback.reload_fallback_models()

    assert model_fallback.get_fallback_models() == model_fallback.DEFAULT_FALLBACK_MODELS


def test_dynamodb_error_falls_back_to_the_default_list_instead_of_raising(monkeypatch):
    monkeypatch.setenv("FALLBACK_MODELS_TABLE", "fallback-models")
    import boto3

    def _raise(service):
        raise RuntimeError("table not found")

    monkeypatch.setattr(boto3, "resource", _raise)
    model_fallback.reload_fallback_models()

    assert model_fallback.get_fallback_models() == model_fallback.DEFAULT_FALLBACK_MODELS


def test_result_is_cached_until_reload(monkeypatch):
    monkeypatch.setenv("FALLBACK_MODELS_TABLE", "fallback-models")
    _install_table(monkeypatch, [{"Rank": 1, "ModelId": "first"}])
    model_fallback.reload_fallback_models()
    assert model_fallback.get_fallback_models() == ["first"]

    # Table changes, but the cache should still serve the stale answer...
    _install_table(monkeypatch, [{"Rank": 1, "ModelId": "changed"}])
    assert model_fallback.get_fallback_models() == ["first"]

    # ...until explicitly reloaded.
    model_fallback.reload_fallback_models()
    assert model_fallback.get_fallback_models() == ["changed"]


def test_cache_refreshes_after_configured_ttl(monkeypatch):
    monkeypatch.setenv("FALLBACK_MODELS_TABLE", "fallback-models")
    monkeypatch.setenv("FALLBACK_MODELS_CACHE_TTL_SECONDS", "10")
    clock = {"now": 100.0}
    monkeypatch.setattr(model_fallback.time, "monotonic", lambda: clock["now"])

    _install_table(monkeypatch, [{"Rank": 1, "ModelId": "first"}])
    assert model_fallback.get_fallback_models() == ["first"]

    _install_table(monkeypatch, [{"Rank": 1, "ModelId": "changed"}])
    clock["now"] = 109.9
    assert model_fallback.get_fallback_models() == ["first"]
    clock["now"] = 110.0
    assert model_fallback.get_fallback_models() == ["changed"]


# --- retry budget -----------------------------------------------------------


def test_default_max_fallback_retries_is_four():
    assert model_fallback.get_max_fallback_retries() == 4
    assert model_fallback.DEFAULT_MAX_FALLBACK_RETRIES == 4


def test_env_overrides_max_fallback_retries(monkeypatch):
    monkeypatch.setenv("MODEL_FALLBACK_MAX_RETRIES", "2")
    assert model_fallback.get_max_fallback_retries() == 2


def test_negative_env_value_is_clamped_to_zero(monkeypatch):
    monkeypatch.setenv("MODEL_FALLBACK_MAX_RETRIES", "-3")
    assert model_fallback.get_max_fallback_retries() == 0


def test_invalid_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MODEL_FALLBACK_MAX_RETRIES", "not-a-number")
    assert model_fallback.get_max_fallback_retries() == model_fallback.DEFAULT_MAX_FALLBACK_RETRIES
