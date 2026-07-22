"""Catalog loading tests: customs types, field sets, rich rules (no AWS)."""

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

    def test_field_sets_resolved(self):
        entry = dc.get_type_entry("FACTURA_AGENCIA_ADUANAL")
        # From COMMON_CFDI_FIELDS
        assert "UUID" in entry.extraction_fields
        assert "EMISOR_RFC" in entry.extraction_fields
        # Own fields preserved after the shared set
        assert "PEDIMENTO_REFERENCE" in entry.extraction_fields

    def test_rule_params_preserved(self):
        entry = dc.get_type_entry("PEDIMENTO_ADUANAL")
        formula_rules = [r for r in entry.validation_rules if r.rule_type == "custom_formula"]
        assert formula_rules and "formula" in formula_rules[0].params
        assert formula_rules[0].params.get("tolerance") == 5

    def test_is_customs_type(self):
        assert dc.is_customs_type("PEDIMENTO_ADUANAL")
        assert dc.is_customs_type("CFDI_GASTO_TERCERO")
        assert not dc.is_customs_type("INE")

    def test_cross_document_rules_loaded(self):
        rules = dc.get_cross_document_rules()
        types = {r.get("rule_type") for r in rules}
        assert "same_pedimento" in types
        assert "unique_cfdi_uuid" in types

    def test_legacy_types_still_load(self):
        entry = dc.get_type_entry("INE")
        assert entry.type_id == "INE"
        assert entry.validator_module == "validators.identification"

    def test_validator_modules_importable(self):
        import importlib

        for type_id, entry in dc.get_all_types().items():
            if entry.validator_module:
                module = importlib.import_module(entry.validator_module)
                assert hasattr(module, "validate_document"), entry.validator_module
