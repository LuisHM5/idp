"""Deterministic-first extraction configured entirely by the field DSL."""

import json

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


def test_pedimento_erp_default_sin_ia_cubre_layout_real_de_17_paginas():
    raw = json.loads((REPO_ROOT / "catalogs/custom/erp/PEDIMENTO_ERP.json").read_text())
    entry = _parse_entry("PEDIMENTO_ERP", raw["PEDIMENTO_ERP"])
    pages = [PageText(1, """PEDIMENTO Página 1 de 17
NUM. PEDIMENTO:26  07  3435  6041353    T.OPER    CVE. PEDIMENTO:    REGIMEN:IMPA1    IMD
DESTINO:    TIPO CAMBIO:    PESO BRUTO:917.50230    6304.154    ADUANA E/S:    071
CUADRO DE LIQUIDACION
CONCEPTO F.P. IMPORTE CONCEPTO F.P. IMPORTE TOTALES
DTA 0 462 IVA 0 48644 EFECTIVO 66,963
IGI/IGE 0 1552 PRV 0 330 OTROS 0
IVA/PRV 0 53 2IG 0 15922 TOTAL 66,963
DEPÓSITO REFERENCIADO
""", "native", 0.98)]

    result = deterministic_text.extract(pages, entry)

    assert deterministic_text.policy_for(entry)["fallback"] == "review"
    assert result.entity_map["Tipo"] == "IMP"
    assert result.entity_map["Cve_Pedimento"] == "A1"
    assert result.entity_map["Tipo_Cambio"] == "17.50230"
    liquidation = result.entity_map["CUADRO_LIQUIDACION"]
    assert liquidation["TOTAL"] == "66,963"
    assert [row["CLAVE_CONCEPTO"] for row in liquidation["CONCEPTOS"]] == [
        "DTA", "IVA", "IGI/IGE", "PRV", "PRV", "2IG",
    ]
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


def test_ahorro_compara_schema_completo_con_parcial_sin_mezclarlo_con_usage():
    entry = _entry()
    result = deterministic_text.extract(PAGES, entry)
    pending = deterministic_text.unresolved_tree(
        entry.field_tree, result.resolved_paths, "missing_fields"
    )

    savings = text_extractor.estimate_deterministic_token_savings(
        PAGES,
        entry,
        pending,
        result.entity_map,
        {"inputTokens": 1000, "outputTokens": 100},
    )

    assert savings["estimated"] is True
    assert savings["billedInputTokens"] == 1000
    assert savings["billedOutputTokens"] == 100
    assert savings["estimatedInputTokensSaved"] > 0
    assert savings["estimatedOutputTokensSaved"] > 0
    assert savings["estimatedInputTokensWithoutDeterministic"] == (
        1000 + savings["estimatedInputTokensSaved"]
    )


def test_full_model_no_reporta_ahorro_por_campos_que_tambien_pidio():
    entry = _entry("full_model")
    result = deterministic_text.extract(PAGES, entry)
    pending = deterministic_text.unresolved_tree(
        entry.field_tree, result.resolved_paths, "full_model"
    )

    savings = text_extractor.estimate_deterministic_token_savings(
        PAGES,
        entry,
        pending,
        result.entity_map,
        {"inputTokens": 1000, "outputTokens": 100},
    )

    assert savings["estimatedInputTokensSaved"] == 0
    assert savings["estimatedOutputTokensSaved"] == 0


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
    savings = doc["extractionMeta"]["deterministicExtraction"]["tokenSavings"]
    assert savings["billedInputTokens"] == 0
    assert savings["estimatedInputTokensSaved"] > 0
    assert savings["estimatedOutputTokensSaved"] > 0
    assert doc["_recovered_sources"]["FOLIO"] == "DETERMINISTIC_TEXT"
    assert doc["typeCheck"]["matches"] is True


def test_override_deterministic_no_llama_bedrock_aunque_haya_faltantes(monkeypatch):
    entry = _entry()
    monkeypatch.setattr(expediente, "get_type_entry", lambda _type: entry)
    monkeypatch.setattr(
        expediente,
        "extract_document",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Bedrock no debe ejecutarse")),
    )

    doc = expediente._extract_segment(
        Segment("PRUEBA", 1, 1, 1.0), PAGES, None,
        extraction_mode="deterministic",
    )

    assert doc["extractionMeta"]["strategy"] == "deterministic"
    assert doc["extractionMeta"]["extractionMode"] == {
        "requested": "deterministic",
        "effective": "deterministic",
    }
    assert doc["extractionMeta"]["usage"] == {}


def test_override_model_reproduce_el_camino_completo_anterior(monkeypatch):
    entry = _entry()
    calls = []

    def fake_extract(_pages, _entry, **kwargs):
        calls.append(kwargs)
        return text_extractor.ExtractedDoc(
            entity_map={"FOLIO": "MODEL"},
            confidences={"FOLIO": 0.8},
            row_confidences={},
            meta={"usage": {"inputTokens": 10, "outputTokens": 2}},
        )

    monkeypatch.setattr(expediente, "get_type_entry", lambda _type: entry)
    monkeypatch.setattr(expediente, "extract_document", fake_extract)

    doc = expediente._extract_segment(
        Segment("PRUEBA", 1, 1, 1.0), PAGES, None,
        extraction_mode="model",
    )

    assert len(calls) == 1
    assert "field_tree" not in calls[0]
    assert doc["_entities"]["FOLIO"] == "MODEL"
    assert doc["extractionMeta"]["strategy"] == "model"
    assert doc["extractionMeta"]["extractionMode"] == {
        "requested": "model",
        "effective": "model",
    }
