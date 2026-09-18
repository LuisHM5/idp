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
from pipeline.derived import (  # noqa: E402
    CONTEXT_CONFIDENCE,
    SOURCE_DERIVED,
    apply_derived_fields,
    build_context,
)


def entry_con(campos):
    """Un tipo mínimo del DSL field-centric con los campos que se le pasen."""
    return _parse_entry("TIPO_DE_PRUEBA", {"schema_version": "4.0.0", "fields": campos})


def derivar(campos, entities, confidences=None, context=None):
    entry = entry_con(campos)
    confidences = dict(confidences or {})
    derived = apply_derived_fields(entities, confidences, entry, context)
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


# ---------------------------------------------------------------------------
# Variables de contexto ($FILE_NAME), `from` por case y `keep_extracted`
# ---------------------------------------------------------------------------

SIGLA = {
    "SIGLA": {
        "type": "string",
        "computed": True,
        "derived": {
            "from": "$FILE_NAME",
            "cases": [
                {"when": {"matches": r"(?:^|[_\-\s])GT\d*\.[A-Za-z0-9]+$"}, "value": "GT"},
                {"from": "ISR", "when": {"gt": 0}, "value": "II", "label": "por contenido"},
            ],
            "default": None,
        },
    },
    "ISR": {"type": "number"},
}


@pytest.mark.parametrize(
    "source_name, esperado",
    [
        ("2607-0823-6001803_GT1.pdf", "2607-0823-6001803_GT1.pdf"),
        ("raw/TIPO_GT/9f1c-uuid/2607-0823-6001803_GT1.pdf", "2607-0823-6001803_GT1.pdf"),
        ("C:\\docs\\2607_GT1.pdf", "2607_GT1.pdf"),
        ("", ""),
        (None, ""),
    ],
)
def test_el_contexto_solo_conserva_el_nombre_del_archivo(source_name, esperado):
    """La key de S3 trae el TIPO y el uuid: si llegaran al patrón, un tipo
    llamado `..._GT` haría matchear cualquier archivo."""
    assert build_context(source_name) == {"$FILE_NAME": esperado}


def test_una_variable_de_contexto_decide_con_confianza_plena():
    entities, confidences, derived = derivar(
        SIGLA, {"ISR": 33.5}, {"ISR": 0.8}, build_context("raw/T/uuid/2607_GT2.xml")
    )
    assert entities["SIGLA"] == "GT"  # el nombre va primero aunque ISR también matchearía
    assert confidences["SIGLA"] == CONTEXT_CONFIDENCE
    assert derived.sources["SIGLA"] == SOURCE_DERIVED
    assert "SIGLA" not in derived.labels


def test_un_case_con_su_propio_from_lee_otro_campo():
    entities, confidences, derived = derivar(
        SIGLA, {"ISR": 33.5}, {"ISR": 0.8}, build_context("factura.xml")
    )
    assert entities["SIGLA"] == "II"
    assert confidences["SIGLA"] == 0.8  # hereda del campo que DECIDIÓ, no del `from` del bloque
    assert derived.labels["SIGLA"] == "por contenido"


def test_sin_contexto_las_variables_no_matchean_y_no_truenan():
    """Quien llame sin contexto (código viejo, un test) obtiene lo de siempre."""
    entities, confidences, _ = derivar(SIGLA, {"ISR": 0})
    assert entities["SIGLA"] is None
    assert "SIGLA" not in confidences


def test_una_variable_desconocida_no_vale_nada():
    campos = {"X": {"type": "string", "computed": True, "derived": {
        "from": "$NO_EXISTE", "cases": [{"when": {"empty": False}, "value": "si"}], "default": "no"}}}
    entities, confidences, _ = derivar(campos, {}, context=build_context("a.pdf"))
    assert entities["X"] == "no"
    assert "X" not in confidences  # nada que heredar de algo que no existe


KEEP = {
    "SIGLA": {
        "type": "string",
        "hint": "la lee el modelo",
        "derived": {
            "from": "$FILE_NAME",
            "keep_extracted": True,
            "cases": [{"when": {"matches": r"_GT\d*\."}, "value": "GT"}],
        },
    },
}


def test_keep_extracted_conserva_la_lectura_cuando_ningun_case_decide():
    entities, confidences, derived = derivar(
        KEEP, {"SIGLA": "OLI"}, {"SIGLA": 0.87}, build_context("factura.pdf")
    )
    assert entities["SIGLA"] == "OLI"
    assert confidences["SIGLA"] == 0.87
    assert "SIGLA" not in derived.sources  # sigue siendo MODEL_EXTRACTION: es lo que es


def test_keep_extracted_no_protege_cuando_un_case_si_decide():
    entities, confidences, derived = derivar(
        KEEP, {"SIGLA": "OLI"}, {"SIGLA": 0.87}, build_context("x_GT3.pdf")
    )
    assert entities["SIGLA"] == "GT"
    assert confidences["SIGLA"] == CONTEXT_CONFIDENCE
    assert derived.sources["SIGLA"] == SOURCE_DERIVED


def test_keep_extracted_sin_lectura_cae_al_default():
    entities, _, derived = derivar(KEEP, {"SIGLA": "  "}, context=build_context("factura.pdf"))
    assert entities["SIGLA"] is None
    assert derived.sources["SIGLA"] == SOURCE_DERIVED


def test_sin_keep_extracted_el_default_pisa_como_siempre():
    campos = {"SIGLA": {**KEEP["SIGLA"], "derived": {**KEEP["SIGLA"]["derived"], "keep_extracted": False}}}
    entities, _, _ = derivar(campos, {"SIGLA": "OLI"}, context=build_context("factura.pdf"))
    assert entities["SIGLA"] is None


def test_un_derivado_que_lee_otro_derivado_no_depende_del_orden_de_declaracion():
    """Un catálogo que pasó por DynamoDB vuelve como Map y no conserva el orden
    de sus claves: NOMBRE declarado ANTES que la SIGLA de la que depende tiene
    que dar lo mismo."""
    nombre = {"type": "string", "computed": True, "derived": {
        "from": "SIGLA", "cases": [{"when": {"eq": "GT"}, "value": "Gastos de Terceros"}]}}
    for campos in ({"NOMBRE": nombre, **SIGLA}, {**SIGLA, "NOMBRE": nombre}):
        entities, confidences, _ = derivar(campos, {}, context=build_context("a_GT1.pdf"))
        assert entities["NOMBRE"] == "Gastos de Terceros"
        assert confidences["NOMBRE"] == CONTEXT_CONFIDENCE


def test_una_dependencia_circular_no_cuelga_ni_truena():
    a = {"type": "string", "computed": True, "derived": {"from": "B", "cases": [], "default": "a"}}
    b = {"type": "string", "computed": True, "derived": {"from": "A", "cases": [], "default": "b"}}
    entities, _, _ = derivar({"A": a, "B": b}, {})
    assert entities == {"A": "a", "B": "b"}
