"""Defensas de salida para schemas field-centric grandes."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline import text_extractor  # noqa: E402
from pipeline.pdf_pages import PageText  # noqa: E402


def _entry():
    return _parse_entry("PRUEBA", {
        "description": "Schema de prueba.",
        "fields": {
            "CABECERA": {
                "type": "object",
                "fields": {
                    "FOLIO": {"type": "string", "hint": "Folio impreso."},
                    "CONCEPTOS": {
                        "type": "array",
                        "item_fields": {
                            "DESCRIPCION": {"type": "string"},
                            "IMPORTE": {"type": "number"},
                        },
                    },
                },
            },
            "SUBTOTAL": {"type": "number", "hint": "Subtotal impreso."},
        },
    })


def test_el_prompt_ordena_omitir_ausentes_en_vez_de_rellenar_null():
    prompt, _ = text_extractor._build_field_tree_prompts(_entry(), [])

    assert "OMITE esa llave" in prompt
    assert "no emitas null" in prompt
    assert "usa null" not in prompt


def test_llave_omitida_y_null_explicito_se_parsean_igual():
    tree = _entry().field_tree

    omitted = text_extractor._parse_field_tree_response({}, tree, 1.0)
    explicit = text_extractor._parse_field_tree_response(
        {"CABECERA": {"FOLIO": None, "CONCEPTOS": None}, "SUBTOTAL": None},
        tree,
        1.0,
    )

    assert omitted == explicit


def test_particion_recursiva_conserva_la_ruta_del_objeto():
    non_arrays, arrays = text_extractor._partition_field_tree(_entry().field_tree)

    assert set(non_arrays) == {"CABECERA", "SUBTOTAL"}
    assert set(non_arrays["CABECERA"].fields) == {"FOLIO"}
    assert set(arrays) == {"CABECERA"}
    assert set(arrays["CABECERA"].fields) == {"CONCEPTOS"}


def test_schema_grande_extrae_escalares_y_arrays_en_dos_llamadas(monkeypatch):
    calls = []

    def fake_converse(prompt, **kwargs):
        props = kwargs["tool_schema"]["properties"]
        calls.append({"props": props, **kwargs})
        tables = props["t"]["properties"]
        if "CABECERA.CONCEPTOS" in tables:
            return {"s": {}, "t": {"CABECERA.CONCEPTOS": [["Flete", 125.0, 0.9]]}}, {"usage": {"outputTokens": 20}}
        return {"s": {"CABECERA.FOLIO": ["A-1", 0.95], "SUBTOTAL": [125.0, 0.95]}, "t": {}}, {"usage": {"outputTokens": 10}}

    monkeypatch.setattr(text_extractor, "converse_structured", fake_converse)
    monkeypatch.setattr(text_extractor, "resolve_model", lambda *a, **k: "modelo-prueba")
    monkeypatch.setattr(text_extractor, "_FIELD_TREE_SPLIT_SCHEMA_CHARS", 1)

    result = text_extractor.extract_document(
        [PageText(1, "Folio A-1, flete 125.00", "native", 1.0)], _entry()
    )

    assert len(calls) == 2
    assert result.entity_map["CABECERA"]["FOLIO"] == "A-1"
    assert result.entity_map["CABECERA"]["CONCEPTOS"][0]["IMPORTE"] == 125.0
    assert result.entity_map["SUBTOTAL"] == 125.0
    assert result.meta["schemaPartitions"] == 2
    assert result.meta["usage"]["outputTokens"] == 30


def test_schema_pequeno_conserva_una_sola_llamada(monkeypatch):
    calls = []

    def fake_converse(prompt, **kwargs):
        calls.append(kwargs)
        return {"s": {}, "t": {}}, {"usage": {}}

    monkeypatch.setattr(text_extractor, "converse_structured", fake_converse)
    monkeypatch.setattr(text_extractor, "resolve_model", lambda *a, **k: "modelo-prueba")
    monkeypatch.setattr(text_extractor, "_FIELD_TREE_SPLIT_SCHEMA_CHARS", 10**9)

    result = text_extractor.extract_document(
        [PageText(1, "Sin datos", "native", 1.0)], _entry()
    )

    assert len(calls) == 1
    assert result.meta["schemaPartitions"] == 1
