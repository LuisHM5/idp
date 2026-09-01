"""El bloque `derived` del catálogo: una condición, no una extracción.

Cubre el mecanismo genérico — el conjunto de operadores, el orden de los
`cases`, el `default` y qué pasa cuando el campo de entrada no se pudo leer —
sin depender de ningún tipo de documento en particular. El caso real que lo
motivó (`Tipo_Pago` de PEDIMENTO_ERP) se verifica en test_pedimento_mapping.py.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline.derived import SOURCE_DERIVED, apply_derived_fields  # noqa: E402


def entry_con(campos):
    """Un tipo mínimo del DSL field-centric con los campos que se le pasen."""
    return _parse_entry("TIPO_DE_PRUEBA", {"schema_version": "4.0.0", "fields": campos})


def derivar(campos, entities, confidences=None):
    entry = entry_con(campos)
    confidences = dict(confidences or {})
    derived = apply_derived_fields(entities, confidences, entry)
    return entities, confidences, derived


UMBRAL = {
    "TIPO": {
        "type": "string",
        "computed": True,
        "derived": {
            "from": "TOTAL",
            "cases": [
                {"when": {"gt": 383}, "value": "ARRIBA"},
                {"when": {"lte": 383}, "value": "ABAJO"},
            ],
        },
    },
    "TOTAL": {"type": "number"},
}


@pytest.mark.parametrize(
    "total, esperado",
    [
        (383, "ABAJO"),      # la frontera exacta cae del lado de `lte`
        (382.99, "ABAJO"),
        (383.01, "ARRIBA"),
        (12450, "ARRIBA"),
        ("$ 12,450.00", "ARRIBA"),  # los montos con formato se parsean igual
        (0, "ABAJO"),
    ],
)
def test_el_umbral_decide_el_valor(total, esperado):
    entities, _, _ = derivar(UMBRAL, {"TOTAL": total})
    assert entities["TIPO"] == esperado


def test_sin_valor_de_entrada_no_se_inventa_nada():
    """Ningún `when` matchea contra un TOTAL ausente: sin `default`, sale None.
    El check de presencia del campo es el que reporta el problema."""
    entities, confidences, derived = derivar(UMBRAL, {"TOTAL": None}, {"TOTAL": 0.9})
    assert entities["TIPO"] is None
    assert "TIPO" not in confidences  # nada de qué heredar confianza
    assert derived.sources["TIPO"] == SOURCE_DERIVED


def test_la_confianza_se_hereda_del_campo_del_que_se_derivo():
    _, confidences, derived = derivar(UMBRAL, {"TOTAL": 900}, {"TOTAL": 0.77})
    assert confidences["TIPO"] == 0.77
    assert derived.sources == {"TIPO": SOURCE_DERIVED}


def test_gana_el_primer_case_que_matchea():
    campos = {
        "NIVEL": {
            "type": "string",
            "computed": True,
            "derived": {
                "from": "MONTO",
                "cases": [
                    {"when": {"gt": 1000}, "value": "ALTO"},
                    {"when": {"gt": 100}, "value": "MEDIO"},
                    {"when": {"gte": 0}, "value": "BAJO"},
                ],
            },
        },
        "MONTO": {"type": "number"},
    }
    assert derivar(campos, {"MONTO": 5000})[0]["NIVEL"] == "ALTO"
    assert derivar(campos, {"MONTO": 500})[0]["NIVEL"] == "MEDIO"
    assert derivar(campos, {"MONTO": 5})[0]["NIVEL"] == "BAJO"


def test_default_cuando_ningun_case_matchea():
    campos = {
        "REGIMEN": {
            "type": "string",
            "computed": True,
            "derived": {
                "from": "CLAVE",
                "cases": [{"when": {"in": ["A1", "A3"]}, "value": "DEFINITIVA"}],
                "default": "OTRA",
            },
        },
        "CLAVE": {"type": "string"},
    }
    assert derivar(campos, {"CLAVE": "A3"})[0]["REGIMEN"] == "DEFINITIVA"
    assert derivar(campos, {"CLAVE": " a1 "})[0]["REGIMEN"] == "DEFINITIVA"  # sin espacios/mayúsculas
    assert derivar(campos, {"CLAVE": "F4"})[0]["REGIMEN"] == "OTRA"


def test_operadores_de_texto_y_presencia():
    campos = {
        "ES_IMPO": {
            "type": "boolean",
            "computed": True,
            "derived": {"from": "CLAVE", "cases": [{"when": {"matches": "^A"}, "value": True}], "default": False},
        },
        "TIENE_REF": {
            "type": "boolean",
            "computed": True,
            "derived": {"from": "REFERENCIA", "cases": [{"when": {"not_empty": True}, "value": True}], "default": False},
        },
        "CLAVE": {"type": "string"},
        "REFERENCIA": {"type": "string"},
    }
    entities, _, _ = derivar(campos, {"CLAVE": "A1", "REFERENCIA": "   "})
    assert entities["ES_IMPO"] is True
    assert entities["TIENE_REF"] is False

    entities, _, _ = derivar(campos, {"CLAVE": "F4", "REFERENCIA": "REF-99"})
    assert entities["ES_IMPO"] is False
    assert entities["TIENE_REF"] is True


def test_lee_un_path_anidado():
    campos = {
        "TIPO": {
            "type": "string",
            "computed": True,
            "derived": {"from": "CUADRO.TOTAL", "cases": [{"when": {"gt": 100}, "value": "ALTO"}]},
        },
        "CUADRO": {"type": "object", "fields": {"TOTAL": {"type": "number"}}},
    }
    entities, confidences, _ = derivar(campos, {"CUADRO": {"TOTAL": 500}}, {"CUADRO.TOTAL": 0.81})
    assert entities["TIPO"] == "ALTO"
    assert confidences["TIPO"] == 0.81


def test_un_operador_inexistente_no_hace_que_el_case_matchee():
    """Un typo en el catálogo debe dejar la regla sin efecto, nunca convertirla
    en una condición que matchea siempre."""
    campos = {
        "TIPO": {
            "type": "string",
            "computed": True,
            "derived": {
                "from": "TOTAL",
                "cases": [{"when": {"mayor_que": 100}, "value": "ARRIBA"}],
                "default": "SIN_REGLA",
            },
        },
        "TOTAL": {"type": "number"},
    }
    assert derivar(campos, {"TOTAL": 5000})[0]["TIPO"] == "SIN_REGLA"


def test_un_tipo_sin_bloque_derived_no_cambia_nada():
    campos = {"TOTAL": {"type": "number"}, "CUADRO": {"type": "object", "fields": {"A": {"type": "number"}}}}
    entities = {"TOTAL": 100}
    resultado, confidences, derived = derivar(campos, entities)

    assert derived.sources == {} and derived.labels == {}
    assert resultado == {"TOTAL": 100}  # ni un objeto vacío de más
    assert confidences == {}


def test_un_bloque_derived_puede_vivir_dentro_de_un_objeto():
    campos = {
        "RESUMEN": {
            "type": "object",
            "fields": {
                "NIVEL": {
                    "type": "string",
                    "computed": True,
                    "derived": {"from": "TOTAL", "cases": [{"when": {"gt": 10}, "value": "ALTO"}]},
                }
            },
        },
        "TOTAL": {"type": "number"},
    }
    entities, _, derived = derivar(campos, {"TOTAL": 50})
    assert entities["RESUMEN"]["NIVEL"] == "ALTO"
    assert derived.sources == {"RESUMEN.NIVEL": SOURCE_DERIVED}


def test_la_etiqueta_acompana_al_codigo_sin_reemplazarlo():
    """El caso real: la columna guarda un número, pero el resultado también
    dice qué significa. El `value` sigue siendo el código, nunca la etiqueta."""
    campos = {
        "TIPO_PAGO": {
            "type": "number",
            "computed": True,
            "derived": {
                "from": "TOTAL",
                "cases": [
                    {"when": {"gt": 383}, "value": 1, "label": "Anticipo y Pasivo"},
                    {"when": {"lte": 383}, "value": 3, "label": "Solo Pasivo"},
                ],
            },
        },
        "TOTAL": {"type": "number"},
    }
    entities, _, derived = derivar(campos, {"TOTAL": 383})
    assert entities["TIPO_PAGO"] == 3
    assert derived.labels == {"TIPO_PAGO": "Solo Pasivo"}

    entities, _, derived = derivar(campos, {"TOTAL": 5000})
    assert entities["TIPO_PAGO"] == 1
    assert derived.labels == {"TIPO_PAGO": "Anticipo y Pasivo"}


def test_la_etiqueta_es_opcional():
    """Un case sin `label` no inventa uno, y sin match tampoco hay etiqueta:
    el `default` no significa nada en palabras."""
    campos = {
        "NIVEL": {
            "type": "string",
            "computed": True,
            "derived": {
                "from": "TOTAL",
                "cases": [{"when": {"gt": 10}, "value": "ALTO"}],
                "default": "BAJO",
            },
        },
        "TOTAL": {"type": "number"},
    }
    assert derivar(campos, {"TOTAL": 50})[2].labels == {}
    assert derivar(campos, {"TOTAL": 1})[2].labels == {}


def test_la_etiqueta_llega_al_arbol_resuelto():
    """El puente hasta la salida: `value_labels` pone `label` junto a `value`,
    y solo en los campos que de verdad tienen una."""
    from validators.engine import resolve_field_tree

    campos = {
        "TIPO_PAGO": {
            "type": "number",
            "computed": True,
            "derived": {
                "from": "TOTAL",
                "cases": [{"when": {"lte": 383}, "value": 3, "label": "Solo Pasivo"}],
            },
        },
        "TOTAL": {"type": "number"},
    }
    entry = entry_con(campos)
    entities = {"TOTAL": 383}
    confidences = {"TOTAL": 0.9}
    derived = apply_derived_fields(entities, confidences, entry)

    tree, _, _ = resolve_field_tree(
        entities, confidences, entry,
        source_overrides=derived.sources, value_labels=derived.labels,
    )

    assert tree["TIPO_PAGO"]["value"] == 3
    assert tree["TIPO_PAGO"]["label"] == "Solo Pasivo"
    assert "label" not in tree["TOTAL"]  # nadie etiquetó este, sale como siempre
