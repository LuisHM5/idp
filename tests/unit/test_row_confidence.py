"""Una confianza por FILA, no una por celda.

El tool schema pedía `{value, confidence}` en cada escalar, incluidas todas las
celdas de cada fila de un array. Medido sobre la salida real del expediente de
139 páginas (`resultados/.../26 07 3435 6041353 CG/001/_prompt_output.json`):
ese `confidence` por celda era el **44%** de todo lo que emitió el modelo
(16,662 tokens de salida → ~9,465 sin él). La salida es la mitad lenta de una
llamada a Bedrock —se genera token a token— y la cara (5× la entrada).

Una fila se lee bien o mal como unidad, así que ahora reporta UNA confianza y
sus celdas la heredan. Los escalares de primer nivel conservan la suya.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline.confidence import row_key  # noqa: E402
from pipeline.text_extractor import (  # noqa: E402
    ROW_CONFIDENCE_KEY,
    _build_field_tree_prompts,
    _field_tree_tool_schema,
    _parse_field_tree_response,
)

OCR = 1.0  # confianza de página neutra, para leer los números sin ruido


def cargar(type_id="PEDIMENTO_ERP"):
    path = REPO_ROOT / "catalogs" / f"{type_id}.json"
    return _parse_entry(type_id, json.loads(path.read_text(encoding="utf-8"))[type_id])


def test_una_celda_de_fila_se_pide_como_valor_pelon():
    props = _field_tree_tool_schema(cargar().field_tree)["properties"]
    fila = props["FACTURAS"]["items"]["properties"]

    assert fila["Factura"] == {"type": ["string", "null"]}
    assert fila["Total_factura"] == {"type": ["number", "null"]}
    assert fila[ROW_CONFIDENCE_KEY] == {"type": "number"}


def test_un_escalar_de_primer_nivel_conserva_su_confianza():
    props = _field_tree_tool_schema(cargar().field_tree)["properties"]

    assert props["No_Pedimento"]["properties"]["confidence"] == {"type": "number"}
    assert props["No_Pedimento"]["properties"]["value"] == {"type": ["string", "null"]}


def test_la_fila_reparte_su_confianza_a_cada_celda():
    entry = cargar()
    raw = {"FACTURAS": [
        {"Factura": "4099", "Total_factura": 155.0, ROW_CONFIDENCE_KEY: 0.6},
        {"Factura": "1671", "Total_factura": 2944.32, ROW_CONFIDENCE_KEY: 1.0},
    ]}

    entities, _, rows = _parse_field_tree_response(raw, entry.field_tree, OCR)

    mala, buena = entities["FACTURAS"]
    assert rows[row_key(mala)]["Factura"] == 0.6
    assert rows[row_key(mala)]["Total_factura"] == 0.6      # toda la fila, no solo la celda
    assert rows[row_key(buena)]["Factura"] == 1.0


def test_la_confianza_de_la_fila_se_combina_con_la_de_la_pagina():
    """Mismo contrato que un escalar: lo que el modelo dice de sí mismo se
    multiplica por qué tan bien se leyó la página."""
    entry = cargar()
    raw = {"FACTURAS": [{"Factura": "4099", ROW_CONFIDENCE_KEY: 0.5}]}

    entities, _, rows = _parse_field_tree_response(raw, entry.field_tree, 0.8)

    assert rows[row_key(entities["FACTURAS"][0])]["Factura"] == 0.4


@pytest.mark.parametrize(
    "declarada",
    [None, "no es un número", float("nan"), float("inf")],
    ids=["ausente", "texto", "nan", "inf"],
)
def test_una_fila_sin_confianza_usable_no_inventa_una(declarada):
    """`float("nan")` pasa el `float()` y sobrevive al clamp como 1.0 — toda
    comparación contra NaN es falsa, así que `max(0.0, min(1.0, nan))` da 1.0:
    una confianza perfecta que nadie reportó. Sin número usable no se mide
    nada y el motor cae a la confianza de la página, como cualquier campo que
    el modelo no numeró."""
    entry = cargar()
    fila = {"Factura": "4099"}
    if declarada is not None:
        fila[ROW_CONFIDENCE_KEY] = declarada

    entities, _, rows = _parse_field_tree_response({"FACTURAS": [fila]}, entry.field_tree, OCR)

    assert row_key(entities["FACTURAS"][0]) not in rows


def test_la_forma_vieja_por_celda_sigue_parseando():
    """Un modelo que responda con `{value, confidence}` en cada celda —o una
    respuesta guardada de antes— no puede dejar de funcionar."""
    entry = cargar()
    raw = {"FACTURAS": [{
        "Factura": {"value": "4099", "confidence": 0.5},
        "Total_factura": {"value": 155.0, "confidence": 0.9},
    }]}

    entities, _, rows = _parse_field_tree_response(raw, entry.field_tree, OCR)

    fila = entities["FACTURAS"][0]
    assert fila["Factura"] == "4099" and fila["Total_factura"] == 155.0
    assert rows[row_key(fila)] == {"Factura": 0.5, "Total_factura": 0.9}


def test_al_modelo_se_le_dice_la_regla():
    prefijo, _ = _build_field_tree_prompts(cargar(), [])

    assert "una sola confidence" in prefijo.lower()


def test_el_valor_extraido_no_cambia():
    """El recorte es de metadatos: los valores de la fila salen igual."""
    entry = cargar()
    raw = {"FACTURAS": [{
        "PROVEEDOR_NOMBRE": "TYSON FOODS INC.", "Factura": "35079805",
        "num_acuse_valor": "COVE2689JP013 / 35079805", "Total_factura": 21178.8,
        ROW_CONFIDENCE_KEY: 0.93,
    }]}

    entities, _, _ = _parse_field_tree_response(raw, entry.field_tree, OCR)

    fila = entities["FACTURAS"][0]
    assert fila["Factura"] == "35079805"
    assert fila["num_acuse_valor"] == "COVE2689JP013 / 35079805"
    assert fila["Total_factura"] == 21178.8
    assert ROW_CONFIDENCE_KEY not in fila  # la confianza no es un campo del ERP
