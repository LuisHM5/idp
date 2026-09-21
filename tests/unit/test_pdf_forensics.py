"""Capa 1 de autenticidad: ¿el PDF es la CAPTURA de un papel o una composición
digital? Se decide con la estructura interna del archivo, sin modelo alguno.

Cada caso reproduce, en un PDF sintético mínimo, algo que se midió en cartas
reales (ver `catalogs/README.md` → "Autenticidad física"):

- escaneo puro: una imagen de página completa, cero texto          -> FISICO
- Word con la firma pegada: texto nativo + imagen parcial           -> DIGITAL
- Word con membrete de fondo: la imagen completa NO lo rescata      -> DIGITAL
- escaneo con OCR: imagen completa + texto INVISIBLE (modo 3)       -> ESCANEO_CON_OCR
- marca de agua de CamScanner: imagen diminuta, no es una firma     -> FISICO
- escaneo pegado dentro de una hoja de Word, sin texto              -> INDETERMINADO
- `/Filter` como arreglo (así lo escribe CamScanner)                -> no truena
- reverso en blanco                                                  -> no cuenta
"""

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.generic import (  # noqa: E402
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from pipeline import pdf_forensics as pf  # noqa: E402

ANCHO, ALTO = 612, 792
TEXTO_CARTA = "Por medio de la presente hacemos constar que la persona es empleada. " * 5


def _imagen(writer, datos=b"\x10" * 64, filtro=None, smask=False, bits=8, ancho=8, alto=8, color="/DeviceGray"):
    """`ancho`/`alto`/`color` deben ser los REALES cuando `datos` es una imagen
    de verdad que alguien va a rasterizar: pdfium se los cree al diccionario."""
    img = DecodedStreamObject()
    img.set_data(datos)
    img.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(ancho),
        NameObject("/Height"): NumberObject(alto),
        NameObject("/ColorSpace"): NameObject(color),
        NameObject("/BitsPerComponent"): NumberObject(bits),
    })
    if filtro is not None:
        img[NameObject("/Filter")] = filtro
    if smask:
        img[NameObject("/SMask")] = writer._add_object(DecodedStreamObject())
    return writer._add_object(img)


def pdf(paginas, metadata=None):
    """`paginas`: lista de dicts {contenido: bytes, imagenes: {nombre: kwargs}, rotar: int}."""
    writer = PdfWriter()
    for spec in paginas:
        page = writer.add_blank_page(ANCHO, ALTO)
        xobjects = DictionaryObject({
            NameObject(f"/{nombre}"): _imagen(writer, **kwargs)
            for nombre, kwargs in (spec.get("imagenes") or {}).items()
        })
        fuente = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): xobjects,
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(fuente)}),
        })
        contenido = DecodedStreamObject()
        contenido.set_data(spec.get("contenido", b""))
        page[NameObject("/Contents")] = writer._add_object(contenido)
        if spec.get("rotar"):
            page[NameObject("/Rotate")] = NumberObject(spec["rotar"])
    if metadata:
        writer.add_metadata(metadata)
    buf = io.BytesIO()
    writer.write(buf)
    return PdfReader(io.BytesIO(buf.getvalue()))


def pintar(nombre, x, y, w, h):
    return f"q {w} 0 0 {h} {x} {y} cm /{nombre} Do Q\n".encode()


def texto(cadena, modo=None):
    tr = f"{modo} Tr " if modo is not None else ""
    return f"BT /F1 11 Tf {tr}({cadena}) Tj ET\n".encode()


PAGINA_COMPLETA = pintar("Pag", 0, 0, ANCHO, ALTO)


def inspeccionar(reader, **policy):
    return pf.inspect_pdf(reader, range(len(reader.pages)), policy or None)


def test_un_escaneo_puro_es_captura_fisica():
    reporte = inspeccionar(pdf([{"contenido": PAGINA_COMPLETA, "imagenes": {"Pag": {}}}]))

    assert reporte["verdict"] == pf.PAGE_PHYSICAL
    assert reporte["physicalCapture"] is True
    assert reporte["reasons"] == []
    assert reporte["perPage"][0]["images"][0]["coverage"] == 1.0


def test_texto_nativo_con_imagen_parcial_es_digital():
    """El caso ancla: carta hecha en Word con el bloque firma+sello pegado."""
    reporte = inspeccionar(pdf([{
        "contenido": texto(TEXTO_CARTA) + pintar("Firma", 204, 170, 204, 199),
        "imagenes": {"Firma": {}},
    }]))

    assert reporte["verdict"] == pf.PAGE_DIGITAL
    assert reporte["physicalCapture"] is False
    assert pf.REASON_PASTED_IMAGE in reporte["reasons"]


def test_un_membrete_de_fondo_a_pagina_completa_no_rescata_al_documento_digital():
    reporte = inspeccionar(pdf([{
        "contenido": PAGINA_COMPLETA + texto(TEXTO_CARTA) + pintar("Firma", 204, 170, 204, 199),
        "imagenes": {"Pag": {}, "Firma": {}},
    }]))

    assert reporte["physicalCapture"] is False
    assert pf.REASON_PASTED_IMAGE in reporte["reasons"]


def test_documento_digital_sin_imagen_de_firma_tambien_es_digital():
    reporte = inspeccionar(pdf([{"contenido": texto(TEXTO_CARTA)}]))

    assert reporte["physicalCapture"] is False
    assert reporte["reasons"] == [pf.REASON_NO_CAPTURE]


def test_un_escaneo_con_capa_ocr_invisible_sigue_siendo_captura():
    reporte = inspeccionar(pdf([{
        "contenido": PAGINA_COMPLETA + texto(TEXTO_CARTA, modo=3),
        "imagenes": {"Pag": {}},
    }]))

    assert reporte["verdict"] == pf.PAGE_PHYSICAL_OCR
    assert reporte["physicalCapture"] is True
    assert reporte["perPage"][0]["visibleTextChars"] == 0
    assert reporte["perPage"][0]["invisibleTextChars"] > 200


def test_el_modo_de_texto_se_restaura_con_Q():
    """`Tr` es estado gráfico: un bloque invisible dentro de q/Q no vuelve
    invisible al texto que viene después."""
    contenido = b"q " + texto("oculto " * 40, modo=3) + b"Q\n" + texto(TEXTO_CARTA)
    reporte = inspeccionar(pdf([{"contenido": contenido}]))

    assert reporte["perPage"][0]["visibleTextChars"] >= 200
    assert reporte["physicalCapture"] is False


def test_la_marca_de_agua_de_la_app_de_captura_no_es_una_firma_pegada():
    reporte = inspeccionar(pdf([{
        "contenido": pintar("Pag", 0, 50, ANCHO, 742) + pintar("Marca", 512, 10, 67, 7),
        "imagenes": {"Pag": {}, "Marca": {}},
    }]))

    assert reporte["physicalCapture"] is True
    assert reporte["reasons"] == []


def test_escaneo_pegado_en_hoja_de_oficina_queda_indeterminado_no_rechazado():
    reporte = inspeccionar(
        pdf([{"contenido": pintar("Scan", 40, 80, 530, 620), "imagenes": {"Scan": {}}}],
            metadata={"/Producer": "Microsoft® Word para Microsoft 365"})
    )

    assert reporte["verdict"] == pf.PAGE_UNDETERMINED
    assert reporte["physicalCapture"] is None
    assert reporte["origin"] == pf.ORIGIN_OFFICE
    assert reporte["reasons"] == []


def test_el_productor_es_evidencia_y_nunca_decide():
    """Un escaneo genuino exportado desde Word (una imagen de página completa,
    sin texto) sigue siendo captura física aunque el productor sea ofimática."""
    reporte = inspeccionar(
        pdf([{"contenido": PAGINA_COMPLETA, "imagenes": {"Pag": {}}}],
            metadata={"/Producer": "Microsoft® Word para Microsoft 365"})
    )

    assert reporte["origin"] == pf.ORIGIN_OFFICE
    assert reporte["physicalCapture"] is True


def test_origen_se_clasifica_por_productor():
    assert pf.classify_origin({"producer": "Lexmark MX622adhe", "creator": "HardCopy"}) == pf.ORIGIN_SCANNER
    assert pf.classify_origin({"producer": "intsig.com pdf producer", "author": "CamScanner"}) == pf.ORIGIN_CAPTURE_APP
    assert pf.classify_origin({"producer": "Microsoft® Word para Microsoft 365"}) == pf.ORIGIN_OFFICE
    assert pf.classify_origin({"producer": "iText 5.5"}) == pf.ORIGIN_UNKNOWN
    assert pf.classify_origin({}) == pf.ORIGIN_UNKNOWN


def test_filter_como_arreglo_no_truena():
    filtro = ArrayObject([NameObject("/DCTDecode")])
    reporte = inspeccionar(pdf([{"contenido": PAGINA_COMPLETA, "imagenes": {"Pag": {"filtro": filtro}}}]))

    assert reporte["perPage"][0]["images"][0]["filters"] == ["/DCTDecode"]
    assert reporte["physicalCapture"] is True


def test_escaneo_bitonal_se_marca_para_que_la_capa_de_pixeles_se_abstenga():
    reporte = inspeccionar(pdf([{"contenido": PAGINA_COMPLETA, "imagenes": {"Pag": {"bits": 1}}}]))

    assert reporte["bilevel"] is True
    assert reporte["physicalCapture"] is True


def test_el_reverso_en_blanco_no_cuenta_como_pagina_de_contenido():
    reporte = inspeccionar(pdf([
        {"contenido": PAGINA_COMPLETA, "imagenes": {"Pag": {}}},
        {"contenido": b""},
    ]))

    assert reporte["contentPages"] == [1]
    assert reporte["perPage"][1]["verdict"] == pf.PAGE_BLANK
    assert reporte["physicalCapture"] is True


def test_una_sola_pagina_digital_vuelve_digital_al_documento():
    reporte = inspeccionar(pdf([
        {"contenido": PAGINA_COMPLETA, "imagenes": {"Pag": {}}},
        {"contenido": texto(TEXTO_CARTA) + pintar("Firma", 204, 170, 204, 199), "imagenes": {"Firma": {}}},
    ]))

    assert reporte["physicalCapture"] is False


def test_la_transparencia_es_evidencia_extra_no_la_senal():
    sin = inspeccionar(pdf([{
        "contenido": texto(TEXTO_CARTA) + pintar("Firma", 204, 170, 204, 199), "imagenes": {"Firma": {}},
    }]))
    con = inspeccionar(pdf([{
        "contenido": texto(TEXTO_CARTA) + pintar("Firma", 204, 170, 204, 199),
        "imagenes": {"Firma": {"smask": True}},
    }]))

    assert sin["physicalCapture"] is False and pf.REASON_TRANSPARENCY not in sin["reasons"]
    assert pf.REASON_TRANSPARENCY in con["reasons"]


def test_la_misma_imagen_da_el_mismo_hash_en_dos_documentos():
    a = inspeccionar(pdf([{"contenido": texto(TEXTO_CARTA) + pintar("F", 204, 170, 204, 199),
                           "imagenes": {"F": {"datos": b"firma-pegada" * 9}}}]))
    b = inspeccionar(pdf([{"contenido": texto("Otra carta. " * 30) + pintar("F", 204, 150, 204, 199),
                           "imagenes": {"F": {"datos": b"firma-pegada" * 9}}}]))

    assert a["perPage"][0]["images"][0]["sha256"] == b["perPage"][0]["images"][0]["sha256"]


def test_el_rectangulo_se_reporta_en_espacio_visual_con_origen_arriba():
    reporte = inspeccionar(pdf([{"contenido": pintar("F", 0, 0, 306, 396), "imagenes": {"F": {}}}]))

    # Abajo-izquierda en el PDF = mitad inferior izquierda en pantalla.
    assert reporte["perPage"][0]["images"][0]["rect"] == [0.0, 0.5, 0.5, 1.0]


def test_el_rectangulo_honra_la_rotacion_de_la_pagina():
    reporte = inspeccionar(pdf([{"contenido": pintar("F", 0, 0, 306, 396), "imagenes": {"F": {}}, "rotar": 270}]))

    assert reporte["perPage"][0]["images"][0]["rect"] == [0.5, 0.5, 1.0, 1.0]


def test_imagen_bajo_la_firma_se_detecta_y_una_grande_no():
    pagina = {"images": [
        {"sha256": "pegada", "coverage": 0.08, "rect": [0.33, 0.53, 0.66, 0.78]},
        {"sha256": "escaneo", "coverage": 0.77, "rect": [0.05, 0.1, 0.95, 0.9]},
        {"sha256": "logo", "coverage": 0.04, "rect": [0.05, 0.02, 0.3, 0.1]},
    ]}

    bajo_la_firma = pf.images_overlapping(pagina, [0.35, 0.55, 0.6, 0.7])

    assert [i["sha256"] for i in bajo_la_firma] == ["pegada"]


def test_una_pagina_ilegible_queda_indeterminada_en_vez_de_tronar():
    class Roto:
        pages = [None]
        metadata = None

    reporte = pf.inspect_pdf(Roto(), [0])

    assert reporte["physicalCapture"] is None
    assert "error" in reporte["perPage"][0]
