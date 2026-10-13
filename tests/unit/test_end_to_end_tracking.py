"""Tiempo total real desde el PUT de S3 hasta el resultado final."""

import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import handler  # noqa: E402


def test_upload_to_completion_uses_s3_event_time_and_pipeline_completion():
    result = {
        "tracking": {
            "createdAt": "2026-10-12T21:05:34.516509+00:00",
            "processingCompletedAt": "2026-10-12T21:05:34.516509+00:00",
            "processingSeconds": 162.47,
        }
    }

    handler._add_upload_to_result_tracking(
        result, {"eventTime": "2026-10-12T21:02:40.000Z"}
    )

    assert result["tracking"]["uploadedAt"] == "2026-10-12T21:02:40+00:00"
    assert result["tracking"]["uploadToResultSeconds"] == 174.52
    assert result["tracking"]["uploadToResultMs"] == 174516.51
    # Existing pipeline-only metric retains its original meaning.
    assert result["tracking"]["processingSeconds"] == 162.47


def test_standard_flow_uses_result_created_at_as_completion():
    result = {"tracking": {"createdAt": "2026-10-12T20:00:05+00:00"}}

    handler._add_upload_to_result_tracking(
        result, {"eventTime": "2026-10-12T20:00:00+00:00"}
    )

    assert result["tracking"]["uploadToResultSeconds"] == 5.0


def test_missing_or_invalid_event_time_is_fail_open():
    for event in ({}, {"eventTime": "not-a-date"}):
        result = {"tracking": {"createdAt": "2026-10-12T20:00:05+00:00"}}
        handler._add_upload_to_result_tracking(result, event)
        assert "uploadToResultSeconds" not in result["tracking"]
