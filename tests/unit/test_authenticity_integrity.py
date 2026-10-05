"""La etapa de autenticidad como la ve un tipo que solo quiere INTEGRIDAD, y
los mecanismos que mantienen barata la etapa para todos:

- un tipo sin bloque `authenticity`, con `$presets: ["INTEGRIDAD_PDF"]`, corre
  SOLO la capa de integridad: nunca rasteriza, nunca llama a un modelo, y las
  demás capas quedan NO_SOLICITADA sin afectar `evaluated`;
- CARTA_NOM35 sigue exactamente igual que antes (+ `integrity: NO_SOLICITADA`);
- `"integrity": false` explícito la deja DESACTIVADA;
- `vision.trigger: "on_suspicion"` no paga el modelo en un archivo limpio y sí
  cuando una capa gratis encontró algo;
- `vision.page_review` es una llamada más, facturada en la misma línea;
- `expected_capture` distingue "debe ser papel" de "debe ser digital";
- la etapa lee los bytes ORIGINALES aunque el pipeline haya truncado el PDF;
- si la capa de integridad truena, el documento va a revisión, no se aprueba.
"""

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pypdfium2")
pytest.importorskip("PIL")

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import document_catalog as dc  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline import authenticity, page_raster, pdf_integrity, vision_inspector  # noqa: E402
from pipeline import expediente  # noqa: E402
from validators.engine import DOC_APPROVED, DOC_REVIEW  # noqa: E402

from unit.test_authenticity_stage import (  # noqa: E402
    EXTRAIDO,
    TIPO,
    escaneo,
    estatus,
    vision_simulada,
    word_con_firma_pegada,
)
from unit.test_authenticity_stage import entry as entry_carta  # noqa: E402,F401
from unit.test_pdf_forensics import ALTO, ANCHO, TEXTO_CARTA, pdf, texto  # noqa: E402
from unit.test_pdf_integrity import incremental, pdf_bytes  # noqa: E402

COMPARTIDO = json.loads((REPO_ROOT / "catalogs/_shared.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.delenv("FINGERPRINT_TABLE", raising=False)
    monkeypatch.delenv("AUTHENTICITY_ENABLED", raising=False)
    dc._FIELD_PRESETS.clear()
    dc._FIELD_PRESETS.update({k: v for k, v in COMPARTIDO["_field_presets"].items() if not k.startswith("__")})
    yield
    dc._FIELD_PRESETS.clear()


def tipo(fields, bloque=None, type_id="FACTURA_PRUEBA"):
    data = {"description": "x", "fields": fields}
    if bloque is not None:
        data["authenticity"] = bloque
    return _parse_entry(type_id, data)


def correr(entry, raw: bytes, reader=None, es_imagen=False, raw_bytes=None):
    from pypdf import PdfReader

    if reader is None and not es_imagen:
        reader = PdfReader(io.BytesIO(raw))
    paginas = 1 if es_imagen else len(reader.pages)
    doc = {"documentType": entry.type_id, "pageStart": 1, "pageEnd": paginas,
           "_entities": dict(EXTRAIDO), "_confidences": {}}
    fuente = authenticity.source_for(raw, reader, es_imagen, raw_bytes=raw_bytes)
    authenticity.run(doc, entry, fuente, {"documentId": "doc-1"})
    return doc


def sin_raster(monkeypatch):
    monkeypatch.setattr(page_raster, "render_page_rgb", lambda *a, **k: pytest.fail("rasterizó una página"))
    monkeypatch.setattr(page_raster, "decode_image", lambda *a, **k: pytest.fail("decodificó la imagen"))
    monkeypatch.setattr(vision_inspector, "locate", lambda *a, **k: pytest.fail("llamó al modelo"))
    monkeypatch.setattr(vision_inspector, "review_page", lambda *a, **k: pytest.fail("llamó al modelo"))


# ---------------------------------------------------------------- solo integridad


def test_un_tipo_con_el_preset_de_integridad_no_rasteriza_ni_llama_al_modelo(monkeypatch):
    sin_raster(monkeypatch)
    entry = tipo({"$presets": ["INTEGRIDAD_PDF"]})

    doc = correr(entry, pdf_bytes(texto("Factura " + TEXTO_CARTA), metadata={"/Producer": "SAT PAC 1.0"}))

    capas = doc["authenticity"]["layers"]
    assert capas["integrity"] == authenticity.LAYER_OK
    assert {capas[n] for n in ("structure", "pixels", "vision", "memory")} == {authenticity.LAYER_NOT_REQUESTED}
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is True
    assert doc["_entities"]["ARCHIVO_SIN_REGUARDADO"] is True
    assert doc["_entities"]["SIN_ANOTACIONES"] is True
    assert doc["_entities"]["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_AUTHENTIC
    assert "authenticityMeta" not in doc            # nada facturado
    assert estatus(entry, doc)[0] == DOC_APPROVED
    assert doc["_recovered_sources"]["ARCHIVO_SIN_REGUARDADO"] == authenticity.SOURCE_LABEL


def test_un_archivo_reguardado_va_a_revision_con_el_preset(monkeypatch):
    sin_raster(monkeypatch)
    entry = tipo({"$presets": ["INTEGRIDAD_PDF"]})

    doc = correr(entry, incremental(pdf_bytes(texto(TEXTO_CARTA))))

    assert doc["_entities"]["ARCHIVO_SIN_REGUARDADO"] is False
    assert doc["_entities"]["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_SUSPICIOUS
    assert "ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES" in doc["_entities"]["MOTIVOS_AUTENTICIDAD"]
    status, checks = estatus(entry, doc)
    assert status == DOC_REVIEW
    assert any(getattr(c, "field", None) == "ARCHIVO_SIN_REGUARDADO" and getattr(c, "severity", None) == "warning"
               for c in checks)


def test_el_tipo_puede_endurecer_una_senal_a_rechazo(monkeypatch):
    sin_raster(monkeypatch)
    from validators.engine import DOC_REJECTED

    entry = tipo({
        "$presets": ["INTEGRIDAD_PDF"],
        "ARCHIVO_SIN_REGUARDADO": {
            "type": "boolean", "required": False, "computed": True,
            "authenticity_source": "integrity.single_generation", "hint": "h",
            "validation": [{"rule_type": "boolean_true", "allow_empty": True, "on_fail": "reject", "message": "no"}],
        },
    })

    doc = correr(entry, incremental(pdf_bytes(texto(TEXTO_CARTA))))

    assert estatus(entry, doc)[0] == DOC_REJECTED


def test_lo_que_no_aplica_queda_vacio_y_no_cuenta_como_no(monkeypatch):
    sin_raster(monkeypatch)
    entry = tipo({"$presets": ["INTEGRIDAD_PDF"]})

    doc = correr(entry, pdf_bytes(texto(TEXTO_CARTA)))    # sin firma digital, sin escaneo, sin fechas

    assert "FIRMA_DIGITAL_INTACTA" not in doc["_entities"]
    assert "SIN_TEXTO_SOBRE_ESCANEO" not in doc["_entities"]
    assert "FECHAS_METADATOS_CONSISTENTES" not in doc["_entities"]
    assert estatus(entry, doc)[0] == DOC_APPROVED


def test_integridad_apagada_explicitamente_queda_desactivada(monkeypatch):
    sin_raster(monkeypatch)
    entry = tipo({"$presets": ["INTEGRIDAD_PDF"]}, {"integrity": False})

    doc = correr(entry, incremental(pdf_bytes()))

    assert doc["authenticity"]["layers"]["integrity"] == authenticity.LAYER_OFF
    assert "ARCHIVO_SIN_REGUARDADO" not in doc["_entities"]
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is True


def test_si_la_capa_de_integridad_truena_el_documento_va_a_revision(monkeypatch):
    sin_raster(monkeypatch)
    monkeypatch.setattr(pdf_integrity, "file_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    entry = tipo({"$presets": ["INTEGRIDAD_PDF"]})

    doc = correr(entry, pdf_bytes())

    assert doc["authenticity"]["layers"]["integrity"] == authenticity.LAYER_ERROR
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is False
    assert doc["_entities"]["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_NOT_EVALUATED
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_el_kill_switch_manda_a_revision_sin_correr_nada(monkeypatch):
    sin_raster(monkeypatch)
    monkeypatch.setenv("AUTHENTICITY_ENABLED", "false")
    entry = tipo({"$presets": ["INTEGRIDAD_PDF"]})

    doc = correr(entry, pdf_bytes())

    assert doc["authenticity"]["layers"]["integrity"] == authenticity.LAYER_OFF
    assert doc["authenticity"]["layers"]["vision"] == authenticity.LAYER_NOT_REQUESTED
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_una_imagen_suelta_con_el_preset_de_imagen(monkeypatch):
    from PIL import Image

    monkeypatch.setattr(vision_inspector, "locate", lambda *a, **k: pytest.fail("llamó al modelo"))
    exif = Image.Exif()
    exif[0x0131] = "Adobe Photoshop 25.0"
    buf = io.BytesIO()
    Image.fromarray(np.full((400, 600, 3), 230, np.uint8)).save(buf, "JPEG", exif=exif.tobytes())
    entry = tipo({"$presets": ["INTEGRIDAD_IMAGEN"]}, type_id="FOTO_PRUEBA")

    doc = correr(entry, buf.getvalue(), es_imagen=True)

    capas = doc["authenticity"]["layers"]
    assert capas["integrity"] == authenticity.LAYER_FAIL
    assert capas["vision"] == authenticity.LAYER_NOT_REQUESTED
    assert doc["_entities"]["SIN_EDITOR_EN_METADATOS"] is False
    assert doc["_entities"]["EDITOR_DETECTADO"] == "Adobe Photoshop 25.0"
    assert doc["_entities"]["METADATOS_DE_CAMARA"] is False
    assert estatus(entry, doc)[0] == DOC_REVIEW


# ---------------------------------------------------------------- CARTA_NOM35 no cambia


def test_carta_nom35_corre_igual_que_antes_mas_integridad_no_solicitada(entry_carta, monkeypatch):
    llamadas = vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = {"documentType": TIPO, "pageStart": 1, "pageEnd": 1, "_entities": dict(EXTRAIDO), "_confidences": {}}
    authenticity.run(doc, entry_carta, authenticity.PageSource(reader=reader, file_sha256="x"),
                     {"documentId": "doc-1", "metadata": {"proveedor": "10021"}})

    capas = doc["authenticity"]["layers"]
    assert capas["integrity"] == authenticity.LAYER_NOT_REQUESTED
    assert capas["structure"] == authenticity.LAYER_OK and capas["vision"] == authenticity.LAYER_OK
    assert llamadas["locate"] == 1
    assert doc["_entities"]["CAPTURA_FISICA"] is True
    assert authenticity.spec_for(entry_carta)["expected_capture"] == "FISICO"
    assert authenticity.spec_for(entry_carta)["layers"] == {"structure", "pixels", "vision", "memory"}


# ---------------------------------------------------------------- palancas de visión


def _tipo_con_elementos(trigger, page_review=False, presets=("CAPTURA_FISICA",)):
    campos = {"$presets": list(presets),
              "FIRMA_PRESENTE": {"type": "boolean", "required": False, "computed": True,
                                 "authenticity_source": "element.FIRMA.present", "hint": "h",
                                 "validation": [{"rule_type": "boolean_true", "allow_empty": True,
                                                 "on_fail": "review", "message": "sin firma"}]}}
    if page_review:
        campos["SIN_INDICIOS_DE_EDICION"] = {"type": "boolean", "required": False, "computed": True,
                                            "authenticity_source": "vision.no_edit_signs", "hint": "h",
                                            "validation": [{"rule_type": "boolean_true", "allow_empty": True,
                                                            "on_fail": "review", "message": "editada"}]}
    bloque = {"vision": {"trigger": trigger, "page_review": page_review, "fallback_models": []},
              "elements": {"FIRMA": {"description": "firma"}}, "memory": {"enabled": False}}
    return tipo(campos, bloque)


def test_on_suspicion_no_paga_el_modelo_en_un_escaneo_limpio(monkeypatch):
    llamadas = vision_simulada(monkeypatch, presentes=("FIRMA",))
    reader, _ = escaneo()
    entry = _tipo_con_elementos("on_suspicion")

    doc = correr(entry, b"", reader=reader)

    assert llamadas["locate"] == 0
    assert doc["authenticity"]["layers"]["vision"] == authenticity.LAYER_NA
    assert doc["authenticity"]["visionSkipped"] == "SIN_SOSPECHA"
    assert doc["_entities"]["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_AUTHENTIC
    assert "FIRMA_PRESENTE" not in doc["_entities"]
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is True
    assert estatus(entry, doc)[0] == DOC_APPROVED


def test_on_suspicion_si_paga_el_modelo_cuando_una_capa_gratis_encontro_algo(monkeypatch):
    llamadas = vision_simulada(monkeypatch, presentes=("FIRMA",))
    entry = _tipo_con_elementos("on_suspicion")

    doc = correr(entry, b"", reader=word_con_firma_pegada())

    assert llamadas["locate"] == 1
    assert doc["authenticity"]["layers"]["vision"] == authenticity.LAYER_OK
    assert "visionSkipped" not in doc["authenticity"]


def test_always_es_el_default_y_no_cambia_nada(monkeypatch):
    llamadas = vision_simulada(monkeypatch, presentes=("FIRMA",))
    reader, _ = escaneo()

    correr(_tipo_con_elementos("always"), b"", reader=reader)

    assert llamadas["locate"] == 1


def test_page_review_es_una_llamada_mas_facturada_en_la_misma_linea(monkeypatch):
    llamadas = vision_simulada(monkeypatch, presentes=("FIRMA",))
    revisiones = []

    def review_page(page_jpeg, type_entry, vision_spec=None, monochrome=False):
        revisiones.append(len(page_jpeg))
        return ({"editSigns": ["PARCHE_DE_FONDO"], "certainty": 0.8, "description": "un parche"},
                {"modelId": "haiku", "usage": {"inputTokens": 1500, "outputTokens": 60}})

    monkeypatch.setattr(vision_inspector, "review_page", review_page)
    reader, _ = escaneo()
    entry = _tipo_con_elementos("always", page_review=True)

    doc = correr(entry, b"", reader=reader)

    assert llamadas["locate"] == 1 and llamadas["inspect"] == 1 and len(revisiones) == 1
    assert doc["authenticityMeta"]["calls"] == 3            # locate + inspect + page review, una sola línea
    assert doc["_entities"]["SIN_INDICIOS_DE_EDICION"] is False
    assert "INDICIOS_DE_EDICION_EN_PAGINA" in doc["_entities"]["MOTIVOS_AUTENTICIDAD"]
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_page_review_sin_elementos_solo_hace_esa_llamada(monkeypatch):
    monkeypatch.setattr(vision_inspector, "locate", lambda *a, **k: pytest.fail("no había elementos que ubicar"))
    monkeypatch.setattr(vision_inspector, "review_page",
                        lambda *a, **k: ({"editSigns": [], "certainty": 0.9, "description": ""},
                                         {"modelId": "haiku", "usage": {"inputTokens": 1500, "outputTokens": 40}}))
    entry = tipo({"SIN_INDICIOS_DE_EDICION": {"type": "boolean", "required": False, "computed": True,
                                              "authenticity_source": "vision.no_edit_signs", "hint": "h",
                                              "validation": []}},
                 {"vision": {"page_review": True, "fallback_models": []}})
    reader, _ = escaneo()

    doc = correr(entry, b"", reader=reader)

    assert doc["authenticity"]["layers"]["vision"] == authenticity.LAYER_OK
    assert doc["_entities"]["SIN_INDICIOS_DE_EDICION"] is True
    assert doc["authenticityMeta"]["calls"] == 1


# ---------------------------------------------------------------- expected_capture


def test_un_tipo_digital_de_origen_no_es_sospechoso_por_ser_digital(monkeypatch):
    sin_raster(monkeypatch)
    entry = tipo({"CAPTURA_COMO_SE_ESPERA": {"type": "boolean", "required": False, "computed": True,
                                            "authenticity_source": "structure.capture_as_expected", "hint": "h",
                                            "validation": [{"rule_type": "boolean_true", "allow_empty": True,
                                                            "on_fail": "review", "message": "escaneado"}]},
                  "ORIGEN": {"type": "string", "required": False, "computed": True,
                             "authenticity_source": "structure.origin", "hint": "h", "validation": []}},
                 {"expected_capture": "DIGITAL"})

    digital = correr(entry, pdf_bytes(texto(TEXTO_CARTA)))
    assert digital["_entities"]["CAPTURA_COMO_SE_ESPERA"] is True
    assert digital["authenticity"]["verdict"] == authenticity.VERDICT_AUTHENTIC
    assert not [r for r in digital["authenticity"]["reasons"] if r["hard"]]

    reader, _ = escaneo()
    escaneado = correr(entry, b"", reader=reader)
    assert escaneado["_entities"]["CAPTURA_COMO_SE_ESPERA"] is False
    assert "CAPTURA_DISTINTA_DE_LA_ESPERADA" in {r["code"] for r in escaneado["authenticity"]["reasons"]}
    assert estatus(entry, escaneado)[0] == DOC_REVIEW


def test_con_any_la_senal_queda_vacia_y_la_estructura_es_solo_evidencia(monkeypatch):
    sin_raster(monkeypatch)
    entry = tipo({"CAPTURA_COMO_SE_ESPERA": {"type": "boolean", "required": False, "computed": True,
                                            "authenticity_source": "structure.capture_as_expected", "hint": "h",
                                            "validation": []}})

    doc = correr(entry, pdf_bytes(texto(TEXTO_CARTA)))

    assert "CAPTURA_COMO_SE_ESPERA" not in doc["_entities"]
    assert doc["authenticity"]["layers"]["structure"] == authenticity.LAYER_OK
    assert doc["authenticity"]["verdict"] == authenticity.VERDICT_AUTHENTIC


# ---------------------------------------------------------------- bytes originales


def test_la_etapa_lee_el_original_aunque_el_pipeline_haya_truncado(monkeypatch):
    sin_raster(monkeypatch)
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(ANCHO, ALTO)
    buf = io.BytesIO()
    writer.write(buf)
    original = incremental(buf.getvalue())              # 3 páginas, 2 generaciones
    truncado, total = expediente._truncate_pdf(original, 1)
    assert total == 3

    fuente = authenticity.source_for(truncado, PdfReader(io.BytesIO(truncado)), False, raw_bytes=original)
    assert len(fuente.reader.pages) == 1 and len(fuente.raw_reader.pages) == 3
    assert fuente.file_sha256 == hashlib.sha256(original).hexdigest()

    entry = tipo({"$presets": ["INTEGRIDAD_PDF"]})
    doc = correr(entry, truncado, reader=fuente.reader, raw_bytes=original)

    assert doc["_entities"]["ARCHIVO_SIN_REGUARDADO"] is False
    assert doc["authenticity"]["integrity"]["file"]["chain"]["generations"] == 2


def test_sin_truncado_se_reutiliza_el_mismo_reader():
    from pypdf import PdfReader

    raw = pdf_bytes()
    reader = PdfReader(io.BytesIO(raw))

    fuente = authenticity.source_for(raw, reader, False, raw_bytes=raw)

    assert fuente.raw_reader is reader


def test_el_reporte_de_archivo_se_calcula_una_vez_por_carga(monkeypatch):
    llamadas = []
    original = pdf_integrity.file_report
    monkeypatch.setattr(pdf_integrity, "file_report", lambda *a, **k: llamadas.append(1) or original(*a, **k))
    from pypdf import PdfReader

    raw = pdf_bytes()
    fuente = authenticity.source_for(raw, PdfReader(io.BytesIO(raw)), False)
    entry = tipo({"$presets": ["INTEGRIDAD_PDF"]})
    for i in range(3):   # tres documentos del mismo expediente
        doc = {"documentType": entry.type_id, "pageStart": 1, "pageEnd": 1, "_entities": {}, "_confidences": {}}
        authenticity.run(doc, entry, fuente, {})
        assert doc["_entities"]["ARCHIVO_SIN_REGUARDADO"] is True

    assert len(llamadas) == 1
