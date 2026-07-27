"""Catalog loading tests: PEDIMENTO_ADUANAL only (no AWS)."""

import document_catalog as dc


def setup_module(module):
    dc.reload_catalog()


class TestCustomsCatalog:
    def test_pedimento_type_loaded(self):
        entry = dc.get_type_entry("PEDIMENTO_ADUANAL")
        assert entry.type_id == "PEDIMENTO_ADUANAL"
        assert "NUMERO_PEDIMENTO_NORMALIZADO" in entry.extraction_fields
        assert "PARTIDAS" in entry.repeatable_fields
        assert "FRACCION_ARANCELARIA" in entry.repeatable_fields["PARTIDAS"]

    def test_rule_params_preserved(self):
        entry = dc.get_type_entry("PEDIMENTO_ADUANAL")
        formula_rules = [r for r in entry.validation_rules if r.rule_type == "custom_formula"]
        assert formula_rules and "formula" in formula_rules[0].params
        assert formula_rules[0].params.get("tolerance") == 5

    def test_is_customs_type(self):
        assert dc.is_customs_type("PEDIMENTO_ADUANAL")

    def test_cross_document_rules_loaded(self):
        rules = dc.get_cross_document_rules()
        types = {r.get("rule_type") for r in rules}
        assert "same_pedimento" in types
