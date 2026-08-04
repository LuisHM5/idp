"""Tests for handler.py's file-source routing helpers.

`_declared_file_type` reads the explicit `content.fileType` the SQS consumer
now sets (patches/default-sqs-consumer-index.py) — the single source of
truth for which pipeline (xml/tabular/pdf) a document routes to, preferred
over re-parsing the S3 key's extension independently in every consumer.
"""

import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import handler  # noqa: E402


def test_declared_file_type_reads_content_filetype():
    event = {"content": {"bucket": "b", "key": "raw/x/file.pdf", "fileType": "pdf"}}
    assert handler._declared_file_type(event) == "pdf"


def test_declared_file_type_normalizes_case_and_whitespace():
    event = {"content": {"fileType": " XML "}}
    assert handler._declared_file_type(event) == "xml"


def test_declared_file_type_absent_returns_none():
    assert handler._declared_file_type({"content": {"bucket": "b", "key": "k"}}) is None
    assert handler._declared_file_type({}) is None
    assert handler._declared_file_type({"content": "not-a-dict"}) is None


def test_declared_file_type_is_preferred_over_key_extension_fallback():
    # The routing line in _run_expediente is `_declared_file_type(event) or
    # (key extension fallback)` — content.fileType wins whenever present,
    # even if it disagrees with the key (e.g. a future ingestion path that
    # renames/normalizes the extension differently from the declared type).
    event = {"content": {"key": "raw/x/report.dat", "fileType": "xlsx"}}
    key = event["content"]["key"]
    ext = handler._declared_file_type(event) or (key.rsplit(".", 1)[-1].lower() if "." in key else "")
    assert ext == "xlsx"


def test_key_extension_fallback_when_filetype_absent():
    event = {"content": {"key": "raw/x/report.csv"}}
    key = event["content"]["key"]
    ext = handler._declared_file_type(event) or (key.rsplit(".", 1)[-1].lower() if "." in key else "")
    assert ext == "csv"
