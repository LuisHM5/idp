"""Contract tests for the project-owned S3/SQS ingress router."""

import importlib.util
from pathlib import Path


ROUTING_FILE = (
    Path(__file__).parents[2]
    / "src/document-processing/resources/sqs-consumer/routing.py"
)
SPEC = importlib.util.spec_from_file_location("sqs_consumer_routing", ROUTING_FILE)
assert SPEC and SPEC.loader
routing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(routing)


def test_field_centric_catalog_uses_enrichment_only_route():
    assert routing.route_from_catalog_item({"fields": {"M": {}}}) == "enrichment-only"


def test_expediente_catalog_uses_enrichment_only_route():
    assert (
        routing.route_from_catalog_item({"pipeline": {"S": "expediente"}})
        == "enrichment-only"
    )


def test_disabled_or_unknown_catalog_fails_closed_to_standard_route():
    assert (
        routing.route_from_catalog_item(
            {"enabled": {"BOOL": False}, "fields": {"M": {}}}
        )
        == "standard"
    )
    assert routing.route_from_catalog_item({}) == "standard"
    assert routing.route_from_catalog_item(None) == "standard"


def test_legacy_catalog_without_pipeline_stays_on_standard_route():
    assert (
        routing.route_from_catalog_item({"extraction_fields": {"L": []}})
        == "standard"
    )
