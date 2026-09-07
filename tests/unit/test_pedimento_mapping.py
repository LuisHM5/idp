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
sys.path.insert(0, str(REPO_ROOT / "tests"))

from catalog_paths import active_catalog_path  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline import expediente, qr_recovery  # noqa: E402
from pipeline.confidence import row_key  # noqa: E402
from pipeline.derived import SOURCE_DERIVED, apply_derived_fields  # noqa: E402
from pipeline.liquidacion import (  # noqa: E402
    apply_liquidacion_normalizada,
    build_liquidacion_normalizada,
)
from validators.engine import document_status_from_checks, resolve_field_tree  # noqa: E402

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
    path = active_catalog_path(REPO_ROOT, type_id)
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
        assert salida.values[columna] == importe, f"{type_id}.{columna} = {salida.values[columna]!r}"


@pytest.mark.parametrize("type_id", TIPOS)
def test_cada_columna_calculada_reporta_su_propia_confianza(type_id):
    """Un total calculado vale lo que valen las celdas que sumó: hereda el
    PROMEDIO de sus confianzas. Un concepto que no está en el cuadro hereda la
    del cuadro en el que se buscó. Antes ninguna columna reportaba nada: la
    salida traía `LIQUIDACION_NORMALIZADA` entera con `confidence: null`."""
    entry = cargar(type_id)
    # dos renglones de IVA, uno leído mal y otro perfecto: el catálogo los
    # concentra en la misma columna, así que su confianza es el promedio.
    conceptos = CONCEPTOS + [{"CLAVE_CONCEPTO": "IVA", "IMPORTE": 7}]
    entity_map = {"CUADRO_LIQUIDACION": {"CONCEPTOS": conceptos, "TOTAL": 390}}
    row_conf = {
        row_key(c): {"IMPORTE": conf}
        for c, conf in zip(conceptos, (0.6, 0.7, 0.9))
    }
    prv = "CL_PRV" if type_id == "PEDIMENTO_ERP" else "PRV"
    iva = "CL_IVA" if type_id == "PEDIMENTO_ERP" else "IVA"
    dta = "CL_DTA" if type_id == "PEDIMENTO_ERP" else "DTA"

    salida = build_liquidacion_normalizada(
        entity_map, entry, {"CUADRO_LIQUIDACION.CONCEPTOS": 0.9}, row_conf
    )

    assert salida.confidences[prv] == 0.6            # su única celda, no el promedio del cuadro
    assert salida.confidences[iva] == 0.8            # promedio de las dos celdas que sumó (0.7, 0.9)
    assert salida.confidences[dta] == 0.9            # concepto ausente: vale lo que el cuadro leído


@pytest.mark.parametrize("type_id", TIPOS)
def test_una_columna_sin_fuente_medida_no_inventa_un_cero_de_confianza(type_id):
    """Si nadie midió la tabla de la que sale la columna, la liquidación no
    reporta confianza: el motor le pone la del documento leído. Reportar 0.0
    aquí borraría ceros legítimos — un pedimento sin partidas de IEPS tiene
    CL_IEPS30 = 0 por buena razón, y el piso de descarte lo volvería null."""
    entry = cargar(type_id)
    entity_map = {"CUADRO_LIQUIDACION": {"CONCEPTOS": CONCEPTOS, "TOTAL": 383}}

    prv = "CL_PRV" if type_id == "PEDIMENTO_ERP" else "PRV"

    salida = build_liquidacion_normalizada(entity_map, entry)

    assert set(salida.confidences.values()) == {None}
    assert salida.values[prv] == 330.0  # el valor sale igual: solo cambia qué tanto se confía


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
# Columnas de Costos.dbo.C_ENC_FACTURA (la tabla de facturas del ERP, otra
# distinta de la de pedimentos) que el pedimento sí imprime.
COLUMNAS_C_ENC_FACTURA = {"Factura", "Fecha_Factura", "Total_factura"}

# Lo que PEDIMENTO_ERP declara sin ser columna de ninguna de las dos tablas:
# insumo del QR y del cuadro de liquidación, la auditoría de que el QR
# decodificó, y los datos del proveedor que el ERP resuelve por su cuenta
# (los códigos internos Proveedor/prov salen del ID fiscal, no de la extracción).
INSUMOS_ERP = {
    "ANIO_PEDIMENTO", "ADUANA_DESPACHO", "NUMERO_CONSECUTIVO",
    "QR_PEDIMENTO", "DEPOSITO_REFERENCIADO_QR_VALIDO",
    "CUADRO_LIQUIDACION", "LIQUIDACION_NORMALIZADA",
    # Las facturas viven en su propio array (una fila = un registro de
    # C_ENC_FACTURA); ACUSES_VALOR son los COVE que el pedimento lista sueltos.
    "FACTURAS", "ACUSES_VALOR",
}
# Columnas de una fila de FACTURAS que NO son columna de C_ENC_FACTURA: el ERP
# resuelve sus códigos internos Proveedor/prov desde el ID fiscal, y el resto
# describe la factura sin tener dónde guardarse.
INSUMOS_FACTURA = {
    "PROVEEDOR_ID_FISCAL", "PROVEEDOR_NOMBRE", "PROVEEDOR_DOMICILIO",
    "PROVEEDOR_VINCULACION", "num_acuse_valor", "INCOTERM", "MONEDA_FACTURA",
    "FACTOR_MONEDA_FACTURA", "FACTURA_VALOR_DOLARES",
}


def test_pedimento_erp_solo_declara_columnas_del_erp():
    entry = cargar("PEDIMENTO_ERP")
    sobran = set(entry.field_tree) - COLUMNAS_ERP - INSUMOS_ERP
    assert not sobran, f"campos que no son columna del ERP ni insumo declarado: {sorted(sobran)}"

    cl = set(entry.field_tree["LIQUIDACION_NORMALIZADA"].fields)
    assert cl <= COLUMNAS_ERP, f"subcampos que no son columna del ERP: {sorted(cl - COLUMNAS_ERP)}"

    fila = set(entry.field_tree["FACTURAS"].item_fields)
    sobran_fila = fila - COLUMNAS_C_ENC_FACTURA - INSUMOS_FACTURA
    assert not sobran_fila, f"columnas de factura no declaradas: {sorted(sobran_fila)}"


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


# ---------------------------------------------------------------- Tipo_Pago

# Qué tan bien se leyó la página: lo que `_extract_segment` le pasa al motor
# como `default_confidence` para lo que el extractor no numeró campo por campo.
CONFIANZA_OCR = 0.98


def resolver_erp(cuadro, confidences=None):
    """Corre el tramo determinista del pipeline sobre un cuadro de liquidación
    dado y devuelve el árbol resuelto y sus checks, tal como los arma
    `_extract_segment`: liquidación normalizada, campos `derived` y luego el
    motor con el `source_overrides` que producen."""
    entry = cargar("PEDIMENTO_ERP")
    entities = {"CUADRO_LIQUIDACION": cuadro}
    confidences = dict(confidences or {})
    apply_liquidacion_normalizada(entities, confidences, entry)
    derived = apply_derived_fields(entities, confidences, entry)
    tree, checks, _ = resolve_field_tree(
        entities, confidences, entry,
        source_overrides=derived.sources, value_labels=derived.labels,
        default_confidence=CONFIANZA_OCR,
    )
    return tree, checks


# Códigos de la columna Tipo_Pago del ERP: 1 = Anticipo y Pasivo, 2 = Solo
# Anticipo, 3 = Solo Pasivo. El 2 no se puede decidir desde el pedimento.
@pytest.mark.parametrize(
    "total, codigo, etiqueta",
    [
        (383, 3, "Solo Pasivo"),      # el pedimento real: PRV + IVA PRV = la cuota fija
        (382.5, 3, "Solo Pasivo"),
        (383.01, 1, "Anticipo y Pasivo"),
        (12450, 1, "Anticipo y Pasivo"),
    ],
)
def test_tipo_pago_se_deriva_del_total_del_cuadro(total, codigo, etiqueta):
    """Tipo_Pago no está impreso en el pedimento: sale del TOTAL del cuadro de
    liquidación, con el umbral declarado en el catálogo (no en código). El
    valor es el código que guarda la columna; la etiqueta viaja aparte."""
    tree, _ = resolver_erp({"CONCEPTOS": CONCEPTOS, "TOTAL": total})

    assert tree["Tipo_Pago"]["value"] == codigo
    assert tree["Tipo_Pago"]["label"] == etiqueta


def test_el_codigo_es_numero_no_texto():
    """La columna del ERP es numérica: un "3" en string entraría distinto."""
    tree, _ = resolver_erp({"CONCEPTOS": CONCEPTOS, "TOTAL": 383})

    assert isinstance(tree["Tipo_Pago"]["value"], int)


def test_tipo_pago_reporta_que_lo_calculo_el_pipeline_y_hereda_la_confianza():
    """No lo escribió el modelo — de hecho el modelo ni siquiera lo ve — así que
    su `source` no puede decir MODEL_EXTRACTION, y su confianza es exactamente
    la del TOTAL del que salió: ni más ni menos."""
    tree, _ = resolver_erp(
        {"CONCEPTOS": CONCEPTOS, "TOTAL": 383},
        {"CUADRO_LIQUIDACION.TOTAL": 0.93},
    )

    assert tree["Tipo_Pago"]["source"] == SOURCE_DERIVED
    assert tree["Tipo_Pago"]["confidence"] == 0.93


def test_sin_total_legible_no_se_supone_un_tipo_de_pago():
    """Si el TOTAL no se pudo leer, el campo sale vacío y el documento cae a
    revisión, en vez de entrar al ERP como puro pasivo."""
    tree, checks = resolver_erp({"CONCEPTOS": [], "TOTAL": None})

    assert tree["Tipo_Pago"]["value"] is None
    assert tree["Tipo_Pago"]["valid"] is False
    assert "label" not in tree["Tipo_Pago"]  # etiquetar un valor que nadie decidió sería inventar
    assert document_status_from_checks(checks) != "APPROVED"


def test_el_modelo_no_gasta_tokens_adivinando_tipo_pago():
    """`computed: true` lo saca del schema que se le manda a Bedrock."""
    entry = cargar("PEDIMENTO_ERP")
    assert entry.field_tree["Tipo_Pago"].computed is True


def test_el_pipeline_real_llena_tipo_pago(monkeypatch, qr_decodificado):
    """No basta con que el evaluador funcione: `_extract_segment` tiene que
    llamarlo. Se corre el tramo real con una extracción enlatada — si alguien
    quita el paso de campos `derived` del pipeline, esto se pone rojo."""
    from pipeline.segmenter import Segment
    from pipeline.text_extractor import ExtractedDoc

    entry = cargar("PEDIMENTO_ERP")
    extraido = ExtractedDoc(
        entity_map={"CUADRO_LIQUIDACION": {"CONCEPTOS": CONCEPTOS, "TOTAL": 383}},
        confidences={"CUADRO_LIQUIDACION.TOTAL": 0.91},
    )
    monkeypatch.setattr(expediente, "get_type_entry", lambda _t: entry)
    monkeypatch.setattr(expediente, "extract_document", lambda *a, **k: extraido)
    monkeypatch.setattr(expediente.page_selector, "enabled_for", lambda _e: False)
    seg = Segment(doc_type="PEDIMENTO_ERP", page_start=1, page_end=1, confidence=0.9)

    doc = expediente._extract_segment(seg, [PAGINA_QR], model_id=None, reader=object())

    assert doc["_entities"]["Tipo_Pago"] == 3
    assert doc["_recovered_sources"]["Tipo_Pago"] == SOURCE_DERIVED
    assert doc["_derived_labels"]["Tipo_Pago"] == "Solo Pasivo"
    assert doc["_confidences"]["Tipo_Pago"] == 0.91


def test_pedimento_aduanal_no_cambia():
    """El catálogo maestro no declara ningún `derived`, así que el paso nuevo es
    un no-op para él: mismo entity_map, sin fuentes que sobreescribir."""
    entry = cargar("PEDIMENTO_ADUANAL")
    entities = {"CUADRO_LIQUIDACION": {"CONCEPTOS": CONCEPTOS, "TOTAL": 383}}
    antes = json.dumps(entities, sort_keys=True)

    derived = apply_derived_fields(entities, {}, entry)

    assert derived.sources == {} and derived.labels == {}
    assert json.dumps(entities, sort_keys=True) == antes


# ------------------------------------------------------- facturas (N filas)

# Bloque real de test-docs/full-dataset-pedimentos/'26 07 3435 6041353 CG.pdf'
# (pág. 4): DOS proveedores, con 8 y 1 facturas, y debajo una tabla APARTE con
# 9 acuses sueltos. Es el caso que la forma plana anterior no podía representar:
# reportaba solo la primera fila y perdía las otras 8.
MNG = {
    "PROVEEDOR_ID_FISCAL": "334274279",
    "PROVEEDOR_NOMBRE": "MNG DISTRIBUTOR LLC",
    "PROVEEDOR_DOMICILIO": "FLORA VISTA AVE 13724 79928 HORIZON CITY TEXAS, ESTADOS UNIDOS DE AMERICA",
    "PROVEEDOR_VINCULACION": "NO",
}
TRADE_FOOD = {
    "PROVEEDOR_ID_FISCAL": "320713796",
    "PROVEEDOR_NOMBRE": "TRADE FOOD LLC",
    "PROVEEDOR_DOMICILIO": "BLUE LAGOON DR 5805 STE.200 33126 MIAMI FLORIDA, ESTADOS UNIDOS DE AMERICA",
    "PROVEEDOR_VINCULACION": "NO",
}
COMUN = {"Fecha_Factura": "03/07/2026", "INCOTERM": "DAP", "MONEDA_FACTURA": "USD",
         "FACTOR_MONEDA_FACTURA": "1.00000000"}
IMPORTES_MNG = [("4099", "155.00"), ("4100", "124.00"), ("4101", "2,569.00"),
                ("4102", "4,017.50"), ("4103", "5,115.07"), ("4104", "5,143.44"),
                ("4105", "686.99"), ("4106", "685.96")]
FACTURAS_CG = (
    [{**MNG, **COMUN, "Factura": n, "Total_factura": v, "FACTURA_VALOR_DOLARES": v}
     for n, v in IMPORTES_MNG]
    + [{**TRADE_FOOD, **COMUN, "Factura": "1671", "Total_factura": "2,944.32",
        "FACTURA_VALOR_DOLARES": "2,944.32"}]
)
ACUSES_CG = ["COVE26871ECA1", "COVE26871ECE6", "COVE26871ECF2", "COVE26871ECK4",
             "COVE26871ECN7", "COVE26871ECO8", "COVE26871ECP2", "COVE26871ECT5",
             "COVE26871ECU3"]

# Fila real de test-docs/pedimentos-screens/2607-0973-6100971_P.PDF-1.pdf: un
# solo proveedor y una sola factura, con el acuse y el número en la misma celda.
FACTURA_TYSON = {
    "PROVEEDOR_ID_FISCAL": "71-0225165", "PROVEEDOR_NOMBRE": "TYSON FOODS INC.",
    "PROVEEDOR_DOMICILIO": "2200 W DON TYSON PARKWAY SPRINGDALE, SPRINGDALE, ARKANSAS 72762 USA",
    "PROVEEDOR_VINCULACION": "NO", "Factura": "35079805",
    "num_acuse_valor": "COVE2689JP013 / 35079805", "Fecha_Factura": "24/08/2026",
    "INCOTERM": "CPT", "MONEDA_FACTURA": "USD", "Total_factura": "21,178.80",
    "FACTOR_MONEDA_FACTURA": "1.00000", "FACTURA_VALOR_DOLARES": "21,178.80",
}

# Tipo que el ERP espera de cada columna de una fila de factura.
FACTURA_ESPERADA = {
    "PROVEEDOR_ID_FISCAL": "string",
    "PROVEEDOR_NOMBRE": "string",
    "PROVEEDOR_DOMICILIO": "string",
    "PROVEEDOR_VINCULACION": "string",
    "Factura": "string",
    "num_acuse_valor": "string",
    "Fecha_Factura": "date",
    "Total_factura": "number",
    "INCOTERM": "string",
    "MONEDA_FACTURA": "string",
    "FACTOR_MONEDA_FACTURA": "number",
    "FACTURA_VALOR_DOLARES": "number",
}


def filas(entities):
    """Las filas de FACTURAS ya resueltas, como dict campo -> valor."""
    entry = cargar("PEDIMENTO_ERP")
    tree, checks, _ = resolve_field_tree(
        entities, {}, entry, default_confidence=CONFIANZA_OCR
    )
    items = tree["FACTURAS"]["items"]
    return [{n: c["value"] for n, c in item["fields"].items()} for item in items], tree, checks


def test_cada_factura_es_una_fila_con_su_proveedor():
    """El contrato con el ERP: una fila del array = un insert en
    Costos.dbo.C_ENC_FACTURA. Nueve facturas de dos proveedores son NUEVE
    filas, cada una con el proveedor al que pertenece — antes, con los campos
    planos, solo llegaba la primera y las otras ocho se perdían."""
    filas_resueltas, _, _ = filas({"FACTURAS": FACTURAS_CG})

    assert len(filas_resueltas) == 9
    assert [f["Factura"] for f in filas_resueltas] == [n for n, _ in IMPORTES_MNG] + ["1671"]
    assert [f["PROVEEDOR_NOMBRE"] for f in filas_resueltas] == ["MNG DISTRIBUTOR LLC"] * 8 + ["TRADE FOOD LLC"]
    assert filas_resueltas[-1]["PROVEEDOR_ID_FISCAL"] == "320713796"
    # cada fila trae su propio importe, no el del primer renglón
    assert [f["Total_factura"] for f in filas_resueltas][:3] == [155.0, 124.0, 2569.0]


def test_los_acuses_sueltos_no_se_cuelan_como_facturas():
    """La tercera tabla de ese pedimento lista 9 COVE sin fecha ni importe.
    Meterlos en FACTURAS serían 9 registros basura en C_ENC_FACTURA, y
    emparejarlos por posición con las facturas de arriba sería inventar una
    correspondencia que el pedimento no imprime."""
    filas_resueltas, tree, _ = filas({"FACTURAS": FACTURAS_CG, "ACUSES_VALOR": ACUSES_CG})

    assert len(filas_resueltas) == 9
    assert [i["value"] for i in tree["ACUSES_VALOR"]["items"]] == ACUSES_CG
    assert all(f["num_acuse_valor"] is None for f in filas_resueltas)


def test_un_acuse_que_no_es_cove_no_pasa():
    entry = cargar("PEDIMENTO_ERP")
    _, checks, _ = resolve_field_tree(
        {"ACUSES_VALOR": ACUSES_CG[:2] + ["4099"]}, {}, entry, default_confidence=CONFIANZA_OCR
    )
    fallidos = [c for c in checks if c.field == "ACUSES_VALOR" and not c.valid and not c.skipped]
    assert fallidos, "regex_each debería rechazar un valor que no es un COVE"


def test_cada_columna_de_la_fila_esta_declarada_con_su_tipo():
    """El bloque DATOS DEL PROVEEDOR O COMPRADOR completo, con el tipo que
    espera el ERP: la fecha como date y los importes como number, no como
    texto que alguien tenga que convertir del otro lado."""
    item_fields = cargar("PEDIMENTO_ERP").field_tree["FACTURAS"].item_fields

    for nombre, tipo in FACTURA_ESPERADA.items():
        assert nombre in item_fields, f"falta {nombre}"
        assert item_fields[nombre].type == tipo, (
            f"{nombre} es {item_fields[nombre].type}, se esperaba {tipo}"
        )


def test_ninguna_columna_de_la_fila_es_obligatoria():
    """Un pedimento puede imprimir la tabla con NUM. FACTURA (sin acuse) o con
    NUMERO DE ACUSE DE VALOR (sin número suelto), y la tabla de acuses sueltos
    no trae ni ID fiscal ni nombre. Exigir cualquiera de esas columnas por fila
    mandaría a revisión pedimentos correctos: lo obligatorio es el array."""
    entry = cargar("PEDIMENTO_ERP")

    assert entry.field_tree["FACTURAS"].required is True
    for nombre, spec in entry.field_tree["FACTURAS"].item_fields.items():
        assert spec.required is False, f"{nombre} no puede ser obligatorio por fila"


def test_ninguno_de_esos_campos_se_le_pide_dos_veces_al_modelo():
    """El valor en dólares de UNA factura y el del encabezado del pedimento son
    cosas distintas y tienen nombres distintos: si alguien los colapsara,
    Valor_ComercialDol (suma de todas las facturas) se llenaría con el importe
    de una sola."""
    entry = cargar("PEDIMENTO_ERP")
    fila = entry.field_tree["FACTURAS"].item_fields

    assert "Valor_ComercialDol" in entry.field_tree
    assert "FACTURA_VALOR_DOLARES" in fila
    assert entry.field_tree["Valor_ComercialDol"].hint != fila["FACTURA_VALOR_DOLARES"].hint
    # y los planos ya no existen: una sola fuente de verdad
    for plano in FACTURA_ESPERADA:
        assert plano not in entry.field_tree, f"{plano} quedó también como campo plano"


@pytest.mark.parametrize(
    "valor, valida",
    [
        ("35079805", True),                    # NUMERO DE ACUSE DE VALOR, solo el número
        ("4099", True),                        # NUM. FACTURA tal cual
        ("A-1234", True),
        ("COVE2689JP013 / 35079805", False),   # el acuse completo va en num_acuse_valor
        ("COVE2689JP013/35079805", False),
    ],
)
def test_la_factura_rechaza_el_acuse_completo(valor, valida):
    """La regla que el usuario fijó — en Factura va SOLO el número — es una
    regla real, no un hint, y corre FILA POR FILA: la fila con el COVE pegado
    sale inválida aunque las demás estén bien."""
    _, tree, _ = filas({"FACTURAS": [{"Factura": "4099"}, {"Factura": valor}]})

    assert tree["FACTURAS"]["items"][0]["valid"] is True
    fila = tree["FACTURAS"]["items"][1]
    assert fila["fields"]["Factura"]["valid"] is valida, fila["reason"]
    assert fila["valid"] is valida


@pytest.mark.parametrize(
    "valor, valida",
    [
        ("COVE2689JP013 / 35079805", True),   # la fila real del pedimento de TYSON FOODS
        ("COVE26871ECA1", True),              # tabla que solo lista acuses, sin factura
        ("COVE2689JP013/35079805", True),     # sin espacios alrededor de la diagonal
        ("35079805", False),                  # solo el número: eso es Factura, no el acuse
        ("268901335079805", False),           # el acuse leído como si fuera un importe
        ("24/08/2026", False),                # la FECHA de la columna de al lado
    ],
)
def test_el_acuse_se_copia_completo_o_no_pasa(valor, valida):
    """El acuse es el COVE COMPLETO. Un valor sin COVE — el número suelto, o la
    cadena convertida a número — no es un acuse a medias: es otro dato."""
    _, tree, _ = filas({"FACTURAS": [{"num_acuse_valor": valor}]})

    fila = tree["FACTURAS"]["items"][0]
    assert fila["fields"]["num_acuse_valor"]["valid"] is valida, fila["reason"]


def test_el_acuse_llega_al_erp_tal_como_esta_impreso():
    """El bug real: `num_acuse_valor` contiene "VALOR", así que la
    normalización por nombre lo parseaba como importe y entregaba
    268901335079805.0 — sin COVE, sin diagonal, marcado NORMALIZED y válido.
    El tipo declarado (`string`) manda sobre la heurística del nombre."""
    filas_resueltas, tree, _ = filas({"FACTURAS": [FACTURA_TYSON]})

    fila = filas_resueltas[0]
    assert fila["num_acuse_valor"] == "COVE2689JP013 / 35079805"
    assert fila["Factura"] == "35079805"                  # el número, aparte
    celda = tree["FACTURAS"]["items"][0]["fields"]["num_acuse_valor"]
    assert celda["source"] == "MODEL_EXTRACTION"          # nadie lo tocó


def test_las_columnas_numericas_llegan_como_numero():
    """La otra cara del mismo contrato: un campo declarado `number` cuyo nombre
    no dispara ninguna heurística de importe (FACTOR_MONEDA_FACTURA, GI_Otros)
    entraba al ERP como texto ("1.0"), no como número."""
    filas_resueltas, tree, _ = filas(
        {"FACTURAS": [FACTURA_TYSON], "GI_Otros": "11,027"}
    )

    fila = filas_resueltas[0]
    assert fila["FACTOR_MONEDA_FACTURA"] == 1.0
    assert fila["Total_factura"] == 21178.8
    assert tree["GI_Otros"]["value"] == 11027.0
    assert isinstance(fila["FACTOR_MONEDA_FACTURA"], float)
    assert isinstance(tree["GI_Otros"]["value"], float)
    assert fila["Fecha_Factura"] == "2026-08-24"


def test_el_incoterm_debe_ser_de_tres_letras():
    _, tree, _ = filas({"FACTURAS": [{"INCOTERM": "CPT"}, {"INCOTERM": "CPT USD 21,178.80"}]})

    assert tree["FACTURAS"]["items"][0]["fields"]["INCOTERM"]["valid"] is True
    assert tree["FACTURAS"]["items"][1]["fields"]["INCOTERM"]["valid"] is False
