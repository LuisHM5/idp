"""Segmentación por ventanas de páginas (`pipeline/segmenter.py`).

Ningún `max_tokens`, por generoso que sea, tiene techo infinito: un
expediente lo bastante grande (ya hay 86 páginas en producción, y un tipo
con `page_selection` sube el tope de páginas de 50 a 500 —
`expediente._MAX_PAGES_WITH_SELECTION`) siempre puede en principio producir
más documentos de los que caben en una sola respuesta. Por encima de un
umbral de páginas, `segment_pages` reparte la clasificación en ventanas
solapadas, clasifica cada una y fusiona los resultados — ver el docstring de
`_merge_window_segments` para el algoritmo de fusión.

Estos tests cubren la geometría de las ventanas, el algoritmo de fusión (el
corazón del mecanismo) y el camino end-to-end, aislado de la lógica de
reintento por respuesta degradada que ya cubre `test_segmenter_candidates.py`.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline import bedrock, segmenter  # noqa: E402
from pipeline.pdf_pages import PageText  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_bedrock_state():
    bedrock.reset_used_models()
    yield
    bedrock.reset_used_models()


def _page(number: int, text: str = "texto de la pagina") -> PageText:
    return PageText(number=number, text=text, source="native", confidence=0.98)


def _seg(doc_type, start, end, confidence=0.9, subtype=None):
    return segmenter.Segment(doc_type, start, end, confidence, subtype)


# --- _build_windows ----------------------------------------------------------


def test_build_windows_geometria_exacta_86_25_3():
    pages = [_page(n) for n in range(1, 87)]
    windows = segmenter._build_windows(pages, window_size=25, overlap=3)
    ranges = [(w[0].number, w[-1].number) for w in windows]
    assert ranges == [(1, 25), (23, 47), (45, 69), (67, 86)]


def test_build_windows_total_por_debajo_del_tamano_de_ventana_da_una_sola():
    pages = [_page(n) for n in range(1, 11)]
    windows = segmenter._build_windows(pages, window_size=25, overlap=3)
    assert len(windows) == 1
    assert [p.number for p in windows[0]] == list(range(1, 11))


def test_build_windows_overlap_mayor_o_igual_al_tamano_no_entra_en_loop_infinito():
    pages = [_page(n) for n in range(1, 101)]
    windows = segmenter._build_windows(pages, window_size=10, overlap=10)
    assert len(windows) > 1
    assert windows[-1][-1].number == 100


def test_build_windows_respeta_el_tope_duro_de_numero_de_ventanas(monkeypatch):
    monkeypatch.setenv("SEGMENTER_MAX_WINDOWS", "3")
    pages = [_page(n) for n in range(1, 201)]
    windows = segmenter._build_windows(pages, window_size=25, overlap=3)
    assert len(windows) <= 3
    assert windows[-1][-1].number == 200


# --- _should_window ------------------------------------------------------------


def test_should_window_default_threshold_40():
    assert segmenter._should_window(31) is False  # el caso real reportado
    assert segmenter._should_window(40) is False
    assert segmenter._should_window(41) is True


def test_should_window_respeta_env_var(monkeypatch):
    monkeypatch.setenv("SEGMENTER_WINDOW_THRESHOLD", "10")
    assert segmenter._should_window(11) is True
    assert segmenter._should_window(10) is False


# --- _merge_window_segments ----------------------------------------------------


def test_merge_mismo_tipo_tocando_el_solape_se_fusiona():
    pages = [_page(n) for n in range(1, 26)]
    windows = [pages[0:25], pages[22:]]  # ventana 2 empieza en la pagina 23 (overlap 3)
    window_segments = [
        [_seg("FACTURA_FLETE_EXTRANJERO", 20, 25, confidence=0.8)],
        [_seg("FACTURA_FLETE_EXTRANJERO", 23, 30, confidence=0.9)],
    ]
    merged = segmenter._merge_window_segments(window_segments, windows)
    assert [(s.doc_type, s.page_start, s.page_end) for s in merged] == [
        ("FACTURA_FLETE_EXTRANJERO", 20, 30)
    ]
    assert merged[0].confidence == 0.9  # se queda con la mayor
    assert merged[0].raw.get("windowMerged") is True


def test_merge_mismo_tipo_sin_tocar_el_solape_no_se_fusiona():
    """Hueco real entre ambos segmentos -> _sanitize, que corre después, es
    quien lo rellena; esta función no inventa cobertura."""
    pages = [_page(n) for n in range(1, 26)]
    windows = [pages[0:25], pages[22:]]
    window_segments = [
        [_seg("PEDIMENTO_ADUANAL", 1, 10)],
        [_seg("PEDIMENTO_ADUANAL", 24, 25)],
    ]
    merged = segmenter._merge_window_segments(window_segments, windows)
    assert [(s.doc_type, s.page_start, s.page_end) for s in merged] == [
        ("PEDIMENTO_ADUANAL", 1, 10),
        ("PEDIMENTO_ADUANAL", 24, 25),
    ]


def test_merge_tipos_distintos_en_el_solape_no_se_fusionan():
    """Un desacuerdo genuino entre ventanas se deja tal cual: _sanitize
    resuelve cualquier solape remanente recortando el más tardío."""
    pages = [_page(n) for n in range(1, 26)]
    windows = [pages[0:25], pages[22:]]
    window_segments = [
        [_seg("PEDIMENTO_ADUANAL", 20, 25)],
        [_seg("FACTURA_HONORARIOS", 23, 30)],
    ]
    merged = segmenter._merge_window_segments(window_segments, windows)
    assert [(s.doc_type, s.page_start, s.page_end) for s in merged] == [
        ("PEDIMENTO_ADUANAL", 20, 25),
        ("FACTURA_HONORARIOS", 23, 30),
    ]


def test_merge_encadena_para_un_documento_que_cruza_3_ventanas():
    pages = [_page(n) for n in range(1, 71)]
    windows = [pages[0:25], pages[22:47], pages[44:70]]
    window_segments = [
        [_seg("PEDIMENTO_ADUANAL", 1, 25)],
        [_seg("PEDIMENTO_ADUANAL", 23, 47)],
        [_seg("PEDIMENTO_ADUANAL", 45, 70)],
    ]
    merged = segmenter._merge_window_segments(window_segments, windows)
    assert [(s.doc_type, s.page_start, s.page_end) for s in merged] == [
        ("PEDIMENTO_ADUANAL", 1, 70)
    ]


def test_merge_no_regresion_facturas_consecutivas_de_la_misma_ventana_quedan_separadas():
    """Dos invoices consecutivos que el MISMO modelo (misma ventana) ya
    distinguió nunca deben compararse entre sí — solo el ÚLTIMO segmento
    acumulado contra el PRIMERO de la ventana siguiente se compara."""
    pages = [_page(n) for n in range(1, 6)]
    windows = [pages]  # una sola ventana: nunca hay "siguiente" con quien fusionar
    window_segments = [
        [
            _seg("FACTURA_FLETE_EXTRANJERO", 1, 1),
            _seg("FACTURA_FLETE_EXTRANJERO", 2, 2),
            _seg("FACTURA_FLETE_EXTRANJERO", 3, 3),
        ]
    ]
    merged = segmenter._merge_window_segments(window_segments, windows)
    assert [(s.doc_type, s.page_start, s.page_end) for s in merged] == [
        ("FACTURA_FLETE_EXTRANJERO", 1, 1),
        ("FACTURA_FLETE_EXTRANJERO", 2, 2),
        ("FACTURA_FLETE_EXTRANJERO", 3, 3),
    ]


def test_merge_vacio_da_vacio():
    assert segmenter._merge_window_segments([], []) == []


# --- _classify_window: red de seguridad ante ClientError -----------------------


def test_classify_window_client_error_marca_solo_esa_ventana_como_otro(monkeypatch):
    pages = [_page(n) for n in range(5, 9)]  # páginas 5-8

    def explode(*_a, **_k):
        raise ClientError(
            {"Error": {"Code": "ModelErrorException", "Message": "boom"}}, "Converse"
        )

    monkeypatch.setattr(segmenter, "_classify_pages", explode)
    monkeypatch.setattr(segmenter, "resolve_model", lambda _stage: "fake-model")

    segments, usage = segmenter._classify_window(pages, None, None)

    assert [(s.doc_type, s.page_start, s.page_end) for s in segments] == [("OTRO", 5, 8)]
    assert usage == {}
    degradations = bedrock.stage_degradations()["segmenter"]
    assert degradations[0]["reason"] == "client_error"
    assert degradations[0]["resolution"] == "marked_otro"


# --- _classify_windowed end-to-end ----------------------------------------------


def test_classify_windowed_end_to_end_fusiona_ventanas_y_agrega_usage(monkeypatch):
    monkeypatch.setenv("SEGMENTER_WINDOW_WORKERS", "1")  # orden determinístico
    pages = [_page(n) for n in range(1, 51)]  # 50 páginas
    windows_seen = []

    def fake_classify_pages(window_pages, allowed, model_id):
        start, end = window_pages[0].number, window_pages[-1].number
        windows_seen.append((start, end))
        # El modelo "ve" el mismo documento PEDIMENTO_ADUANAL cubriendo toda
        # la ventana en cada una -> deben encadenar en un solo segmento.
        return (
            [{"type": "PEDIMENTO_ADUANAL", "page_start": start, "page_end": end, "confidence": 0.9}],
            {"modelId": "modelo-a", "usage": {"inputTokens": 100, "outputTokens": 50}},
        )

    monkeypatch.setattr(segmenter, "_classify_pages", fake_classify_pages)

    segments, usage = segmenter._classify_windowed(pages, ["PEDIMENTO_ADUANAL"], None)

    assert len(windows_seen) > 1  # sí ventaneó
    # Todas las ventanas reportaron el mismo doc_type tocando el solape ->
    # una fusión encadenada en un solo segmento que cubre todo el documento.
    assert len(segments) == 1
    assert segments[0].page_start == 1
    assert segments[0].page_end == 50
    # Usage de TODAS las ventanas, no solo la primera.
    assert usage["modelId"] == "modelo-a"
    assert usage["usage"]["inputTokens"] == 100 * len(windows_seen)


def test_classify_windowed_perModel_cuando_las_ventanas_usan_modelos_distintos(monkeypatch):
    monkeypatch.setenv("SEGMENTER_WINDOW_WORKERS", "1")
    pages = [_page(n) for n in range(1, 51)]

    def fake_classify_pages(window_pages, allowed, model_id):
        start = window_pages[0].number
        used = "modelo-a" if start == 1 else "modelo-b"
        return (
            [{"type": "OTRA_COSA", "page_start": start, "page_end": start, "confidence": 0.9}],
            {"modelId": used, "usage": {"inputTokens": 10, "outputTokens": 5}},
        )

    monkeypatch.setattr(segmenter, "_classify_pages", fake_classify_pages)

    _segments, usage = segmenter._classify_windowed(pages, None, None)

    assert "," in usage["modelId"]
    assert {m["modelId"] for m in usage["perModel"]} == {"modelo-a", "modelo-b"}
