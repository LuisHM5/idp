"""Análisis de nivel de error (ELA) en `pixel_forensics.error_level_stats`.

Lo que se garantiza: una página JPEG genuina (comprimida una vez, con texto)
sale UNIFORME — acusar a un documento genuino es el error más caro — y todo lo
que no es un JPEG con contenido suficiente se ABSTIENE. Que un parche real se
detecte depende de la imagen y no está calibrado con archivos reales: por eso
el preset lo deja en `on_fail: info`.
"""

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src/document-processing/resources/enrichment-lambda"))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from pipeline import pixel_forensics as px  # noqa: E402


def _pagina_jpeg(calidad=75, ruido=8, semilla=1) -> bytes:
    rng = np.random.default_rng(semilla)
    fondo = np.full((1100, 850, 3), 245, np.uint8) + rng.integers(0, ruido, (1100, 850, 3)).astype(np.uint8)
    img = Image.fromarray(fondo)
    dibujo = ImageDraw.Draw(img)
    for y in range(100, 1000, 30):
        dibujo.text((80, y), "Por medio de la presente hacemos constar que la persona es empleada", fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=calidad)
    return buf.getvalue()


def test_una_pagina_genuina_es_uniforme_a_cualquier_calidad():
    for calidad in (50, 75, 90):
        stats = px.error_level_stats(_pagina_jpeg(calidad))
        assert stats["errorLevelUniform"] is True, (calidad, stats)
        assert stats["blocks"] >= 20
        assert abs(stats["estimatedQuality"] - calidad) <= 5


def test_un_png_no_se_analiza():
    buf = io.BytesIO()
    Image.fromarray(np.full((400, 400, 3), 255, np.uint8)).save(buf, "PNG")

    stats = px.error_level_stats(buf.getvalue())

    assert stats == {"errorLevelUniform": None, "reason": "not_jpeg"}


def test_una_imagen_chica_o_vacia_se_abstiene():
    buf = io.BytesIO()
    Image.fromarray(np.full((64, 64, 3), 200, np.uint8)).save(buf, "JPEG")
    assert px.error_level_stats(buf.getvalue())["errorLevelUniform"] is None

    buf = io.BytesIO()
    Image.fromarray(np.full((600, 600, 3), 255, np.uint8)).save(buf, "JPEG")   # sin contenido
    assert px.error_level_stats(buf.getvalue())["errorLevelUniform"] is None

    assert px.error_level_stats(b"")["errorLevelUniform"] is None


def test_el_umbral_es_configurable():
    stats = px.error_level_stats(_pagina_jpeg(), {"ela_max_ratio": 0.5})

    assert stats["errorLevelUniform"] is False
    assert stats["peakBlock"] and len(stats["peakBlock"]) == 4
