"""La normalización obedece al `type` declarado en el catálogo, no al nombre.

`pipeline.normalize.normalize_field` decide qué hacer con un valor a partir de
convenciones de NOMBRE (algo que se llame TOTAL/IMPORTE/VALOR es un importe,
algo que se llame FECHA es una fecha). Esa heurística no distingue un número de
un texto que apenas MENCIONA uno, y por eso rompía datos reales:

- `num_acuse_valor` (PEDIMENTO_ERP) contiene "VALOR" -> el acuse
  "COVE2689JP013 / 35079805" salía como 268901335079805.0;
- `ENTIDAD_FEDERATIVA` contiene "IVA" -> el estado salía como número;
- y al revés, `FACTOR_MONEDA_FACTURA` no contiene ningún token de importe, así
  que una columna `number` del ERP salía como el string "1.0".

El contrato que fija este test: con `declared_type` el catálogo manda; sin él
(DSL legacy, o un escalar dentro de un array, que no declara tipo por ítem) la
heurística se comporta exactamente igual que antes.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline.normalize import normalize_field  # noqa: E402


@pytest.mark.parametrize(
    "nombre, valor",
    [
        ("num_acuse_valor", "COVE2689JP013 / 35079805"),
        ("num_acuse_valor", "COVE26871ECA1"),
        ("ENTIDAD_FEDERATIVA", "NUEVO LEON"),
        ("TIPO_SEGURO", "GASTOS MEDICOS MAYORES"),
        ("VALOR_LITERAL", "DOS MILLONES DE PESOS"),
    ],
)
def test_un_campo_string_nunca_se_convierte_en_numero(nombre, valor):
    assert normalize_field(nombre, valor, "string") == valor


@pytest.mark.parametrize(
    "nombre, valor, esperado",
    [
        ("FACTOR_MONEDA_FACTURA", "1.00000", 1.0),   # ningún token de importe en el nombre
        ("GI_Otros", "11,027", 11027.0),
        ("Total_factura", "21,178.80", 21178.8),
    ],
)
def test_un_campo_number_se_parsea_aunque_su_nombre_no_suene_a_importe(nombre, valor, esperado):
    assert normalize_field(nombre, valor, "number") == esperado


def test_un_number_ilegible_se_conserva_en_vez_de_inventarse():
    """Si el valor no es un número, no se fabrica uno: se devuelve el texto y
    las reglas del campo deciden qué hacer con él."""
    assert normalize_field("GI_Otros", "N/A", "number") == "N/A"


def test_un_boolean_sigue_siendo_boolean():
    """Sin tipo declarado, str() lo volvía "True": el ERP recibiría texto."""
    assert normalize_field("DEPOSITO_REFERENCIADO_QR_VALIDO", True, "boolean") is True


def test_una_fecha_se_normaliza_por_tipo_aunque_el_nombre_no_diga_fecha():
    assert normalize_field("VIGENCIA", "24/08/2026", "date") == "2026-08-24"


@pytest.mark.parametrize(
    "nombre, valor, esperado",
    [
        ("TOTAL", "21,178.80", 21178.8),
        ("FECHA_ENTRADA", "24/08/2026", "2026-08-24"),
        ("NUMERO_PEDIMENTO", "26 07 0973 6100971", "260709736100971"),
        ("RFC", " ofu910626uq0 ", "OFU910626UQ0"),
        ("PROVEEDOR_NOMBRE", "  TYSON FOODS INC. ", "TYSON FOODS INC."),
    ],
)
def test_sin_tipo_declarado_la_heuristica_no_cambia(nombre, valor, esperado):
    assert normalize_field(nombre, valor) == esperado
