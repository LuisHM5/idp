"""SIGLAS_DOCUMENTO / TIPO_DOCUMENTO de los dos catálogos de gastos aduanales.

PEDIMENTO_GASTOS_ADUANALES_ERP (PDF) y su gemelo _XML abarcan varios documentos
del expediente de un pedimento. El contrato que este test fija:

- la sigla sale PRIMERO del nombre del archivo (`<pedimento>_<SIGLA><n>.<ext>`),
  siempre en forma canónica (FAA -> II, PE -> FPE, GT3 -> GT);
- sin sigla en el nombre se decide por contenido: en el XML con reglas sobre el
  CFDI, en el PDF con la lectura del modelo (que entonces se conserva);
- TIPO_DOCUMENTO es el nombre de esa sigla y nunca la contradice;
- los dos catálogos entregan lo mismo para el mismo nombre de archivo.

La tabla de abajo está escrita a mano a propósito: es la tabla del negocio, lo
que se está verificando, no algo que se derive del catálogo.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from catalog_paths import active_catalog_path  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline import expediente  # noqa: E402
from pipeline.derived import (  # noqa: E402
    CONTEXT_CONFIDENCE,
    SOURCE_DERIVED,
    apply_derived_fields,
    build_context,
)
from validators.engine import resolve_field_tree  # noqa: E402

PDF = "PEDIMENTO_GASTOS_ADUANALES_ERP"
XML = "PEDIMENTO_GASTOS_ADUANALES_ERP_XML"

# sigla en el nombre del archivo -> (sigla canónica, tipo de documento)
TABLA = {
    "P": ("P", "Pedimento"),
    "HE": ("HE", "Honorarios Extranjeros"),
    "II": ("II", "Honorarios"),
    "FAA": ("II", "Honorarios"),
    "OLI": ("OLI", "Operaciones Logísticas"),
    "PFA": ("PFA", "Fletes"),
    "GT": ("GT", "Otros Servicios Aduanales / Gastos de Terceros"),
    "GT1": ("GT", "Otros Servicios Aduanales / Gastos de Terceros"),
    "GT12": ("GT", "Otros Servicios Aduanales / Gastos de Terceros"),
    "PS": ("PS", "Permisos de Salud"),
    "FPE": ("FPE", "Factura de Proveedores Extranjeros"),
    "PE": ("FPE", "Factura de Proveedores Extranjeros"),
}


def cargar(type_id):
    path = active_catalog_path(REPO_ROOT, type_id)
    return _parse_entry(type_id, json.loads(path.read_text(encoding="utf-8"))[type_id])


def derivar(type_id, nombre, entities=None, confidences=None):
    entities = dict(entities or {})
    confidences = dict(confidences or {})
    derived = apply_derived_fields(entities, confidences, cargar(type_id), build_context(nombre))
    return entities, confidences, derived


@pytest.mark.parametrize("type_id, ext", [(PDF, "pdf"), (XML, "xml")])
@pytest.mark.parametrize("en_nombre", sorted(TABLA))
def test_la_sigla_sale_del_nombre_del_archivo(type_id, ext, en_nombre):
    sigla, tipo = TABLA[en_nombre]
    nombre = f"raw/{type_id}/0b7e-uuid/2607-0823-6001803_{en_nombre}.{ext}"
    entities, confidences, derived = derivar(type_id, nombre)
    assert entities["SIGLAS_DOCUMENTO"] == sigla
    assert entities["TIPO_DOCUMENTO"] == tipo
    assert confidences["SIGLAS_DOCUMENTO"] == CONTEXT_CONFIDENCE
    assert confidences["TIPO_DOCUMENTO"] == CONTEXT_CONFIDENCE
    assert derived.sources["SIGLAS_DOCUMENTO"] == SOURCE_DERIVED


@pytest.mark.parametrize("type_id", [PDF, XML])
@pytest.mark.parametrize(
    "nombre, sigla",
    [
        ("2607-0823-6001803_gt2.PDF", "GT"),     # mayúsculas/minúsculas indistintas
        ("2607-0823-6001803-OLI.pdf", "OLI"),    # guion como separador
        ("26 07 0823 6001803 P.pdf", "P"),       # espacio como separador
        ("FAA.xml", "II"),                       # la sigla sola
    ],
)
def test_variantes_de_escritura_del_nombre(type_id, nombre, sigla):
    entities, _, _ = derivar(type_id, nombre)
    assert entities["SIGLAS_DOCUMENTO"] == sigla


@pytest.mark.parametrize(
    "nombre",
    [
        "factura.pdf",
        "26 07 3435 6041353 CG.pdf",        # CG no está en la tabla
        "GT1_2607-0823-6001803.pdf",        # la sigla va al FINAL, no al inicio
        "2607-0823-6001803_SOP.pdf",        # termina en P pero no es la sigla P
        "2607-0823-6001803_TIPS.pdf",       # ni PS
        "2607-0823-6001803_GT1",            # sin extensión
        "",
    ],
)
def test_un_nombre_sin_sigla_no_decide_nada_en_el_pdf(nombre):
    """Sin lectura del modelo y sin sigla en el nombre no se inventa nada: el
    `required` manda el documento a revisión."""
    entities, confidences, _ = derivar(PDF, nombre)
    assert entities["SIGLAS_DOCUMENTO"] is None
    assert entities["TIPO_DOCUMENTO"] is None
    assert "SIGLAS_DOCUMENTO" not in confidences


def test_el_tipo_en_la_key_de_s3_no_cuenta_como_nombre():
    """`raw/<TIPO>/<uuid>/archivo`: ni el TIPO ni el uuid son el nombre."""
    entities, _, _ = derivar(PDF, "raw/ALGO_GT/uuid_P/factura.pdf")
    assert entities["SIGLAS_DOCUMENTO"] is None


# ---- PDF: sin sigla en el nombre, decide el modelo por contenido -----------

def test_pdf_conserva_la_lectura_del_modelo_cuando_el_nombre_no_dice_nada():
    entities, confidences, derived = derivar(
        PDF, "factura.pdf", {"SIGLAS_DOCUMENTO": "OLI"}, {"SIGLAS_DOCUMENTO": 0.9}
    )
    assert entities["SIGLAS_DOCUMENTO"] == "OLI"
    assert confidences["SIGLAS_DOCUMENTO"] == 0.9
    assert "SIGLAS_DOCUMENTO" not in derived.sources   # sigue siendo MODEL_EXTRACTION
    assert entities["TIPO_DOCUMENTO"] == "Operaciones Logísticas"
    assert confidences["TIPO_DOCUMENTO"] == 0.9        # tan confiable como la sigla
    assert derived.sources["TIPO_DOCUMENTO"] == SOURCE_DERIVED


def test_pdf_el_nombre_manda_sobre_la_lectura_del_modelo():
    entities, confidences, _ = derivar(
        PDF, "2607-0823-6001803_GT3.pdf", {"SIGLAS_DOCUMENTO": "OLI"}, {"SIGLAS_DOCUMENTO": 0.9}
    )
    assert entities["SIGLAS_DOCUMENTO"] == "GT"
    assert confidences["SIGLAS_DOCUMENTO"] == CONTEXT_CONFIDENCE


def test_pdf_la_sigla_se_le_pide_al_modelo_y_el_tipo_no():
    fields = cargar(PDF).field_tree
    assert not fields["SIGLAS_DOCUMENTO"].computed and fields["SIGLAS_DOCUMENTO"].hint
    assert fields["TIPO_DOCUMENTO"].computed


def test_pdf_una_sigla_fuera_de_la_tabla_va_a_revision():
    entry = cargar(PDF)
    entities = {"SIGLAS_DOCUMENTO": "GT1"}   # el modelo no quitó el consecutivo
    confidences = {"SIGLAS_DOCUMENTO": 0.9}
    derived = apply_derived_fields(entities, confidences, entry, build_context("factura.pdf"))
    tree, _, _ = resolve_field_tree(
        entities, confidences, entry,
        source_overrides=derived.sources, value_labels=derived.labels,
    )
    assert tree["SIGLAS_DOCUMENTO"]["valid"] is False
    assert tree["TIPO_DOCUMENTO"]["valid"] is False   # requerido y sin valor


def test_pdf_el_nombre_llega_hasta_extract_segment(monkeypatch):
    """No basta con que `derived` sepa leer `$FILE_NAME`: el pipeline tiene que
    pasarle el nombre. Si alguien deja de propagar `source_name`, esto se pone
    rojo."""
    from pipeline.pdf_pages import PageText
    from pipeline.segmenter import Segment
    from pipeline.text_extractor import ExtractedDoc

    entry = cargar(PDF)
    extraido = ExtractedDoc(entity_map={"SIGLAS_DOCUMENTO": "GT"}, confidences={"SIGLAS_DOCUMENTO": 0.7})
    monkeypatch.setattr(expediente, "get_type_entry", lambda _t: entry)
    monkeypatch.setattr(expediente, "extract_document", lambda *a, **k: extraido)
    monkeypatch.setattr(expediente, "_recover_from_qr", lambda *a, **k: None)
    seg = Segment(doc_type=PDF, page_start=1, page_end=1, confidence=1.0)
    pagina = PageText(number=1, text="HONORARIOS", source="native", confidence=0.98)

    doc = expediente._extract_segment(
        seg, [pagina], model_id=None, source_name=f"raw/{PDF}/uuid/2607-0823-6001803_FAA.pdf"
    )

    assert doc["_entities"]["SIGLAS_DOCUMENTO"] == "II"
    assert doc["_entities"]["TIPO_DOCUMENTO"] == "Honorarios"
    assert doc["_recovered_sources"]["SIGLAS_DOCUMENTO"] == SOURCE_DERIVED


# ---- XML: sin sigla en el nombre, deciden reglas sobre el CFDI --------------

UUID = "0A1B2C3D-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    "entities, sigla",
    [
        # FAA de la muestra: honorarios de persona física, retiene ISR
        ({"No_Factura": UUID, "ISR": 33.5, "Concepto": "HONORARIOS"}, "II"),
        # OLI de la muestra
        ({"No_Factura": UUID, "Concepto": "SERVICIOS POR LOGISTICA"}, "OLI"),
        ({"No_Factura": UUID, "Concepto": "Servicios de logística"}, "OLI"),
        # GT1 / GT2 / GT3 de la muestra
        ({"No_Factura": UUID, "Concepto": "REVISIÓN FITOSANITARIA DE PAPA CONSUMO HUMANO"}, "GT"),
        ({"No_Factura": UUID, "ISR": 0, "Concepto": "DERECHO DE ACCESO POR VEHICULO"}, "GT"),
        # retención + logística: gana la retención, va primero
        ({"No_Factura": UUID, "ISR": 10, "Concepto": "SERVICIOS POR LOGISTICA"}, "II"),
    ],
)
def test_xml_sin_sigla_en_el_nombre_decide_el_contenido(entities, sigla):
    out, _, derived = derivar(XML, "factura.xml", entities, {"No_Factura": 1.0, "ISR": 1.0, "Concepto": 1.0})
    assert out["SIGLAS_DOCUMENTO"] == sigla
    assert out["TIPO_DOCUMENTO"] == TABLA[sigla][1]
    # lo decidido por contenido lo dice: no se confunde con lo que dijo el nombre
    assert derived.labels["SIGLAS_DOCUMENTO"].startswith("Decidido por contenido")


def test_xml_el_nombre_manda_sobre_el_contenido():
    out, _, derived = derivar(XML, "2607-0823-6001803_GT1.xml", {"No_Factura": UUID, "ISR": 33.5})
    assert out["SIGLAS_DOCUMENTO"] == "GT"
    assert "SIGLAS_DOCUMENTO" not in derived.labels


def test_xml_sin_timbre_no_se_supone_gasto_de_tercero():
    out, _, _ = derivar(XML, "factura.xml", {"Concepto": "ALGO"})
    assert out["SIGLAS_DOCUMENTO"] is None


def test_xml_no_le_pide_nada_a_nadie():
    fields = cargar(XML).field_tree
    assert fields["SIGLAS_DOCUMENTO"].computed and fields["TIPO_DOCUMENTO"].computed


def test_los_gemelos_declaran_la_misma_tabla():
    """Un solo mapeo del ERP sirve a las dos fuentes: mismos patrones de nombre,
    mismas siglas válidas y mismos nombres de tipo."""
    pdf, xml = cargar(PDF).field_tree, cargar(XML).field_tree

    def por_nombre(spec):
        return [c for c in spec.params["derived"]["cases"] if "from" not in c]

    assert por_nombre(pdf["SIGLAS_DOCUMENTO"]) == por_nombre(xml["SIGLAS_DOCUMENTO"])
    assert pdf["TIPO_DOCUMENTO"].params["derived"] == xml["TIPO_DOCUMENTO"].params["derived"]
    patron = lambda spec: [r.pattern for r in spec.validation]  # noqa: E731
    assert patron(pdf["SIGLAS_DOCUMENTO"]) == patron(xml["SIGLAS_DOCUMENTO"])
