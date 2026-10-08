"""Capa 0 de autenticidad: ¿el ARCHIVO fue tocado después de generarse?

Cada caso construye, con pypdf o Pillow, el rastro que deja una manipulación
real y comprueba que `pdf_integrity` lo lee — y, con el mismo cuidado, que lo
que NO es una manipulación no se acusa:

- guardado incremental (`PdfWriter(incremental=True)`)   -> ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES
- archivo linearizado (dos secciones por diseño)         -> una sola generación
- trailer híbrido con /XRefStm                           -> una sola generación
- ModDate anterior a CreationDate / Info vs XMP          -> FECHAS_DE_METADATOS_INCONSISTENTES
- Producer de un editor, historial XMP de Photoshop      -> EDITOR_DE_PDF_EN_METADATOS
- "Paper Capture" de Acrobat                             -> no es un editor
- FreeText / Stamp con imagen                            -> ANOTACIONES_EN_PAGINA; /Link no cuenta
- campo de formulario con valor                          -> CAMPOS_DE_FORMULARIO; un /Sig no
- firma digital cuyo ByteRange cubre el archivo          -> intacta; + guardado incremental -> MODIFICADO_DESPUES_DE_FIRMAR (duro)
- JavaScript, adjunto, OpenAction                        -> CONTENIDO_ACTIVO_EN_PDF
- texto visible tras la imagen de página completa        -> TEXTO_ESCRITO_SOBRE_ESCANEO; margen y OCR no
- imagen pintada encima del texto                        -> IMAGEN_SOBRE_TEXTO_NATIVO; debajo no
- EXIF Software / APP13 / PNG Software / XMP en imagen   -> IMAGEN_CON_SOFTWARE_DE_EDICION
- sin EXIF                                               -> solo información (SIN_METADATOS_DE_CAMARA)
"""

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

import numpy as np  # noqa: E402
from PIL import Image, PngImagePlugin  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.annotations import FreeText, Link  # noqa: E402
from pypdf.generic import (  # noqa: E402
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from pipeline import pdf_forensics as pf  # noqa: E402
from pipeline import pdf_integrity as pi  # noqa: E402
from test_pdf_forensics import ANCHO, ALTO, PAGINA_COMPLETA, TEXTO_CARTA, _imagen, pintar  # noqa: E402


# ------------------------------------------------------------------ fixtures


def _bytes(writer: PdfWriter) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def pdf_bytes(contenido=b"", imagenes=None, metadata=None, xmp=None):
    writer = PdfWriter()
    page = writer.add_blank_page(ANCHO, ALTO)
    xobjects = DictionaryObject({
        NameObject(f"/{nombre}"): _imagen(writer, **kwargs) for nombre, kwargs in (imagenes or {}).items()
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
    stream = DecodedStreamObject()
    stream.set_data(contenido)
    page[NameObject("/Contents")] = writer._add_object(stream)
    if metadata:
        writer.add_metadata(metadata)
    if xmp:
        writer.xmp_metadata = xmp
    return _bytes(writer)


def texto_en(x, y, cadena, modo=None):
    tr = f"{modo} Tr " if modo is not None else ""
    return f"BT /F1 11 Tf {tr}{x} {y} Td ({cadena}) Tj ET\n".encode()


def incremental(base: bytes, **metadata) -> bytes:
    writer = PdfWriter(fileobj=io.BytesIO(base), incremental=True)
    writer.add_metadata(metadata or {"/Title": "guardado de nuevo"})
    return _bytes(writer)


def xmp_paquete(create="2026-01-01T12:00:00Z", modify="2026-01-01T12:00:00Z", agentes=()):
    historia = "".join(
        f'<rdf:li stEvt:action="saved" stEvt:softwareAgent="{a}"/>' for a in agentes
    )
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
        'xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/" xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#">'
        f'<xmp:CreateDate>{create}</xmp:CreateDate><xmp:ModifyDate>{modify}</xmp:ModifyDate>'
        f'<xmpMM:History><rdf:Seq>{historia}</rdf:Seq></xmpMM:History>'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    ).encode()


def evaluar(raw: bytes, policy=None, paginas=None):
    reader = PdfReader(io.BytesIO(raw))
    archivo = pi.file_report(raw, reader, policy)
    reportes = [pi.page_report(pf.inspect_page(reader, i, policy), policy)
                for i in (paginas if paginas is not None else range(len(reader.pages)))]
    señales, motivos = pi.signals_from(archivo, reportes, None, policy)
    return archivo, señales, {c: s for c, s, _ in motivos}


# ------------------------------------------------------------------ cadena de guardados


def test_un_archivo_escrito_una_vez_es_una_generacion():
    cadena = pi.update_chain(pdf_bytes())

    assert cadena["generations"] == 1
    assert cadena["startxrefCount"] == 1
    assert not cadena["linearized"]


def test_cada_guardado_incremental_suma_una_generacion_aunque_pypdf_no_exponga_prev():
    base = pdf_bytes()
    dos = incremental(base)
    tres = incremental(dos, **{"/Subject": "otra vez"})

    assert pi.update_chain(dos)["generations"] == 2
    assert pi.update_chain(tres)["generations"] == 3
    assert b"/Prev" in tres
    # La razón de leer los bytes: el trailer combinado de pypdf ya no trae /Prev.
    assert "/Prev" not in PdfReader(io.BytesIO(tres)).trailer


def test_incremental_produce_el_motivo_y_la_senal():
    _, señales, motivos = evaluar(incremental(pdf_bytes()))

    assert señales["integrity.single_generation"] is False
    assert señales["integrity.generations"] == 2
    assert motivos[pi.R_INCREMENTAL] == "soft"


def test_las_dos_pasadas_de_word_no_son_un_reguardado():
    """Medido: Word escribe el PDF y luego una sección incremental con su XMP,
    con creación == modificación al segundo. Es el generador terminando su
    archivo, no un editor volviendo a él."""
    raw = pdf_bytes(metadata={"/Producer": "Microsoft® Word para Microsoft 365",
                              "/CreationDate": "D:20220831020712Z", "/ModDate": "D:20220831020712Z"})
    # La segunda pasada agrega algo (Word agrega su XMP) sin mover /ModDate.
    dos_pasadas = incremental(raw, **{"/Title": "Carta"})

    archivo, señales, motivos = evaluar(dos_pasadas)

    assert archivo["chain"]["generations"] == 2
    assert archivo["chain"]["generatorTwoPass"] is True
    assert señales["integrity.single_generation"] is True
    assert pi.R_INCREMENTAL not in motivos


def test_un_reguardado_posterior_si_cuenta_aunque_sea_el_segundo():
    raw = pdf_bytes(metadata={"/CreationDate": "D:20220831020712Z", "/ModDate": "D:20220831020712Z"})
    despues = incremental(raw, **{"/ModDate": "D:20220905100000Z"})

    archivo, señales, motivos = evaluar(despues)

    assert archivo["chain"]["generatorTwoPass"] is False
    assert señales["integrity.single_generation"] is False
    assert pi.R_INCREMENTAL in motivos


def test_sin_fechas_un_segundo_guardado_se_cuenta():
    _, señales, _ = evaluar(incremental(pdf_bytes()))

    assert señales["integrity.single_generation"] is False


def _linearizado(largo_correcto=True) -> bytes:
    """Un archivo linearizado sintético: diccionario /Linearized al inicio, una
    sección de primera página cuyo trailer apunta (/Prev) a la principal."""
    cuerpo = b"%PDF-1.4\n1 0 obj\n<< /Linearized 1 /L LLLLLLLLLL /O 3 /E 500 /N 1 /T 400 >>\nendobj\n"
    primera_xref = len(cuerpo)
    cuerpo += b"xref\n0 1\n0000000000 65535 f \ntrailer\n<< /Size 4 /Root 2 0 R /Prev PPPPPPPPPP >>\nstartxref\n"
    cuerpo += str(primera_xref).encode() + b"\n%%EOF\n"
    cuerpo += b"2 0 obj\n<< /Type /Catalog /Pages 3 0 R >>\nendobj\n3 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    principal = len(cuerpo)
    cuerpo += b"xref\n0 4\n0000000000 65535 f \ntrailer\n<< /Size 4 >>\nstartxref\n" + str(primera_xref).encode() + b"\n%%EOF\n"
    cuerpo = cuerpo.replace(b"PPPPPPPPPP", str(principal).encode().rjust(10, b"0"))
    largo = len(cuerpo) if largo_correcto else len(cuerpo) + 99
    return cuerpo.replace(b"LLLLLLLLLL", str(largo).encode().rjust(10, b"0"))


def test_un_linearizado_tiene_dos_secciones_pero_una_generacion():
    cadena = pi.update_chain(_linearizado())

    assert cadena["linearized"] is True
    assert cadena["linearizedLengthMatches"] is True
    assert len(cadena["chain"]) == 2
    assert cadena["generations"] == 1


def test_un_linearizado_al_que_le_agregaron_algo_ya_no_cuadra_su_largo():
    cadena = pi.update_chain(_linearizado(largo_correcto=False))

    assert cadena["linearizedLengthMatches"] is False
    assert cadena["generations"] == 2


def test_el_xrefstm_de_un_hibrido_no_cuenta_como_generacion():
    raw = pdf_bytes()
    hibrido = raw.replace(b"trailer\n<<", b"trailer\n<< /XRefStm 15", 1)

    cadena = pi.update_chain(hibrido)

    assert cadena["hybridXrefStm"] is True
    assert cadena["generations"] == 1


def test_una_cadena_ilegible_se_abstiene_en_vez_de_acusar():
    assert pi.update_chain(b"esto no es un pdf")["generations"] is None

    # El último startxref apunta fuera del archivo: la cadena no se puede seguir.
    raw = pdf_bytes()
    roto = raw[:raw.rfind(b"startxref")] + b"startxref\n999999999\n%%EOF\n"
    assert pi.update_chain(roto)["generations"] is None
    señales, _ = pi.signals_from({"chain": pi.update_chain(roto)}, [], None)
    assert señales["integrity.single_generation"] is None
    assert señales["integrity.generations"] is None


# ------------------------------------------------------------------ fechas y software


def test_fechas_coherentes_no_levantan_nada():
    _, señales, motivos = evaluar(pdf_bytes(metadata={
        "/CreationDate": "D:20260101120000Z", "/ModDate": "D:20260101120030Z"}))

    assert señales["integrity.dates_consistent"] is True
    assert pi.R_DATES_INCONSISTENT not in motivos
    assert pi.R_MODIFIED_AFTER_CREATION not in motivos


def test_modificacion_muy_posterior_es_solo_informacion():
    _, señales, motivos = evaluar(pdf_bytes(metadata={
        "/CreationDate": "D:20260101120000Z", "/ModDate": "D:20260301120000Z"}))

    assert señales["integrity.dates_consistent"] is True
    assert motivos[pi.R_MODIFIED_AFTER_CREATION] == "info"


def test_modificacion_anterior_a_la_creacion_es_inconsistente():
    _, señales, motivos = evaluar(pdf_bytes(metadata={
        "/CreationDate": "D:20260201120000Z", "/ModDate": "D:20260101120000Z"}))

    assert señales["integrity.dates_consistent"] is False
    assert motivos[pi.R_DATES_INCONSISTENT] == "soft"


def test_info_y_xmp_en_desacuerdo_es_inconsistente():
    raw = pdf_bytes(metadata={"/CreationDate": "D:20260101120000Z", "/ModDate": "D:20260101120000Z"},
                    xmp=xmp_paquete(modify="2026-02-15T09:00:00Z"))

    _, señales, motivos = evaluar(raw)

    assert señales["integrity.dates_consistent"] is False
    assert pi.R_DATES_INCONSISTENT in motivos


def test_sin_fechas_la_senal_queda_vacia():
    _, señales, motivos = evaluar(pdf_bytes())

    assert señales["integrity.dates_consistent"] is None


def test_un_escaner_no_es_un_editor():
    _, señales, motivos = evaluar(pdf_bytes(metadata={"/Producer": "Lexmark MX611", "/Creator": "HardCopy"}))

    assert señales["integrity.producer_consistent"] is True
    assert señales["integrity.editor"] is None


def test_un_editor_en_producer_se_reporta_con_su_nombre():
    _, señales, motivos = evaluar(pdf_bytes(metadata={"/Producer": "Adobe Acrobat Pro DC 23.001", "/Creator": "CamScanner"}))

    assert señales["integrity.producer_consistent"] is False
    assert "Acrobat Pro" in señales["integrity.editor"]
    assert motivos[pi.R_EDITOR] == "soft"


def test_el_historial_xmp_delata_a_photoshop():
    raw = pdf_bytes(xmp=xmp_paquete(agentes=("Adobe Photoshop 25.0 (Windows)",)))

    archivo, señales, motivos = evaluar(raw)

    assert "Adobe Photoshop 25.0 (Windows)" in archivo["software"]["historyAgents"]
    assert señales["integrity.producer_consistent"] is False
    assert pi.R_EDITOR in motivos


def test_paper_capture_de_acrobat_es_software_de_captura():
    _, señales, _ = evaluar(pdf_bytes(metadata={"/Producer": "Adobe Acrobat 11 Paper Capture Plug-in"}))

    assert señales["integrity.producer_consistent"] is True


def test_los_patrones_de_editor_son_configurables():
    policy = {"editor_patterns": ["mi editor casero"]}

    _, con, _ = evaluar(pdf_bytes(metadata={"/Producer": "Mi Editor Casero 1.0"}), policy)
    _, sin, _ = evaluar(pdf_bytes(metadata={"/Producer": "Adobe Acrobat Pro DC"}), policy)

    assert con["integrity.producer_consistent"] is False
    assert sin["integrity.producer_consistent"] is True


# ------------------------------------------------------------------ anotaciones y formularios


def test_una_anotacion_de_texto_libre_se_reporta_con_su_subtipo():
    writer = PdfWriter()
    writer.add_blank_page(ANCHO, ALTO)
    writer.add_annotation(0, FreeText(text="15 de marzo", rect=(50, 50, 250, 90), font="Arial", bold=False,
                                      italic=False, font_size="12pt", font_color="000000",
                                      border_color="000000", background_color="ffffff"))

    _, señales, motivos = evaluar(_bytes(writer))

    assert señales["integrity.no_annotations"] is False
    assert motivos[pi.R_ANNOTATIONS] == "soft"


def test_un_vinculo_no_es_una_anotacion_sobre_el_contenido():
    writer = PdfWriter()
    writer.add_blank_page(ANCHO, ALTO)
    writer.add_annotation(0, Link(rect=(50, 50, 250, 90), url="https://example.org"))

    _, señales, motivos = evaluar(_bytes(writer))

    assert señales["integrity.no_annotations"] is True
    assert pi.R_ANNOTATIONS not in motivos


def _sello_como_anotacion() -> bytes:
    """Un /Stamp cuya apariencia pinta una imagen: así deja un editor una firma
    'colocada' sobre la página sin tocar el contenido."""
    writer = PdfWriter()
    page = writer.add_blank_page(ANCHO, ALTO)
    imagen = _imagen(writer)
    apariencia = DecodedStreamObject()
    apariencia.set_data(b"q 200 0 0 80 0 0 cm /Im0 Do Q")
    apariencia.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(200), NumberObject(80)]),
        NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): imagen})}),
    })
    anot = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Stamp"),
        NameObject("/Rect"): ArrayObject([NumberObject(300), NumberObject(100), NumberObject(500), NumberObject(180)]),
        NameObject("/AP"): DictionaryObject({NameObject("/N"): writer._add_object(apariencia)}),
    })
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(anot)])
    return _bytes(writer)


def test_una_imagen_pegada_como_sello_se_ve_y_queda_marcada():
    raw = _sello_como_anotacion()
    reporte = pf.inspect_page(PdfReader(io.BytesIO(raw)), 0)

    imagenes = [i for i in reporte["images"] if i.get("fromAnnotation")]
    assert len(imagenes) == 1
    assert imagenes[0]["annotSubtype"] == "/Stamp"
    # Colocada donde dice el /Rect del sello (mitad inferior derecha), no en el origen.
    assert imagenes[0]["rect"][0] > 0.4 and imagenes[0]["rect"][1] > 0.7
    assert reporte["annotations"][0]["hasImage"] is True
    assert reporte["visibleTextChars"] == 0

    _, señales, motivos = evaluar(raw)
    assert señales["integrity.no_annotations"] is False


def _con_formulario(campo_tipo="/Tx", valor="Juan") -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(ANCHO, ALTO)
    campo = DictionaryObject({
        NameObject("/FT"): NameObject(campo_tipo), NameObject("/T"): TextStringObject("campo1"),
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/Rect"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(100), NumberObject(20)]),
    })
    if valor is not None:
        campo[NameObject("/V")] = TextStringObject(valor)
    ref = writer._add_object(campo)
    page[NameObject("/Annots")] = ArrayObject([ref])
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject([ref])})
    return _bytes(writer)


def test_un_campo_de_formulario_con_valor_se_reporta():
    archivo, señales, motivos = evaluar(_con_formulario())

    assert archivo["forms"] == {"fields": 1, "filled": 1, "kinds": ["/Tx"], "needAppearances": False}
    assert señales["integrity.no_form_fields"] is False
    assert motivos[pi.R_FORM_FIELDS] == "soft"
    # El widget del campo no cuenta además como anotación: se califica una sola vez.
    assert señales["integrity.no_annotations"] is True


def test_un_campo_de_firma_no_es_un_formulario():
    _, señales, _ = evaluar(_con_formulario("/Sig", valor=None))

    assert señales["integrity.no_form_fields"] is True


# ------------------------------------------------------------------ firmas digitales


def _firmado() -> bytes:
    """Un PDF con un diccionario de firma cuyo /ByteRange cubre el archivo
    completo (el marcador de 10 dígitos se parcha con el largo real)."""
    writer = PdfWriter()
    writer.add_blank_page(ANCHO, ALTO)
    firma = DictionaryObject({
        NameObject("/Type"): NameObject("/Sig"), NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
        NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"), NameObject("/Name"): TextStringObject("Perito"),
        NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(100), NumberObject(200), NumberObject(9999999999)]),
    })
    campo = DictionaryObject({
        NameObject("/FT"): NameObject("/Sig"), NameObject("/T"): TextStringObject("Sig1"),
        NameObject("/V"): writer._add_object(firma),
    })
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject([writer._add_object(campo)])})
    raw = _bytes(writer)
    return raw.replace(b"9999999999", str(len(raw) - 200).rjust(10, "0").encode())


def test_una_firma_que_cubre_el_archivo_esta_intacta():
    archivo, señales, motivos = evaluar(_firmado())

    assert archivo["signatures"]["signed"] is True
    assert archivo["signatures"]["intact"] is True
    assert señales["integrity.digitally_signed"] is True
    assert señales["integrity.signature_intact"] is True
    assert señales["integrity.single_generation"] is True
    assert motivos[pi.R_SIGNED] == "info"


def test_guardar_encima_de_una_firma_es_un_motivo_duro():
    _, señales, motivos = evaluar(incremental(_firmado()))

    assert señales["integrity.signature_intact"] is False
    assert señales["integrity.single_generation"] is False
    assert motivos[pi.R_MODIFIED_AFTER_SIGNING] == "hard"
    assert pi.R_INCREMENTAL not in motivos


def test_sin_firma_la_senal_de_firma_queda_vacia():
    _, señales, _ = evaluar(pdf_bytes())

    assert señales["integrity.digitally_signed"] is False
    assert señales["integrity.signature_intact"] is None


# ------------------------------------------------------------------ contenido activo


def test_javascript_adjuntos_y_openaction_son_contenido_activo():
    writer = PdfWriter()
    writer.add_blank_page(ANCHO, ALTO)
    writer.add_js("app.alert('hola')")
    writer.add_attachment("cfdi.xml", b"<cfdi/>")
    writer._root_object[NameObject("/OpenAction")] = DictionaryObject({NameObject("/S"): NameObject("/JavaScript")})

    archivo, señales, motivos = evaluar(_bytes(writer))

    assert set(archivo["activeContent"]["kinds"]) == {"OpenAction", "JavaScript", "EmbeddedFiles"}
    assert archivo["activeContent"]["embeddedFiles"] == ["cfdi.xml"]
    assert señales["integrity.no_active_content"] is False
    assert motivos[pi.R_ACTIVE_CONTENT] == "soft"


def test_un_adjunto_permitido_por_el_tipo_no_cuenta():
    writer = PdfWriter()
    writer.add_blank_page(ANCHO, ALTO)
    writer.add_attachment("cfdi.xml", b"<cfdi/>")

    _, señales, _ = evaluar(_bytes(writer), {"allow_embedded_files": True})

    assert señales["integrity.no_active_content"] is True


# ------------------------------------------------------------------ orden de pintado


def test_texto_visible_escrito_sobre_el_escaneo():
    raw = pdf_bytes(PAGINA_COMPLETA + texto_en(72, 500, "15 de marzo de 2025"), {"Pag": {}})

    _, señales, motivos = evaluar(raw)

    assert señales["integrity.no_text_overlay"] is False
    assert motivos[pi.R_TEXT_OVER_SCAN] == "soft"


def test_un_folio_en_el_margen_no_es_texto_sobre_el_escaneo():
    raw = pdf_bytes(PAGINA_COMPLETA + texto_en(500, 775, "Pag 1 de 3"), {"Pag": {}})

    _, señales, _ = evaluar(raw)

    assert señales["integrity.no_text_overlay"] is True


def test_la_capa_ocr_invisible_no_es_texto_sobre_el_escaneo():
    raw = pdf_bytes(PAGINA_COMPLETA + texto_en(72, 500, TEXTO_CARTA, modo=3), {"Pag": {}})

    _, señales, _ = evaluar(raw)

    assert señales["integrity.no_text_overlay"] is True


def test_una_carta_de_word_sobre_un_membrete_de_fondo_no_es_texto_sobre_escaneo():
    """Medido en cartas reales: Word pone la imagen del membrete a página
    completa y el cuerpo entero encima. Eso es una composición digital (lo
    dice la capa de estructura), no un escaneo al que le escribieron algo."""
    raw = pdf_bytes(PAGINA_COMPLETA + texto_en(72, 600, TEXTO_CARTA), {"Pag": {}})

    _, señales, motivos = evaluar(raw)

    assert señales["integrity.no_text_overlay"] is None
    assert pi.R_TEXT_OVER_SCAN not in motivos


def test_sin_imagen_de_pagina_completa_la_senal_queda_vacia():
    _, señales, _ = evaluar(pdf_bytes(texto_en(72, 700, TEXTO_CARTA)))

    assert señales["integrity.no_text_overlay"] is None
    assert señales["integrity.no_image_over_text"] is True


def test_una_imagen_pintada_encima_del_texto_es_un_parche():
    raw = pdf_bytes(texto_en(72, 600, TEXTO_CARTA[:60]) + pintar("Parche", 60, 590, 300, 20), {"Parche": {}})

    _, señales, motivos = evaluar(raw)

    assert señales["integrity.no_image_over_text"] is False
    assert motivos[pi.R_IMAGE_OVER_TEXT] == "soft"


def test_una_imagen_debajo_del_texto_no_es_un_parche():
    raw = pdf_bytes(pintar("Fondo", 60, 590, 300, 20) + texto_en(72, 600, TEXTO_CARTA[:60]), {"Fondo": {}})

    _, señales, _ = evaluar(raw)

    assert señales["integrity.no_image_over_text"] is True


def test_los_hallazgos_de_pagina_solo_miran_las_paginas_pedidas():
    writer = PdfWriter()
    writer.add_blank_page(ANCHO, ALTO)
    writer.add_blank_page(ANCHO, ALTO)
    writer.add_annotation(1, FreeText(text="x", rect=(50, 50, 250, 90), font="Arial", bold=False, italic=False,
                                      font_size="12pt", font_color="000000", border_color="000000",
                                      background_color="ffffff"))
    raw = _bytes(writer)

    _, solo_primera, _ = evaluar(raw, paginas=[0])
    _, ambas, _ = evaluar(raw, paginas=[0, 1])

    assert solo_primera["integrity.no_annotations"] is True
    assert ambas["integrity.no_annotations"] is False


def test_la_severidad_se_puede_subir_por_tipo():
    _, _, motivos = evaluar(incremental(pdf_bytes()), {"severity": {pi.R_INCREMENTAL: "hard"}})

    assert motivos[pi.R_INCREMENTAL] == "hard"


# ------------------------------------------------------------------ imágenes


def _jpeg(exif=None, xmp=None, app13=False, tamaño=(320, 240)) -> bytes:
    img = Image.fromarray(np.full((tamaño[1], tamaño[0], 3), 230, np.uint8))
    buf = io.BytesIO()
    extra = {}
    if exif is not None:
        extra["exif"] = exif.tobytes()
    if xmp is not None:
        extra["xmp"] = xmp
    img.save(buf, "JPEG", quality=85, **extra)
    raw = buf.getvalue()
    if app13:
        segmento = b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x00"
        raw = raw[:2] + b"\xff\xed" + (len(segmento) + 2).to_bytes(2, "big") + segmento + raw[2:]
    return raw


def _exif(software=None, fecha=None, original=None, camara=True):
    exif = Image.Exif()
    if camara:
        exif[0x010F], exif[0x0110] = "Canon", "EOS R"
    if software:
        exif[0x0131] = software
    if fecha:
        exif[0x0132] = fecha
    if original:
        exif.get_ifd(0x8769)[0x9003] = original
    return exif


def test_una_foto_de_camara_sin_edicion_no_levanta_nada():
    rep = pi.image_report(_jpeg(_exif(original="2026:03:01 10:00:00", fecha="2026:03:01 10:00:00")))
    señales, motivos = pi.signals_from(None, [], rep, None, is_image=True)

    assert rep["cameraMetadata"] is True
    assert señales["integrity.producer_consistent"] is True
    assert señales["integrity.camera_metadata"] is True
    assert señales["integrity.dates_consistent"] is True
    assert not motivos


def test_exif_software_de_photoshop():
    rep = pi.image_report(_jpeg(_exif(software="Adobe Photoshop 25.0 (Windows)")))
    señales, motivos = pi.signals_from(None, [], rep, None, is_image=True)

    assert rep["editor"] == "Adobe Photoshop 25.0 (Windows)"
    assert señales["integrity.producer_consistent"] is False
    assert {c for c, _, _ in motivos} == {pi.R_IMAGE_EDITOR}


def test_el_segmento_app13_delata_a_photoshop_aunque_no_haya_exif():
    rep = pi.image_report(_jpeg(app13=True))

    assert rep["photoshopApp13"] is True
    assert "Photoshop" in rep["editor"]


def test_el_historial_xmp_de_una_imagen_delata_al_editor():
    rep = pi.image_report(_jpeg(xmp=xmp_paquete(agentes=("GIMP 2.10",))))

    assert rep["xmpAgents"] == ["GIMP 2.10"]
    assert rep["editor"] == "GIMP 2.10"


def test_el_chunk_software_de_un_png():
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "GIMP 2.10.34")
    buf = io.BytesIO()
    Image.fromarray(np.full((240, 320, 3), 230, np.uint8)).save(buf, "PNG", pnginfo=info)

    rep = pi.image_report(buf.getvalue())

    assert rep["format"] == "PNG"
    assert rep["editor"] == "GIMP 2.10.34"


def test_fecha_exif_de_modificacion_posterior_a_la_captura():
    rep = pi.image_report(_jpeg(_exif(original="2026:03:01 10:00:00", fecha="2026:03:02 18:30:00")))
    señales, motivos = pi.signals_from(None, [], rep, None, is_image=True)

    assert señales["integrity.dates_consistent"] is False
    assert {c for c, _, _ in motivos} == {pi.R_EXIF_DATES}


def test_sin_exif_solo_se_informa():
    rep = pi.image_report(_jpeg())
    señales, motivos = pi.signals_from(None, [], rep, None, is_image=True)

    assert señales["integrity.camera_metadata"] is False
    assert señales["integrity.producer_consistent"] is True
    assert [(c, s) for c, s, _ in motivos] == [(pi.R_NO_CAMERA, "info")]


def test_una_imagen_ilegible_no_truena():
    rep = pi.image_report(b"no soy una imagen")

    assert rep["error"]
    señales, motivos = pi.signals_from(None, [], rep, None, is_image=True)
    assert señales["integrity.camera_metadata"] is None
    assert not motivos


# ------------------------------------------------------------------ alteraciones del contenido


def pdf_alterado(*contenidos, incrustada=False, alfa=None, capa_oculta=False):
    """Una página por contenido. `/F1` es Helvetica sin incrustar; con
    `incrustada`, `/FE` es una fuente que el archivo sí trae. `alfa` define el
    estado gráfico `/GS`; `capa_oculta`, un grupo de contenido opcional
    `/Capa` apagado."""
    writer = PdfWriter()
    helvetica = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    fuentes = DictionaryObject({NameObject("/F1"): helvetica})
    if incrustada:
        programa = DecodedStreamObject()
        programa.set_data(b"\x00" * 16)
        descriptor = DictionaryObject({NameObject("/Type"): NameObject("/FontDescriptor"),
                                       NameObject("/FontFile2"): writer._add_object(programa)})
        fuentes[NameObject("/FE")] = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/TrueType"),
            NameObject("/BaseFont"): NameObject("/ABCDEF+BancoSans"),
            NameObject("/FontDescriptor"): writer._add_object(descriptor),
        }))
    recursos = DictionaryObject({
        NameObject("/Font"): fuentes,
        NameObject("/XObject"): DictionaryObject({NameObject("/Pag"): _imagen(writer)}),
    })
    if alfa is not None:
        recursos[NameObject("/ExtGState")] = DictionaryObject({
            NameObject("/GS"): DictionaryObject({NameObject("/ca"): FloatObject(alfa)})})
    if capa_oculta:
        grupo = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/OCG"),
                                                     NameObject("/Name"): TextStringObject("Original")}))
        recursos[NameObject("/Properties")] = DictionaryObject({NameObject("/Capa"): grupo})
        writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
            NameObject("/OCGs"): ArrayObject([grupo]),
            NameObject("/D"): DictionaryObject({NameObject("/OFF"): ArrayObject([grupo])}),
        })
    for contenido in contenidos:
        page = writer.add_blank_page(ANCHO, ALTO)
        page[NameObject("/Resources")] = recursos
        stream = DecodedStreamObject()
        stream.set_data(contenido)
        page[NameObject("/Contents")] = writer._add_object(stream)
    return _bytes(writer)


CUERPO = texto_en(72, 700, TEXTO_CARTA)
CUERPO_INCRUSTADO = f"BT /FE 11 Tf 72 700 Td ({TEXTO_CARTA}) Tj ET\n".encode()
DATO = texto_en(72, 500, "012180001234567895")
SOBRE_EL_DATO = b"70 495 140 16 re f\n"


def test_un_recuadro_opaco_pintado_despues_del_texto_es_un_parche():
    _, señales, motivos = evaluar(pdf_alterado(CUERPO + DATO + b"1 1 1 rg " + SOBRE_EL_DATO))

    assert señales["integrity.no_shape_over_text"] is False
    assert motivos[pi.R_SHAPE_OVER_TEXT] == "soft"


def test_el_recuadro_reporta_su_color_y_cuanto_texto_tapa():
    reader = PdfReader(io.BytesIO(pdf_alterado(CUERPO + DATO + b"0 g " + SOBRE_EL_DATO)))

    (recuadro,) = pi.page_report(pf.inspect_page(reader, 0))["shapesOverText"]

    assert recuadro["colour"] == "NEGRO" and recuadro["coveredChars"] == 18


def test_un_recuadro_dibujado_con_cuatro_lineas_tambien_cuenta():
    lineas = b"1 1 1 rg 70 495 m 210 495 l 210 511 l 70 511 l h f\n"

    _, señales, _ = evaluar(pdf_alterado(CUERPO + DATO + lineas))

    assert señales["integrity.no_shape_over_text"] is False


def test_el_fondo_de_una_celda_pintado_antes_del_texto_no_es_un_parche():
    _, señales, motivos = evaluar(pdf_alterado(CUERPO + b"0.9 g " + SOBRE_EL_DATO + b"0 g " + DATO))

    assert señales["integrity.no_shape_over_text"] is True
    assert pi.R_SHAPE_OVER_TEXT not in motivos


def test_un_resaltado_translucido_no_tapa_el_texto():
    _, señales, _ = evaluar(pdf_alterado(CUERPO + DATO + b"/GS gs 1 1 0 rg " + SOBRE_EL_DATO, alfa=0.4))

    assert señales["integrity.no_shape_over_text"] is True


def test_un_relleno_recortado_fuera_del_texto_no_lo_tapa():
    # El generador recorta a una franja de arriba y rellena "toda la página".
    recortado = b"q 0 740 612 52 re W n 1 1 1 rg 0 0 612 792 re f Q\n"

    _, señales, _ = evaluar(pdf_alterado(DATO + recortado + CUERPO))

    assert señales["integrity.no_shape_over_text"] is True


def test_una_leyenda_en_diagonal_es_una_marca_de_agua():
    diagonal = b"0.8 g BT /F1 72 Tf 0.707 0.707 -0.707 0.707 150 200 Tm (COPIA) Tj ET\n"
    reader = PdfReader(io.BytesIO(pdf_alterado(CUERPO + diagonal)))

    reporte = pi.page_report(pf.inspect_page(reader, 0))
    señales, motivos = pi.signals_from(None, [reporte], None)

    assert reporte["watermarkKinds"] == ["TEXTO_DIAGONAL"]
    assert señales["integrity.no_watermark"] is False
    assert [c for c, _, _ in motivos] == [pi.R_WATERMARK]


def test_el_texto_vertical_del_margen_no_es_una_marca_de_agua():
    vertical = b"BT /F1 20 Tf 0 1 -1 0 30 100 Tm (Folio fiscal al margen) Tj ET\n"

    _, señales, _ = evaluar(pdf_alterado(CUERPO + vertical))

    assert señales["integrity.no_watermark"] is True


def test_lo_que_el_archivo_etiqueta_como_marca_de_agua_lo_es():
    etiquetada = (b"/Artifact <</Subtype /Watermark /Type /Pagination>> BDC "
                  b"BT /F1 9 Tf 200 400 Td (SIN VALOR) Tj ET EMC\n")
    reader = PdfReader(io.BytesIO(pdf_alterado(CUERPO + etiquetada)))

    reporte = pi.page_report(pf.inspect_page(reader, 0))

    assert reporte["watermarkKinds"] == ["ARTEFACTO_MARCA_DE_AGUA"] and reporte["noWatermark"] is False


def test_un_texto_grande_y_translucido_es_una_marca_de_agua():
    translucido = b"q /GS gs BT /F1 48 Tf 100 400 Td (BORRADOR) Tj ET Q\n"
    reader = PdfReader(io.BytesIO(pdf_alterado(CUERPO + translucido, alfa=0.3)))

    assert pi.page_report(pf.inspect_page(reader, 0))["watermarkKinds"] == ["TEXTO_TRANSLUCIDO"]


def test_la_linea_que_deja_una_app_de_escaneo_es_una_marca_de_agua():
    raw = pdf_alterado(CUERPO + texto_en(200, 20, "Scanned with CamScanner"))
    reader = PdfReader(io.BytesIO(raw))
    hechos = pf.inspect_page(reader, 0)

    frases = pi.tool_phrases(reader, 0)
    reporte = pi.page_report({**hechos, "toolPhrases": frases})
    _, motivos = pi.signals_from(None, [reporte], None)

    assert frases == ["camscanner", "scanned with"]
    assert reporte["watermarkKinds"] == ["LEYENDA_DE_HERRAMIENTA"]
    assert "camscanner" in motivos[0][2]


def test_en_un_escaneo_puro_la_marca_de_agua_no_se_puede_leer_del_archivo():
    _, señales, _ = evaluar(pdf_alterado(PAGINA_COMPLETA))

    assert señales["integrity.no_watermark"] is None          # la ve la revisión visual
    assert señales["integrity.no_shape_over_text"] is None


def test_unos_caracteres_en_una_fuente_sin_incrustar_son_texto_agregado():
    _, señales, motivos = evaluar(pdf_alterado(CUERPO_INCRUSTADO + DATO, incrustada=True))

    assert señales["integrity.fonts_consistent"] is False
    assert motivos[pi.R_FOREIGN_FONT] == "soft"


def test_un_archivo_que_no_incrusta_sus_fuentes_no_se_juzga_por_ellas():
    _, señales, _ = evaluar(pdf_alterado(CUERPO + DATO))

    assert señales["integrity.fonts_consistent"] is None


def test_todo_en_fuentes_incrustadas_es_consistente():
    _, señales, _ = evaluar(pdf_alterado(CUERPO_INCRUSTADO, incrustada=True))

    assert señales["integrity.fonts_consistent"] is True


def test_una_capa_apagada_es_contenido_oculto():
    oculto = b"/OC /Capa BDC BT /F1 9 Tf 72 300 Td (dato original) Tj ET EMC\n"

    archivo, señales, motivos = evaluar(pdf_alterado(CUERPO + oculto, capa_oculta=True))

    assert archivo["optionalContent"]["hidden"] == ["Original"]
    assert señales["integrity.no_hidden_content"] is False
    assert motivos[pi.R_HIDDEN_CONTENT] == "soft"


def test_sin_capas_no_hay_contenido_oculto():
    _, señales, _ = evaluar(pdf_alterado(CUERPO))

    assert señales["integrity.no_hidden_content"] is True


def test_una_pagina_escaneada_entre_paginas_digitales_delata_una_sustitucion():
    _, señales, motivos = evaluar(pdf_alterado(CUERPO, PAGINA_COMPLETA))

    assert señales["integrity.pages_consistent"] is False
    assert motivos[pi.R_PAGES_MIXED] == "soft"


def test_paginas_del_mismo_origen_son_homogeneas_y_una_sola_no_dice_nada():
    _, varias, _ = evaluar(pdf_alterado(CUERPO, CUERPO))
    _, una, _ = evaluar(pdf_alterado(CUERPO))

    assert varias["integrity.pages_consistent"] is True
    assert una["integrity.pages_consistent"] is None


def test_un_pdf_limpio_no_levanta_ninguna_alteracion():
    _, señales, motivos = evaluar(pdf_alterado(CUERPO_INCRUSTADO, CUERPO_INCRUSTADO, incrustada=True))

    assert not motivos
    assert all(señales[s] is True for s in (
        "integrity.no_watermark", "integrity.no_shape_over_text", "integrity.fonts_consistent",
        "integrity.no_hidden_content", "integrity.pages_consistent"))


def test_los_espacios_que_word_deja_en_una_fuente_sin_incrustar_no_son_texto_agregado():
    espacios = texto_en(72, 500, "          ")

    _, señales, motivos = evaluar(pdf_alterado(CUERPO_INCRUSTADO + espacios, incrustada=True))

    assert señales["integrity.fonts_consistent"] is True and not motivos


def test_un_texto_con_opacidad_casi_cero_no_es_una_marca_de_agua():
    ancla = b"q /GS gs BT /F1 15 Tf 400 100 Td (\\\\s1\\\\) Tj ET Q\n"
    reader = PdfReader(io.BytesIO(pdf_alterado(CUERPO + ancla, alfa=0.01)))

    assert pi.page_report(pf.inspect_page(reader, 0))["noWatermark"] is True
