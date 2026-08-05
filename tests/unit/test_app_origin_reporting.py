"""The response stored in DynamoDB must name which app uploaded the document
(traceability), the same way it already names which model ran each phase —
see test_model_ids_reporting.py, which this mirrors.
"""

import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import handler  # noqa: E402


def test_app_origin_lands_in_the_stored_response():
    result = handler.build_canonical_result(
        "doc-1", "INE", {}, [], "APROBADO", app_origin="CEDIS",
    )
    assert result["appOrigin"] == "CEDIS"


def test_absent_rather_than_empty_when_no_app_origin():
    """No x-api-key on the request (e.g. dev with the key requirement off) —
    don't emit a misleading empty/None value."""
    result = handler.build_canonical_result("doc-1", "INE", {}, [], "APROBADO")
    assert "appOrigin" not in result
