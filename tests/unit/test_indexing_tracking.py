"""`Tracking` is a top-level DynamoDB attribute (sibling of DocumentType/
ProcessedAt/..., not nested inside the EnrichmentResult JSON blob) so it's
visible/queryable on the item itself. See indexing.py::_tracking_attribute.
"""

import sys
from decimal import Decimal
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import indexing  # noqa: E402


def test_tracking_attribute_has_full_parity_with_the_nested_one():
    """Same fields build_tracking() sets on expediente.tracking (see
    pipeline/result_common.py) — this column must never lag behind it."""
    result = {
        "tracking": {
            "createdAt": "2026-08-05T00:00:00Z",
            "updatedAt": "2026-08-05T00:00:01Z",
            "processingStartedAt": "2026-08-05T00:00:00Z",
            "processingCompletedAt": "2026-08-05T00:00:01Z",
            "processingSeconds": 1.23,
            "processingMs": 1230.0,
            "schema": {"pipelineVersion": "1.0.0", "model": "model-x", "typeCount": 5},
            "appOrigin": "CEDIS",
            "modelIds": {"extractor": "model-x"},
        }
    }
    attr = indexing._tracking_attribute(result)
    assert attr == {
        "CreatedAt": "2026-08-05T00:00:00Z",
        "UpdatedAt": "2026-08-05T00:00:01Z",
        "ProcessingStartedAt": "2026-08-05T00:00:00Z",
        "ProcessingCompletedAt": "2026-08-05T00:00:01Z",
        "ProcessingSeconds": Decimal("1.23"),
        "ProcessingTime": Decimal("1230.0"),
        "Schema": {"pipelineVersion": "1.0.0", "model": "model-x", "typeCount": 5},
        "AppOrigin": "CEDIS",
        "ModelIds": {"extractor": "model-x"},
    }


def test_floats_become_decimal_everywhere_including_nested_schema():
    """boto3's DynamoDB *resource* API (used by write_index_attributes)
    rejects a raw Python float outright, anywhere in the structure — this
    must never regress back to one, including floats nested inside Schema."""
    attr = indexing._tracking_attribute(
        {"tracking": {"processingMs": 12.5, "processingSeconds": 0.0125,
                       "schema": {"someFutureFloat": 3.14}}}
    )
    assert isinstance(attr["ProcessingTime"], Decimal)
    assert isinstance(attr["ProcessingSeconds"], Decimal)
    assert isinstance(attr["Schema"]["someFutureFloat"], Decimal)
    assert not isinstance(attr["ProcessingTime"], float)


def test_omits_fields_that_are_absent():
    attr = indexing._tracking_attribute({"tracking": {"createdAt": "2026-08-05T00:00:00Z"}})
    assert attr == {"CreatedAt": "2026-08-05T00:00:00Z"}


def test_none_when_no_tracking_at_all():
    assert indexing._tracking_attribute({}) is None


def test_derive_index_attributes_includes_tracking():
    result = {
        "documentClassification": "PEDIMENTO_ADUANAL",
        "processedAt": "2026-08-05T00:00:00Z",
        "validation": {"status": "APROBADO"},
        "tracking": {"createdAt": "2026-08-05T00:00:00Z", "appOrigin": "ERP"},
    }
    attrs = indexing.derive_index_attributes(result)
    assert attrs["Tracking"] == {"CreatedAt": "2026-08-05T00:00:00Z", "AppOrigin": "ERP"}


def test_index_column_names_lists_tracking():
    assert "Tracking" in indexing.index_column_names()
