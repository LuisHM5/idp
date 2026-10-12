"""Deterministic-first extraction configured entirely by the field DSL."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline import deterministic_text, expediente, text_extractor, type_check  # noqa: E402
from pipeline.segmenter import Segment  # noqa: E402
from pipeline.pdf_pages import PageText  # noqa: E402
from validators.engine import resolve_field_tree  # noqa: E402


def _entry(fallback="missing_fields"):
    return _parse_entry("PRUEBA", {
        "description": "Documento de prueba.",
        "deterministic_extraction": {"enabled": True, "fallback": fallback},
        "page_selection": {"markers": ["DOCUMENTO", "FOLIO"], "min_score": 2},
        "fields": {
            "FOLIO": {
                "type": "string", "required": True,
                "text_source": {
                    "selectors": [
                        {"regex": r"FOLIO\s*:\s*(?P<value>[A-Z]-\d+)", "group": "value"}
                    ]
                },
            },
            "TOTAL": {
                "type": "number", "required": True,
                "text_source": {
                    "selectors": [
                        {"regex": r"TOTAL\s*:\s*\$?\s*(?P<value>[\d,.]+)", "group": "value"}
                    ]
                },
            },
            "PARTIDAS": {
                "type": "array", "required": True,
                "text_source": {
                    "row_regex": r"(?m)^(?P<sku>SKU-\d+)\s+(?P<importe>[\d.]+)$",
                    "groups": {"SKU": "sku", "IMPORTE": "importe"},
                    "deduplicate_by": ["SKU"],
                },
                "item_fields": {
                    "SKU": {"type": "string", "required": True},
                    "IMPORTE": {"type": "number", "required": True},
                },
            },
            "NOTA": {"type": "string", "required": False},
            "OPCIONAL_SIN_IA": {
                "type": "string", "required": False,
                "text_source": {"selectors": [], "model_fallback": "never"},
            },
            "CALCULADO": {"type": "string", "computed": True},
        },
    })


PAGES = [PageText(
    1,
    "DOCUMENTO\nFOLIO: A-123\nSKU-1 10.50\nSKU-2 20.00\nSKU-1 10.50\nTOTAL: $30.50",
    "native",
    0.98,
)]


def test_extrae_escalares_y_filas_y_deduplica():
    result = deterministic_text.extract(PAGES, _entry())

    assert result.entity_map == {
        "FOLIO": "A-123",
        "TOTAL": "30.50",
        "PARTIDAS": [
            {"SKU": "SKU-1", "IMPORTE": "10.50"},
            {"SKU": "SKU-2", "IMPORTE": "20.00"},
        ],
    }
    assert result.confidences == {"FOLIO": 0.98, "TOTAL": 0.98, "PARTIDAS": 0.98}
    assert result.source_overrides["PARTIDAS[1].IMPORTE"] == "DETERMINISTIC_TEXT"
    assert result.row_confidences


def test_array_combina_layouts_de_row_selectors():
    entry = _parse_entry("TABLA", {
        "description": "Tabla con dos layouts.",
        "deterministic_extraction": {"enabled": True},
        "fields": {
            "FILAS": {
                "type": "array",
                "required": True,
                "text_source": {
                    "row_selectors": [
                        {
                            "row_regex": r"(?m)^NORMAL (?P<folio>\w+) (?P<importe>\d+)$",
                            "groups": {"FOLIO": "folio", "IMPORTE": "importe"},
                        },
                        {
                            "row_regex": r"(?m)^ALT (?P<acuse>COVE\w+) / (?P<folio>\w+) (?P<importe>\d+)$",
                            "groups": {
                                "FOLIO": "folio", "ACUSE": "acuse", "IMPORTE": "importe",
                            },
                        },
                    ],
                    "deduplicate_by": ["FOLIO"],
                },
                "item_fields": {
                    "FOLIO": {"type": "string", "required": True},
                    "ACUSE": {"type": "string", "required": False},
                    "IMPORTE": {"type": "number", "required": True},
                },
            }
        },
    })
    pages = [PageText(1, "NORMAL A1 10\nALT COVE99 / B2 20\nNORMAL A1 10", "native", 1.0)]

    result = deterministic_text.extract(pages, entry)

    assert result.entity_map["FILAS"] == [
        {"FOLIO": "A1", "ACUSE": None, "IMPORTE": "10"},
        {"FOLIO": "B2", "ACUSE": "COVE99", "IMPORTE": "20"},
    ]


def test_subschema_contiene_solo_faltantes_con_fallback():
    result = deterministic_text.extract(PAGES, _entry())
    pending = deterministic_text.unresolved_tree(
        _entry().field_tree, result.resolved_paths, "missing_fields"
    )

    assert set(pending) == {"NOTA"}
    assert "OPCIONAL_SIN_IA" not in pending
    assert "CALCULADO" not in pending


def test_review_no_invoca_model_y_full_model_conserva_todo_no_computado():
    entry = _entry()
    result = deterministic_text.extract(PAGES, entry)

    assert deterministic_text.unresolved_tree(entry.field_tree, result.resolved_paths, "review") == {}
    assert set(deterministic_text.unresolved_tree(entry.field_tree, result.resolved_paths, "full_model")) == {
        "FOLIO", "TOTAL", "PARTIDAS", "NOTA", "OPCIONAL_SIN_IA",
    }


def test_schema_parcial_del_model_solo_contiene_el_faltante(monkeypatch):
    entry = _entry()
    result = deterministic_text.extract(PAGES, entry)
    pending = deterministic_text.unresolved_tree(entry.field_tree, result.resolved_paths, "missing_fields")
    seen = []

    def fake_converse(prompt, **kwargs):
        seen.append(kwargs["tool_schema"])
        return {"NOTA": {"value": "modelo", "confidence": 0.9}}, {"usage": {"outputTokens": 3}}

    monkeypatch.setattr(text_extractor, "converse_structured", fake_converse)
    monkeypatch.setattr(text_extractor, "resolve_model", lambda *a, **k: "modelo-prueba")
    model = text_extractor.extract_document(PAGES, entry, field_tree=pending)
    merged = deterministic_text.merge_entities(
        result.entity_map, model.entity_map, entry.field_tree,
        resolved_paths=result.resolved_paths,
    )

    assert len(seen) == 1
    assert set(seen[0]["properties"]) == {"NOTA", "_DOCUMENTO"}
    assert merged["FOLIO"] == "A-123"
    assert merged["NOTA"] == "modelo"


def test_config_invalida_falla_abierta_hacia_modelo():
    entry = _entry()
    entry.field_tree["FOLIO"].params["text_source"]["selectors"][0]["flags"] = ["ejecutar_codigo"]
    result = deterministic_text.extract(PAGES, entry)
    pending = deterministic_text.unresolved_tree(entry.field_tree, result.resolved_paths, "missing_fields")

    assert "FOLIO" not in result.entity_map
    assert result.warnings and "flag no permitido" in result.warnings[0]
    assert "FOLIO" in pending


def test_fuente_determinista_llega_a_escalares_y_arrays():
    entry = _entry(fallback="review")
    result = deterministic_text.extract(PAGES, entry)
    tree, _, _ = resolve_field_tree(
        result.entity_map,
        result.confidences,
        entry,
        source_overrides=result.source_overrides,
        row_confidences=result.row_confidences,
    )

    assert tree["FOLIO"]["source"] == "DETERMINISTIC_TEXT"
    assert tree["PARTIDAS"]["items"][0]["fields"]["SKU"]["source"] == "DETERMINISTIC_TEXT"


def test_type_check_usa_marcadores_positivos_sin_senal_de_modelo():
    checked = type_check.evaluate(None, {}, {}, _entry(), PAGES)

    assert checked.evaluated is True
    assert checked.matches is True
    assert checked.guard == {"markers": "MATCHED", "source": "DETERMINISTIC_TEXT"}


def test_segmento_completo_omite_bedrock_y_publica_meta(monkeypatch):
    entry = _parse_entry("PRUEBA", {
        "description": "Documento de prueba.",
        "deterministic_extraction": {"enabled": True, "fallback": "missing_fields"},
        "page_selection": {"markers": ["DOCUMENTO", "FOLIO"], "min_score": 2},
        "fields": {
            "FOLIO": {
                "type": "string", "required": True,
                "text_source": {
                    "selectors": [{"regex": r"FOLIO:\s*(?P<v>[A-Z]-\d+)", "group": "v"}]
                },
            }
        },
    })
    monkeypatch.setattr(expediente, "get_type_entry", lambda _type: entry)
    monkeypatch.setattr(
        expediente,
        "extract_document",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Bedrock no debe ejecutarse")),
    )

    doc = expediente._extract_segment(
        Segment("PRUEBA", 1, 1, 1.0),
        [PageText(1, "DOCUMENTO\nFOLIO: A-1", "native", 0.98)],
        None,
    )

    assert doc["_entities"]["FOLIO"] == "A-1"
    assert doc["extractionMeta"]["strategy"] == "deterministic"
    assert doc["extractionMeta"]["chunks"] == 0
    assert doc["extractionMeta"]["usage"] == {}
    assert doc["_recovered_sources"]["FOLIO"] == "DETERMINISTIC_TEXT"
    assert doc["typeCheck"]["matches"] is True
