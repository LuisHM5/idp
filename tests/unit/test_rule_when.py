"""`when` deja que el CATÁLOGO diga a qué documentos aplica una regla.

Sin él, una regla aplica a todo documento del tipo, así que un tipo que acepta
variantes del mismo documento (la constancia fiscal de un contribuyente
mexicano o la de uno extranjero) solo tenía dos salidas: aflojar la regla para
todos, o reprobar siempre a la variante que no la cumple. Con `when` la regla
propia de una variante se queda con esa variante.

Es del motor, no de un tipo: vale para cualquier `rule_type` de cualquier
catálogo, y usa los mismos operadores cerrados que `derived`.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from validators.engine import (  # noqa: E402
    DOC_APPROVED,
    DOC_REJECTED,
    document_status_from_checks,
    resolve_field_tree,
)

CONFIANZA = 0.95
ES_MEX = {"field": "PAIS", "eq": "MEX"}


def catalogo(reglas_folio, reglas_domicilio=(), reglas_fila=()):
    return {
        "schema_version": "1.0.0",
        "display_name": "Tipo de prueba",
        "description": "t",
        "min_confidence": 0.8,
        "fields": {
            "PAIS": {"type": "string", "required": False, "hint": "h", "validation": []},
            "FOLIO": {"type": "string", "required": False, "hint": "h", "validation": list(reglas_folio)},
            "DOMICILIO": {
                "type": "object", "required": False, "hint": "h",
                "validation": list(reglas_domicilio),
                "fields": {"CP": {"type": "string", "required": False, "hint": "h", "validation": []}},
            },
            "FILAS": {
                "type": "array", "required": False, "hint": "h", "validation": [],
                "item_fields": {
                    "CLASE": {"type": "string", "required": False, "hint": "h", "validation": []},
                    "CODIGO": {"type": "string", "required": False, "hint": "h", "validation": list(reglas_fila)},
                },
            },
        },
    }


def resolver(entidades, **reglas):
    entry = _parse_entry("TIPO_PRUEBA", catalogo(reglas.pop("folio", ()), **reglas))
    arbol, checks, _ = resolve_field_tree(entidades, {}, entry, default_confidence=CONFIANZA)
    return arbol, checks


def checks_de(checks, campo):
    return [c for c in checks if c.field == campo]


REGEX_MEX = {"rule_type": "regex", "pattern": "^[A-Z]{3}\\d{3}$", "message": "folio mexicano inválido", "when": ES_MEX}


def test_la_regla_corre_cuando_la_condicion_se_cumple():
    arbol, checks = resolver({"PAIS": "MEX", "FOLIO": "81-2154193"}, folio=[REGEX_MEX])
    (check,) = checks_de(checks, "FOLIO")
    assert not check.valid and check.message == "folio mexicano inválido"
    assert arbol["FOLIO"]["valid"] is False

    arbol, checks = resolver({"PAIS": "MEX", "FOLIO": "ABC123"}, folio=[REGEX_MEX])
    assert checks_de(checks, "FOLIO")[0].valid
    assert arbol["FOLIO"]["valid"] is True


def test_la_regla_no_opina_cuando_la_condicion_no_se_cumple():
    """Ni falla ni deja rastro: para este documento la regla no existe."""
    arbol, checks = resolver({"PAIS": "USA", "FOLIO": "81-2154193"}, folio=[REGEX_MEX])
    assert checks_de(checks, "FOLIO") == []
    assert arbol["FOLIO"] == {
        "value": "81-2154193", "confidence": CONFIANZA, "valid": True,
        "reason": None, "source": "MODEL_EXTRACTION",
    }
    assert document_status_from_checks(checks) == DOC_APPROVED


def test_sin_el_campo_de_la_condicion_la_regla_no_aplica():
    """Un PAIS que no se leyó no es "MEX": quien lo exija lo declara `required`."""
    _, checks = resolver({"FOLIO": "81-2154193"}, folio=[REGEX_MEX])
    assert checks_de(checks, "FOLIO") == []


def test_una_regla_sin_when_se_comporta_como_siempre():
    sin_when = {k: v for k, v in REGEX_MEX.items() if k != "when"}
    _, checks = resolver({"PAIS": "USA", "FOLIO": "81-2154193"}, folio=[sin_when])
    assert not checks_de(checks, "FOLIO")[0].valid


def test_presencia_condicional_es_el_required_por_variante():
    """`required` no se puede condicionar; una `presence` con `when` sí."""
    regla = {"rule_type": "presence", "message": "falta el folio", "when": ES_MEX}
    _, checks = resolver({"PAIS": "MEX"}, folio=[regla])
    assert not checks_de(checks, "FOLIO")[0].valid
    _, checks = resolver({"PAIS": "USA"}, folio=[regla])
    assert checks_de(checks, "FOLIO") == []


def test_on_fail_gradua_solo_la_regla_que_si_aplico():
    regla = {**REGEX_MEX, "on_fail": "reject"}
    _, checks = resolver({"PAIS": "MEX", "FOLIO": "x"}, folio=[regla])
    assert document_status_from_checks(checks) == DOC_REJECTED
    _, checks = resolver({"PAIS": "USA", "FOLIO": "x"}, folio=[regla])
    assert document_status_from_checks(checks) == DOC_APPROVED


@pytest.mark.parametrize("pais, aplica", [("MEX", True), (" mex ", True), ("MX", True), ("México", True), ("USA", False)])
def test_los_operadores_son_los_de_derived(pais, aplica):
    regla = {**REGEX_MEX, "when": {"field": "PAIS", "matches": "^\\s*(MEX|MX|M[EÉ]XICO)\\s*$"}}
    _, checks = resolver({"PAIS": pais, "FOLIO": "x"}, folio=[regla])
    assert bool(checks_de(checks, "FOLIO")) is aplica


def test_varias_condiciones_se_combinan_con_and():
    regla = {**REGEX_MEX, "when": [ES_MEX, {"field": "DOMICILIO.CP", "not_empty": True}]}
    _, checks = resolver({"PAIS": "MEX", "FOLIO": "x"}, folio=[regla])
    assert checks_de(checks, "FOLIO") == []
    _, checks = resolver({"PAIS": "MEX", "FOLIO": "x", "DOMICILIO": {"CP": "25280"}}, folio=[regla])
    assert not checks_de(checks, "FOLIO")[0].valid


def test_una_regla_de_objeto_con_subfield_tambien_se_condiciona():
    regla = {"rule_type": "regex", "subfield": "CP", "pattern": "^\\d{5}$", "message": "CP inválido", "when": ES_MEX}
    arbol, checks = resolver({"PAIS": "MEX", "DOMICILIO": {"CP": "K1A 0B1"}}, reglas_domicilio=[regla])
    assert not checks_de(checks, "DOMICILIO.CP")[0].valid
    assert arbol["DOMICILIO"]["fields"]["CP"]["valid"] is False
    arbol, checks = resolver({"PAIS": "CAN", "DOMICILIO": {"CP": "K1A 0B1"}}, reglas_domicilio=[regla])
    assert checks_de(checks, "DOMICILIO.CP") == []
    assert arbol["DOMICILIO"]["fields"]["CP"]["valid"] is True


def test_dentro_de_item_fields_la_condicion_lee_la_misma_fila():
    regla = {"rule_type": "regex", "pattern": "^\\d+$", "message": "código inválido",
             "when": {"field": "CLASE", "eq": "NUMERICA"}}
    filas = [{"CLASE": "NUMERICA", "CODIGO": "A1"}, {"CLASE": "LIBRE", "CODIGO": "A1"}]
    arbol, checks = resolver({"FILAS": filas}, reglas_fila=[regla])
    assert [c.field for c in checks if not c.valid] == ["FILAS[0].CODIGO"]
    assert arbol["FILAS"]["items"][0]["valid"] is False
    assert arbol["FILAS"]["items"][1]["valid"] is True


@pytest.mark.parametrize("when, problema", [
    ({"eq": "MEX"}, "needs a 'field'"),
    ({"field": "PAIS"}, "has no operator"),
    ({"field": "PAIS", "equals": "MEX"}, "unknown operator"),
    ("PAIS == MEX", "needs a 'field'"),
    ([], "no conditions"),
])
def test_un_when_mal_escrito_se_reporta_como_regla_omitida(when, problema):
    """Un typo no puede ni prender la regla para todos ni apagarla sin avisar:
    sale `skipped`, igual que un `rule_type` que no existe."""
    for pais in ("MEX", "USA"):
        _, checks = resolver({"PAIS": pais, "FOLIO": "x"}, folio=[{**REGEX_MEX, "when": when}])
        (check,) = checks_de(checks, "FOLIO")
        assert check.skipped and check.severity == "warning"
        assert "invalid 'when'" in check.message and problema in check.message


# --- un objeto obligatorio sin un solo dato no está "presente" ---------------


def test_un_objeto_obligatorio_con_todos_sus_subcampos_vacios_cuenta_como_faltante():
    """El extractor siempre arma el dict del objeto declarado, con cada
    subcampo en None si el modelo no leyó nada: eso no es tener el dato."""
    tipo = catalogo(())
    tipo["fields"]["DOMICILIO"]["required"] = True
    entry = _parse_entry("TIPO_PRUEBA", tipo)

    def presencia(domicilio):
        _, checks, _ = resolve_field_tree({"DOMICILIO": domicilio}, {}, entry, default_confidence=CONFIANZA)
        return next(c for c in checks if c.field == "DOMICILIO" and c.rule == "presence").valid

    assert presencia({"CP": None}) is False
    assert presencia({"CP": "  "}) is False
    assert presencia({}) is False
    assert presencia({"CP": "25280"}) is True
