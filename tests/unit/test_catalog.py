"""Catalog loading tests: PEDIMENTO_ADUANAL only (no AWS)."""

import document_catalog as dc


def setup_module(module):
    dc.reload_catalog()


class TestCustomsCatalog:
    def test_pedimento_type_loaded(self):
        # PEDIMENTO_ADUANAL uses the field-centric DSL (schema_version 4.x,
        # see catalogs/_TEMPLATE.json): fields live in `field_tree`, not the
        # legacy extraction_fields/repeatable_fields lists.
        entry = dc.get_type_entry("PEDIMENTO_ADUANAL")
        assert entry.type_id == "PEDIMENTO_ADUANAL"
        assert "NUMERO_PEDIMENTO_NORMALIZADO" in entry.field_tree
        assert entry.field_tree["PARTIDAS"].type == "array"
        assert "FRACCION_ARANCELARIA" in entry.field_tree["PARTIDAS"].item_fields

    def test_rule_params_preserved(self):
        # `validation_rules` on a field-centric-DSL type is the parsed
        # `cross_field_validation` list (single-field/object-level rules live
        # nested inside field_tree instead — see FieldSpec.validation).
        entry = dc.get_type_entry("PEDIMENTO_ADUANAL")
        formula_rules = [r for r in entry.validation_rules if r.rule_type == "custom_formula"]
        assert formula_rules and all("formula" in r.params for r in formula_rules)
        valor_aduana = next(r for r in formula_rules if r.field == "VALOR_ADUANA")
        assert valor_aduana.params.get("tolerance") == 5

    def test_is_customs_type(self):
        assert dc.is_customs_type("PEDIMENTO_ADUANAL")

    def test_cross_document_rules_loaded(self):
        rules = dc.get_cross_document_rules()
        types = {r.get("rule_type") for r in rules}
        assert "same_pedimento" in types
