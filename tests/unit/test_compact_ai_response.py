"""Contrato del protocolo compacto entre Bedrock y el ensamblador Python."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAMBDA = REPO / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA))

from document_catalog import FieldSpec, TypeRegistryEntry, _parse_entry  # noqa: E402
from pipeline import text_extractor  # noqa: E402
from pipeline.pdf_pages import PageText  # noqa: E402


PAGES = [PageText(1, "Póliza ABC Factura F-1 IVA 16", "native", 0.9)]


def entry(required=True):
    impuesto = FieldSpec(
        "IMPUESTOS", "array", item_fields={
            "CLAVE": FieldSpec("CLAVE", "string"),
            "IMPORTE": FieldSpec("IMPORTE", "number"),
        }
    )
    return TypeRegistryEntry(
        type_id="PRUEBA_COMPACTA",
        extraction_fields=[],
        required_fields=[],
        description="Documento de prueba",
        field_tree={
            "POLIZA": FieldSpec("POLIZA", "string", required=required),
            "DATOS": FieldSpec("DATOS", "object", fields={
                "TITULAR": FieldSpec("TITULAR", "string"),
            }),
            "FACTURAS": FieldSpec("FACTURAS", "array", item_fields={
                "FOLIO": FieldSpec("FOLIO", "string"),
                "TOTAL": FieldSpec("TOTAL", "number"),
                "IMPUESTOS": impuesto,
            }),
        },
    )


def install(monkeypatch, responses):
    calls = []

    def fake(prompt, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(text_extractor, "converse_structured", fake)
    monkeypatch.setattr(text_extractor, "resolve_model", lambda *a, **k: "modelo-prueba")
    return calls


def test_compacto_arma_objetos_tablas_y_arrays_anidados(monkeypatch):
    calls = install(monkeypatch, [(
        {
            "d": [True, 0.99, None],
            "s": {"POLIZA": ["ABC", 0.98], "DATOS.TITULAR": ["ANA", 0.96]},
            "t": {"FACTURAS": [["F-1", 116.0, [["002", 16.0, 0.92]], 0.95]]},
        },
        {"usage": {"inputTokens": 100, "outputTokens": 30}},
    )])

    result = text_extractor.extract_document(PAGES, entry())

    assert len(calls) == 1
    assert result.entity_map["POLIZA"] == "ABC"
    assert result.entity_map["DATOS"]["TITULAR"] == "ANA"
    assert result.entity_map["FACTURAS"][0] == {
        "FOLIO": "F-1", "TOTAL": 116.0,
        "IMPUESTOS": [{"CLAVE": "002", "IMPORTE": 16.0}],
    }
    assert result.confidences["POLIZA"] == 0.882
    assert result.meta["typeSignal"]["corresponde"] is True
    assert result.meta["aiResponseFormat"]["effective"] == "compact"
    assert result.meta["aiResponseFormat"]["tokenSavings"]["billedOutputTokens"] == 30


def test_payload_roto_reintenta_legacy_una_vez_y_suma_consumo(monkeypatch):
    calls = install(monkeypatch, [
        ({"s": {"RUTA_INVENTADA": ["x", 0.9]}, "t": {}},
         {"usage": {"inputTokens": 20, "outputTokens": 5}}),
        ({"POLIZA": {"value": "ABC", "confidence": 0.95}},
         {"usage": {"inputTokens": 30, "outputTokens": 10}}),
    ])

    result = text_extractor.extract_document(PAGES, entry())

    assert len(calls) == 2
    assert result.entity_map["POLIZA"] == "ABC"
    assert result.meta["usage"] == {"inputTokens": 50, "outputTokens": 15}
    audit = result.meta["aiResponseFormat"]
    assert audit["fallbackExecuted"] is True
    assert audit["compactCalls"] == 1
    assert audit["legacyCalls"] == 1
    assert audit["tokenSavings"]["estimatedOutputTokensSaved"] == -5


def test_omitir_campo_requerido_no_dispara_regeneracion_legacy(monkeypatch):
    calls = install(monkeypatch, [
        ({"s": {}, "t": {}}, {"usage": {"inputTokens": 10, "outputTokens": 2}}),
    ])

    result = text_extractor.extract_document(PAGES, entry())

    assert len(calls) == 1
    assert result.entity_map.get("POLIZA") is None
    assert result.meta["aiResponseFormat"]["fallbackExecuted"] is False


def test_precedencia_api_catalogo_entorno_y_default(monkeypatch):
    item = entry()
    monkeypatch.delenv("AI_RESPONSE_FORMAT", raising=False)
    assert text_extractor.resolve_ai_response_format(item)["selected"] == "compact"
    monkeypatch.setenv("AI_RESPONSE_FORMAT", "legacy")
    assert text_extractor.resolve_ai_response_format(item)["source"] == "environment"
    item.ai_response_format = "compact"
    assert text_extractor.resolve_ai_response_format(item)["source"] == "catalog"
    resolved = text_extractor.resolve_ai_response_format(item, "legacy")
    assert resolved == {"selected": "legacy", "source": "api", "requested": "legacy"}


def test_catalogo_parsea_formato_sin_acoplarlo_al_tipo():
    parsed = _parse_entry("PRUEBA", {
        "ai_response_format": "LEGACY",
        "fields": {"FOLIO": {"type": "string"}},
    })

    assert parsed.ai_response_format == "legacy"
