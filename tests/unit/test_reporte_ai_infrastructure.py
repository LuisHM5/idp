from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = _load("reporte_ai", "reporte.py")
infra_costs = _load("infra_costs_test", "infrastructure_costs.py")


def _cost(document_id: str, *, bedrock: bool) -> dict:
    services = {
        "LambdaCompute": {"cost": 0.0001, "input_tokens": 0, "output_tokens": 0, "model_id": None},
    }
    if bedrock:
        services["BedrockExtraction"] = {
            "cost": 0.01, "input_tokens": 100, "output_tokens": 20, "model_id": "model",
        }
    return {
        "document_id": document_id, "document_type": "XML" if not bedrock else "PDF",
        "document_type_name": "", "source_file": "", "app_origin": "", "processed_at": "2026-09-08",
        "pricing_version": "x", "pages_total": 1, "pages_native": 0, "pages_ocr": 0, "pages_empty": 0,
        "total_cost": sum(s["cost"] for s in services.values()), "cost_per_page": 0,
        "total_input_tokens": 100 if bedrock else 0, "total_output_tokens": 20 if bedrock else 0,
        "services": services, "page_selection": None,
    }


def test_report_marks_actual_bedrock_usage_and_sorts_no_ai_first() -> None:
    rows = report.join_rows([_cost("ai", bedrock=True), _cost("det", bedrock=False)], [])
    by_id = {row["id"]: row for row in rows}

    assert by_id["det"]["processingMode"] == "Sin IA"
    assert by_id["det"]["aiCost"] == 0
    assert by_id["ai"]["processingMode"] == "Con IA"
    assert by_id["ai"]["aiCost"] == 0.01

    overall = report.overall_snapshot(rows)
    assert overall["n_no_ai"] == 1
    assert overall["no_ai_docs_cost"] == 0.0001
    assert [name for name, _ in report.sorted_types(report.by_key_snapshot(rows, "type"))] == ["XML", "PDF"]

    html = report.render_html(rows, "dev", infrastructure={"fixedMonthlyUsd": 0, "taggedMtd": {}})
    assert "Procesamiento con y sin IA" in html
    assert 'id="f-modes"' in html
    assert "sortedTypeAgg" in html
    assert "Costo de docs sin IA" in html


def test_structured_catalog_marks_old_document_without_cost_as_no_ai() -> None:
    rows = report.join_rows([], [{
        "document_id": "old-xml", "document_type": "FACTURA_HONORARIOS_XML",
        "document_type_name": "Factura XML", "document_name": "factura.xml", "app_origin": "",
        "processed_at": "2026-08-01", "workflow_status": "complete", "validation_status": "APROBADO",
        "model": "(desconocido)", "processing_seconds": None, "avg_confidence": 1.0,
        "warnings": [], "field_stats": {}, "sub_document_count": 1, "metadata": {},
    }])

    assert rows[0]["usesAi"] is None
    assert rows[0]["expectedNoAi"] is True
    assert rows[0]["processingMode"] == "Sin IA"
    assert report.overall_snapshot(rows)["n_no_ai_with_cost"] == 0


class FakeClient:
    def __init__(self, **operations):
        self.operations = operations

    def get_paginator(self, operation):
        raise RuntimeError("sin paginator en el fake")

    def __getattr__(self, name):
        value = self.operations[name]
        return value if callable(value) else (lambda **kwargs: value)


def test_live_inventory_builds_scoped_prod_floor_and_ce_mtd() -> None:
    waf_arn = "arn:aws:wafv2:us-east-1:123:regional/webacl/name/id"
    resources = [
        {"ResourceType": "AWS::WAFv2::WebACL", "PhysicalResourceId": "name|id|REGIONAL"},
        {"ResourceType": "AWS::KMS::Key", "PhysicalResourceId": "key-1"},
        {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": "bucket"},
        {"ResourceType": "AWS::DynamoDB::Table", "PhysicalResourceId": "table"},
    ]
    cfn = FakeClient(list_stack_resources={"StackResourceSummaries": resources})
    waf = FakeClient(
        get_web_acl={"WebACL": {"ARN": waf_arn, "Rules": [{}, {}, {}, {}]}},
        list_web_acls={"WebACLs": [{"ARN": waf_arn}]},
    )
    cloudfront = FakeClient(list_web_acls={"WebACLs": []})
    kms = FakeClient(
        list_keys={"Keys": [{"KeyId": "key-1"}]},
        describe_key={"KeyMetadata": {"KeyId": "key-1", "KeyManager": "CUSTOMER", "KeyState": "Enabled"}},
    )

    def ce_get(**kwargs):
        if kwargs["GroupBy"][0]["Key"] == "SERVICE":
            return {"ResultsByTime": [{"Estimated": True, "Groups": []}]}
        return {"ResultsByTime": [{"Estimated": True, "Groups": [
            {"Keys": ["USE1-WebACLV2"], "Metrics": {"UnblendedCost": {"Amount": "1.2"}, "UsageQuantity": {"Amount": ".24", "Unit": "Months"}}},
            {"Keys": ["USE1-RuleV2"], "Metrics": {"UnblendedCost": {"Amount": ".96"}, "UsageQuantity": {"Amount": ".96", "Unit": "Months"}}},
            {"Keys": ["us-east-1-KMS-Keys"], "Metrics": {"UnblendedCost": {"Amount": ".24"}, "UsageQuantity": {"Amount": ".24", "Unit": "Keys"}}},
        ]}]}

    snapshot = infra_costs.collect_infrastructure_costs(
        "DocumentValidationStack", "us-east-1", environment="prod", now=date(2026, 9, 8),
        clients={"cfn": cfn, "waf": waf, "cloudfront_waf": cloudfront, "kms": kms, "ce": FakeClient(get_cost_and_usage=ce_get)},
    )

    assert snapshot["fixedMonthlyUsd"] == 10
    assert snapshot["fixedMtdActualUsd"] == 2.4
    assert snapshot["taggedMtd"]["available"] is False
    assert any("no devolvió cargos" in warning for warning in snapshot["warnings"])
    assert next(item for item in snapshot["usageOnlyItems"] if item["service"] == "Amazon S3")["fixedMonthlyUsd"] == 0
