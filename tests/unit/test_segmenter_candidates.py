"""Clasificación guiada: qué tipos puede elegir el segmentador.

Tres modos, y lo que se protege aquí es la frontera entre ellos:

- **un** tipo declarado -> ni siquiera se llama al modelo (`_declared_segments`);
- **varios** -> el modelo SÍ corre, pero solo puede elegir entre esos tipos, y
  cualquier otra cosa que conteste se degrada a OTRO;
- **ninguno** -> candidatos por FORMA del catálogo (`is_segmentation_candidate`),
  no por el flag `pipeline: "expediente"`, que dejaba fuera a
  `PEDIMENTO_ADUANAL` de la clasificación automática.

El guard estricto es lo que hace que "lista cerrada" sea verdad y no solo una
frase del prompt: la respuesta del modelo se toma verbatim en todo lo demás.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

import document_catalog  # noqa: E402
from document_catalog import _parse_entry, normalize_declared_types  # noqa: E402
from pipeline import bedrock, expediente, segmenter  # noqa: E402
from pipeline.pdf_pages import PageText  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_bedrock_state():
    bedrock.reset_used_models()
    yield
    bedrock.reset_used_models()


def _page(number: int, text: str = "texto de la pagina") -> PageText:
    return PageText(number=number, text=text, source="native", confidence=0.98)


def _entry(type_id: str, description: str = "Un documento cualquiera.", **extra):
    return _parse_entry(type_id, {"fields": {}, "description": description, **extra})


@pytest.fixture
def fake_catalog(monkeypatch):
    """Catálogo mínimo, para no atar estos tests a los tipos reales."""
    catalog = {
        "PEDIMENTO_ADUANAL": _entry("PEDIMENTO_ADUANAL", "Pedimento: NUM. PEDIMENTO, sin UUID."),
        "FACTURA_HONORARIOS": _entry("FACTURA_HONORARIOS", "CFDI de honorarios, con UUID."),
        "OTRA_COSA": _entry("OTRA_COSA", "Un tercer tipo que nadie declaró."),
    }
    monkeypatch.setattr(segmenter, "get_all_types", lambda: catalog)
    return catalog


def _stub_model(monkeypatch, documents):
    """Sustituye la llamada a Bedrock y captura el prompt que habría recibido."""
    seen = {}

    def fake_converse(prompt, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"documents": documents}), {"modelId": "fake-model", "usage": {"in": 1}}

    monkeypatch.setattr(segmenter, "converse", fake_converse)
    monkeypatch.setattr(segmenter, "resolve_model", lambda _stage: "fake-model")
    return seen


def _stub_model_sequence(monkeypatch, responses):
    """Como `_stub_model`, pero una respuesta DISTINTA por llamada, en orden.
    Cada entrada es `(documents_or_None, meta_extra)`: `documents_or_None=None`
    simula una respuesta vacía (texto ""); si no, se serializa a
    `{"documents": [...]}`. `meta_extra` se mezcla sobre
    `{"modelId": kwargs["model_id"], "usage": {}}`. Agotar la lista dispara
    AssertionError — una llamada de más es tan un bug como una de menos."""
    calls = []
    remaining = list(responses)

    def fake_converse(prompt, **kwargs):
        calls.append(kwargs)
        if not remaining:
            raise AssertionError(f"llamada inesperada a converse(): {kwargs}")
        documents, meta_extra = remaining.pop(0)
        meta = {"modelId": kwargs.get("model_id"), "usage": {}, **meta_extra}
        text = "" if documents is None else json.dumps({"documents": documents})
        return text, meta

    monkeypatch.setattr(segmenter, "converse", fake_converse)
    return calls


# --- normalize_declared_types ---------------------------------------------


def test_normalize_accepts_a_bare_string_without_iterating_its_characters():
    """Un `str` cumple `Sequence[str]`: sin este guard, pasar "PEDIMENTO" en el
    slot de la lista buscaría tipos llamados "P", "E", "D"..."""
    assert normalize_declared_types("PEDIMENTO_ADUANAL") == ["PEDIMENTO_ADUANAL"]


def test_normalize_dedupes_trims_and_keeps_order():
    assert normalize_declared_types([" B ", "A", "B", "", None]) == ["B", "A"]


def test_normalize_falls_back_to_declared_type():
    assert normalize_declared_types(None, "PEDIMENTO_ADUANAL") == ["PEDIMENTO_ADUANAL"]
    assert normalize_declared_types(None, None) == []


def test_occurrence_mode_se_parsea_y_un_valor_roto_equivale_a_ausente():
    assert _entry("UNO").occurrence_mode is None
    assert _entry("VARIOS", occurrence_mode="multiple").occurrence_mode == "multiple"
    assert _entry("ROTO", occurrence_mode="muchos").occurrence_mode is None


def test_cardinalidad_resuelve_request_catalogo_y_default_global(monkeypatch):
    entries = {
        "CAT_SINGLE": _entry("CAT_SINGLE", occurrence_mode="single"),
        "CAT_MULTIPLE": _entry("CAT_MULTIPLE", occurrence_mode="multiple"),
        "SIN_DEFAULT": _entry("SIN_DEFAULT"),
    }
    monkeypatch.setattr(
        document_catalog, "get_type_entry", lambda type_id: entries[type_id]
    )

    # Catálogo pisa el default global en ambas direcciones.
    assert document_catalog.resolve_declared_counts(["CAT_MULTIPLE"], {}) == {
        "CAT_MULTIPLE": "all"
    }
    assert document_catalog.resolve_declared_counts(["SIN_DEFAULT"], {}) == {
        "SIN_DEFAULT": 1
    }
    assert document_catalog.resolve_declared_counts(
        ["CAT_SINGLE", "SIN_DEFAULT"], {}
    ) == {"CAT_SINGLE": 1, "SIN_DEFAULT": "all"}
    # Lo materializado por el POST (:N/:all u occurrenceMode) pisa catálogo.
    assert document_catalog.resolve_declared_counts(
        ["CAT_SINGLE", "CAT_MULTIPLE"],
        {"CAT_SINGLE": "all", "CAT_MULTIPLE": 1},
    ) == {"CAT_SINGLE": "all", "CAT_MULTIPLE": 1}


# --- lista de candidatos ---------------------------------------------------


def test_declared_list_is_the_candidate_list_verbatim(fake_catalog):
    """Sin ningún gate: declarar un tipo es señal más fuerte que cualquier
    default del catálogo. Es lo que permite pedir PEDIMENTO_ADUANAL, que no
    lleva `pipeline: "expediente"`."""
    candidates = segmenter._candidate_types(["FACTURA_HONORARIOS", "PEDIMENTO_ADUANAL"])
    assert list(candidates) == ["FACTURA_HONORARIOS", "PEDIMENTO_ADUANAL"]


def test_a_declared_type_the_catalog_does_not_know_is_ignored(fake_catalog):
    assert list(segmenter._candidate_types(["PEDIMENTO_ADUANAL", "NO_EXISTE"])) == [
        "PEDIMENTO_ADUANAL"
    ]


def test_without_a_declared_list_candidacy_is_by_catalog_shape(monkeypatch, fake_catalog):
    monkeypatch.setattr(
        segmenter, "is_segmentation_candidate", lambda t: t != "OTRA_COSA"
    )
    assert set(segmenter._candidate_types()) == {"PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"}


# --- el prompt -------------------------------------------------------------


def test_the_prompt_only_offers_the_declared_types(fake_catalog):
    prompt = segmenter._build_prompt([_page(1)], allowed=["PEDIMENTO_ADUANAL"])
    assert "Tipos posibles:\n- PEDIMENTO_ADUANAL:" in prompt
    assert "FACTURA_HONORARIOS" not in prompt
    assert "OTRA_COSA" not in prompt
    assert "es CERRADA" in prompt


def test_disambiguation_rules_are_filtered_to_the_candidates(fake_catalog):
    prompt = segmenter._build_prompt([_page(1)], allowed=["PEDIMENTO_ADUANAL"])
    # La regla del pedimento sí, porque es candidato.
    assert "NUM. PEDIMENTO / T. OPER" in prompt
    # Las de tipos que viven en catalogs/save/ y no están en el catálogo, no.
    for parked in ("CFDI_GASTO_TERCERO", "FACTURA_AGENCIA_ADUANAL", "FACTURA_LOGISTICA"):
        assert parked not in prompt
    # La regla genérica (sin tipo) se emite siempre.
    assert "clasifícalas como OTRO" in prompt


def test_the_open_prompt_says_nothing_about_a_closed_list(monkeypatch, fake_catalog):
    monkeypatch.setattr(segmenter, "is_segmentation_candidate", lambda _t: True)
    assert "es CERRADA" not in segmenter._build_prompt([_page(1)])


# --- segment_pages: los tres modos ----------------------------------------


def test_one_declared_type_never_calls_the_model(monkeypatch, fake_catalog):
    def explode(*_a, **_k):
        raise AssertionError("se llamó al modelo con un solo tipo declarado")

    monkeypatch.setattr(segmenter, "converse", explode)
    for kwargs in ({"declared_type": "PEDIMENTO_ADUANAL"},
                   {"declared_types": ["PEDIMENTO_ADUANAL"]}):
        segments, usage = segmenter.segment_pages([_page(1), _page(2)], **kwargs)
        assert usage == {}
        assert [(s.doc_type, s.page_start, s.page_end) for s in segments] == [
            ("PEDIMENTO_ADUANAL", 1, 2)
        ]


def test_several_declared_types_segment_and_classify_among_them(monkeypatch, fake_catalog):
    seen = _stub_model(monkeypatch, [
        {"type": "PEDIMENTO_ADUANAL", "page_start": 1, "page_end": 2, "confidence": 0.9},
        {"type": "FACTURA_HONORARIOS", "page_start": 3, "page_end": 3, "confidence": 0.8},
    ])
    segments, usage = segmenter.segment_pages(
        [_page(1), _page(2), _page(3)],
        declared_types=["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
    )
    assert usage["modelId"] == "fake-model"
    assert [(s.doc_type, s.page_start, s.page_end) for s in segments] == [
        ("PEDIMENTO_ADUANAL", 1, 2),
        ("FACTURA_HONORARIOS", 3, 3),
    ]
    assert "OTRA_COSA" not in seen["prompt"]


def test_a_type_outside_the_declared_list_is_demoted_to_otro(monkeypatch, fake_catalog):
    """El prompt dice que la lista es cerrada; ESTO es lo que la cierra.

    Las páginas se conservan como OTRO en vez de descartarse, para que la
    cobertura del archivo siga siendo auditable.
    """
    _stub_model(monkeypatch, [
        {"type": "PEDIMENTO_ADUANAL", "page_start": 1, "page_end": 1, "confidence": 0.9},
        {"type": "OTRA_COSA", "page_start": 2, "page_end": 2, "confidence": 0.7},
        {"type": "TIPO_ALUCINADO", "page_start": 3, "page_end": 3, "confidence": 0.6},
    ])
    segments, _ = segmenter.segment_pages(
        [_page(1), _page(2), _page(3)],
        declared_types=["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
    )
    assert [(s.doc_type, s.page_start) for s in segments] == [
        ("PEDIMENTO_ADUANAL", 1),
        ("OTRO", 2),
        ("OTRO", 3),
    ]


def test_without_a_declared_list_the_model_answer_is_still_taken_verbatim(
    monkeypatch, fake_catalog
):
    """El guard es exclusivo del camino declarado: en automático no hay lista
    del caller contra la cual cerrar, así que el comportamiento no cambia."""
    monkeypatch.setattr(segmenter, "is_segmentation_candidate", lambda _t: True)
    _stub_model(monkeypatch, [
        {"type": "OTRA_COSA", "page_start": 1, "page_end": 1, "confidence": 0.7},
    ])
    segments, _ = segmenter.segment_pages([_page(1)])
    assert [s.doc_type for s in segments] == ["OTRA_COSA"]


def test_the_container_type_forces_open_segmentation_not_an_empty_list(
    monkeypatch, fake_catalog
):
    """`documentType=EXPEDIENTE_ADUANAL_MULTIDOCUMENTO` sigue significando
    "segmenta sin acotar".

    Es una DIRECTIVA, no un candidato — ni siquiera está en el catálogo activo.
    Tratarla como candidato dejaría la lista vacía y el guard estricto mandaría
    TODO el archivo a OTRO.
    """
    monkeypatch.setattr(segmenter, "is_segmentation_candidate", lambda _t: True)
    seen = _stub_model(monkeypatch, [
        {"type": "OTRA_COSA", "page_start": 1, "page_end": 1, "confidence": 0.7},
    ])
    segments, _ = segmenter.segment_pages(
        [_page(1)], declared_type=segmenter.CONTAINER_TYPE
    )
    assert [s.doc_type for s in segments] == ["OTRA_COSA"]
    assert "es CERRADA" not in seen["prompt"]


def test_the_container_type_alongside_real_types_still_restricts(monkeypatch, fake_catalog):
    """Con la directiva Y tipos reales, los tipos reales mandan: la directiva
    solo se cae de la lista de candidatos."""
    _stub_model(monkeypatch, [
        {"type": "OTRA_COSA", "page_start": 1, "page_end": 1, "confidence": 0.7},
    ])
    segments, _ = segmenter.segment_pages(
        [_page(1)],
        declared_types=[segmenter.CONTAINER_TYPE, "PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
    )
    assert [s.doc_type for s in segments] == ["OTRO"]


# --- presupuesto de tokens y reintento ante truncado -----------------------
#
# Caso real reproducido en producción (2026-09-04): un PDF de 31 páginas
# declarando 2 tipos hizo que el modelo de segmentación agotara 4096 tokens
# de salida SIN escribir texto (log real: "unparseable model output (empty
# model response)"), colapsando TODO el archivo a un solo segmento OTRO y
# dejando ambos tipos declarados en REVIEW_REQUIRED. Ver
# documentacion/COSTOS.md §3.1 y la memoria del proyecto para el contexto
# completo del bug.


def test_segmenter_max_tokens_escala_con_paginas_pero_nunca_baja_de_8192():
    assert segmenter._segmenter_max_tokens(1) == 8192
    assert segmenter._segmenter_max_tokens(5) == 8192
    # 512 + 31*90 = 3302 -> sigue por debajo del piso de 8192.
    assert segmenter._segmenter_max_tokens(31) == 8192
    # 512 + 86*90 = 8252 -> aquí sí escala por encima del piso.
    assert segmenter._segmenter_max_tokens(86) == 8252


def test_segmenter_max_tokens_respeta_el_techo_configurable(monkeypatch):
    # 512 + 86*90 = 8252 -> por encima de un techo de 8000, se recorta.
    monkeypatch.setenv("SEGMENTER_MAX_TOKENS_CEILING", "8000")
    assert segmenter._segmenter_max_tokens(86) == 8000
    monkeypatch.delenv("SEGMENTER_MAX_TOKENS_CEILING")
    assert segmenter._segmenter_max_tokens(100000) == segmenter._SEGMENTER_MAX_TOKENS_CEILING_DEFAULT


def test_retry_model_id_es_el_ultimo_de_la_lista_de_fallback(monkeypatch):
    monkeypatch.setattr(segmenter, "get_fallback_models", lambda: ["a", "b", "haiku"])
    assert segmenter._retry_model_id("minimax") == "haiku"


def test_retry_model_id_es_none_si_el_que_fallo_ya_es_el_mejor(monkeypatch):
    monkeypatch.setattr(segmenter, "get_fallback_models", lambda: ["a", "b", "haiku"])
    assert segmenter._retry_model_id("haiku") is None


def test_regresion_bug_real_31_paginas_respuesta_vacia_se_recupera_con_reintento(
    monkeypatch, fake_catalog
):
    """La secuencia EXACTA del incidente real: primera llamada (minimax,
    truncada, texto vacío) -> segunda llamada (el modelo de respaldo,
    respuesta válida). El resultado final debe traer los documentos reales,
    no un OTRO cubriendo todo el archivo."""
    monkeypatch.setattr(segmenter, "resolve_model", lambda _stage: "minimax.minimax-m2.5")
    monkeypatch.setattr(
        segmenter, "get_fallback_models",
        lambda: ["minimax.minimax-m2.5", "us.anthropic.claude-haiku-5-5"],
    )
    calls = _stub_model_sequence(monkeypatch, [
        (None, {"modelId": "minimax.minimax-m2.5", "stopReason": "max_tokens", "usage": {"outputTokens": 4096}}),
        (
            [
                {"type": "PEDIMENTO_ADUANAL", "page_start": 1, "page_end": 20, "confidence": 0.9},
                {"type": "FACTURA_HONORARIOS", "page_start": 21, "page_end": 31, "confidence": 0.85},
            ],
            {"modelId": "us.anthropic.claude-haiku-5-5", "stopReason": "end_turn", "usage": {"outputTokens": 900}},
        ),
    ])

    pages = [_page(n) for n in range(1, 32)]
    segments, usage = segmenter.segment_pages(
        pages, declared_types=["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
    )

    assert len(calls) == 2
    # El presupuesto pedido ya no es el 4096 fijo que causó el truncado.
    assert calls[0]["max_tokens"] >= 8192
    assert calls[1]["model_id"] == "us.anthropic.claude-haiku-5-5"
    assert [(s.doc_type, s.page_start, s.page_end) for s in segments] == [
        ("PEDIMENTO_ADUANAL", 1, 20),
        ("FACTURA_HONORARIOS", 21, 31),
    ]
    degradations = bedrock.stage_degradations()["segmenter"]
    assert len(degradations) == 1
    assert degradations[0]["resolution"] == "recovered_on_retry"
    assert degradations[0]["reason"] == "empty_response"


def test_truncado_persistente_en_ambos_intentos_cae_a_otro_tras_exactamente_2_llamadas(
    monkeypatch, fake_catalog
):
    monkeypatch.setattr(segmenter, "resolve_model", lambda _stage: "minimax.minimax-m2.5")
    monkeypatch.setattr(
        segmenter, "get_fallback_models",
        lambda: ["minimax.minimax-m2.5", "us.anthropic.claude-haiku-5-5"],
    )
    calls = _stub_model_sequence(monkeypatch, [
        (None, {"modelId": "minimax.minimax-m2.5", "stopReason": "max_tokens", "usage": {"outputTokens": 4096}}),
        (None, {"modelId": "us.anthropic.claude-haiku-5-5", "stopReason": "max_tokens", "usage": {"outputTokens": 8192}}),
    ])

    segments, _usage = segmenter.segment_pages(
        [_page(1), _page(2)], declared_types=["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
    )

    assert len(calls) == 2
    assert [s.doc_type for s in segments] == ["OTRO"]
    degradations = bedrock.stage_degradations()["segmenter"]
    assert len(degradations) == 1
    assert degradations[0]["resolution"] == "marked_otro"
    assert len(degradations[0]["attempts"]) == 2


def test_stop_reason_max_tokens_con_json_parseable_tambien_reintenta(
    monkeypatch, fake_catalog
):
    """No basta con que json.loads no truene: si Bedrock dice que se quedó
    sin presupuesto (`stopReason=max_tokens`), la lista de documentos puede
    estar incompleta aunque el JSON haya cerrado bien — también cuenta como
    degradado."""
    monkeypatch.setattr(segmenter, "resolve_model", lambda _stage: "minimax.minimax-m2.5")
    monkeypatch.setattr(
        segmenter, "get_fallback_models",
        lambda: ["minimax.minimax-m2.5", "us.anthropic.claude-haiku-5-5"],
    )
    calls = _stub_model_sequence(monkeypatch, [
        (
            [{"type": "PEDIMENTO_ADUANAL", "page_start": 1, "page_end": 1, "confidence": 0.9}],
            {"modelId": "minimax.minimax-m2.5", "stopReason": "max_tokens", "usage": {"outputTokens": 4096}},
        ),
        (
            [
                {"type": "PEDIMENTO_ADUANAL", "page_start": 1, "page_end": 1, "confidence": 0.9},
                {"type": "FACTURA_HONORARIOS", "page_start": 2, "page_end": 2, "confidence": 0.9},
            ],
            {"modelId": "us.anthropic.claude-haiku-5-5", "stopReason": "end_turn", "usage": {}},
        ),
    ])

    segments, _usage = segmenter.segment_pages(
        [_page(1), _page(2)], declared_types=["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
    )

    assert len(calls) == 2
    assert [s.doc_type for s in segments] == ["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"]


def test_un_solo_tipo_declarado_sigue_sin_llamar_al_modelo_con_el_nuevo_codigo(
    monkeypatch, fake_catalog
):
    """No-regresión explícita: el refactor de segment_pages para el reintento
    y el ventaneo no debe tocar el camino de 1 tipo declarado."""
    def explode(*_a, **_k):
        raise AssertionError("se llamó al modelo con un solo tipo declarado")

    monkeypatch.setattr(segmenter, "converse", explode)
    segments, usage = segmenter.segment_pages(
        [_page(1), _page(2)], declared_types=["PEDIMENTO_ADUANAL"]
    )
    assert usage == {}
    assert [s.doc_type for s in segments] == ["PEDIMENTO_ADUANAL"]


# --- cuántos documentos de cada tipo (`TIPO:N`) -------------------------------


def _seg(doc_type, start, end):
    return segmenter.Segment(doc_type, start, end, 0.9)


def test_sin_cuenta_el_default_del_api_conserva_solo_el_primero():
    segs = [_seg("A", 1, 1), _seg("A", 2, 2), _seg("B", 3, 3)]
    for counts in (None, {}):
        out, dropped = segmenter._apply_type_limits(segs, counts)
        assert [s.doc_type for s in out] == ["A", "OTRO", "B"]
        assert dropped == {"A": 1}


def test_multiple_del_api_conserva_todas():
    segs = [_seg("A", 1, 1), _seg("A", 2, 2)]
    out, dropped = segmenter._apply_type_limits(segs, {"A": "all"})
    assert out == segs
    assert dropped == {}


def test_el_excedente_se_reetiqueta_OTRO_en_vez_de_borrarse():
    """La cobertura de páginas tiene que seguir completa: el documento de más
    no desaparece, se marca. Mismo patrón que COPIA_DUPLICADA."""
    segs = [_seg("FACTURA", 1, 1), _seg("FACTURA", 2, 2), _seg("FACTURA", 3, 3)]
    out, dropped = segmenter._apply_type_limits(segs, {"FACTURA": 1})
    assert [(s.doc_type, s.subtype, s.page_start) for s in out] == [
        ("FACTURA", None, 1),
        ("OTRO", "LIMITE_ALCANZADO", 2),
        ("OTRO", "LIMITE_ALCANZADO", 3),
    ]
    assert dropped == {"FACTURA": 2}
    # Ninguna página se perdió.
    assert [(s.page_start, s.page_end) for s in out] == [(1, 1), (2, 2), (3, 3)]


def test_el_limite_es_por_tipo_no_global():
    segs = [_seg("A", 1, 1), _seg("B", 2, 2), _seg("A", 3, 3), _seg("B", 4, 4)]
    out, dropped = segmenter._apply_type_limits(segs, {"A": 1, "B": 2})
    assert [s.doc_type for s in out] == ["A", "B", "OTRO", "B"]
    assert dropped == {"A": 1}


def test_override_all_gana_y_otro_tipo_usa_el_default_single():
    segs = [_seg("A", 1, 1), _seg("A", 2, 2), _seg("C", 3, 3), _seg("C", 4, 4)]
    out, dropped = segmenter._apply_type_limits(segs, {"A": "all"})
    assert [s.doc_type for s in out] == ["A", "A", "C", "OTRO"]
    assert dropped == {"C": 1}


def test_los_OTRO_nunca_cuentan_contra_el_limite():
    """Son páginas de relleno para que la cobertura sea auditable, no
    documentos; contarlos gastaría el cupo del tipo real."""
    segs = [_seg("OTRO", 1, 1), _seg("A", 2, 2), _seg("OTRO", 3, 3), _seg("A", 4, 4)]
    out, _ = segmenter._apply_type_limits(segs, {"A": 2})
    assert [s.doc_type for s in out] == ["OTRO", "A", "OTRO", "A"]


def test_un_tipo_declarado_con_limite_recorta_las_instancias(monkeypatch, fake_catalog):
    """Camino de UN tipo: `_declared_segments` parte por `multi_instance` y el
    límite se aplica encima, sin llamar al modelo."""
    def explode(*_a, **_k):
        raise AssertionError("se llamó al modelo con un solo tipo declarado")

    monkeypatch.setattr(segmenter, "converse", explode)
    monkeypatch.setattr(
        segmenter, "_declared_segments",
        lambda pages, t, total, **_kwargs: [_seg(t, 1, 1), _seg(t, 2, 2), _seg(t, 3, 3)],
    )
    segments, usage = segmenter.segment_pages(
        [_page(1), _page(2), _page(3)],
        declared_type="PEDIMENTO_ADUANAL",
        declared_counts={"PEDIMENTO_ADUANAL": 2},
    )
    assert usage == {}
    assert [s.doc_type for s in segments] == ["PEDIMENTO_ADUANAL", "PEDIMENTO_ADUANAL", "OTRO"]


def test_el_limite_tambien_aplica_en_el_camino_guiado(monkeypatch, fake_catalog):
    _stub_model(monkeypatch, [
        {"type": "PEDIMENTO_ADUANAL", "page_start": 1, "page_end": 1, "confidence": 0.9},
        {"type": "FACTURA_HONORARIOS", "page_start": 2, "page_end": 2, "confidence": 0.9},
        {"type": "FACTURA_HONORARIOS", "page_start": 3, "page_end": 3, "confidence": 0.9},
    ])
    segments, _ = segmenter.segment_pages(
        [_page(1), _page(2), _page(3)],
        declared_types=["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
        declared_counts={"PEDIMENTO_ADUANAL": 1, "FACTURA_HONORARIOS": 1},
    )
    assert [(s.doc_type, s.subtype) for s in segments] == [
        ("PEDIMENTO_ADUANAL", None),
        ("FACTURA_HONORARIOS", None),
        ("OTRO", "LIMITE_ALCANZADO"),
    ]


def test_camino_guiado_vuelve_a_partir_un_rango_multiple_mal_agrupado(monkeypatch):
    """El modelo reconoce el tipo, pero junta dos invoices consecutivos. Los
    límites del catálogo son la autoridad antes de extraer."""
    spec = {
        "enabled": True,
        "min_score": 1,
        "markers": [r"INVOICE"],
        "start_markers": [r"INVOICE\s+#"],
        "multi_instance": {"follow_pages": 0},
    }
    invoice = _entry("INVOICE", page_selection=spec)
    other = _entry("OTHER")
    monkeypatch.setattr(
        segmenter,
        "get_type_entry",
        lambda type_id: invoice if type_id == "INVOICE" else other,
    )
    _stub_model(monkeypatch, [
        {"type": "INVOICE", "page_start": 1, "page_end": 2, "confidence": 0.9},
    ])

    segments, _ = segmenter.segment_pages(
        [_page(1, "INVOICE # A100"), _page(2, "INVOICE # B200")],
        declared_types=["INVOICE", "OTHER"],
        declared_counts={"INVOICE": "all", "OTHER": 1},
    )

    assert [(s.doc_type, s.page_start, s.page_end) for s in segments] == [
        ("INVOICE", 1, 1),
        ("INVOICE", 2, 2),
    ]


# --- salida cerrada y marcadores de faltantes -----------------------------


def test_una_solicitud_cerrada_descarta_todos_los_segmentos_otro():
    segments = [
        _seg("OTRO", 1, 1),
        _seg("PEDIMENTO_ADUANAL", 2, 4),
        segmenter.Segment("OTRO", 5, 5, 0.0, "COPIA_DUPLICADA"),
        _seg("FACTURA_HONORARIOS", 6, 6),
    ]
    kept = expediente._only_requested_segments(
        segments, ["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"]
    )
    assert [(s.doc_type, s.page_start, s.page_end) for s in kept] == [
        ("PEDIMENTO_ADUANAL", 2, 4),
        ("FACTURA_HONORARIOS", 6, 6),
    ]


def test_sin_tipos_reales_solicitados_se_conserva_la_cobertura_completa():
    segments = [_seg("OTRO", 1, 1), _seg("A", 2, 2)]
    assert expediente._only_requested_segments(segments, []) == segments


def test_un_marcador_por_cada_ocurrencia_finita_faltante():
    documents = [{"documentType": "A"}, {"documentType": "B"}]
    markers = expediente._missing_requested_documents(
        documents, ["A", "B"], {"A": 3, "B": 1}
    )
    assert markers == [
        {
            "documentType": "A",
            "pageStart": None,
            "pageEnd": None,
            "status": "REVIEW_REQUIRED",
            "confidence": None,
            "fields": {},
        },
        {
            "documentType": "A",
            "pageStart": None,
            "pageEnd": None,
            "status": "REVIEW_REQUIRED",
            "confidence": None,
            "fields": {},
        },
    ]


def test_all_solo_marca_un_tipo_cuando_no_aparecio_ninguna_vez():
    documents = [{"documentType": "A"}, {"documentType": "A"}]
    markers = expediente._missing_requested_documents(
        documents, ["A", "B"], {"A": "all", "B": "all"}
    )
    assert [marker["documentType"] for marker in markers] == ["B"]


def test_defaults_directos_son_single_si_no_hay_metadata_del_api():
    assert expediente._missing_requested_documents([], ["A"], None) == [
        {
            "documentType": "A",
            "pageStart": None,
            "pageEnd": None,
            "status": "REVIEW_REQUIRED",
            "confidence": None,
            "fields": {},
        }
    ]
    assert [
        marker["documentType"]
        for marker in expediente._missing_requested_documents(
            [{"documentType": "A"}, {"documentType": "A"}], ["A", "B"], None
        )
    ] == ["B"]


def test_process_pdf_extrae_solo_solicitados_y_agrega_el_faltante(monkeypatch):
    pages = [_page(1), _page(2), _page(3)]
    classified = [
        _seg("A", 1, 1),
        _seg("A", 2, 2),
        segmenter.Segment("OTRO", 3, 3, 0.0, "DODA"),
    ]
    extracted = []

    monkeypatch.setattr(expediente, "load_image_page", lambda _data: pages)
    monkeypatch.setattr(
        expediente,
        "segment_pages",
        lambda *_args, **_kwargs: (classified, {}),
    )
    monkeypatch.setattr(expediente, "get_type_entry", lambda type_id: _entry(type_id))
    monkeypatch.setattr(expediente, "_run_cross_document_rules", lambda *_args: [])

    def fake_extract(seg, *_args):
        extracted.append(seg.doc_type)
        return {
            "documentType": seg.doc_type,
            "pageStart": seg.page_start,
            "pageEnd": seg.page_end,
            "status": "APPROVED",
            "confidence": 0.9,
            "fields": {},
            "_entities": {},
        }

    monkeypatch.setattr(expediente, "_extract_segment", fake_extract)

    result = expediente.process_pdf(
        b"image",
        source_format="png",
        declared_types=["A", "B"],
        declared_counts={"A": "all", "B": "all"},
    )

    assert extracted == ["A", "A"]
    assert [doc["documentType"] for doc in result["documents"]] == ["A", "A", "B"]
    assert result["documents"][-1] == {
        "documentType": "B",
        "pageStart": None,
        "pageEnd": None,
        "status": "REVIEW_REQUIRED",
        "confidence": None,
        "fields": {},
    }
    assert result["reviewRequired"] is True
