"""A qué documentos aplica una regla cruzada.

Una regla de nivel expediente solo dice algo cuando los documentos que nombra
están de verdad en el archivo. `expected_importer_rfc` no declaraba
`documents`, así que corría sobre lo que hubiera: en un PDF con dos facturas
extranjeras del mismo proveedor comparaba el RFC del importador contra sí
mismo y reportaba "importer RFC consistent across documents" — una
verificación que no verifica nada, ocupando lugar en la respuesta.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/document-processing/resources/enrichment-lambda"))

from pipeline.expediente import _cross_importer_rfc  # noqa: E402

REGLA = {
    "rule_type": "expected_importer_rfc",
    "documents": ["PEDIMENTO_ADUANAL", "CFDI_GASTO_TERCERO"],
}


def _doc(tipo, rfc):
    return {"documentType": tipo, "extractedEntities": {"IMPORTADOR_RFC": {"value": rfc}}}


def test_se_salta_cuando_ningun_documento_del_archivo_esta_en_la_regla():
    out = _cross_importer_rfc(
        REGLA,
        [_doc("FACTURA_FLETE_EXTRANJERO", "OFU910626UQ0"),
         _doc("FACTURA_FLETE_EXTRANJERO", "OFU910626UQ0")],
        {},
    )
    assert out["skipped"] is True
    assert "fewer than two documents" in out["message"]


def test_se_salta_con_un_solo_documento_involucrado():
    """Consistencia entre uno y él mismo no existe."""
    out = _cross_importer_rfc(REGLA, [_doc("PEDIMENTO_ADUANAL", "AAA010101AAA")], {})
    assert out["skipped"] is True


def test_sigue_detectando_dos_importadores_distintos_en_un_expediente_real():
    """El caso que la regla existe para atrapar no se puede perder."""
    out = _cross_importer_rfc(
        REGLA,
        [_doc("PEDIMENTO_ADUANAL", "AAA010101AAA"), _doc("CFDI_GASTO_TERCERO", "BBB020202BBB")],
        {},
    )
    assert out["skipped"] is False and out["valid"] is False
    assert "multiple importer RFCs" in out["message"]


def test_con_un_rfc_esperado_corre_aunque_haya_un_solo_documento():
    """Si la app configuró contra QUÉ comparar, ya no es una consistencia
    interna: un solo documento basta para contradecirla."""
    out = _cross_importer_rfc(
        REGLA, [_doc("PEDIMENTO_ADUANAL", "BBB020202BBB")], {"IMPORTER_RFC": "AAA010101AAA"}
    )
    assert out["skipped"] is False and out["valid"] is False


def test_el_catalogo_declara_a_que_documentos_aplica_la_regla():
    """No-regresión de `catalogs/_shared.json`: sin `documents`, la regla
    vuelve a correr sobre cualquier archivo."""
    from document_catalog import get_cross_document_rules

    regla = next(r for r in get_cross_document_rules()
                 if r.get("rule_type") == "expected_importer_rfc")
    tipos = regla.get("documents") or []
    assert "PEDIMENTO_ADUANAL" in tipos
    assert "FACTURA_FLETE_EXTRANJERO" not in tipos
