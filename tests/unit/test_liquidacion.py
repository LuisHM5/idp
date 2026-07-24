"""Cuadro de liquidación normalization (config-driven, no AWS)."""

import document_catalog as dc
from pipeline.liquidacion import build_liquidacion_normalizada


def setup_module(module):
    dc.reload_catalog()


def _entry():
    return dc.get_type_entry("PEDIMENTO_ADUANAL")


class TestLiquidacionNormalizada:
    def test_concepts_mapped_by_alias(self):
        entity_map = {
            "CUADRO_LIQUIDACION.CONCEPTOS": [
                {"CLAVE_CONTRIBUCION": "DTA", "FORMA_PAGO": "0", "IMPORTE": "383"},
                {"CLAVE_CONTRIBUCION": "IGI", "FORMA_PAGO": "0", "IMPORTE": "1,200.50"},
                {"CLAVE_CONTRIBUCION": "IVA", "DESCRIPCION": "IVA PRV", "IMPORTE": "0"},
            ],
        }
        liq = build_liquidacion_normalizada(entity_map, _entry())
        assert liq is not None
        assert liq["DTA"] == 383
        assert liq["IGI"] == 1200.5
        assert liq["IVA"] == 0

    def test_ieps_from_contribuciones_filter(self):
        entity_map = {
            "CONTRIBUCIONES": [
                {"CLAVE_CONTRIBUCION": "IEPS", "TASA": "8", "FORMA_PAGO": "0", "IMPORTE": "150"},
                {"CLAVE_CONTRIBUCION": "IEPS", "TASA": "30", "FORMA_PAGO": "0", "IMPORTE": "42"},
            ],
        }
        liq = build_liquidacion_normalizada(entity_map, _entry())
        assert liq["IEPS_8"] == 150
        assert liq["IEPS_30"] == 42
        assert liq["FORMA_PAGO_IEPS"] == "0"

    def test_concepts_fall_back_to_contribuciones(self):
        # Real case: model files the liquidation line items under CONTRIBUCIONES
        # and leaves CUADRO_LIQUIDACION.CONCEPTOS empty. IVA/PRV must still resolve.
        entity_map = {
            "CUADRO_LIQUIDACION.CONCEPTOS": None,
            "CONTRIBUCIONES": [
                {"CLAVE_CONTRIBUCION": "PRV", "FORMA_PAGO": "0", "IMPORTE": "330"},
                {"CLAVE_CONTRIBUCION": "IVA PRV", "FORMA_PAGO": "0", "IMPORTE": "53"},
            ],
        }
        liq = build_liquidacion_normalizada(entity_map, _entry())
        assert liq["IVA"] == 53
        assert liq["PRV"] == 330

    def test_totals_from_scalars(self):
        entity_map = {"TOTAL_EFECTIVO": "383", "TOTAL_OTROS": "0"}
        liq = build_liquidacion_normalizada(entity_map, _entry())
        assert liq["TOTAL_EFECTIVO"] == 383
        assert liq["TOTAL_OTROS"] == 0

    def test_missing_data_falls_back_to_defaults(self):
        liq = build_liquidacion_normalizada({}, _entry())
        assert liq["DTA"] == 0
        assert liq["IEPS_8"] == 0

    def test_type_without_config_returns_none(self):
        # A CFDI type declares no cuadro_liquidacion_fields → no output.
        entry = dc.get_type_entry("FACTURA_AGENCIA_ADUANAL")
        assert build_liquidacion_normalizada({}, entry) is None
