"""Unit tests for deterministic normalizers (no AWS required)."""

from pipeline.normalize import (
    normalize_field,
    normalize_pedimento,
    normalize_rfc,
    normalize_uuid,
    parse_amount,
    parse_date_iso,
    pedimento_consecutivo,
)


class TestParseAmount:
    def test_currency_with_commas(self):
        assert parse_amount("$ 142,717.00") == 142717.0

    def test_plain_decimal(self):
        assert parse_amount("927.0000") == 927.0

    def test_thousands_no_decimals(self):
        assert parse_amount("10,800") == 10800.0

    def test_negative(self):
        assert parse_amount("-53.00") == -53.0

    def test_numeric_passthrough(self):
        assert parse_amount(5252) == 5252.0

    def test_garbage_returns_none(self):
        assert parse_amount("N/A") is None
        assert parse_amount("") is None
        assert parse_amount(None) is None

    def test_european_format(self):
        assert parse_amount("1.234,56") == 1234.56


class TestParseDate:
    def test_iso(self):
        assert parse_date_iso("2026-05-21") == "2026-05-21"

    def test_iso_datetime(self):
        assert parse_date_iso("2026-05-21T13:19:00") == "2026-05-21"

    def test_dmy_slash(self):
        assert parse_date_iso("14/05/2026") == "2026-05-14"

    def test_dmy_with_time_suffix(self):
        assert parse_date_iso("27/05/2026 12:50:11 p. m.") == "2026-05-27"

    def test_spanish_long_form(self):
        assert parse_date_iso("21 de mayo de 2026") == "2026-05-21"

    def test_invalid(self):
        assert parse_date_iso("no es fecha") is None
        assert parse_date_iso("32/13/2026") is None


class TestPedimento:
    def test_normalize_spaced(self):
        assert normalize_pedimento("26 07 3435 6041248") == "260734356041248"

    def test_consecutivo_full_number(self):
        assert pedimento_consecutivo("26 07 3435 6041248") == "6041248"

    def test_consecutivo_partial_reference(self):
        assert pedimento_consecutivo("P0823 6001187") == "6001187"

    def test_consecutivo_dashed(self):
        assert pedimento_consecutivo("-999-9999-6041248") == "6041248"

    def test_consecutivo_bare(self):
        assert pedimento_consecutivo("6041248") == "6041248"

    def test_too_short(self):
        assert pedimento_consecutivo("123") is None


class TestIds:
    def test_rfc_moral(self):
        assert normalize_rfc(" ofu910626uq0 ") == "OFU910626UQ0"

    def test_rfc_invalid(self):
        assert normalize_rfc("NOPE") is None

    def test_uuid(self):
        assert (
            normalize_uuid("6b2af82f-7e4f-4606-90ca-e7f44bf2f011")
            == "6B2AF82F-7E4F-4606-90CA-E7F44BF2F011"
        )


class TestNormalizeField:
    def test_amount_field(self):
        assert normalize_field("TOTAL", "$5,672.16") == 5672.16

    def test_date_field(self):
        assert normalize_field("FECHA_PAGO", "19/05/2026") == "2026-05-19"

    def test_pedimento_field(self):
        assert normalize_field("NUMERO_PEDIMENTO", "26 07 3435 6041248") == "260734356041248"

    def test_rfc_field(self):
        assert normalize_field("RECEPTOR_RFC", "ofu910626uq0") == "OFU910626UQ0"

    def test_lists_untouched(self):
        items = [{"IMPORTE": "1.00"}]
        assert normalize_field("CONCEPTOS", items) is items

    def test_fecha_not_treated_as_amount(self):
        assert normalize_field("FECHA_FACTURA", "2026-05-21") == "2026-05-21"
