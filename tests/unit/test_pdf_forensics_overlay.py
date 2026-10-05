"""El walker de `pdf_forensics` sigue el ORDEN de pintado y DÓNDE cae cada
string, para que la capa de integridad pueda decir "esto se escribió encima de
aquello". Lo que se comprueba aquí:

- un string en (72, 700) cae arriba-izquierda en espacio visual
- `Td`, `T*` y `Tm` mueven el cursor; el avance depende de los anchos de la fuente
- un string tras la imagen de página completa cuenta como texto sobre escaneo
- recorrer la apariencia de una anotación NO cambia el conteo de texto visible
  (los veredictos de captura física siguen intactos)
- `dct_stream_bytes` devuelve el JPEG de un XObject /DCTDecode y nada más
"""

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from pypdf import PdfReader  # noqa: E402
from pypdf.generic import ArrayObject, NameObject, NumberObject  # noqa: E402

from pipeline import pdf_forensics as pf  # noqa: E402
from test_pdf_forensics import ANCHO, ALTO, PAGINA_COMPLETA, TEXTO_CARTA, pdf, pintar  # noqa: E402


def texto_en(x, y, cadena, modo=None):
    tr = f"{modo} Tr " if modo is not None else ""
    return f"BT /F1 11 Tf {tr}{x} {y} Td ({cadena}) Tj ET\n".encode()


def _walk(contenido, imagenes=None):
    reader = pdf([{"contenido": contenido, "imagenes": imagenes or {}}])
    page = reader.pages[0]
    box = page.cropbox
    walk = pf._PageWalk(reader, (float(box.left), float(box.bottom), float(box.width), float(box.height)), 0)
    walk.walk(page.get_contents(), page.get("/Resources") or {}, pf._IDENTITY, 0)
    return walk


def test_un_string_cae_donde_se_escribio_en_espacio_visual():
    walk = _walk(texto_en(72, 700, "Hola mundo"))

    (run,) = walk.text_runs
    x0, y0, x1, y1 = run["rect"]
    assert run["chars"] == 10 and run["visible"] is True
    assert abs(x0 - 72 / ANCHO) < 0.01
    assert 0.09 < y0 < 0.13 and y1 > y0        # 700 pt desde abajo = ~11% desde arriba
    assert x1 > x0 + 0.05                       # el avance no es cero


def test_el_cursor_avanza_con_td_tstar_y_tm():
    walk = _walk(b"BT /F1 11 Tf 14 TL 72 700 Td (uno) Tj T* (dos) Tj 1 0 0 1 300 400 Tm (tres) Tj ET\n")

    uno, dos, tres = walk.text_runs
    assert abs(uno["rect"][0] - dos["rect"][0]) < 0.001     # misma columna
    assert dos["rect"][1] > uno["rect"][1]                  # un renglón más abajo
    assert abs(tres["rect"][0] - 300 / ANCHO) < 0.01        # Tm reposiciona en absoluto
    assert [r["seq"] for r in (uno, dos, tres)] == [1, 2, 3]


def test_el_texto_y_las_imagenes_comparten_el_orden_de_pintado():
    walk = _walk(texto_en(72, 700, "antes") + PAGINA_COMPLETA + texto_en(72, 500, "despues"), {"Pag": {}})

    assert [r["seq"] for r in walk.text_runs] == [1, 3]
    assert walk.images[0]["seq"] == 2
    hechos = pf.overlay_facts(walk)
    assert hechos["textAfterFullPageImageChars"] == len("despues")


def test_la_apariencia_de_una_anotacion_no_cuenta_como_texto_de_la_pagina():
    reader = pdf([{"contenido": PAGINA_COMPLETA, "imagenes": {"Pag": {}}}])
    from pypdf.generic import DecodedStreamObject, DictionaryObject

    page = reader.pages[0]
    fuente = page["/Resources"]["/Font"]
    apariencia = DecodedStreamObject()
    apariencia.set_data(b"BT /F1 12 Tf 10 10 Td (" + TEXTO_CARTA[:100].encode() + b") Tj ET")
    apariencia.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(300), NumberObject(40)]),
        NameObject("/Resources"): DictionaryObject({NameObject("/Font"): fuente}),
    })
    anot = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/FreeText"),
        NameObject("/Rect"): ArrayObject([NumberObject(100), NumberObject(100), NumberObject(400), NumberObject(140)]),
        NameObject("/AP"): DictionaryObject({NameObject("/N"): apariencia}),
    })
    page[NameObject("/Annots")] = ArrayObject([anot])

    reporte = pf.inspect_page(reader, 0)

    assert reporte["visibleTextChars"] == 0
    assert reporte["annotations"][0]["subtype"] == "/FreeText"
    assert reporte["annotations"][0]["hasAppearance"] is True
    assert pf.page_verdict(reporte)["verdict"] == pf.PAGE_PHYSICAL


def test_dct_stream_bytes_solo_devuelve_jpeg_de_verdad():
    buf = io.BytesIO()
    Image.fromarray(np.full((16, 16, 3), 200, np.uint8)).save(buf, "JPEG")
    jpeg = buf.getvalue()
    reader = pdf([{"contenido": pintar("Foto", 0, 0, ANCHO, ALTO) + pintar("Cruda", 0, 0, 10, 10),
                   "imagenes": {"Foto": {"datos": jpeg, "filtro": NameObject("/DCTDecode"), "ancho": 16, "alto": 16,
                                         "color": "/DeviceRGB"},
                                "Cruda": {}}}])

    assert pf.dct_stream_bytes(reader, 0, "/Foto") == jpeg
    assert pf.dct_stream_bytes(reader, 0, "/Cruda") is None
    assert pf.dct_stream_bytes(reader, 0, "/NoExiste") is None
