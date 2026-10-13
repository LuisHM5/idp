"""The catalog cache avoids one DynamoDB scan per document without making
runtime catalog changes require a deployment or a cold start.
"""

import sys
from pathlib import Path

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import document_catalog  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("CATALOG_CACHE_TTL_SECONDS", "30")
    document_catalog.reload_catalog()
    yield
    document_catalog.reload_catalog()


def test_catalog_is_reused_inside_ttl_and_refreshed_after_expiry(monkeypatch):
    clock = {"now": 100.0}
    loads = {"count": 0}

    monkeypatch.setattr(document_catalog.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(document_catalog, "_load_from_json", lambda: {})

    def load_db():
        loads["count"] += 1
        return {"TYPE": object()}

    monkeypatch.setattr(document_catalog, "_load_from_dynamodb", load_db)

    first = document_catalog._load_catalog()
    clock["now"] = 129.9
    assert document_catalog._load_catalog() is first
    assert loads["count"] == 1

    clock["now"] = 130.0
    second = document_catalog._load_catalog()
    assert second is not first
    assert loads["count"] == 2


def test_explicit_in_memory_catalog_remains_a_supported_test_seam(monkeypatch):
    injected = {"TYPE": object()}
    monkeypatch.setattr(document_catalog, "_CATALOG_CACHE", injected)
    monkeypatch.setattr(document_catalog, "_CATALOG_CACHE_LOADED_AT", None)
    monkeypatch.setattr(
        document_catalog,
        "_load_from_dynamodb",
        lambda: pytest.fail("an injected catalog must not hit DynamoDB"),
    )

    assert document_catalog._load_catalog() is injected
