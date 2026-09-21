"""Capa 2 de autenticidad (ruido de fondo) y las huellas de tinta de la capa 4.

Todo es numpy puro sobre arreglos sintéticos: no hay PDF, ni OCR, ni modelo.
Lo que se fija aquí salió de medir cartas reales:

- fondo de una exportación digital: desviación 0.000, blanco puro en todo;
- fondo de una captura real: ruido de sensor/papel — pero a veces con un margen
  recortado a blanco por el escáner, que NO debe acusarse;
- dos firmas genuinas del mismo firmante: parecidas 0.10–0.50;
- una firma pegada y reutilizada: 0.90–1.00.
"""

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline import page_raster, pixel_forensics as px  # noqa: E402

ALTO, ANCHO = 1100, 850


def hoja(ruido=0.0, semilla=1):
    rng = np.random.default_rng(semilla)
    base = np.full((ALTO, ANCHO), 255.0)
    if ruido:
        base = 246.0 + rng.normal(0, ruido, (ALTO, ANCHO))
    # "Texto": renglones oscuros en el tercio central, con borde suavizado
    # como lo deja el antialias de cualquier render real (una página de solo
    # dos tonos exactos sería una imagen bitonal, que es otro caso).
    for y in range(300, 700, 28):
        base[y:y + 9, 120:730] = 40
        base[y - 1, 120:730] = 150
        base[y + 9, 120:730] = 190
    rgb = np.clip(base, 0, 255).astype(np.uint8)
    return np.repeat(rgb[:, :, None], 3, axis=2)


def garabato(semilla, alto=200, ancho=500, grosor=3):
    """Un trazo tipo firma: caminata aleatoria suave, distinta por semilla."""
    rng = np.random.default_rng(semilla)
    lienzo = np.full((alto, ancho, 3), 245, dtype=np.uint8)
    y = alto / 2
    for x in range(30, ancho - 30):
        y = float(np.clip(y + rng.normal(0, 6), 25, alto - 25))
        lienzo[int(y) - grosor:int(y) + grosor, x - 1:x + 2] = 25
    return lienzo


def test_una_exportacion_digital_no_tiene_ruido_de_captura():
    assert px.background_stats(hoja(ruido=0.0))["naturalBackground"] is False


def test_una_captura_real_tiene_ruido_de_captura():
    stats = px.background_stats(hoja(ruido=2.5))

    assert stats["naturalBackground"] is True
    assert stats["noiseStdP90"] > 1.0


def test_un_margen_recortado_a_blanco_por_el_escaner_no_acusa_a_la_captura():
    pagina = hoja(ruido=2.5)
    pagina[760:, :, :] = 255  # el escáner "limpió" todo el tercio inferior

    assert px.background_stats(pagina)["naturalBackground"] is True


def test_sin_suficiente_area_en_blanco_no_se_opina():
    llena = np.full((ALTO, ANCHO, 3), 60, dtype=np.uint8)

    assert px.background_stats(llena)["naturalBackground"] is None


def test_una_imagen_bitonal_no_se_juzga():
    pagina = hoja(ruido=0.0)
    pagina[pagina < 128] = 0
    pagina[pagina >= 128] = 255

    assert px.is_bilevel(pagina) is True
    assert px.background_stats(pagina)["naturalBackground"] is None


def test_la_pagina_sin_tinta_se_reporta_en_blanco():
    blanca = np.full((ALTO, ANCHO, 3), 250, dtype=np.uint8)

    assert px.background_stats(blanca)["blank"] is True


def test_la_misma_tinta_da_similitud_casi_total_aunque_cambie_el_recorte():
    firma = garabato(7)
    holgado = np.full((260, 580, 3), 245, dtype=np.uint8)
    holgado[30:230, 40:540] = firma  # mismo trazo, recorte más amplio

    similitud = px.grid_similarity(px.fingerprint(firma), px.fingerprint(holgado))

    assert similitud >= 0.97


def test_dos_firmas_distintas_quedan_lejos_del_umbral_de_rechazo():
    similitud = px.grid_similarity(px.fingerprint(garabato(7)), px.fingerprint(garabato(8)))

    assert similitud < 0.85


def test_sin_tinta_no_hay_huella():
    vacio = np.full((200, 500, 3), 245, dtype=np.uint8)

    assert px.fingerprint(vacio) is None
    assert px.grid_similarity(None, px.fingerprint(garabato(1))) == 0.0


def test_la_tinta_de_color_se_separa_de_la_negra():
    """Sello azul estampado ENCIMA de una firma negra: cada uno su huella."""
    recorte = garabato(3)
    recorte[60:140, 150:350] = (40, 60, 200)  # bloque azul sobre el trazo

    oscuro, color = px.split_ink_by_colour(recorte)

    assert color[100, 250] and not oscuro[100, 250]
    assert px.fingerprint(recorte, ink="colour") != px.fingerprint(recorte, ink="dark")
    assert px.dominant_ink(recorte) == "COLOR"


def test_el_bbox_ajustado_ignora_motas_sueltas():
    mascara = np.zeros((200, 400), dtype=bool)
    mascara[80:120, 100:300] = True
    mascara[5, 390] = True  # una mota de polvo en la esquina

    assert px.tight_ink_bbox(mascara) == (100, 80, 300, 120)


def test_recorte_normalizado_con_holgura_y_tope_en_los_bordes():
    imagen = np.zeros((100, 200, 3), dtype=np.uint8)

    assert page_raster.crop(imagen, [0.25, 0.25, 0.75, 0.75]).shape[:2] == (50, 100)
    assert page_raster.crop(imagen, [0.9, 0.9, 1.0, 1.0], pad=2.0).shape[:2] == (30, 60)
    assert page_raster.crop(imagen, [0.5, 0.5, 0.5, 0.5]) is None
    assert page_raster.crop(imagen, ["x", 0, 1, 1]) is None


def test_jpeg_se_reduce_al_borde_maximo_y_una_png_con_alfa_se_aplana_en_blanco():
    pytest.importorskip("PIL")
    import io

    from PIL import Image

    jpeg = page_raster.encode_jpeg(hoja(ruido=2.0), max_edge=500)
    assert max(Image.open(io.BytesIO(jpeg)).size) == 500

    transparente = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    buf = io.BytesIO()
    transparente.save(buf, format="PNG")
    assert int(page_raster.decode_image(buf.getvalue()).min()) == 255
