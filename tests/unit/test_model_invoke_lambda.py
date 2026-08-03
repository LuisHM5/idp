"""Unit tests for the Converse-based classification/extraction Lambda.

The Lambda's value is that it works with *any* Bedrock model, so the tests
that matter here are the ones covering the model-layer translation: Converse
content blocks (raw bytes, not base64), Converse's flat response blocks, and
the bypasses that must keep working unchanged after the swap.

boto3 and powertools are stubbed so the module imports without AWS
credentials or the bundled Lambda dependencies.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/model-invoke-lambda"
)


def _stub_dependencies(monkeypatch):
    """Install minimal stand-ins for the AWS-only imports."""
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *a, **k: types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "boto3", boto3)

    botocore = types.ModuleType("botocore")
    config_mod = types.ModuleType("botocore.config")
    config_mod.Config = lambda *a, **k: None
    exc_mod = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, response=None, op=None):
            super().__init__("stub")
            self.response = response or {}

    exc_mod.ClientError = ClientError
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", config_mod)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exc_mod)

    powertools = types.ModuleType("aws_lambda_powertools")

    class _NoopObservability:
        """Metrics/Tracer stand-in: decorators pass through, calls no-op."""

        def __getattr__(self, _name):
            def _call(func=None, **_kwargs):
                return func if callable(func) else None

            return _call

    powertools.Metrics = lambda *a, **k: _NoopObservability()
    powertools.Tracer = lambda *a, **k: _NoopObservability()
    metrics_mod = types.ModuleType("aws_lambda_powertools.metrics")
    metrics_mod.MetricUnit = types.SimpleNamespace(Count="Count")
    monkeypatch.setitem(sys.modules, "aws_lambda_powertools", powertools)
    monkeypatch.setitem(sys.modules, "aws_lambda_powertools.metrics", metrics_mod)


@pytest.fixture
def mod(monkeypatch):
    _stub_dependencies(monkeypatch)
    monkeypatch.setenv("INVOKE_TYPE", "classification")
    monkeypatch.setenv("MODEL_ID", "test.model-v1:0")
    monkeypatch.setenv("PROMPT", "classify this")

    spec = importlib.util.spec_from_file_location(
        "model_invoke_lambda", LAMBDA_DIR / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Converse content blocks -------------------------------------------------


def test_image_block_carries_raw_bytes_not_base64(mod):
    """Converse takes raw bytes; base64 would be read as file content."""
    payload = b"\xff\xd8\xff binary jpeg"
    block = mod._media_block("jpg", "raw/uuid/scan.jpg", payload)

    assert block["image"]["format"] == "jpeg"  # 'jpg' is not a Converse format
    assert block["image"]["source"]["bytes"] == payload


def test_pdf_uses_document_block_with_sanitised_name(mod):
    """Converse rejects dots/underscores in a document name."""
    block = mod._media_block("pdf", "raw/uuid/pedimento_6001187.final.pdf", b"%PDF-")

    assert block["document"]["format"] == "pdf"
    assert block["document"]["source"]["bytes"] == b"%PDF-"
    name = block["document"]["name"]
    assert "." not in name and "_" not in name


def test_document_name_never_empty(mod):
    """A basename of only illegal characters must still yield a valid name."""
    assert mod._document_name("raw/uuid/___.pdf") == "document"


# --- Converse response parsing ----------------------------------------------


def _converse_response(blocks):
    return {"output": {"message": {"content": blocks}}}


def test_parses_plain_json_text(mod):
    out = mod.extract_json_from_response(
        _converse_response([{"text": '{"documentClassification": "CFDI"}'}])
    )
    assert out["documentClassification"] == "CFDI"


def test_parses_json_wrapped_in_markdown_fence(mod):
    out = mod.extract_json_from_response(
        _converse_response([{"text": '```json\n{"documentClassification": "CFDI"}\n```'}])
    )
    assert out["documentClassification"] == "CFDI"


def test_parses_json_surrounded_by_prose(mod):
    out = mod.extract_json_from_response(
        _converse_response(
            [{"text": 'Sure! {"documentClassification": "CFDI"} hope that helps'}]
        )
    )
    assert out["documentClassification"] == "CFDI"


def test_tool_use_block_wins_over_text(mod):
    """With toolChoice forced, the payload is already a parsed dict."""
    out = mod.extract_json_from_response(
        _converse_response(
            [
                {"text": "ignore me"},
                {"toolUse": {"input": {"documentClassification": "PEDIMENTO_ADUANAL"}}},
            ]
        )
    )
    assert out["documentClassification"] == "PEDIMENTO_ADUANAL"


def test_empty_response_raises(mod):
    with pytest.raises(ValueError):
        mod.extract_json_from_response(_converse_response([]))


def test_unparseable_response_raises(mod):
    with pytest.raises(ValueError):
        mod.extract_json_from_response(_converse_response([{"text": "no json here"}]))


# --- bypasses carried over from the patched Lambda ---------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("raw/PEDIMENTO_ADUANAL/uuid-1234/file.pdf", "PEDIMENTO_ADUANAL"),
        ("raw/9f8e7d6c-1234-4321-abcd-000000000000/file.pdf", None),  # uuid → auto
        ("raw/file.pdf", None),
        ("", None),
    ],
)
def test_manual_classification_from_key(mod, key, expected):
    assert mod.extract_manual_classification(key) == expected


def test_extraction_bypass_for_types_absent_from_prompt(mod):
    prompt = "CFDI: campos...\nINE: campos..."
    assert mod.type_has_upstream_extraction("CFDI", prompt) is True
    assert mod.type_has_upstream_extraction("PEDIMENTO_ADUANAL", prompt) is False


def test_manual_classification_skips_the_model(mod, monkeypatch):
    """The bypass must return before any Converse call is attempted."""

    def explode(*_args, **_kwargs):
        raise AssertionError("model was called despite manual classification")

    monkeypatch.setattr(mod, "_converse_with_retry", explode)

    out = mod.handler(
        {
            "documentId": "doc-1",
            "contentType": "file",
            "content": {
                "location": "s3",
                "bucket": "b",
                "key": "raw/PEDIMENTO_ADUANAL/uuid-1/file.pdf",
            },
        },
        None,
    )
    # modelId is None because no model ran — the caller records it per phase.
    assert out == {"documentClassification": "PEDIMENTO_ADUANAL", "modelId": None}


def test_large_document_falls_back_instead_of_failing(mod, monkeypatch, tmp_path):
    """Oversized files degrade to the expediente type, not a workflow failure."""
    monkeypatch.setattr(mod, "_MAX_DOC_BYTES", 10)

    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * 100)

    monkeypatch.setattr(
        mod.s3, "download_file", lambda b, k, p: Path(p).write_bytes(b"x" * 100), raising=False
    )
    monkeypatch.setattr(mod.os.path, "getsize", lambda _p: 100)
    monkeypatch.setattr(
        mod, "_converse_with_retry", lambda *_a, **_k: pytest.fail("model called on oversized doc")
    )

    out = mod.handler(
        {
            "documentId": "doc-2",
            "contentType": "file",
            # uuid segment → not a manual classification, so it reaches the guard
            "content": {
                "location": "s3",
                "bucket": "b",
                "key": "raw/9f8e7d6c-1234-4321-abcd-000000000000/big.pdf",
            },
        },
        None,
    )
    assert out == {
        "documentClassification": mod._LARGE_DOC_FALLBACK_TYPE,
        "modelId": None,
    }


def test_unsupported_extension_rejected(mod, monkeypatch):
    with pytest.raises(ValueError, match="Unsupported file type"):
        mod.handler(
            {
                "documentId": "doc-3",
                "contentType": "file",
                "content": {
                    "location": "s3",
                    "bucket": "b",
                    "key": "raw/9f8e7d6c-1234-4321-abcd-000000000000/notes.exe",
                },
            },
            None,
        )


def test_request_shape_is_model_agnostic(mod, monkeypatch):
    """No Anthropic-specific keys should reach Bedrock."""
    captured = {}

    def capture(kwargs, **_opts):
        captured.update(kwargs)
        return _converse_response([{"text": '{"documentClassification": "CFDI"}'}])

    monkeypatch.setattr(mod, "_converse_with_retry", capture)

    mod.handler({"documentId": "doc-4", "contentType": "data", "content": {"data": "text"}}, None)

    assert captured["modelId"] == "test.model-v1:0"
    assert captured["messages"][0]["role"] == "user"
    body = json.dumps(captured)
    assert "anthropic_version" not in body
    assert "toolConfig" not in captured  # structured mode off by default
