"""La etapa de autenticidad física de punta a punta, contra el catálogo REAL
de `CARTA_NOM35` y el motor de reglas real — con el modelo de visión y DynamoDB
simulados, así que corre sin AWS y sin costo.

Lo que importa aquí no es cada capa (tienen sus propias pruebas) sino el
CONTRATO entre la etapa y el catálogo:

- una carta hecha en Word con la firma pegada queda marcada, y en modo
  observación va a REVISIÓN, no a rechazo;
- la MISMA firma en la carta de otro empleado RECHAZA desde el día uno;
- un proveedor sin referencias registradas NO se rechaza por eso;
- si la etapa no pudo correr, el documento va a revisión: nunca aprueba callado;
- el código no conoce ningún TYPE_ID: cualquier tipo que declare el bloque entra.
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

from catalog_paths import active_catalog_path  # noqa: E402
import document_catalog as dc  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline import authenticity, fingerprint_store, vision_inspector  # noqa: E402
from validators.engine import (  # noqa: E402
    DOC_APPROVED,
    DOC_REJECTED,
    DOC_REVIEW,
    document_status_from_checks,
    resolve_field_tree,
)

from unit.test_fingerprint_store import TablaFalsa  # noqa: E402
from unit.test_pdf_forensics import ALTO, ANCHO, TEXTO_CARTA, pdf, pintar, texto  # noqa: E402

TIPO = "CARTA_NOM35"
CAJA_FIRMA = [0.35, 0.55, 0.62, 0.72]
CAJA_SELLO = [0.66, 0.56, 0.9, 0.7]
CAJA_LOGO = [0.05, 0.03, 0.3, 0.1]

# Dónde están DE VERDAD los elementos en las hojas sintéticas de este archivo.
DONDE_ESTA = {"FIRMA": [0.33, 0.52, 0.64, 0.74], "SELLO": [0.66, 0.55, 0.92, 0.72], "LOGO": [0.0, 0.0, 0.4, 0.15]}

EXTRAIDO = {
    "DESTINATARIO": "Operadora Futurama S.A. de C.V.",
    "NOMBRE_EMPLEADO": "Ana Pérez López",
    "EMPRESA_EMISORA": "BIMBO, S.A. DE C.V.",
    "FECHA_CARTA": "2026-09-01",
    "NOMBRE_FIRMANTE": "Pablo Ontiveros",
    "REFERENCIA_NOM035": "NOM-035",
}


COMPARTIDO = json.loads((REPO_ROOT / "catalogs/_shared.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def compartido_del_repo():
    """`CARTA_NOM35` pide sus elementos y sus campos de referencia por nombre
    (`_authenticity_elements`, `$presets`): se cargan del repo, como en el bundle."""
    dc._FIELD_PRESETS.clear()
    dc._AUTHENTICITY_ELEMENTS.clear()
    dc.load_shared({k: COMPARTIDO[k] for k in ("_field_presets", "_authenticity_elements")})
    yield
    dc._FIELD_PRESETS.clear()
    dc._AUTHENTICITY_ELEMENTS.clear()


@pytest.fixture
def entry():
    crudo = json.loads(active_catalog_path(REPO_ROOT, TIPO).read_text(encoding="utf-8"))[TIPO]
    return _parse_entry(TIPO, crudo)


def _jpeg_de_hoja(semilla=1):
    """Una 'hoja escaneada' con ruido de papel, renglones, firma y sello."""
    from PIL import Image

    rng = np.random.default_rng(semilla)
    alto, ancho = 1100, 850
    gris = np.clip(244 + rng.normal(0, 2.5, (alto, ancho)), 0, 255)
    hoja = np.repeat(gris[:, :, None], 3, axis=2)
    for y in range(200, 560, 26):
        hoja[y:y + 8, 90:760] = 45
    y = 700.0
    for x in range(310, 520):  # firma: trazo negro distinto por semilla
        y = float(np.clip(y + rng.normal(0, 5), 620, 780))
        hoja[int(y) - 2:int(y) + 2, x - 1:x + 2] = 30
    hoja[640:750, 580:750] = np.where(rng.random((110, 170, 1)) > 0.45, (40, 60, 190), hoja[640:750, 580:750])
    buf = io.BytesIO()
    Image.fromarray(hoja.astype(np.uint8)).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def escaneo(semilla=1):
    """PDF de escaneo genuino: UNA imagen JPEG a página completa, cero texto."""
    from pypdf.generic import NameObject

    datos = _jpeg_de_hoja(semilla)
    reader = pdf([{"contenido": pintar("Pag", 0, 0, ANCHO, ALTO),
                   "imagenes": {"Pag": {"datos": datos, "filtro": NameObject("/DCTDecode"),
                                        "ancho": 850, "alto": 1100, "color": "/DeviceRGB"}}}],
                 metadata={"/Producer": "Lexmark MX622adhe", "/Creator": "HardCopy"})
    return reader, datos


def word_con_firma_pegada(nombre_empleado="Ana"):
    """PDF de Word: texto nativo + el MISMO bloque firma/sello incrustado."""
    cuerpo = texto(f"Hacemos constar que {nombre_empleado} es empleada. " + TEXTO_CARTA)
    return pdf(
        [{"contenido": cuerpo + pintar("Firma", 214, 222, 165, 135),
          "imagenes": {"Firma": {"datos": b"bloque-firma-y-sello" * 40}}}],
        metadata={"/Producer": "Microsoft® Word para Microsoft 365"},
    )


def vision_simulada(monkeypatch, presentes=("FIRMA", "SELLO", "LOGO"), indicios=None, leyenda="BIMBO, S.A. DE C.V AGENCIA SALTILLO",
                    referencia=None, falla=False, cajas=None, incrustadas=None):
    """`incrustadas`: lo que el modelo "dice" que es cada imagen incrustada, por
    índice; por defecto la primera (y única del PDF de prueba) es la firma."""
    llamadas = {"locate": 0, "inspect": 0, "referencias": None, "incrustadas": 0, "rects": [], "objetos": None}
    cajas = cajas or {"FIRMA": CAJA_FIRMA, "SELLO": CAJA_SELLO, "LOGO": CAJA_LOGO}

    # El modelo real recibe RECORTES y dice qué es cada uno. El simulado hace lo
    # mismo "mirando" dónde cae cada recorte en la hoja sintética: se espía la
    # geometría que calcula la etapa (nunca la calcula el modelo).
    original = authenticity._candidates

    def espiar(spec, page_report, raster, digital):
        candidatos = original(spec, page_report, raster, digital)
        llamadas["rects"] = [c["rect"] for c in candidatos]
        return candidatos

    monkeypatch.setattr(authenticity, "_candidates", espiar)

    def que_es(rect):
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        return [k for k, v in DONDE_ESTA.items()
                if k in presentes and v[0] <= cx <= v[2] and v[1] <= cy <= v[3]]

    def locate(page_jpeg, elements, type_entry, vision_spec=None, monochrome=False, embedded=None,
               embedded_are_objects=True):
        llamadas["locate"] += 1
        llamadas["incrustadas"] = len(embedded or [])
        llamadas["objetos"] = embedded_are_objects
        if falla:
            raise RuntimeError("Bedrock no respondió")
        clasificadas = (incrustadas if incrustadas is not None
                        else {i: que_es(rect) for i, rect in enumerate(llamadas["rects"])})
        return ({"captureKind": "ESCANEO", "elements": {
            k: {"present": k in presentes, "bbox": cajas[k] if k in presentes else None, "certainty": 0.9}
            for k in elements},
            "embedded": {i: kinds for i, kinds in clasificadas.items() if i < len(embedded or [])}},
            {"modelId": "haiku", "usage": {"inputTokens": 1600, "outputTokens": 120}})

    def inspect(crops, references, elements, type_entry, vision_spec=None, monochrome=False):
        llamadas["inspect"] += 1
        llamadas["referencias"] = {k: [r["refId"] for r in v] for k, v in references.items()}
        return ({k: {"pastedSigns": list((indicios or {}).get(k, [])), "inkColour": "AZUL",
                     "legendText": leyenda if k == "SELLO" else "",
                     "referencesCompared": len(references.get(k) or []),
                     "matchedReference": (referencia or {}).get(k), "certainty": 0.9} for k in crops},
                {"modelId": "haiku", "usage": {"inputTokens": 2400, "outputTokens": 200}})

    monkeypatch.setattr(vision_inspector, "locate", locate)
    monkeypatch.setattr(vision_inspector, "inspect", inspect)
    return llamadas


def correr(entry, reader, archivo=b"archivo-1", extraido=None, contexto=None):
    doc = {"documentType": TIPO, "pageStart": 1, "pageEnd": len(reader.pages),
           "_entities": dict(extraido or EXTRAIDO), "_confidences": {}}
    fuente = authenticity.PageSource(reader=reader, file_sha256=hashlib.sha256(archivo).hexdigest())
    authenticity.run(doc, entry, fuente, contexto or {"documentId": "doc-1", "metadata": {"proveedor": "10021"}})
    return doc


def estatus(entry, doc):
    calculados = {n for n, s in entry.field_tree.items() if s.computed}
    _, checks, _ = resolve_field_tree(
        doc["_entities"], doc["_confidences"], entry, computed_field_names=calculados,
        source_overrides=doc.get("_recovered_sources"), default_confidence=0.95,
    )
    return document_status_from_checks(checks), checks


@pytest.fixture(autouse=True)
def sin_aws(monkeypatch):
    monkeypatch.delenv("FINGERPRINT_TABLE", raising=False)
    monkeypatch.delenv("AUTHENTICITY_ENABLED", raising=False)


def con_tabla(monkeypatch, tabla):
    monkeypatch.setenv("FINGERPRINT_TABLE", "tabla-de-prueba")
    monkeypatch.setattr(fingerprint_store, "_table", lambda: tabla)


# ---------------------------------------------------------------- el contrato


def test_un_escaneo_genuino_con_todo_en_su_lugar_se_aprueba(entry, monkeypatch):
    vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry, reader)
    estado, checks = estatus(entry, doc)

    campos = doc["_entities"]
    assert campos["CAPTURA_FISICA"] is True and campos["ORIGEN_DOCUMENTO"] == "ESCANER"
    assert campos["FIRMA_PRESENTE"] and campos["SELLO_PRESENTE"] and campos["LOGO_PRESENTE"]
    assert campos["FIRMA_SIN_INSERCION"] is True and campos["SELLO_CORRESPONDE_EMISOR"] is True
    assert campos["AUTENTICIDAD_EVALUADA"] is True
    assert campos["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_AUTHENTIC
    assert estado == DOC_APPROVED, [c.message for c in checks if not c.valid]


def test_sin_referencias_registradas_no_se_rechaza_y_los_campos_quedan_vacios(entry, monkeypatch):
    """Decisión del usuario: proveedor sin alta previa => basta presencia y no-inserción."""
    con_tabla(monkeypatch, TablaFalsa())
    vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry, reader)

    for campo in ("FIRMA_COINCIDE_REFERENCIA", "SELLO_COINCIDE_REFERENCIA", "LOGO_COINCIDE_REFERENCIA"):
        assert campo not in doc["_entities"]
    assert "SIN_REFERENCIAS_REGISTRADAS" in doc["_entities"]["MOTIVOS_AUTENTICIDAD"]
    assert doc["authenticity"]["references"]["found"] is False
    assert estatus(entry, doc)[0] == DOC_APPROVED


def test_con_referencias_la_firma_que_no_corresponde_va_a_revision(entry, monkeypatch):
    tabla = TablaFalsa()
    pk = fingerprint_store.reference_pk("PROVEEDOR", "10021")
    tabla.put_item({"Pk": pk, "Sk": "FIRMA#pablo", "Label": "Pablo Ontiveros", "S3Key": "references/x.png"})
    tabla.put_item({"Pk": pk, "Sk": "SELLO#saltillo", "Label": "Agencia Saltillo", "S3Key": "references/y.png"})
    con_tabla(monkeypatch, tabla)
    imagen = _jpeg_de_hoja()  # bytes de imagen REALES: una referencia ilegible se descarta
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: imagen)
    llamadas = vision_simulada(monkeypatch, referencia={"SELLO": "saltillo"})
    reader, _ = escaneo()

    doc = correr(entry, reader)
    estado, checks = estatus(entry, doc)

    assert llamadas["referencias"] == {"FIRMA": ["pablo"], "SELLO": ["saltillo"]}
    assert doc["_entities"]["SELLO_COINCIDE_REFERENCIA"] is True
    assert doc["_entities"]["FIRMA_COINCIDE_REFERENCIA"] is False
    assert "LOGO_COINCIDE_REFERENCIA" not in doc["_entities"]  # no hay logos registrados
    assert estado == DOC_REVIEW
    assert any(c.field == "FIRMA_COINCIDE_REFERENCIA" and not c.valid for c in checks)


def test_carta_de_word_con_firma_pegada_en_observacion_va_a_revision(entry, monkeypatch):
    llamadas = vision_simulada(monkeypatch)

    doc = correr(entry, word_con_firma_pegada())
    estado, _ = estatus(entry, doc)

    campos = doc["_entities"]
    assert campos["CAPTURA_FISICA"] is False
    assert campos["FIRMA_SIN_INSERCION"] is False  # hay una imagen incrustada justo bajo la firma
    assert campos["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_REJECTED
    motivos = campos["MOTIVOS_AUTENTICIDAD"].split(",")
    assert "TEXTO_NATIVO_CON_IMAGEN_PEGADA" in motivos and "FIRMA_ES_IMAGEN_INSERTADA" in motivos
    # Ya está probado que es digital: no se paga el examen de recortes.
    assert (llamadas["locate"], llamadas["inspect"], llamadas["incrustadas"]) == (1, 0, 1)
    assert estado == DOC_REVIEW


def test_cambiar_on_fail_a_reject_en_el_catalogo_basta_para_rechazar(entry, monkeypatch):
    """El paso de observación a rechazo es config (`pnpm sync:one`), no código."""
    vision_simulada(monkeypatch)
    regla = entry.field_tree["CAPTURA_FISICA"].validation[0]
    regla.params["on_fail"] = "reject"

    doc = correr(entry, word_con_firma_pegada())

    assert estatus(entry, doc)[0] == DOC_REJECTED


def test_la_misma_firma_en_la_carta_de_otro_empleado_rechaza_desde_el_dia_uno(entry, monkeypatch):
    con_tabla(monkeypatch, TablaFalsa())
    vision_simulada(monkeypatch)

    primera = correr(entry, word_con_firma_pegada("Ana"), archivo=b"carta-ana",
                     contexto={"documentId": "doc-ana", "metadata": {"proveedor": "10037"}})
    segunda = correr(entry, word_con_firma_pegada("Kevin"), archivo=b"carta-kevin",
                     extraido={**EXTRAIDO, "NOMBRE_EMPLEADO": "Kevin Cruz Jáquez"},
                     contexto={"documentId": "doc-kevin", "metadata": {"proveedor": "10037"}})

    assert primera["_entities"]["FIRMA_SELLO_NO_REUTILIZADOS"] is True
    assert segunda["_entities"]["FIRMA_SELLO_NO_REUTILIZADOS"] is False
    assert "FIRMA_REUTILIZADO_EN_OTRA_CARTA" in segunda["_entities"]["MOTIVOS_AUTENTICIDAD"]
    assert segunda["authenticity"]["memory"]["matches"][0]["otherDocumentId"] == "doc-ana"
    assert estatus(entry, segunda)[0] == DOC_REJECTED


def test_la_misma_carta_recibida_dos_veces_es_un_aviso_no_un_rechazo(entry, monkeypatch):
    con_tabla(monkeypatch, TablaFalsa())
    vision_simulada(monkeypatch)
    reader, _ = escaneo(semilla=3)
    otro_escaneo, _ = escaneo(semilla=4)

    correr(entry, reader, archivo=b"escaneo-1", contexto={"documentId": "doc-1", "metadata": {"proveedor": "10021"}})
    repetida = correr(entry, otro_escaneo, archivo=b"escaneo-2",
                      contexto={"documentId": "doc-2", "metadata": {"proveedor": "10021"}})

    assert repetida["_entities"]["CARTA_NO_DUPLICADA"] is False
    assert repetida["_entities"]["FIRMA_SELLO_NO_REUTILIZADOS"] is True
    assert estatus(entry, repetida)[0] == DOC_APPROVED


def test_falta_el_sello_va_a_revision(entry, monkeypatch):
    vision_simulada(monkeypatch, presentes=("FIRMA", "LOGO"))
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert doc["_entities"]["SELLO_PRESENTE"] is False
    assert "SELLO_AUSENTE" in doc["_entities"]["MOTIVOS_AUTENTICIDAD"]
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_indicios_de_insercion_vistos_por_el_modelo_van_a_revision_nunca_a_rechazo(entry, monkeypatch):
    vision_simulada(monkeypatch, indicios={"SELLO": ["HALO_RECTANGULAR"]})
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert doc["_entities"]["SELLO_SIN_INSERCION"] is False
    assert doc["_entities"]["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_SUSPICIOUS
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_sello_de_otra_empresa_va_a_revision(entry, monkeypatch):
    vision_simulada(monkeypatch, leyenda="QUALTIA ALIMENTOS OPERACIONES")
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert doc["_entities"]["SELLO_CORRESPONDE_EMISOR"] is False
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_si_la_vision_falla_el_documento_va_a_revision_nunca_se_aprueba_callado(entry, monkeypatch):
    vision_simulada(monkeypatch, falla=True)
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is False
    assert doc["authenticity"]["layers"]["vision"] == authenticity.LAYER_ERROR
    assert "FIRMA_PRESENTE" not in doc["_entities"]  # vacío = "no se supo", no "no está"
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_el_kill_switch_no_convierte_cartas_sin_revisar_en_aprobadas(entry, monkeypatch):
    monkeypatch.setenv("AUTHENTICITY_ENABLED", "false")
    llamadas = vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert llamadas["locate"] == 0
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is False
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_un_error_interno_cuesta_una_revision_no_el_documento(entry, monkeypatch):
    monkeypatch.setattr(authenticity, "_evaluate", lambda *a, **k: (_ for _ in ()).throw(ValueError("bug")))
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert doc["authenticity"]["error"] == "bug"
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is False


def test_las_llamadas_de_vision_se_facturan_en_su_propia_linea(entry, monkeypatch):
    vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert doc["authenticityMeta"]["modelId"] == "haiku"
    assert doc["authenticityMeta"]["calls"] == 2
    assert doc["authenticityMeta"]["usage"]["inputTokens"] == 4000
    assert doc["authenticityMeta"]["usage"]["outputTokens"] == 320


def test_la_auditoria_es_json_y_la_vista_del_api_es_compacta(entry, monkeypatch):
    vision_simulada(monkeypatch)

    doc = correr(entry, word_con_firma_pegada())
    vista = authenticity.envelope_view(doc["authenticity"])

    json.dumps(doc["authenticity"])  # nada de numpy ni bytes en el resultado completo
    assert set(vista) == {"verdict", "layers", "reasons", "referencesFound"}
    assert len(json.dumps(vista)) < 1500
    assert all(set(r) == {"code", "hard", "message"} for r in vista["reasons"])


def test_una_foto_jpg_no_tiene_estructura_pdf_pero_si_las_demas_capas(entry, monkeypatch):
    vision_simulada(monkeypatch)
    doc = {"documentType": TIPO, "pageStart": 1, "pageEnd": 1, "_entities": dict(EXTRAIDO), "_confidences": {}}
    fuente = authenticity.source_for(_jpeg_de_hoja(), reader=None, is_image=True)

    authenticity.run(doc, entry, fuente, {})

    assert doc["authenticity"]["layers"]["structure"] == authenticity.LAYER_NA
    assert doc["_entities"]["ORIGEN_DOCUMENTO"] == "IMAGEN"
    assert "CAPTURA_FISICA" not in doc["_entities"]
    assert doc["_entities"]["FONDO_NATURAL"] is True and doc["_entities"]["FIRMA_PRESENTE"] is True
    assert estatus(entry, doc)[0] == DOC_APPROVED


def test_cualquier_tipo_que_declare_el_bloque_entra_y_el_que_no_no_paga_nada(entry, monkeypatch):
    llamadas = vision_simulada(monkeypatch)
    otro = _parse_entry("TIPO_INVENTADO", {
        "schema_version": "1.0.0", "display_name": "t", "description": "t",
        "authenticity": {"vision": False, "memory": {"enabled": False}},
        "fields": {"ES_PAPEL": {"type": "boolean", "computed": True, "hint": "h", "validation": [],
                                "authenticity_source": "structure.physical_capture"}},
    })
    sin_bloque = _parse_entry("TIPO_NORMAL", {
        "schema_version": "1.0.0", "display_name": "t", "description": "t",
        "fields": {"X": {"type": "string", "hint": "h", "validation": []}},
    })
    reader, _ = escaneo()

    doc = {"documentType": "TIPO_INVENTADO", "pageStart": 1, "pageEnd": 1}
    authenticity.run(doc, otro, authenticity.PageSource(reader=reader, file_sha256="a" * 64), {})
    intacto = {"documentType": "TIPO_NORMAL"}
    authenticity.run(intacto, sin_bloque, authenticity.PageSource(reader=reader, file_sha256="a" * 64), {})

    assert doc["_entities"] == {"ES_PAPEL": True}
    assert authenticity.applies_to(sin_bloque) is False and intacto == {"documentType": "TIPO_NORMAL"}
    assert llamadas["locate"] == 0


def test_la_leyenda_del_sello_se_compara_en_python_ignorando_el_relleno_legal():
    igual = vision_inspector.issuer_consistent
    assert igual("BIMBO, S.A. DE C.V AGENCIA SALTILLO", "Grupo Bimbo S.A.B. de C.V.") is True
    assert igual("QUALTIA ALIMENTOS", "BIMBO, S.A. DE C.V.") is False
    assert igual("S.A. DE C.V.", "OTRA, S.A. DE C.V.") is None  # solo relleno legal: no hay qué comparar
    assert igual("", "BIMBO") is None


def test_un_catalogo_recargado_de_dynamodb_llega_con_decimal_y_funciona_igual(monkeypatch):
    """DynamoDB devuelve todo número como Decimal; el JSON empaquetado no. La
    etapa debe dar lo mismo con ambos — un umbral Decimal contra una medición
    float es un TypeError que solo se vería en un stack desplegado."""
    from decimal import Decimal

    def a_decimal(valor):
        if isinstance(valor, bool) or valor is None:
            return valor
        if isinstance(valor, (int, float)):
            return Decimal(str(valor))
        if isinstance(valor, dict):
            return {k: a_decimal(v) for k, v in valor.items()}
        if isinstance(valor, list):
            return [a_decimal(v) for v in valor]
        return valor

    crudo = json.loads(active_catalog_path(REPO_ROOT, TIPO).read_text(encoding="utf-8"))[TIPO]
    desde_dynamo = _parse_entry(TIPO, {**crudo, "authenticity": a_decimal(crudo["authenticity"])})
    con_tabla(monkeypatch, TablaFalsa())
    vision_simulada(monkeypatch)

    spec = authenticity.spec_for(desde_dynamo)
    doc = correr(desde_dynamo, word_con_firma_pegada())

    assert isinstance(spec["memory"]["element_similarity_reject"], float)
    assert isinstance(spec["pages"]["max_pages"], int)
    assert spec["elements"]["SELLO"]["similarity_reject"] is None
    assert "error" not in doc["authenticity"]
    assert doc["_entities"]["CAPTURA_FISICA"] is False
    json.dumps(doc["authenticity"])


def word_con_firma_y_sello_pegados():
    """Como la carta real que lo destapó: firma y sello son DOS imágenes
    incrustadas, una junto a la otra, debajo del cuerpo."""
    return pdf(
        [{"contenido": texto(TEXTO_CARTA) + pintar("Firma", 96, 235, 96, 100) + pintar("Sello", 240, 158, 131, 83),
          "imagenes": {"Firma": {"datos": b"firma-pegada" * 40}, "Sello": {"datos": b"sello-pegado" * 40}}}],
        metadata={"/Producer": "Microsoft® Word 2019"},
    )


def test_la_geometria_exacta_del_pdf_manda_sobre_la_caja_que_adivina_el_modelo(entry, monkeypatch):
    """Medido con una carta real: el modelo puso el sello ~10 % de la página
    más arriba de donde está, así que su caja no tocaba la imagen incrustada y
    el sello pegado pasaba sin marcar. Clasificar un recorte sí lo hace bien:
    se le pregunta QUÉ es cada imagen incrustada y la posición la da el PDF."""
    corridas = {"FIRMA": [0.13, 0.40, 0.35, 0.50], "SELLO": [0.35, 0.40, 0.65, 0.52], "LOGO": CAJA_LOGO}
    # Se mandan de mayor a menor cobertura: 0 = sello, 1 = firma.
    llamadas = vision_simulada(monkeypatch, cajas=corridas, incrustadas={0: ["SELLO"], 1: ["FIRMA"]})

    doc = correr(entry, word_con_firma_y_sello_pegados())

    elementos = doc["authenticity"]["elements"]
    assert llamadas["incrustadas"] == 2
    assert doc["_entities"]["FIRMA_SIN_INSERCION"] is False
    assert doc["_entities"]["SELLO_SIN_INSERCION"] is False
    assert elementos["SELLO"]["bboxSource"] == "PDF" and elementos["LOGO"]["bboxSource"] == "MODELO"
    assert elementos["SELLO"]["bbox"] != corridas["SELLO"]
    motivos = doc["_entities"]["MOTIVOS_AUTENTICIDAD"].split(",")
    assert "FIRMA_ES_IMAGEN_INSERTADA" in motivos and "SELLO_ES_IMAGEN_INSERTADA" in motivos


def test_un_solo_bloque_con_firma_y_sello_cuenta_para_los_dos(entry, monkeypatch):
    vision_simulada(monkeypatch, presentes=("LOGO",), incrustadas={0: ["FIRMA", "SELLO"]})

    doc = correr(entry, word_con_firma_pegada())

    assert doc["_entities"]["FIRMA_PRESENTE"] is True and doc["_entities"]["SELLO_PRESENTE"] is True
    assert doc["_entities"]["FIRMA_SIN_INSERCION"] is False and doc["_entities"]["SELLO_SIN_INSERCION"] is False


def test_una_imagen_incrustada_que_no_es_ningun_elemento_no_acusa_a_nadie(entry, monkeypatch):
    lejos = {"FIRMA": [0.1, 0.1, 0.2, 0.15], "SELLO": [0.7, 0.1, 0.9, 0.2], "LOGO": CAJA_LOGO}
    vision_simulada(monkeypatch, cajas=lejos, incrustadas={0: []})

    doc = correr(entry, word_con_firma_pegada())

    assert "FIRMA_ES_IMAGEN_INSERTADA" not in doc["_entities"]["MOTIVOS_AUTENTICIDAD"]
    assert doc["_entities"]["CAPTURA_FISICA"] is False  # sigue siendo digital: eso no depende del modelo


def test_en_un_escaneo_las_regiones_salen_de_la_tinta_y_el_modelo_solo_clasifica(entry, monkeypatch):
    """Medido con cartas reales: al pedirle al modelo DÓNDE está la firma puso
    la caja sobre el párrafo, 20 % de página arriba. Así que en un escaneo las
    regiones las propone el código (tinta que no es texto corrido) y el modelo
    solo dice qué es cada una. Aquí sus cajas vienen mal a propósito."""
    malas = {"FIRMA": [0.08, 0.30, 0.35, 0.38], "SELLO": [0.35, 0.30, 0.65, 0.40], "LOGO": CAJA_LOGO}
    llamadas = vision_simulada(monkeypatch, cajas=malas)
    reader, _ = escaneo()

    doc = correr(entry, reader)

    elementos = doc["authenticity"]["elements"]
    assert llamadas["incrustadas"] >= 2 and llamadas["objetos"] is False
    assert elementos["FIRMA"]["bboxSource"] == "TINTA" and elementos["SELLO"]["bboxSource"] == "TINTA"
    assert 0.5 < elementos["FIRMA"]["bbox"][1] < 0.75       # donde está la tinta, no donde dijo el modelo
    assert "embeddedImages" not in elementos["FIRMA"]        # una región de tinta NO prueba inserción
    assert doc["_entities"]["FIRMA_SIN_INSERCION"] is True
    assert llamadas["inspect"] == 1


def test_si_el_modelo_no_reconoce_ninguna_region_no_se_recorta_desde_su_caja(entry, monkeypatch):
    """Ve el sello en la página pero no lo reconoce en ningún recorte: se anota
    que está presente y NO se examina ni se compara — recortar desde su caja
    fabricaría un "no coincide" a partir de un pedazo de párrafo."""
    llamadas = vision_simulada(monkeypatch, incrustadas={})
    reader, _ = escaneo()

    doc = correr(entry, reader)

    sello = doc["authenticity"]["elements"]["SELLO"]
    assert doc["_entities"]["SELLO_PRESENTE"] is True and sello["bboxSource"] == "MODELO"
    assert "SELLO_SIN_INSERCION" not in doc["_entities"] and llamadas["inspect"] == 0
    assert doc["authenticity"].get("fingerprints") in (None, {})


def test_un_documento_digital_si_se_compara_cuando_el_proveedor_tiene_referencias(entry, monkeypatch):
    """`inspect_when_digital: "with_references"`: sin referencias el examen se
    omite (no agrega nada a lo ya probado); con referencias, "¿esta firma pegada
    es al menos la registrada?" sí es una pregunta — y el recorte es exacto."""
    tabla = TablaFalsa()
    pk = fingerprint_store.reference_pk("PROVEEDOR", "11734")
    tabla.put_item({"Pk": pk, "Sk": "FIRMA#monica", "Label": "Mónica Menera", "S3Key": "references/f.png"})
    con_tabla(monkeypatch, tabla)
    imagen = _jpeg_de_hoja()  # bytes de imagen REALES: una referencia ilegible se descarta
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: imagen)
    llamadas = vision_simulada(monkeypatch, referencia={"FIRMA": "monica"})

    doc = correr(entry, word_con_firma_pegada(), contexto={"documentId": "d", "metadata": {"proveedor": "11734"}})

    assert llamadas["inspect"] == 1 and llamadas["referencias"] == {"FIRMA": ["monica"]}
    assert doc["authenticity"]["references"]["found"] is True
    assert doc["_entities"]["FIRMA_COINCIDE_REFERENCIA"] is True
    assert doc["_entities"]["FIRMA_SIN_INSERCION"] is False  # coincidir no la vuelve genuina
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_una_referencia_mal_etiquetada_no_tumba_la_llamada_de_vision(entry, monkeypatch):
    """Caso real: `firma1.png` que por dentro era JPEG. Bedrock rechaza la
    llamada COMPLETA si el tipo declarado no es el de los bytes, así que toda
    referencia se decodifica y se vuelve a codificar como JPEG antes de mandarse."""
    tabla = TablaFalsa()
    pk = fingerprint_store.reference_pk("PROVEEDOR", "11734")
    tabla.put_item({"Pk": pk, "Sk": "FIRMA#1", "Label": "1", "S3Key": "references/firma1.png", "Format": "png"})
    tabla.put_item({"Pk": pk, "Sk": "FIRMA#rota", "Label": "x", "S3Key": "references/rota.png", "Format": "png"})
    con_tabla(monkeypatch, tabla)
    jpeg_disfrazado = _jpeg_de_hoja()[:]  # bytes JPEG bajo una llave .png
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes",
                        lambda ref, s3=None: jpeg_disfrazado if ref["refId"] == "1" else b"no soy una imagen")
    recibidas = {}

    def inspect(crops, references, elements, type_entry, vision_spec=None, monochrome=False):
        recibidas.update(references)
        return {k: {"pastedSigns": [], "inkColour": "NEGRA", "legendText": "", "referencesCompared": 0,
                    "matchedReference": None, "certainty": 0.9} for k in crops}, {}

    vision_simulada(monkeypatch)
    monkeypatch.setattr(vision_inspector, "inspect", inspect)
    reader, _ = escaneo()

    correr(entry, reader, contexto={"documentId": "d", "metadata": {"proveedor": "11734"}})

    assert [r["refId"] for r in recibidas["FIRMA"]] == ["1"]          # la ilegible se descarta, no truena
    assert recibidas["FIRMA"][0]["format"] == "jpeg"
    assert recibidas["FIRMA"][0]["bytes"][:3] == b"\xff\xd8\xff"     # y son JPEG de verdad


def test_con_varias_imagenes_del_mismo_elemento_gana_la_que_senalo_el_modelo(entry, monkeypatch):
    """Membrete arriba + banda de marcas abajo, ambos "LOGO": el recorte debe
    salir del de arriba, que es donde el modelo dijo que está el logo."""
    doble_logo = pdf(
        [{"contenido": texto(TEXTO_CARTA) + pintar("Arriba", 10, 710, 200, 60) + pintar("Abajo", 5, 5, 520, 80),
          "imagenes": {"Arriba": {"datos": b"logo" * 50}, "Abajo": {"datos": b"banda" * 50}}}],
        metadata={"/Producer": "Microsoft® Word 2019"},
    )
    # Mayor cobertura primero: 0 = banda de abajo, 1 = membrete de arriba.
    vision_simulada(monkeypatch, presentes=("LOGO",), incrustadas={0: ["LOGO"], 1: ["LOGO"]})

    doc = correr(entry, doble_logo)

    caja = doc["authenticity"]["elements"]["LOGO"]["bbox"]
    assert doc["authenticity"]["elements"]["LOGO"]["bboxSource"] == "PDF"
    assert caja[1] < 0.2  # arriba de la página, no la banda del pie


# ---------------------------------------------------------------- referencias: lo que no se pudo comparar


def _referencias(tabla, proveedor, **por_sk):
    pk = fingerprint_store.reference_pk("PROVEEDOR", proveedor)
    for sk, extra in por_sk.items():
        tabla.put_item({"Pk": pk, "Sk": sk.replace("__", "#"), "Label": sk, "S3Key": f"references/{sk}.png", **extra})


def test_si_las_imagenes_de_referencia_no_se_pueden_leer_no_es_un_no_coincide(entry, monkeypatch):
    """El bug: un fallo de S3 dejaba al modelo sin referencias y aun así se
    reportaba `FIRMA_NO_COINCIDE_CON_REFERENCIAS` — una acusación por un error
    nuestro. Ahora el campo queda vacío y el documento va a una persona."""
    tabla = TablaFalsa()
    _referencias(tabla, "10021", FIRMA__pablo={}, SELLO__saltillo={})
    con_tabla(monkeypatch, tabla)
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: None)
    llamadas = vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry, reader)
    estado, checks = estatus(entry, doc)

    assert llamadas["referencias"] == {}                       # el modelo no vio ninguna
    for campo in ("FIRMA_COINCIDE_REFERENCIA", "SELLO_COINCIDE_REFERENCIA"):
        assert campo not in doc["_entities"]
    motivos = doc["_entities"]["MOTIVOS_AUTENTICIDAD"]
    assert "REFERENCIAS_NO_DISPONIBLES" in motivos and "NO_COINCIDE" not in motivos
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is False
    assert doc["_entities"]["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_NOT_EVALUATED
    assert estado == DOC_REVIEW
    assert [c.field for c in checks if not c.valid] == ["AUTENTICIDAD_EVALUADA"]


def test_si_la_tabla_de_referencias_no_responde_el_documento_no_se_aprueba_callado(entry, monkeypatch):
    """Antes: `list_references` devolvía None, no quedaba ningún motivo y la
    carta podía aprobarse sin haberse comparado contra nada."""
    con_tabla(monkeypatch, TablaFalsa())
    monkeypatch.setattr(fingerprint_store, "list_references", lambda scope, key, table=None: None)
    vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert "REFERENCIAS_NO_DISPONIBLES" in doc["_entities"]["MOTIVOS_AUTENTICIDAD"]
    assert doc["authenticity"]["references"]["unavailable"] is True
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is False
    assert estatus(entry, doc)[0] == DOC_REVIEW


def test_sin_tabla_configurada_la_corrida_local_sigue_callada(entry, monkeypatch):
    vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert "REFERENCIAS_NO_DISPONIBLES" not in (doc["_entities"].get("MOTIVOS_AUTENTICIDAD") or "")
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is True


# ---------------------------------------------------------------- referencias: recuperación


class ProveedorFalso:
    name, model_id, dimension = "falso", "modelo-falso", 3


def _con_embeddings(monkeypatch, vector_del_recorte):
    from pipeline import reference_embeddings

    calculados = {"n": 0}

    def embed(imagen, proveedor):
        calculados["n"] += 1
        return (reference_embeddings.normalize(vector_del_recorte),
                {"provider": "falso", "modelId": "modelo-falso", "dimension": 3, "images": 1})

    monkeypatch.setattr(reference_embeddings, "provider_for", lambda config=None: ProveedorFalso())
    monkeypatch.setattr(reference_embeddings, "embed", embed)
    return calculados


def _vector(*valores):
    from pipeline import reference_embeddings

    return reference_embeddings.to_bytes(reference_embeddings.normalize(valores))


def test_con_mas_referencias_de_las_que_caben_se_mandan_las_mas_parecidas_por_embedding(entry, monkeypatch):
    """`max_references_per_kind: 3` y cinco firmantes registrados: la correcta
    es la ÚLTIMA por orden alfabético y aun así llega al modelo."""
    tabla = TablaFalsa()
    modelo = {"EmbeddingModel": "modelo-falso"}
    _referencias(tabla, "10021",
                 FIRMA__a={**modelo, "Embedding": _vector(1, 0, 0)}, FIRMA__b={**modelo, "Embedding": _vector(1, 0.1, 0)},
                 FIRMA__c={**modelo, "Embedding": _vector(1, 0, 0.1)}, FIRMA__d={**modelo, "Embedding": _vector(0, 1, 0)},
                 FIRMA__z_la_buena={**modelo, "Embedding": _vector(0, 0, 1)})
    con_tabla(monkeypatch, tabla)
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: _jpeg_de_hoja())
    calculados = _con_embeddings(monkeypatch, (0, 0.05, 1))
    llamadas = vision_simulada(monkeypatch, referencia={"FIRMA": "z_la_buena"})
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert llamadas["referencias"]["FIRMA"][0] == "z_la_buena" and len(llamadas["referencias"]["FIRMA"]) == 3
    assert doc["_entities"]["FIRMA_COINCIDE_REFERENCIA"] is True
    ranking = doc["authenticity"]["references"]["ranking"]["FIRMA"]
    assert ranking["mode"] == "EMBEDDING" and ranking["chosen"][0]["refId"] == "z_la_buena"
    assert calculados["n"] == 1
    assert doc["embeddingMeta"] == {"modelId": "modelo-falso", "images": 1, "calls": 1}
    json.dumps(doc["authenticity"])  # ni vectores ni bytes en el resultado


def test_con_pocas_referencias_no_se_paga_ningun_embedding(entry, monkeypatch):
    tabla = TablaFalsa()
    _referencias(tabla, "10021", FIRMA__pablo={"EmbeddingModel": "modelo-falso", "Embedding": _vector(0, 0, 1)})
    con_tabla(monkeypatch, tabla)
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: _jpeg_de_hoja())
    calculados = _con_embeddings(monkeypatch, (0, 0, 1))
    vision_simulada(monkeypatch, referencia={"FIRMA": "pablo"})
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert calculados["n"] == 0 and "embeddingMeta" not in doc
    assert doc["authenticity"]["references"]["ranking"]["FIRMA"]["mode"] == "TODAS"


def test_referencias_sin_embedding_del_modelo_actual_se_ordenan_por_huella(entry, monkeypatch):
    """Registradas antes de los embeddings (o con otro modelo): no se comparan
    vectores incomparables; decide la huella de tinta, como siempre."""
    tabla = TablaFalsa()
    _referencias(tabla, "10021", FIRMA__a={}, FIRMA__b={}, FIRMA__c={},
                 FIRMA__d={"EmbeddingModel": "otro-modelo", "Embedding": _vector(0, 0, 1)})
    con_tabla(monkeypatch, tabla)
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: _jpeg_de_hoja())
    calculados = _con_embeddings(monkeypatch, (0, 0, 1))
    vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry, reader)

    assert calculados["n"] == 0
    assert doc["authenticity"]["references"]["ranking"]["FIRMA"]["mode"] in ("HUELLA", "ORDEN")


# ---------------------------------------------------------------- referencias: búsqueda sin proveedor


@pytest.fixture
def entry_con_busqueda():
    crudo = json.loads(active_catalog_path(REPO_ROOT, TIPO).read_text(encoding="utf-8"))[TIPO]
    crudo["authenticity"]["references"]["search"] = {"enabled": True, "min_similarity": 0.8, "top_k": 3}
    return _parse_entry(TIPO, crudo)


class IndiceFalso:
    def __init__(self, hallazgos):
        self.hallazgos, self.consultas = hallazgos, []

    def search(self, vector, *, scope, kind, top_k, model):
        self.consultas.append((scope, kind, top_k, model))
        return self.hallazgos if self.hallazgos is None else self.hallazgos.get(kind, [])


def _con_indice(monkeypatch, hallazgos):
    from pipeline import reference_index

    indice = IndiceFalso(hallazgos)
    monkeypatch.setattr(reference_index, "default_index", lambda proveedor=None: indice)
    return indice


SIN_PROVEEDOR = {"documentId": "doc-1", "metadata": {}}


def test_sin_proveedor_declarado_la_busqueda_sugiere_de_quien_es_la_carta(entry_con_busqueda, monkeypatch):
    tabla = TablaFalsa()
    _referencias(tabla, "10040", FIRMA__julio={}, SELLO__planta={})
    _referencias(tabla, "10021", FIRMA__pablo={})
    con_tabla(monkeypatch, tabla)
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: _jpeg_de_hoja())
    _con_embeddings(monkeypatch, (0, 0, 1))
    indice = _con_indice(monkeypatch, {
        "FIRMA": [{"key": "10040", "refId": "julio", "similarity": 0.93},
                  {"key": "10021", "refId": "pablo", "similarity": 0.85},
                  {"key": "99999", "refId": "lejana", "similarity": 0.40}],     # bajo el umbral
        "SELLO": [{"key": "10040", "refId": "planta", "similarity": 0.9},
                  {"key": "10040", "refId": "ya-borrada", "similarity": 0.9}],  # está en el índice, no en la tabla
    })
    llamadas = vision_simulada(monkeypatch, referencia={"FIRMA": "julio", "SELLO": "planta"})
    reader, _ = escaneo()

    doc = correr(entry_con_busqueda, reader, contexto=SIN_PROVEEDOR)
    estado, _ = estatus(entry_con_busqueda, doc)

    assert llamadas["referencias"] == {"FIRMA": ["julio", "pablo"], "SELLO": ["planta"]}
    assert ("PROVEEDOR", "FIRMA", 3, "modelo-falso") in indice.consultas
    campos = doc["_entities"]
    assert campos["FIRMA_COINCIDE_REFERENCIA"] is True and campos["SELLO_COINCIDE_REFERENCIA"] is True
    assert campos["PROVEEDOR_SUGERIDO"] == "10040"
    assert "PROVEEDOR_SUGERIDO_POR_REFERENCIAS" in campos["MOTIVOS_AUTENTICIDAD"]
    assert "SIN_CLAVE_DE_REFERENCIAS" in campos["MOTIVOS_AUTENTICIDAD"]
    # Una sugerencia no es un hallazgo: el veredicto y el estatus no se mueven.
    assert campos["VEREDICTO_AUTENTICIDAD"] == authenticity.VERDICT_AUTHENTIC and estado == DOC_APPROVED
    assert doc["embeddingMeta"]["images"] == 2      # un embedding por elemento buscado
    json.dumps(doc["authenticity"])


def test_que_la_busqueda_no_confirme_nada_no_es_evidencia_contra_la_carta(entry_con_busqueda, monkeypatch):
    """Sin proveedor declarado, "no se parece a ninguna de las candidatas" no
    dice nada: el campo queda vacío, igual que si no hubiera búsqueda."""
    tabla = TablaFalsa()
    _referencias(tabla, "10040", FIRMA__julio={})
    con_tabla(monkeypatch, tabla)
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: _jpeg_de_hoja())
    _con_embeddings(monkeypatch, (0, 0, 1))
    _con_indice(monkeypatch, {"FIRMA": [{"key": "10040", "refId": "julio", "similarity": 0.9}]})
    vision_simulada(monkeypatch)                    # el modelo no reconoce ninguna
    reader, _ = escaneo()

    doc = correr(entry_con_busqueda, reader, contexto=SIN_PROVEEDOR)

    assert "FIRMA_COINCIDE_REFERENCIA" not in doc["_entities"] and "PROVEEDOR_SUGERIDO" not in doc["_entities"]
    assert "NO_COINCIDE" not in doc["_entities"]["MOTIVOS_AUTENTICIDAD"]
    assert estatus(entry_con_busqueda, doc)[0] == DOC_APPROVED


def test_elementos_de_proveedores_distintos_no_sugieren_a_ninguno(entry_con_busqueda, monkeypatch):
    tabla = TablaFalsa()
    _referencias(tabla, "10040", FIRMA__julio={})
    _referencias(tabla, "10021", SELLO__saltillo={})
    con_tabla(monkeypatch, tabla)
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: _jpeg_de_hoja())
    _con_embeddings(monkeypatch, (0, 0, 1))
    _con_indice(monkeypatch, {"FIRMA": [{"key": "10040", "refId": "julio", "similarity": 0.9}],
                              "SELLO": [{"key": "10021", "refId": "saltillo", "similarity": 0.9}]})
    vision_simulada(monkeypatch, referencia={"FIRMA": "julio", "SELLO": "saltillo"})
    reader, _ = escaneo()

    doc = correr(entry_con_busqueda, reader, contexto=SIN_PROVEEDOR)

    assert "PROVEEDOR_SUGERIDO" not in doc["_entities"]
    assert "REFERENCIAS_DE_VARIOS_PROVEEDORES" in doc["_entities"]["MOTIVOS_AUTENTICIDAD"]


def test_si_el_indice_o_el_embedding_fallan_la_carta_corre_como_sin_busqueda(entry_con_busqueda, monkeypatch):
    con_tabla(monkeypatch, TablaFalsa())
    _con_embeddings(monkeypatch, (0, 0, 1))
    _con_indice(monkeypatch, None)                  # la búsqueda falló
    llamadas = vision_simulada(monkeypatch)
    reader, _ = escaneo()

    doc = correr(entry_con_busqueda, reader, contexto=SIN_PROVEEDOR)

    assert llamadas["referencias"] == {} and doc["authenticity"]["references"]["searchError"] is True
    assert doc["_entities"]["AUTENTICIDAD_EVALUADA"] is True
    assert estatus(entry_con_busqueda, doc)[0] == DOC_APPROVED


def test_con_la_busqueda_apagada_en_el_catalogo_no_se_consulta_nada(entry, monkeypatch):
    """`CARTA_NOM35` la trae apagada hasta que `pnpm refs:eval` diga que separa."""
    con_tabla(monkeypatch, TablaFalsa())
    calculados = _con_embeddings(monkeypatch, (0, 0, 1))
    indice = _con_indice(monkeypatch, {})
    vision_simulada(monkeypatch)
    reader, _ = escaneo()

    correr(entry, reader, contexto=SIN_PROVEEDOR)

    assert calculados["n"] == 0 and indice.consultas == []


# ---------------------------------------------------------------- generalidad: otro tipo, solo catálogo


def test_un_tipo_nuevo_compara_su_logo_solo_con_catalogo_y_la_clave_sale_del_documento(monkeypatch):
    """Lo que costaría la próxima "comparación de logos": un JSON. Elemento por
    nombre (`_authenticity_elements`), campos por preset, y la clave leída del
    propio documento (`fields.RFC`) en un scope distinto del de la NOM-035."""
    tipo = _parse_entry("CONTRATO_PRUEBA", {
        "description": "x",
        "authenticity": {"elements": ["LOGO"], "memory": {"enabled": False},
                         "vision": {"inspect_when_digital": "with_references", "fallback_models": []},
                         "references": {"scope": "EMISOR", "key_from": "fields.RFC"}},
        "fields": {"$presets": ["LOGO_CONTRA_REFERENCIA"],
                   "RFC": {"type": "string", "required": False, "hint": "h", "validation": []}},
    })
    spec = authenticity.spec_for(tipo)
    assert list(spec["elements"]) == ["LOGO"] and spec["elements"]["LOGO"]["fingerprint"] is False
    assert authenticity.reference_scopes([tipo]) == {"EMISOR": {"LOGO": spec["elements"]["LOGO"]}}

    tabla = TablaFalsa()
    tabla.put_item({"Pk": fingerprint_store.reference_pk("EMISOR", "ABC010203XY1"), "Sk": "LOGO#membrete",
                    "S3Key": "references/logo.png"})
    con_tabla(monkeypatch, tabla)
    monkeypatch.setattr(fingerprint_store, "fetch_reference_bytes", lambda ref, s3=None: _jpeg_de_hoja())
    # El logo como imagen incrustada en un PDF digital: recorte exacto.
    con_logo = pdf(
        [{"contenido": texto(TEXTO_CARTA) + pintar("Logo", 10, 710, 200, 60),
          "imagenes": {"Logo": {"datos": b"logo" * 50}}}],
        metadata={"/Producer": "Microsoft® Word 2019"},
    )
    llamadas = vision_simulada(monkeypatch, presentes=("LOGO",), incrustadas={0: ["LOGO"]},
                               referencia={"LOGO": "membrete"})
    doc = {"documentType": "CONTRATO_PRUEBA", "pageStart": 1, "pageEnd": 1,
           "_entities": {"RFC": "ABC010203XY1"}, "_confidences": {}}

    authenticity.run(doc, tipo, authenticity.PageSource(reader=con_logo, file_sha256="x" * 64), {"documentId": "d"})

    assert llamadas["referencias"] == {"LOGO": ["membrete"]}
    assert doc["_entities"]["LOGO_PRESENTE"] is True and doc["_entities"]["LOGO_COINCIDE_REFERENCIA"] is True
    assert doc["authenticity"]["references"]["key"] == "ABC010203XY1"


def test_carta_nom35_resuelve_los_mismos_elementos_y_senales_que_cuando_los_declaraba_a_mano(entry):
    """`CARTA_NOM35` pasó a pedir sus elementos y campos de referencia por
    nombre. Esto fija que lo que la etapa ve no cambió."""
    spec = authenticity.spec_for(entry)

    assert spec["elements"]["FIRMA"]["similarity_reject"] == 0.97
    assert spec["elements"]["SELLO"] == {
        "description": spec["elements"]["SELLO"]["description"], "required": True, "read_text": True,
        "issuer_field": "EMPRESA_EMISORA", "ink_when_overlapping": "colour",
        "similarity_reject": None, "similarity_review": 0.95}
    assert spec["elements"]["LOGO"]["fingerprint"] is False
    assert spec["elements"]["LOGO"]["embedded_image_is_insertion"] is False
    assert spec["outputs"] == {
        "AUTENTICIDAD_EVALUADA": "evaluated", "CAPTURA_FISICA": "structure.physical_capture",
        "ORIGEN_DOCUMENTO": "structure.origin", "FONDO_NATURAL": "pixels.natural_background",
        "FIRMA_PRESENTE": "element.FIRMA.present", "SELLO_PRESENTE": "element.SELLO.present",
        "LOGO_PRESENTE": "element.LOGO.present", "FIRMA_SIN_INSERCION": "element.FIRMA.not_pasted",
        "SELLO_SIN_INSERCION": "element.SELLO.not_pasted", "TEXTO_SELLO": "element.SELLO.text",
        "SELLO_CORRESPONDE_EMISOR": "element.SELLO.issuer_consistent",
        "FIRMA_COINCIDE_REFERENCIA": "element.FIRMA.matches_reference",
        "SELLO_COINCIDE_REFERENCIA": "element.SELLO.matches_reference",
        "LOGO_COINCIDE_REFERENCIA": "element.LOGO.matches_reference",
        "FIRMA_SELLO_NO_REUTILIZADOS": "memory.elements_unique",
        "FIRMA_SELLO_NO_PARECIDOS": "memory.elements_not_similar", "CARTA_NO_DUPLICADA": "memory.not_duplicate",
        "PROVEEDOR_SUGERIDO": "references.suggested_key",
        "VEREDICTO_AUTENTICIDAD": "verdict", "MOTIVOS_AUTENTICIDAD": "reasons",
    }
    assert authenticity.reference_scopes([entry]) == {"PROVEEDOR": spec["elements"]}
