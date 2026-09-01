"""Los dos catálogos de pedimento mapean, cada uno a sus propios nombres.

PEDIMENTO_ADUANAL usa los nombres canónicos; PEDIMENTO_ERP usa los nombres de
columna de la tabla del ERP y declara `pedimento_role` para conservar la
recuperación determinista pese al renombre. El contrato que este test fija es
que ninguno de los dos dependa de nombres escritos a mano dentro del pipeline:
si alguien vuelve a cablear "NUMERO_PEDIMENTO" o "PATENTE" en expediente.py,
PEDIMENTO_ERP deja de armar su número de pedimento y este test se pone rojo.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline import expediente, qr_recovery  # noqa: E402
from pipeline.liquidacion import build_liquidacion_normalizada  # noqa: E402
from validators.engine import resolve_field_tree  # noqa: E402

# Pedimento real de test-docs/full-dataset-pedimentos/normal_pend.pdf: su QR
# (Pa=0823, dn=6001630, ap=2026, pad=072) produce estos cuatro componentes.
COMPONENTES = {"anio": "26", "aduana": "07", "patente": "0823", "consecutivo": "6001630"}
NUMERO_COMPLETO = "260708236001630"

# Nombre de campo que cada catálogo usa para cada rol. Escrito a mano a
# propósito: es el mapeo que se está verificando, no se deriva del código.
NOMBRES = {
    "PEDIMENTO_ADUANAL": {
        "anio": "ANIO_PEDIMENTO",
        "aduana": "ADUANA_DESPACHO",
        "patente": "PATENTE",
        "consecutivo": "NUMERO_CONSECUTIVO",
        "numero": "NUMERO_PEDIMENTO",
        "normalizado": "NUMERO_PEDIMENTO_NORMALIZADO",
    },
    "PEDIMENTO_ERP": {
        "anio": "ANIO_PEDIMENTO",
        "aduana": "ADUANA_DESPACHO",
        "patente": "Patente",
        "consecutivo": "NUMERO_CONSECUTIVO",
        "numero": "No_Pedimento",
        "normalizado": "No_Pedimento",
    },
}

# Conceptos tal como los imprime el cuadro de liquidación de ese pedimento,
# y la columna en la que cada catálogo los deja.
CONCEPTOS = [
    {"CLAVE_CONCEPTO": "PRV", "IMPORTE": 330},
    {"CLAVE_CONCEPTO": "IVA PRV", "IMPORTE": 53},
]
LIQUIDACION_ESPERADA = {
    "PEDIMENTO_ADUANAL": {"PRV": 330.0, "IVA": 53.0, "TOTAL_CONTRIBUCIONES": 383.0, "DTA": 0},
    "PEDIMENTO_ERP": {"CL_PRV": 330.0, "CL_IVA": 53.0, "CL_Total": 383.0, "CL_DTA": 0},
}

TIPOS = sorted(NOMBRES)


def cargar(type_id):
    path = REPO_ROOT / "catalogs" / f"{type_id}.json"
    return _parse_entry(type_id, json.loads(path.read_text(encoding="utf-8"))[type_id])


@pytest.mark.parametrize("type_id", TIPOS)
def test_cada_catalogo_resuelve_sus_propios_nombres(type_id):
    resueltos = expediente._pedimento_field_names(cargar(type_id))
    for rol, esperado in NOMBRES[type_id].items():
        assert resueltos[rol] == esperado, f"{type_id}: rol '{rol}' resolvió a {resueltos[rol]!r}"


@pytest.mark.parametrize("type_id", TIPOS)
def test_el_numero_de_pedimento_se_arma_desde_los_componentes_del_qr(type_id):
    """Los 4 componentes que deja el QR se concatenan en los 15 dígitos, y
    pisan el valor corto que el modelo saca del recuadro de PAGO ELECTRÓNICO."""
    entry = cargar(type_id)
    nombres = NOMBRES[type_id]
    entities = {nombres[rol]: valor for rol, valor in COMPONENTES.items()}
    entities[nombres["numero"]] = "6001630"  # el error real: solo el consecutivo
    # 0.99 es la confianza que `_recover_from_qr` le pone a lo que sale del QR;
    # es lo que autoriza a pisar el valor del modelo en el campo "numero".
    confidences = {nombre: 0.99 for nombre in entities}
    doc = {"_entities": entities, "_text": "", "_confidences": confidences}

    expediente._recover_references(doc, entry)

    assert entities[nombres["normalizado"]] == NUMERO_COMPLETO
    assert entities[nombres["numero"]] == NUMERO_COMPLETO
    assert doc["recoveredFields"][nombres["normalizado"]] == NUMERO_COMPLETO
    assert entities[nombres["patente"]] == "0823"


@pytest.mark.parametrize("type_id", TIPOS)
def test_los_conceptos_del_cuadro_caen_en_las_columnas_del_catalogo(type_id):
    entry = cargar(type_id)
    entity_map = {"CUADRO_LIQUIDACION": {"CONCEPTOS": CONCEPTOS, "TOTAL": 383}}

    salida = build_liquidacion_normalizada(entity_map, entry)

    for columna, importe in LIQUIDACION_ESPERADA[type_id].items():
        assert salida[columna] == importe, f"{type_id}.{columna} = {salida[columna]!r}"


# Columnas de la tabla de pedimentos del ERP (encabezados del export CSV).
COLUMNAS_ERP = {
    "Num_Pedimento", "Lote", "No_Pedimento", "Cve_Pedimento", "Fecha_Entrada", "Tipo",
    "Pais_Origen", "Patente", "Valor_ComercialDol", "ValorAduana", "Valor_ComercialPesos",
    "Tipo_Cambio", "Tipo_Pago", "Ped_Rectificacion", "GI_ValSeguros", "GI_Seguros", "GI_Fletes",
    "GI_Embalajes", "GI_Otros", "GI_Total", "CL_DTA", "CL_IGI", "CL_PRV", "CL_ECI", "CL_MT",
    "CL_IVA", "CL_IEPS25", "CL_IEPS30", "CL_IEPS50", "CL_Total", "Num_Anticipo", "Com_Pendiente",
    "Referencia", "Status", "Fecha_Captura", "Fecha_Empate", "Dia_Empate", "CL_IEPS08", "Usuario",
    "tipo_mercancia", "CL_FLETEIEPS", "num_documento", "folio", "CL_CC", "CL_IEPS_CAFE",
    "Usuario_Libero", "Fecha_Cierre", "Ped_A1_Exportacion", "CL_REC",
}
# Lo único que PEDIMENTO_ERP declara sin ser columna: insumo del QR y del
# cuadro de liquidación, más la auditoría de que el QR decodificó.
INSUMOS_ERP = {
    "ANIO_PEDIMENTO", "ADUANA_DESPACHO", "NUMERO_CONSECUTIVO",
    "QR_PEDIMENTO", "DEPOSITO_REFERENCIADO_QR_VALIDO",
    "CUADRO_LIQUIDACION", "LIQUIDACION_NORMALIZADA",
}


def test_pedimento_erp_solo_declara_columnas_del_erp():
    entry = cargar("PEDIMENTO_ERP")
    sobran = set(entry.field_tree) - COLUMNAS_ERP - INSUMOS_ERP
    assert not sobran, f"campos que no son columna del ERP ni insumo declarado: {sorted(sobran)}"

    cl = set(entry.field_tree["LIQUIDACION_NORMALIZADA"].fields)
    assert cl <= COLUMNAS_ERP, f"subcampos que no son columna del ERP: {sorted(cl - COLUMNAS_ERP)}"


# QR real de ese pedimento (test-docs/full-dataset-pedimentos/normal_pend.pdf).
QR_URL = (
    "https://aplicacionesc.mat.sat.gob.mx/SOIANET/oia_consultarapd_cep.aspx"
    "?Pa=0823&dn=6001630&s=0&ap=2026&pad=072&ad=CD%20JUAREZ,%20CHIH"
)
PAGINA_QR = SimpleNamespace(
    number=1,
    text="NUM. PEDIMENTO: 26 07 0823 6001630 DEPOSITO REFERENCIADO - LINEA DE CAPTURA "
         "PAGO ELECTRONICO IMPORTE PAGADO",
)


@pytest.fixture
def qr_decodificado(monkeypatch):
    """Sustituye solo el rasterizado y el decodificado del QR (las libs de
    imagen), para que el resto del camino real corra: elegir la página, filtrar
    por `url_contains` y aplicar el `qr_recovery` que declara cada catálogo."""
    monkeypatch.setattr(qr_recovery, "render_page_array", lambda *a, **k: object())
    monkeypatch.setattr(qr_recovery, "decode_qr_texts", lambda *a, **k: [QR_URL])


@pytest.mark.parametrize("type_id", TIPOS)
def test_el_valor_recuperado_reporta_valor_confianza_y_origen(type_id, qr_decodificado):
    """Lo que sale del QR llega al resultado con el valor corregido, la
    confianza del QR y un `source` que NO dice que lo escribió el modelo —
    en los dos catálogos, se llame como se llame el campo."""
    entry = cargar(type_id)
    nombres = NOMBRES[type_id]
    # el modelo dejó el consecutivo corto del recuadro de pago, con confianza baja
    doc = {
        "_entities": {nombres["numero"]: "6001630"},
        "_text": PAGINA_QR.text,
        "_confidences": {nombres["numero"]: 0.72},
    }

    expediente._recover_from_qr(doc, entry, SimpleNamespace(page_start=1), object(), [PAGINA_QR])
    expediente._recover_references(doc, entry)

    entities, confidences = doc["_entities"], doc["_confidences"]
    tree, _, _ = resolve_field_tree(
        entities, confidences, entry, source_overrides=doc.get("_recovered_sources")
    )

    nodo = tree[nombres["normalizado"]]
    assert nodo["value"] == NUMERO_COMPLETO
    assert nodo["confidence"] == 0.99
    assert nodo["source"] == expediente._SOURCE_QR
    assert tree[nombres["patente"]]["value"] == "0823"
    assert tree[nombres["patente"]]["source"] == expediente._SOURCE_QR
    assert tree["DEPOSITO_REFERENCIADO_QR_VALIDO"]["value"] is True
    assert tree["QR_PEDIMENTO"]["value"] == QR_URL
    # la traza de auditoría conserva lo que leyó el QR y lo que derivó el código
    assert nombres["patente"] in doc["recoveredFields"]
    assert doc["recoveredFields"][nombres["normalizado"]] == NUMERO_COMPLETO


@pytest.mark.parametrize("type_id", TIPOS)
def test_lo_que_llena_el_modelo_sigue_marcado_como_del_modelo(type_id, qr_decodificado):
    entry = cargar(type_id)
    campo = "Cve_Pedimento" if type_id == "PEDIMENTO_ERP" else "CLAVE_PEDIMENTO"
    doc = {"_entities": {campo: "A1"}, "_text": PAGINA_QR.text, "_confidences": {campo: 0.88}}

    expediente._recover_from_qr(doc, entry, SimpleNamespace(page_start=1), object(), [PAGINA_QR])
    expediente._recover_references(doc, entry)
    tree, _, _ = resolve_field_tree(
        doc["_entities"], doc["_confidences"], entry,
        source_overrides=doc.get("_recovered_sources"),
    )

    assert tree[campo]["source"] == "MODEL_EXTRACTION"
    assert tree[campo]["confidence"] == 0.88
