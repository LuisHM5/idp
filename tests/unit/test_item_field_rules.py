"""Las reglas declaradas dentro de `item_fields` se evalúan FILA POR FILA.

`_flatten_field_tree_rules` deja fuera los `item_fields` a propósito (un
`Factura` plano chocaría entre filas), y hasta que existió
`resolve_field_tree::evaluate_row_rules` nadie las evaluaba en ningún lado: una
fila podía traer el COVE completo en `Factura`, o no traer `Factura` pese a
estar declarada `required`, y salía `valid: true`. El único check que producía
un grupo repetible era el `presence` del array entero.

Esto es del motor, no de un tipo: aplica a cualquier catálogo con `item_fields`
(FACTURAS de los dos pedimentos, CONCEPTOS del cuadro de liquidación,
ASEGURADOS de una póliza...).
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from validators.engine import (  # noqa: E402
    document_status_from_checks,
    resolve_field_tree,
)

CATALOGO = {
    "schema_version": "1.0.0",
    "display_name": "Tipo de prueba",
    "description": "t",
    "min_confidence": 0.8,
    "fields": {
        "FILAS": {
            "type": "array", "required": True, "hint": "h", "validation": [],
            "item_fields": {
                "CLAVE": {
                    "type": "string", "required": True, "hint": "h",
                    "validation": [{
                        "rule_type": "regex", "pattern": "^[A-Za-z0-9._-]+$",
                        "message": "solo el número",
                    }],
                },
                "IMPORTE": {"type": "number", "required": False, "hint": "h", "validation": []},
                "DETALLE": {
                    "type": "object", "required": False, "hint": "h", "validation": [],
                    "fields": {
                        "MONEDA": {
                            "type": "string", "required": False, "hint": "h",
                            "validation": [{
                                "rule_type": "regex", "pattern": "^[A-Z]{3}$",
                                "message": "tres letras",
                            }],
                        },
                    },
                },
            },
        },
    },
}

CONFIANZA = 0.95


def resolver(filas):
    entry = _parse_entry("TIPO_PRUEBA", CATALOGO)
    return resolve_field_tree({"FILAS": filas}, {}, entry, default_confidence=CONFIANZA)


def test_una_fila_mala_no_contamina_a_las_buenas():
    tree, _, _ = resolver([{"CLAVE": "4099"}, {"CLAVE": "COVE2689JP013 / 35079805"}])

    buena, mala = tree["FILAS"]["items"]
    assert buena["valid"] is True and buena["reason"] is None
    assert mala["valid"] is False
    assert mala["fields"]["CLAVE"]["valid"] is False
    assert "solo el número" in mala["reason"]


def test_cada_fila_reporta_su_propia_ruta():
    """`FILAS[1].CLAVE`, no `FILAS.CLAVE`: sin el índice, el resultado de una
    fila pisaría el de todas las demás."""
    _, checks, _ = resolver([{"CLAVE": "4099"}, {"CLAVE": "COVE2689JP013 / 35079805"}])

    fallidos = [c.field for c in checks if not c.valid and not c.skipped]
    assert fallidos == ["FILAS[1].CLAVE"]


def test_un_required_de_fila_se_exige_de_verdad():
    tree, checks, _ = resolver([{"CLAVE": "4099"}, {"IMPORTE": 10}])

    assert tree["FILAS"]["items"][1]["valid"] is False
    assert any(
        c.field == "FILAS[1].CLAVE" and c.rule == "presence" and not c.valid for c in checks
    )


def test_el_documento_refleja_la_fila_rota():
    """Antes esto salía APPROVED: el motor solo veía el `presence` del array."""
    _, checks, _ = resolver([{"CLAVE": "4099"}, {"CLAVE": "COVE2689JP013 / 35079805"}])

    assert document_status_from_checks(checks) != "APPROVED"


def test_un_objeto_anidado_en_la_fila_tambien_valida():
    tree, _, _ = resolver([
        {"CLAVE": "4099", "DETALLE": {"MONEDA": "USD"}},
        {"CLAVE": "4100", "DETALLE": {"MONEDA": "dolares"}},
    ])

    ok, mala = tree["FILAS"]["items"]
    assert ok["fields"]["DETALLE"]["fields"]["MONEDA"]["valid"] is True
    assert mala["fields"]["DETALLE"]["fields"]["MONEDA"]["valid"] is False
    assert mala["valid"] is False


def test_todas_las_filas_buenas_dejan_el_documento_aprobado():
    tree, checks, _ = resolver([{"CLAVE": "4099"}, {"CLAVE": "4100", "IMPORTE": "1,234.50"}])

    assert all(item["valid"] for item in tree["FILAS"]["items"])
    assert tree["FILAS"]["items"][1]["fields"]["IMPORTE"]["value"] == 1234.5
    assert document_status_from_checks(checks) == "APPROVED"


def test_una_regla_de_fila_lee_las_columnas_de_su_propia_fila():
    """El `entity_map` de una regla de fila es la FILA, no el documento: una
    fórmula que cruza columnas de la misma fila tiene que ver los valores de
    ESA fila y no los de la primera."""
    catalogo = {
        **CATALOGO,
        "fields": {
            "FILAS": {
                "type": "array", "required": True, "hint": "h", "validation": [],
                "item_fields": {
                    "TOTAL": {
                        "type": "number", "required": True, "hint": "h",
                        "validation": [{
                            "rule_type": "custom_formula",
                            "formula": "SUBTOTAL + IVA",
                            "message": "el total de la fila no cuadra",
                        }],
                    },
                    "SUBTOTAL": {"type": "number", "required": True, "hint": "h", "validation": []},
                    "IVA": {"type": "number", "required": True, "hint": "h", "validation": []},
                },
            },
        },
    }
    entry = _parse_entry("TIPO_PRUEBA", catalogo)
    tree, _, _ = resolve_field_tree(
        {"FILAS": [
            {"SUBTOTAL": 100, "IVA": 16, "TOTAL": 116},
            {"SUBTOTAL": 200, "IVA": 32, "TOTAL": 116},
        ]},
        {}, entry, default_confidence=CONFIANZA,
    )

    assert tree["FILAS"]["items"][0]["valid"] is True
    assert tree["FILAS"]["items"][1]["valid"] is False
