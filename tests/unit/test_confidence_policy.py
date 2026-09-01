"""El contrato de `confidence` en la salida: siempre un número, nunca null.

Tres reglas, genéricas (ningún TYPE_ID hardcodeado — se prueban sobre un tipo
mínimo armado aquí mismo):

1. Todo escalar resuelto reporta un número 0-1. Un campo que no está en el
   documento reporta 0.0 ("no lo sé"), no `null`.
2. Un valor calculado a partir de varias celdas hereda el PROMEDIO de sus
   confianzas (`pipeline.confidence.combine`).
3. Un valor por debajo del piso de descarte (0.50 por default,
   `discard_below_confidence` por catálogo) no se entrega: `value` sale null,
   `valid` false y la confianza baja se conserva para explicar por qué.

El bug que las motivó: `LIQUIDACION_NORMALIZADA` entera salía con
`confidence: null` porque nadie registraba la confianza de un campo computado.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline import confidence as conf_policy  # noqa: E402
from validators.engine import resolve_field_tree  # noqa: E402

CAMPOS = {
    "NOMBRE": {"type": "string", "required": False},
    "IMPORTE": {"type": "number", "required": False},
    "OPCIONAL": {"type": "string", "required": False},
    "CONCEPTOS": {
        "type": "array",
        "item_fields": {
            "CLAVE": {"type": "string"},
            "IMPORTE": {"type": "number"},
        },
    },
}


def entry_con(campos=None, **extra):
    data = {"schema_version": "4.0.0", "min_confidence": 0.8, "fields": campos or CAMPOS}
    data.update(extra)
    return _parse_entry("TIPO_DE_PRUEBA", data)


def paths_con_confianza(node, prefix=""):
    """Todo (path, confidence) escalar del árbol, incluyendo filas de arrays."""
    salida = []
    for name, sub in node.items():
        path = f"{prefix}{name}"
        if "fields" in sub:
            salida.extend(paths_con_confianza(sub["fields"], path + "."))
        elif "items" in sub:
            for i, item in enumerate(sub["items"]):
                if "fields" in item:
                    salida.extend(paths_con_confianza(item["fields"], f"{path}[{i}]."))
                else:
                    salida.append((f"{path}[{i}]", item["confidence"]))
        else:
            salida.append((path, sub["confidence"]))
    return salida


def test_ningun_campo_sale_con_confianza_null():
    """La queja original: campos con dato y campos sin dato, ninguno en null."""
    entities = {
        "NOMBRE": "ACME",
        "IMPORTE": 100,
        "OPCIONAL": None,               # no estaba en el documento
        "CONCEPTOS": [{"CLAVE": "DTA", "IMPORTE": 10}],
    }

    tree, _, _ = resolve_field_tree(entities, {"NOMBRE": 0.97}, entry_con())

    medidas = paths_con_confianza(tree)
    assert medidas, "el árbol no trajo campos"
    for path, conf in medidas:
        assert isinstance(conf, (int, float)), f"{path} salió con confidence {conf!r}"
        assert 0.0 <= conf <= 1.0


def test_un_campo_ausente_reporta_cero_y_no_se_marca_como_descartado():
    """Sin dato no hay nada que descartar: 0.0 significa "no lo sé", y un campo
    opcional que no venía en el documento no puede tumbar el status."""
    tree, checks, efectivas = resolve_field_tree(
        {"OPCIONAL": None}, {}, entry_con(), default_confidence=0.98
    )

    assert tree["OPCIONAL"]["confidence"] == 0.0
    assert tree["OPCIONAL"]["value"] is None
    assert tree["OPCIONAL"]["valid"] is True
    assert [c for c in checks if not c.valid] == []
    # y no entra al promedio del documento: no describe nada leído
    assert efectivas["OPCIONAL"] is None


def test_un_valor_por_debajo_del_piso_no_se_entrega():
    tree, checks, _ = resolve_field_tree(
        {"NOMBRE": "ACMÉ ilegible"}, {"NOMBRE": 0.31}, entry_con()
    )

    nodo = tree["NOMBRE"]
    assert nodo["value"] is None                       # descartado, no entregado
    assert nodo["confidence"] == 0.31                  # el número que lo explica se conserva
    assert nodo["valid"] is False
    assert "discarded" in nodo["reason"]
    assert [c.rule for c in checks if not c.valid] == ["discard_low_confidence"]


def test_entre_el_piso_y_el_minimo_el_valor_se_conserva_marcado():
    """`min_confidence` señala; el piso de descarte borra. No son lo mismo."""
    tree, _, _ = resolve_field_tree({"NOMBRE": "ACME"}, {"NOMBRE": 0.62}, entry_con())

    assert tree["NOMBRE"]["value"] == "ACME"
    assert tree["NOMBRE"]["valid"] is False
    assert "below minimum" in tree["NOMBRE"]["reason"]


def test_el_piso_se_configura_por_tipo_en_el_catalogo():
    entry = entry_con(discard_below_confidence=0.9)

    tree, _, _ = resolve_field_tree({"NOMBRE": "ACME"}, {"NOMBRE": 0.85}, entry)

    assert tree["NOMBRE"]["value"] is None


def test_sin_medicion_propia_se_hereda_la_del_documento_en_vez_de_borrar():
    """Un valor que el extractor no numeró NO se borra por eso: hereda qué tan
    bien se leyó la fuente. Si no, un hueco del pipeline borraría dato bueno."""
    tree, _, _ = resolve_field_tree(
        {"NOMBRE": "ACME"}, {}, entry_con(), default_confidence=0.96
    )

    assert tree["NOMBRE"]["value"] == "ACME"
    assert tree["NOMBRE"]["confidence"] == 0.96


def test_cada_fila_reporta_su_propia_confianza():
    """Antes toda fila de un array reportaba el mismo número (el del segmento):
    una fila mal leída se veía tan confiable como el resto."""
    filas = [{"CLAVE": "DTA", "IMPORTE": 10}, {"CLAVE": "IVA", "IMPORTE": 20}]
    row_conf = {
        conf_policy.row_key(filas[0]): {"CLAVE": 0.99, "IMPORTE": 0.99},
        conf_policy.row_key(filas[1]): {"CLAVE": 0.99, "IMPORTE": 0.55},
    }

    tree, _, _ = resolve_field_tree(
        {"CONCEPTOS": filas}, {"CONCEPTOS": 0.9}, entry_con(), row_confidences=row_conf
    )

    items = tree["CONCEPTOS"]["items"]
    assert items[0]["fields"]["IMPORTE"]["confidence"] == 0.99
    assert items[1]["fields"]["IMPORTE"]["confidence"] == 0.55
    # la celda sin número propio cae a la del array, no a null
    assert items[0]["fields"]["CLAVE"]["confidence"] == 0.99


@pytest.mark.parametrize(
    "valores, esperado",
    [
        ([0.9, 0.8], 0.85),
        ([0.9, None], 0.9),        # lo que no se midió no arrastra el promedio
        ([None, None], None),
        ([], None),
    ],
)
def test_el_promedio_ignora_lo_que_nadie_midio(valores, esperado):
    assert conf_policy.combine(valores) == esperado
