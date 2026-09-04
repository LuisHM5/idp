from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/document-processing/resources/enrichment-lambda"))
spec = importlib.util.spec_from_file_location("test_pedimentos_structured", REPO / "scripts/test-pedimentos.py")
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def leaf(value):
    return {"value": value, "valid": True, "confidence": .9}


def test_structured_expectation_counts_fields_required_and_business_reference() -> None:
    result = module.evaluate_expectation({
        "type": ["PEDIMENTO_ADUANAL"],
        "fields": {"PATENTE": "0823"},
        "required_present": ["PATENTE"],
        "pedimento_consecutivo": "6001630",
    }, [{"documentType": "PEDIMENTO_ADUANAL", "status": "APPROVED", "extractedEntities": {
        "PATENTE": leaf("0823"), "NUMERO_PEDIMENTO_NORMALIZADO": leaf("260708236001630"),
    }}])
    assert result["passed"] is True
    assert result["classificationPassed"] is True
    assert result["fieldChecks"][0]["passed"] is True
    assert result["referenceChecks"][0]["passed"] is True


def test_structured_expectation_reports_classification_failure_without_values() -> None:
    result = module.evaluate_expectation({"type": ["PEDIMENTO_ADUANAL"]}, [{"documentType": "OTRO"}])
    assert result["passed"] is False
    assert result["classificationPassed"] is False
    assert result["fieldChecks"] == []
