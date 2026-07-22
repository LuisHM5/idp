"""Unit tests for the dynamic validation engine (no AWS required)."""

from datetime import date

from document_catalog import TypeRegistryEntry, ValidationRule
from validators import engine
from validators.engine import ValidationContext, evaluate_formula, run_rules


def _entry(**kwargs) -> TypeRegistryEntry:
    defaults = dict(
        type_id="TEST",
        extraction_fields=[],
        required_fields=[],
        validation_rules=[],
    )
    defaults.update(kwargs)
    return TypeRegistryEntry(**defaults)


def _rule(field, rule_type, pattern=None, message="", **params) -> ValidationRule:
    return ValidationRule(field=field, rule_type=rule_type, pattern=pattern, message=message, params=params)


class TestFormulaEvaluator:
    def test_simple_sum(self):
        value, notes = evaluate_formula("A + B", {"A": "1,000.50", "B": "2"})
        assert value == 1002.5

    def test_missing_counts_as_zero(self):
        value, notes = evaluate_formula("A + B - C", {"A": "10"})
        assert value == 10.0
        assert any("counted as 0" in n for n in notes)

    def test_sum_call(self):
        entities = {"ITEMS": [{"AMOUNT": "10.5"}, {"AMOUNT": "4.5"}, {"AMOUNT": None}]}
        value, notes = evaluate_formula("SUM(ITEMS.AMOUNT)", entities)
        assert value == 15.0

    def test_pedimento_valor_aduana_formula(self):
        entities = {
            "PRECIO_PAGADO_VALOR_COMERCIAL": "121,840.00",
            "SEGUROS": None, "FLETES": "20,877", "EMBALAJES": None,
            "OTROS_INCREMENTABLES": None, "TRANSPORTE_DECREMENTABLE": None,
            "SEGURO_DECREMENTABLE": None, "CARGA_DECREMENTABLE": None,
            "DESCARGA_DECREMENTABLE": None, "OTROS_DECREMENTABLES": None,
        }
        formula = (
            "PRECIO_PAGADO_VALOR_COMERCIAL + SEGUROS + FLETES + EMBALAJES + "
            "OTROS_INCREMENTABLES - TRANSPORTE_DECREMENTABLE - SEGURO_DECREMENTABLE - "
            "CARGA_DECREMENTABLE - DESCARGA_DECREMENTABLE - OTROS_DECREMENTABLES"
        )
        value, _ = evaluate_formula(formula, entities)
        assert value == 142717.0

    def test_malformed_formula(self):
        value, notes = evaluate_formula("A +* B", {"A": "1"})
        assert value is None

    def test_no_code_injection(self):
        value, notes = evaluate_formula("__import__('os').system('true')", {})
        assert value is None


class TestRules:
    def test_regex_pass_and_fail(self):
        entry = _entry(validation_rules=[
            _rule("NUMERO_PEDIMENTO_NORMALIZADO", "regex", pattern=r"^\d{15}$"),
        ])
        ok = run_rules({"NUMERO_PEDIMENTO_NORMALIZADO": "260734356041248"}, entry)
        assert all(c.valid for c in ok)
        bad = run_rules({"NUMERO_PEDIMENTO_NORMALIZADO": "12345"}, entry)
        assert any(not c.valid for c in bad)

    def test_required_presence(self):
        entry = _entry(required_fields=["UUID"])
        checks = run_rules({}, entry)
        assert checks[0].rule == "presence" and not checks[0].valid

    def test_cfdi_total_formula(self):
        entry = _entry(validation_rules=[
            _rule("TOTAL", "cfdi_total",
                  formula="SUBTOTAL - DESCUENTO + TOTAL_IMPUESTOS_TRASLADADOS - TOTAL_IMPUESTOS_RETENIDOS",
                  tolerance=0.02),
        ])
        entities = {"TOTAL": "9,359.84", "SUBTOTAL": "8,068.83",
                    "TOTAL_IMPUESTOS_TRASLADADOS": "1,291.01"}
        checks = run_rules(entities, entry)
        assert all(c.valid for c in checks)

    def test_sum_array(self):
        entry = _entry(validation_rules=[
            _rule("TOTAL_PEDIMENTO", "sum_array", source="CONTRIBUCIONES.IMPORTE", tolerance=1),
        ])
        entities = {
            "TOTAL_PEDIMENTO": "61,934",
            "CONTRIBUCIONES": [
                {"CLAVE_CONTRIBUCION": "DTA", "IMPORTE": "108"},
                {"CLAVE_CONTRIBUCION": "IVA", "IMPORTE": "28,159"},
                {"CLAVE_CONTRIBUCION": "PRV", "IMPORTE": "330"},
                {"CLAVE_CONTRIBUCION": "IVA/PRV", "IMPORTE": "53"},
                {"CLAVE_CONTRIBUCION": "IGI", "IMPORTE": "33,284"},
            ],
        }
        checks = run_rules(entities, entry)
        assert all(c.valid for c in checks), [c.message for c in checks]

    def test_sum_array_mismatch(self):
        entry = _entry(validation_rules=[
            _rule("TOTAL", "sum_array", source="ITEMS.AMOUNT", tolerance=0.02),
        ])
        entities = {"TOTAL": "100", "ITEMS": [{"AMOUNT": "40"}, {"AMOUNT": "40"}]}
        checks = run_rules(entities, entry)
        assert any(not c.valid for c in checks)

    def test_minimum_items(self):
        entry = _entry(validation_rules=[_rule("DETECTED_DOCUMENTS", "minimum_items", value=1)])
        assert run_rules({"DETECTED_DOCUMENTS": []}, entry)[0].valid is False
        assert run_rules({"DETECTED_DOCUMENTS": [{"DOCUMENT_TYPE": "X"}]}, entry)[0].valid is True

    def test_regex_each(self):
        entry = _entry(validation_rules=[
            _rule("CONTAINER_NUMBERS.CONTAINER_NUMBER", "regex_each",
                  pattern=r"^[A-Z]{4}\d{7}$", allow_empty=True),
        ])
        entities = {"CONTAINER_NUMBERS": [
            {"CONTAINER_NUMBER": "MSKU1234567"}, {"CONTAINER_NUMBER": "BAD"},
        ]}
        checks = run_rules(entities, entry)
        assert any(not c.valid for c in checks)

    def test_cross_document_match_pedimento(self):
        entry = _entry(validation_rules=[
            _rule("PEDIMENTO_REFERENCE", "cross_document_match",
                  target_document_type="PEDIMENTO_ADUANAL",
                  target_field="NUMERO_PEDIMENTO_NORMALIZADO"),
        ])
        ctx = ValidationContext(sibling_documents=[
            {"type": "PEDIMENTO_ADUANAL",
             "entities": {"NUMERO_PEDIMENTO_NORMALIZADO": "260708236001187"}},
        ])
        ok = run_rules({"PEDIMENTO_REFERENCE": "P0823 6001187"}, entry, ctx)
        assert all(c.valid for c in ok), [c.message for c in ok]
        bad = run_rules({"PEDIMENTO_REFERENCE": "P0823 9999999"}, entry, ctx)
        assert any(not c.valid for c in bad)

    def test_cross_document_missing_target_is_skipped(self):
        entry = _entry(validation_rules=[
            _rule("PEDIMENTO_REFERENCE", "cross_document_match",
                  target_document_type="PEDIMENTO_ADUANAL",
                  target_field="NUMERO_PEDIMENTO_NORMALIZADO"),
        ])
        checks = run_rules({"PEDIMENTO_REFERENCE": "6001187"}, entry, ValidationContext())
        assert checks[0].skipped and checks[0].valid

    def test_negative_keyword_detects_misclassified_invoice(self):
        entry = _entry(validation_rules=[
            _rule("TIPO_PERMISO", "negative_keyword",
                  keywords=["FACTURA ELECTRONICA", "CFDI", "FOLIO FISCAL", "UUID"]),
        ])
        ctx = ValidationContext(document_text="Factura Electrónica ... Folio Fiscal (UUID)")
        checks = run_rules({"TIPO_PERMISO": "certificado fitosanitario"}, entry, ctx)
        assert any(not c.valid for c in checks)

    def test_expected_value_by_app(self):
        entry = _entry(validation_rules=[_rule("RECEPTOR_RFC", "expected_value_by_app")])
        ctx = ValidationContext(expected_values={"IMPORTER_RFC": "OFU910626UQ0"})
        ok = run_rules({"RECEPTOR_RFC": "OFU910626UQ0"}, entry, ctx)
        assert all(c.valid for c in ok)
        bad = run_rules({"RECEPTOR_RFC": "XAXX010101000"}, entry, ctx)
        assert any(not c.valid for c in bad)
        skipped = run_rules({"RECEPTOR_RFC": "XAXX010101000"}, entry, ValidationContext())
        assert skipped[0].skipped

    def test_date_expiry_allow_empty(self):
        entry = _entry(validation_rules=[
            _rule("FECHA_VIGENCIA_FIN", "date_expiry", allow_empty=True),
        ])
        assert run_rules({}, entry) == []
        ctx = ValidationContext(today=date(2026, 7, 22))
        expired = run_rules({"FECHA_VIGENCIA_FIN": "01/01/2026"}, entry, ctx)
        assert any(not c.valid for c in expired)

    def test_unknown_rule_type_is_warning_skip(self):
        entry = _entry(validation_rules=[_rule("X", "rule_del_futuro")])
        checks = run_rules({"X": "1"}, entry)
        assert checks[0].skipped and checks[0].severity == "warning"


class TestFieldResolution:
    def _resolve(self, entities, confidences, entry):
        checks = run_rules(entities, entry)
        return engine.resolve_fields(entities, confidences, checks, entry), checks

    def test_statuses(self):
        entry = _entry(
            extraction_fields=["UUID", "TOTAL", "OPCIONAL", "BORROSO"],
            required_fields=["UUID"],
            validation_rules=[
                _rule("TOTAL", "regex", pattern=r"^\d+$"),
            ],
        )
        entities = {"UUID": None, "TOTAL": "abc", "OPCIONAL": None, "BORROSO": "algo"}
        confidences = {"TOTAL": 0.95, "BORROSO": 0.4}
        fields, checks = self._resolve(entities, confidences, entry)
        assert fields["UUID"]["status"] == engine.NOT_FOUND
        assert fields["TOTAL"]["status"] == engine.INVALID
        assert fields["OPCIONAL"]["status"] == engine.NOT_APPLICABLE
        assert fields["BORROSO"]["status"] == engine.LOW_CONFIDENCE

    def test_document_status_rejected_when_nothing_valid(self):
        entry = _entry(extraction_fields=["UUID"], required_fields=["UUID"])
        fields, checks = self._resolve({"UUID": None}, {}, entry)
        assert engine.document_status(fields, checks, entry) == engine.DOC_REJECTED

    def test_document_status_partial(self):
        entry = _entry(
            extraction_fields=["UUID", "TOTAL"],
            required_fields=["UUID", "TOTAL"],
            validation_rules=[_rule("TOTAL", "regex", pattern=r"^\d+$")],
        )
        entities = {"UUID": "6B2AF82F-7E4F-4606-90CA-E7F44BF2F011", "TOTAL": "abc"}
        fields, checks = self._resolve(entities, {"UUID": 0.99, "TOTAL": 0.99}, entry)
        assert engine.document_status(fields, checks, entry) == engine.DOC_PARTIAL

    def test_document_status_approved(self):
        entry = _entry(
            extraction_fields=["UUID"],
            required_fields=["UUID"],
            validation_rules=[_rule("UUID", "regex",
                                    pattern=r"^[0-9A-Fa-f-]{36}$")],
        )
        entities = {"UUID": "6B2AF82F-7E4F-4606-90CA-E7F44BF2F011"}
        fields, checks = self._resolve(entities, {"UUID": 0.99}, entry)
        assert engine.document_status(fields, checks, entry) == engine.DOC_APPROVED

    def test_normalized_value_never_replaces_extracted(self):
        entry = _entry(extraction_fields=["TOTAL"])
        fields, _ = self._resolve({"TOTAL": "$5,672.16"}, {"TOTAL": 0.99}, entry)
        assert fields["TOTAL"]["extractedValue"] == "$5,672.16"
        assert fields["TOTAL"]["normalizedValue"] == 5672.16
