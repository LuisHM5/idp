"""El localizador por IA: el plan B cuando los marcadores no alcanzan.

Solo entra cuando el tipo declara `expected_pages: "auto"` (tamaño variable,
no hay rango fijo que declarar) Y el camino gratis ya se rindió. Nunca debe
ser la razón por la que se pierde una página: ante cualquier duda —salida
impredecible, sin rangos, o un rango que cubre todo— devuelve None y el
pipeline procesa el documento completo.

Bedrock va simulado: estos tests no gastan un centavo.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline import page_locator  # noqa: E402

ENTRY = _parse_entry("TIPO_VARIABLE", {
    "description": "Un documento de largo impredecible.",
    "fields": {"CAMPO_A": {"type": "string", "hint": "algo"}},
})


def _pages(n=20):
    return [SimpleNamespace(number=i, text=f"texto de la pagina {i}", source="native")
            for i in range(1, n + 1)]


def _fake_converse(payload, monkeypatch, usage=None):
    def fake(prompt, **kw):
        return payload, {"modelId": "modelo-x", "usage": usage or {"inputTokens": 900, "outputTokens": 40}}
    monkeypatch.setattr(page_locator, "converse", fake)


# --- cuándo se activa -----------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("auto", True), ("variable", True), ("AUTO", True), ("  auto  ", True),
    (6, False), ([3, 6], False), (None, False), ("6", False),
])
def test_only_auto_asks_the_model(value, expected):
    assert page_locator.wants_locator({"expected_pages": value}) is expected


def test_no_spec_no_locator():
    assert page_locator.wants_locator(None) is False
    assert page_locator.wants_locator({}) is False


# --- resultado útil -------------------------------------------------------

def test_locates_the_ranges_the_model_returns(monkeypatch):
    _fake_converse('{"ranges": [{"page_start": 4, "page_end": 8, "confidence": 0.9}]}', monkeypatch)
    selection, usage = page_locator.locate(_pages(20), ENTRY)
    assert selection.selected == [4, 5, 6, 7, 8]
    assert len(selection.skipped) == 15
    assert usage["usage"]["inputTokens"] == 900


def test_several_ranges_for_repeated_copies(monkeypatch):
    _fake_converse('{"ranges": [{"page_start": 2, "page_end": 3}, {"page_start": 9, "page_end": 10}]}', monkeypatch)
    selection, _ = page_locator.locate(_pages(20), ENTRY)
    assert selection.selected == [2, 3, 9, 10]


def test_inverted_range_is_repaired_not_dropped(monkeypatch):
    _fake_converse('{"ranges": [{"page_start": 8, "page_end": 4}]}', monkeypatch)
    selection, _ = page_locator.locate(_pages(20), ENTRY)
    assert selection.selected == [4, 5, 6, 7, 8]


def test_pages_outside_the_document_are_clamped(monkeypatch):
    _fake_converse('{"ranges": [{"page_start": 8, "page_end": 999}]}', monkeypatch)
    selection, _ = page_locator.locate(_pages(10), ENTRY)
    assert selection.selected == [8, 9, 10]


# --- todo lo que debe caer en "procesa todo" ------------------------------

@pytest.mark.parametrize("payload", [
    "no es json en absoluto",
    '{"ranges": []}',
    '{"otra_cosa": 1}',
    '{"ranges": [{"page_start": "x", "page_end": "y"}]}',
    '{"ranges": ["no soy un objeto"]}',
])
def test_untrustworthy_answers_never_drop_a_page(payload, monkeypatch):
    _fake_converse(payload, monkeypatch)
    selection, _ = page_locator.locate(_pages(20), ENTRY)
    assert selection is None


def test_covering_everything_is_treated_as_no_answer(monkeypatch):
    """'Todo es del tipo' no aporta nada sobre procesar todo, y registrarlo
    como una selección ensuciaría el reporte de ahorro con un cero."""
    _fake_converse('{"ranges": [{"page_start": 1, "page_end": 20}]}', monkeypatch)
    selection, _ = page_locator.locate(_pages(20), ENTRY)
    assert selection is None


def test_a_bedrock_failure_degrades_instead_of_raising(monkeypatch):
    def boom(prompt, **kw):
        raise RuntimeError("throttled")
    monkeypatch.setattr(page_locator, "converse", boom)
    selection, usage = page_locator.locate(_pages(20), ENTRY)
    assert selection is None and usage == {}


def test_no_pages_no_call(monkeypatch):
    def fail(prompt, **kw):
        raise AssertionError("no debería llamar a Bedrock sin páginas")
    monkeypatch.setattr(page_locator, "converse", fail)
    assert page_locator.locate([], ENTRY) == (None, {})


# --- el prompt ------------------------------------------------------------

def test_prompt_names_the_type_and_its_fields():
    prompt = page_locator._build_prompt(_pages(3), ENTRY)
    assert "TIPO_VARIABLE" in prompt
    assert "CAMPO_A" in prompt
    assert "[pag 1]" in prompt and "[pag 3]" in prompt
