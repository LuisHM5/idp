"""Un objeto o una lista que el modelo envolvió en el sobre de los escalares.

El formato de respuesta pide `{"value": ..., "confidence": ...}` solo alrededor
de los escalares; los objetos llevan sus subcampos directo. MiniMax M2.5 le pone
el sobre también a los objetos (medido sobre un extracto real del registro
mercantil holandés): el domicilio que el modelo SÍ había leído salía vacío.

Es del extractor, no de un tipo ni de un modelo: cualquier respuesta con esa
forma exacta se desenvuelve, y nada más.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline.text_extractor import _parse_field_tree_response  # noqa: E402

CAMPOS = {
    "NOMBRE": {"type": "string", "hint": "h"},
    "DOMICILIO": {
        "type": "object", "hint": "h",
        "fields": {
            "CP": {"type": "string", "hint": "h"},
            "CALLE": {"type": "string", "hint": "h"},
        },
    },
    "MEDIDA": {
        "type": "object", "hint": "h",
        "fields": {"value": {"type": "number", "hint": "h"}, "confidence": {"type": "number", "hint": "h"}},
    },
    "FILAS": {"type": "array", "hint": "h", "item_fields": {"CODIGO": {"type": "string", "hint": "h"}}},
    "ETIQUETAS": {"type": "array", "hint": "h", "item_type": "string"},
}


def parsear(respuesta):
    tree = _parse_entry("TIPO_PRUEBA", {"schema_version": "1.0.0", "fields": CAMPOS}).field_tree
    return _parse_field_tree_response(respuesta, tree, seg_conf=1.0)


DIRECTO = {"CP": {"value": "2333CH", "confidence": 1.0}, "CALLE": {"value": "Kerkstraat", "confidence": 0.9}}


def test_un_objeto_con_sus_subcampos_directos_se_lee_como_siempre():
    entidad, confianzas, _ = parsear({"DOMICILIO": DIRECTO})
    assert entidad["DOMICILIO"] == {"CP": "2333CH", "CALLE": "Kerkstraat"}
    assert confianzas == {"DOMICILIO.CP": 1.0, "DOMICILIO.CALLE": 0.9}


def test_un_objeto_envuelto_en_value_confidence_se_desenvuelve():
    entidad, confianzas, _ = parsear({"DOMICILIO": {"value": DIRECTO, "confidence": 1.0}})
    assert entidad["DOMICILIO"] == {"CP": "2333CH", "CALLE": "Kerkstraat"}
    assert confianzas == {"DOMICILIO.CP": 1.0, "DOMICILIO.CALLE": 0.9}  # las de los subcampos, no la del sobre


def test_el_sobre_tambien_puede_traer_el_objeto_como_texto_json():
    entidad, _, _ = parsear({"DOMICILIO": {"value": '{"CP": {"value": "2333CH"}}'}})
    assert entidad["DOMICILIO"]["CP"] == "2333CH"


def test_un_objeto_cuyos_subcampos_se_llaman_value_no_se_confunde_con_un_sobre():
    entidad, _, _ = parsear({"MEDIDA": {"value": {"value": 12.5}, "confidence": {"value": 0.4}}})
    assert entidad["MEDIDA"] == {"value": 12.5, "confidence": 0.4}


def test_una_llave_ajena_junto_a_value_no_es_un_sobre():
    entidad, _, _ = parsear({"DOMICILIO": {"value": DIRECTO, "CP": {"value": "11000"}}})
    assert entidad["DOMICILIO"] == {"CP": "11000", "CALLE": None}


def test_una_lista_envuelta_se_desenvuelve():
    entidad, _, _ = parsear({
        "FILAS": {"value": [{"CODIGO": "A1"}, {"CODIGO": "B2"}], "confidence": 0.9},
        "ETIQUETAS": {"value": ["x", "y"]},
    })
    assert entidad["FILAS"] == [{"CODIGO": "A1"}, {"CODIGO": "B2"}]
    assert entidad["ETIQUETAS"] == ["x", "y"]


def test_un_sobre_con_un_escalar_dentro_no_inventa_un_objeto():
    entidad, _, _ = parsear({"DOMICILIO": {"value": "Kerkstraat 12", "confidence": 1.0}, "NOMBRE": {"value": "Ana"}})
    assert entidad["DOMICILIO"] == {"CP": None, "CALLE": None}
    assert entidad["NOMBRE"] == "Ana"


# --- un objeto que llegó como texto JSON al que solo le falta cerrar ---------

SIN_CERRAR = '{"value": {"CP": {"value": "93312", "confidence": 1.0}, "CALLE": {"value": "Main St.", "confidence": 1.0}}'


def test_un_objeto_como_texto_al_que_le_falta_la_ultima_llave_se_recupera():
    """Medido con MiniMax sobre un W-9 escaneado: el domicilio completo venía
    en un string al que solo le faltaba la llave final."""
    entidad, _, _ = parsear({"DOMICILIO": SIN_CERRAR})
    assert entidad["DOMICILIO"] == {"CP": "93312", "CALLE": "Main St."}


def test_un_texto_cortado_a_media_cadena_no_se_adivina():
    entidad, _, _ = parsear({"DOMICILIO": '{"CP": {"value": "933'})
    assert entidad["DOMICILIO"] == {"CP": None, "CALLE": None}


def test_un_texto_con_las_llaves_en_desorden_no_se_toca():
    entidad, _, _ = parsear({"DOMICILIO": '{"CP": ["93312"}'})
    assert entidad["DOMICILIO"] == {"CP": None, "CALLE": None}
