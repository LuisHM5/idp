"""Análisis agregado del E2E por dataset (`pnpm test:e2e:pedimento` sin
`--file`, y `pnpm test:e2e:suite`): índice por parámetro, coherencia del
número de pedimento, reglas inertes, veredicto, comparación entre corridas y
estimación de costo. Todo offline: resultados sintéticos con la forma real de
`resolve_field_tree`."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ea = _load("e2e_analysis_test", REPO / "scripts/_e2e_analysis.py")
runner = _load("test_e2e_pedimento_test", REPO / "scripts/test-e2e-pedimento.py")

TYPE_SPEC = {
    "min_confidence": 0.8,
    "fields": {
        "No_Pedimento": {"type": "string", "required": True, "pedimento_role": ["numero", "normalizado"], "unique_key": True},
        "ANIO_PEDIMENTO": {"type": "string", "required": True},
        "ADUANA_DESPACHO": {"type": "string", "required": True},
        "Patente": {"type": "string", "required": True, "pedimento_role": "patente"},
        "NUMERO_CONSECUTIVO": {"type": "string", "required": True},
        "Tipo_Cambio": {"type": "number", "required": True},
        "Referencia": {"type": "string", "required": False},
        "ValorAduana": {"type": "number", "required": True},
        "Valor_ComercialPesos": {"type": "number", "required": True},
        "GI_Fletes": {"type": "number", "required": False},
        "FACTURAS": {"type": "array", "required": True, "item_fields": {
            "Factura": {"type": "string"}, "Total_factura": {"type": "number"},
        }},
    },
}


def leaf(value, confidence=0.95, valid=True, reason=None, source=None):
    node = {"value": value, "confidence": confidence, "valid": valid, "reason": reason}
    if source:
        node["source"] = source
    return node


def entities(**overrides):
    base = {
        "No_Pedimento": leaf("260734356041353", 0.99, source="QR_RECOVERY"),
        "ANIO_PEDIMENTO": leaf("26", 0.99), "ADUANA_DESPACHO": leaf("07", 0.99),
        "Patente": leaf("3435", 0.99), "NUMERO_CONSECUTIVO": leaf("6041353", 0.99),
        "Tipo_Cambio": leaf(17.5), "Referencia": leaf(None, 0.0),
        "ValorAduana": leaf(1105.0), "Valor_ComercialPesos": leaf(1000.0), "GI_Fletes": leaf(100.0),
        "FACTURAS": {"items": [
            {"valid": True, "reason": None, "fields": {"Factura": leaf("A1"), "Total_factura": leaf(10.0)}},
            {"valid": True, "reason": None, "fields": {"Factura": leaf("A2"), "Total_factura": leaf(20.0)}},
        ]},
    }
    base.update(overrides)
    return base


def case(file, ents, status="APPROVED", checks=None, **summary):
    return {
        "file": file,
        "summary": {"documentId": f"id-{file}", "passed": True, "infraPassed": True, "executionStatus": "SUCCEEDED",
                    "groundTruthPassed": None, "businessStatus": "APROBADO", "durationSeconds": 90, "costUsd": 0.1, **summary},
        "result": {"pages": {"totalPages": 5, "ocrPages": 0}, "processingSeconds": 40,
                   "documents": [{"documentType": "PEDIMENTO_ERP", "status": status, "confidence": 0.95,
                                  "pageStart": 1, "pageEnd": 5, "extractedEntities": ents, "checks": checks or []}]},
    }


def field(analysis, path):
    return next(r for r in analysis["fields"] if r["path"] == path)


def test_index_counts_missing_required_as_zero_and_ignores_absent_optionals() -> None:
    cases = [
        case("26 07 3435 6041353 CG.pdf", entities()),
        case("normal_pend.pdf", entities(Tipo_Cambio=leaf(None, 0.0, valid=False, reason="required field 'Tipo_Cambio' missing"))),
    ]
    a = ea.analyze(cases, "PEDIMENTO_ERP", TYPE_SPEC)

    tipo_cambio = field(a, "Tipo_Cambio")
    assert tipo_cambio["occurrences"] == 2 and tipo_cambio["present"] == 1 and tipo_cambio["invalid"] == 1
    assert tipo_cambio["index"] == 47.5  # (0.95 + 0) / 2
    assert tipo_cambio["topReasons"][0][0].startswith("required field")
    assert a["lowestFields"][0]["path"] == "Tipo_Cambio"
    # Opcional vacío y válido: no aplica, no castiga el índice.
    assert field(a, "Referencia")["index"] is None
    assert "Referencia" in a["neverExtracted"]
    # Una fila de arreglo = una ocurrencia, bajo la ruta normalizada.
    assert field(a, "FACTURAS[].Factura")["occurrences"] == 4
    assert a["arrays"] == [{"path": "FACTURAS", "documents": 2, "rowsMin": 2.0, "rowsMean": 2.0, "rowsMax": 2.0, "emptyDocuments": 0}]
    assert any("Tipo_Cambio" in r for r in a["verdict"]["reservations"])


def test_low_confidence_value_is_flagged_and_invalid_is_capped_at_min_confidence() -> None:
    a = ea.analyze([
        case("a.pdf", entities(Tipo_Cambio=leaf(17.5, 0.6))),
        case("b.pdf", entities(Tipo_Cambio=leaf(17.5, 0.97, valid=False, reason="regex"))),
    ], "PEDIMENTO_ERP", TYPE_SPEC)
    row = field(a, "Tipo_Cambio")
    assert row["lowConfidence"] == 1
    assert row["index"] == 70.0  # (0.6 + min(0.97, 0.8)) / 2


def test_filename_reference_shapes() -> None:
    assert ea.reference_from_filename("26 07 3435 6041353 CG.pdf")["numero"] == "260734356041353"
    assert ea.reference_from_filename("OPERADORA P2607 0823 6001840-1.pdf")["numero"] == "260708236001840"
    assert ea.reference_from_filename("2607-0973-6100971_P.PDF-1")["aduana"] == "07"
    assert ea.reference_from_filename("normal_pend.pdf") is None


def test_approved_document_with_wrong_aduana_blocks_the_run() -> None:
    """Caso real: el QR trae pad=70 y el número salió 2670... aprobado con 0.99;
    el nombre del archivo dice aduana 07. Aprobar un valor equivocado es el
    peor resultado posible, así que bloquea."""
    wrong = entities(No_Pedimento=leaf("267009736100971", 0.99, source="QR_RECOVERY"),
                     ADUANA_DESPACHO=leaf("70", 0.99), Patente=leaf("0973", 0.99), NUMERO_CONSECUTIVO=leaf("6100971", 0.99))
    a = ea.analyze([case("2607-0973-6100971_P.PDF", wrong)], "PEDIMENTO_ERP", TYPE_SPEC)

    assert a["coherence"]["numeroCompuesto"] == {"evaluated": 1, "passed": 1}  # coherente consigo mismo…
    assert a["coherence"]["referenciaArchivo"] == {"evaluated": 1, "passed": 0}  # …pero no con el archivo
    assert a["verdict"]["level"] == "NO_CONFIABLE"
    assert "ADUANA_DESPACHO 70 ≠ 07" in a["verdict"]["blockers"][0]

    review = ea.analyze([case("2607-0973-6100971_P.PDF", wrong, status="REVIEW_REQUIRED")], "PEDIMENTO_ERP", TYPE_SPEC)
    assert review["verdict"]["level"] == "CON_RESERVAS"


def test_reference_checks_only_apply_to_types_that_declare_pedimento_components() -> None:
    curp_spec = {"fields": {"CURP": {"type": "string", "required": True}}}
    a = ea.analyze([case("26 07 3435 6041353.pdf", {"CURP": leaf("XEXX010101HNEXXXA4")})], "PEDIMENTO_ERP", curp_spec)
    assert a["coherence"]["referenciaArchivo"]["evaluated"] == 0
    assert a["verdict"]["level"] == "CONFIABLE"


def test_arithmetic_check_uses_the_pedimento_aduanal_formula_with_tolerance() -> None:
    a = ea.analyze([
        case("ok.pdf", entities(ValorAduana=leaf(1104.0))),     # 1000 + 100, dif 4 ≤ 5
        case("bad.pdf", entities(ValorAduana=leaf(1300.0))),    # dif 200
    ], "PEDIMENTO_ERP", TYPE_SPEC)
    assert a["coherence"]["aritmetica"] == {"evaluated": 2, "passed": 1}
    assert [f["file"] for f in a["coherence"]["failures"]] == ["bad.pdf"]
    # Un cuadre que falla es reserva, no bloqueo: el ERP también lo cuadra.
    assert a["verdict"]["level"] == "CON_RESERVAS"


def test_inert_and_failing_rules_are_reported_separately() -> None:
    checks = [
        {"field": "Tipo", "rule": "in", "valid": True, "skipped": True, "severity": "warning", "message": "unknown rule type 'in' skipped"},
        {"field": "FACTURAS[0].Factura", "rule": "regex", "valid": False, "skipped": False, "severity": "error", "message": "no match"},
        {"field": "FACTURAS[1].Factura", "rule": "regex", "valid": True, "skipped": False, "severity": "error", "message": "ok"},
    ]
    a = ea.analyze([case("a.pdf", entities(), status="REVIEW_REQUIRED", checks=checks)], "PEDIMENTO_ERP", TYPE_SPEC)
    assert a["failingRules"][0] | {"files": None} == {
        "rule": "regex", "field": "FACTURAS[].Factura", "evaluated": 2, "failed": 1, "skipped": 0,
        "severity": "error", "files": None, "topMessage": "no match"}
    assert [(r["rule"], r["field"]) for r in a["inertRules"]] == [("in", "Tipo")]
    assert any("no se ejecutan" in r for r in a["verdict"]["reservations"])
    assert a["worstDocuments"][0]["failedRules"] == 1


def test_infra_and_ground_truth_failures_block_and_gates_are_opt_in() -> None:
    broken = case("x.pdf", entities(), passed=False, infraPassed=False)
    a = ea.analyze([broken, case("y.pdf", entities(), status="REJECTED", groundTruthPassed=False, passed=False)],
                   "PEDIMENTO_ERP", TYPE_SPEC)
    assert a["verdict"]["level"] == "NO_CONFIABLE"
    assert any("infraestructura" in b for b in a["verdict"]["blockers"])
    assert any("ground truth" in b for b in a["verdict"]["blockers"])

    healthy = [case("a.pdf", entities()), case("b.pdf", entities(), status="REVIEW_REQUIRED")]
    assert ea.analyze(healthy, "PEDIMENTO_ERP", TYPE_SPEC)["verdict"]["level"] == "CONFIABLE"
    gated = ea.analyze(healthy, "PEDIMENTO_ERP", TYPE_SPEC, min_approval_rate=0.9)
    assert gated["verdict"]["level"] == "NO_CONFIABLE"


def test_ground_truth_only_applies_to_the_declared_type() -> None:
    spec = {"expect_documents": [{"type": ["PEDIMENTO_ADUANAL"], "fields": {"PATENTE": "0823"}}]}
    assert ea.applicable_expectations(spec, "PEDIMENTO_ERP") == ([], ["PEDIMENTO_ADUANAL"])
    assert len(ea.applicable_expectations(spec, "PEDIMENTO_ADUANAL")[0]) == 1
    assert ea.applicable_expectations(None, "PEDIMENTO_ERP") == ([], [])


def test_comparison_flags_changed_values_as_reservation() -> None:
    previous = ea.analyze([case("a.pdf", entities())], "PEDIMENTO_ERP", TYPE_SPEC)
    current = ea.analyze([case("a.pdf", entities(Tipo_Cambio=leaf(18.1)))], "PEDIMENTO_ERP", TYPE_SPEC)
    comparison = ea.compare_runs(current, json.loads(json.dumps(previous, default=str)))
    assert comparison["valueChanges"] == [{"file": "a.pdf", "field": "Tipo_Cambio", "before": 17.5, "after": 18.1}]
    ea.apply_comparison(current, comparison)
    assert current["verdict"]["level"] == "CON_RESERVAS"
    assert "## 10. Contra la corrida anterior" in ea.render_markdown(current)
    assert "Contra la corrida anterior" in ea.render_text(current, color=False)


def test_find_previous_analysis_reads_dataset_and_suite_layouts(tmp_path: Path) -> None:
    def write(rel: str, docs_dir: str) -> None:
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"docType": "PEDIMENTO_ERP", "docsDir": docs_dir, "generatedAt": rel}))

    write("20260901-000000-abc-e2e-dataset/analisis.json", "test-docs/PEDIMENTO_ERP")
    write("20260910-000000-abc-e2e-suite/PEDIMENTO_ERP/analisis.json", "test-docs/e2e-suite/PEDIMENTO_ERP")
    write("20260914-000000-abc-e2e-suite/PEDIMENTO_ERP/analisis.json", "test-docs/e2e-suite/PEDIMENTO_ERP")

    found = ea.find_previous_analysis(tmp_path, "PEDIMENTO_ERP", "PEDIMENTO_ERP",
                                      exclude=tmp_path / "20260914-000000-abc-e2e-suite")
    assert found["generatedAt"].startswith("20260910")
    assert ea.find_previous_analysis(tmp_path, "CURP", "CURP") is None


def test_real_erp_catalog_resolves_roles_and_unique_key() -> None:
    spec = ea.load_type_spec("PEDIMENTO_ERP")
    assert spec is not None
    roles = ea.pedimento_roles(spec)
    assert roles["numero"] == roles["normalizado"] == "No_Pedimento"
    assert roles["patente"] == "Patente" and roles["aduana"] == "ADUANA_DESPACHO"
    assert ea.unique_key_fields(spec) == ["No_Pedimento"]
    assert "FACTURAS[].Factura" in ea.catalog_fields(spec)
    assert "PEDIMENTO_ERP" in ea.active_type_ids() and "CURP" in ea.active_type_ids()


def test_markdown_and_text_render_every_section() -> None:
    a = ea.analyze([case("26 07 3435 6041353 CG.pdf", entities())], "PEDIMENTO_ERP", TYPE_SPEC, env="prod", docs_dir="x")
    md = ea.render_markdown(a)
    for heading in ("## Veredicto", "## 1. Ejecución", "## 3. Confianza por parámetro", "## 5. Reglas del motor",
                    "## 6. Coherencia interna", "## 9. Por archivo"):
        assert heading in md
    text = ea.render_text(a, color=False)
    assert "Parámetros con índice más bajo" in text and "\033[" not in text
    json.dumps(a)  # analisis.json se escribe tal cual
    assert ea.run_summary(a)["verdict"] == "CONFIABLE"


def test_each_run_measures_its_cost_estimate_against_the_audit() -> None:
    def with_cost(name, real, estimated, high, bedrock_real, bedrock_est):
        c = case(name, entities(), costUsd=real)
        c["summary"]["costEstimate"] = {"totalUsd": estimated, "totalHighUsd": high, "textractUsd": 0.0465,
                                        "bedrockUsd": bedrock_est, "lambdaUsd": 0.001, "inputTokens": 7000, "outputTokens": 2000}
        c["summary"]["costBreakdown"] = {"totalUsd": real, "textractUsd": 0.0465, "bedrockUsd": bedrock_real,
                                         "lambdaUsd": 0.0007, "inputTokens": 5000, "outputTokens": 1800}
        return c

    a = ea.analyze([with_cost("a.pdf", 0.0629, 0.0546, 0.1, 0.0157, 0.0071),
                    with_cost("b.pdf", 0.0100, 0.0300, 0.035, 0.0050, 0.0250)], "PEDIMENTO_ERP", TYPE_SPEC)
    cost = a["execution"]["costEstimate"]
    assert cost["files"] == 2 and cost["realUsd"] == pytest.approx(0.0729)
    assert cost["aggregateErr"] == pytest.approx((0.0846 - 0.0729) / 0.0729, abs=1e-4)
    assert cost["byService"]["textractUsd"] == {"estimated": 0.093, "real": 0.093}
    assert cost["withinCeiling"] == 2 and cost["worst"][1] == "b.pdf"
    assert "Estimado" in ea.render_text(a, color=False)
    assert "Costo estimado antes de correr" in ea.render_markdown(a)

    far = ea.analyze([with_cost("c.pdf", 0.01, 0.05, 0.06, 0.005, 0.045)], "PEDIMENTO_ERP", TYPE_SPEC)
    assert any("costos:validar" in note for note in far["verdict"]["notes"])


def test_ground_truth_paths_accept_erp_mixed_case_names() -> None:
    tp = runner.tp
    ents = {"No_Pedimento": leaf("260708236001630"),
            "FACTURAS": {"items": [{"fields": {"Factura": leaf("SJ191703-1")}}]}}
    assert tp._leaf_value(ents, "No_Pedimento") == "260708236001630"
    assert tp._leaf_value(ents, "FACTURAS[0].Factura") == "SJ191703-1"
