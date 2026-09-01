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
    "PROVEEDOR_ID_FISCAL", "PROVEEDOR_NOMBRE", "PROVEEDOR_DOMICILIO",
    "PROVEEDOR_VINCULACION", "num_acuse_valor", "INCOTERM", "MONEDA_FACTURA",
    "FACTOR_MONEDA_FACTURA", "FACTURA_VALOR_DOLARES",
}


def test_pedimento_erp_solo_declara_columnas_del_erp():
    entry = cargar("PEDIMENTO_ERP")
    sobran = set(entry.field_tree) - COLUMNAS_ERP - COLUMNAS_C_ENC_FACTURA - INSUMOS_ERP
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


# ---------------------------------------------------------------- proveedor

# Fila real de test-docs/full-dataset-pedimentos/'26 07 3435 6041353 CG.pdf':
# "334274279 NO" bajo VINCULACION, y la tabla de facturas debajo.
PROVEEDOR_ESPERADO = {
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


def test_los_datos_del_proveedor_estan_declarados_con_su_tipo():
    """El bloque DATOS DEL PROVEEDOR O COMPRADOR completo, con el tipo que
    espera el ERP: la fecha como date y los importes como number, no como
    texto que alguien tenga que convertir del otro lado."""
    entry = cargar("PEDIMENTO_ERP")

    for nombre, tipo in PROVEEDOR_ESPERADO.items():
        assert nombre in entry.field_tree, f"falta {nombre}"
        assert entry.field_tree[nombre].type == tipo, (
            f"{nombre} es {entry.field_tree[nombre].type}, se esperaba {tipo}"
        )


def test_ninguno_de_esos_campos_se_le_pide_dos_veces_al_modelo():
    """El valor en dólares de UNA factura y el del encabezado del pedimento son
    cosas distintas y tienen nombres distintos: si alguien los colapsara,
    Valor_ComercialDol (suma de todas las facturas) se llenaría con el importe
    de una sola."""
    entry = cargar("PEDIMENTO_ERP")

    assert "Valor_ComercialDol" in entry.field_tree
    assert "FACTURA_VALOR_DOLARES" in entry.field_tree
    assert entry.field_tree["Valor_ComercialDol"].hint != entry.field_tree["FACTURA_VALOR_DOLARES"].hint


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
    regla real, no un hint: el pedimento con el COVE pegado sale inválido."""
    entry = cargar("PEDIMENTO_ERP")
    tree, _, _ = resolve_field_tree(
        {"Factura": valor}, {}, entry, default_confidence=CONFIANZA_OCR
    )

    assert tree["Factura"]["valid"] is valida, tree["Factura"]["reason"]


def test_el_incoterm_debe_ser_de_tres_letras():
    entry = cargar("PEDIMENTO_ERP")
    tree, _, _ = resolve_field_tree(
        {"INCOTERM": "CPT"}, {}, entry, default_confidence=CONFIANZA_OCR
    )
    assert tree["INCOTERM"]["valid"] is True

    tree, _, _ = resolve_field_tree(
        {"INCOTERM": "CPT USD 21,178.80"}, {}, entry, default_confidence=CONFIANZA_OCR
    )
    assert tree["INCOTERM"]["valid"] is False
