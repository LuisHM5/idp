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


# --- Respuestas reales de MiniMax que antes tiraban toda la llamada ---------
# Constancia de persona física (2026-10-14): `NOMBRE_LOCALIDAD` en blanco en el
# PDF y la fila de una actividad sin su `FECHA_FIN` vacía. Cada una descartaba
# la respuesta compacta completa y la repetía en legacy (4 llamadas, ~16 s).

CONSTANCIA_PAGES = [
    PageText(1, "Nombre de la Localidad:    Nombre del Municipio: AGUASCALIENTES", "native", 1.0),
    PageText(2, "1    Instalaciones eléctricas en construcciones    50    04/05/2011\n"
                "2    Trabajos de albañilería    5    01/04/2025", "native", 1.0),
]


def constancia_entry():
    import json
    shared = json.loads((REPO / "catalogs/_shared.json").read_text(encoding="utf-8"))
    return _parse_entry("CONSTANCIA_PRUEBA", {
        "description": "Constancia de prueba",
        "fields": {
            "DOMICILIO": {"type": "object", "fields": {
                "NOMBRE_LOCALIDAD": {"type": "string"},
                "MUNICIPIO": {"type": "string"},
            }},
            "ACTIVIDADES": {"type": "array", "item_fields": {
                "$common": ["PAGINA", "ORDEN", "VALOR_CRUDO"],
                "ACTIVIDAD": {"type": "string", "required": True},
                "PORCENTAJE": {"type": "number"},
                "FECHA_INICIO": {"type": "date"},
                "FECHA_FIN": {"type": "date"},
            }},
        },
    }, common_item_fields=shared["_common_item_fields"])


def test_escalar_vacio_y_fila_sin_celda_final_se_reparan_sin_legacy(monkeypatch):
    calls = install(monkeypatch, [(
        {
            "d": [True, 0.99, None],
            "s": {"DOMICILIO.NOMBRE_LOCALIDAD": [None], "DOMICILIO.MUNICIPIO": ["AGUASCALIENTES", 0.98]},
            "t": {"ACTIVIDADES": [
                ["Instalaciones eléctricas en construcciones", 50, "04/05/2011", 0.96],
                ["Trabajos de albañilería", 5, "01/04/2025", None, 0.9],
            ]},
        },
        {"usage": {"inputTokens": 100, "outputTokens": 40}},
    )])

    result = text_extractor.extract_document(CONSTANCIA_PAGES, constancia_entry())

    assert len(calls) == 1
    audit = result.meta["aiResponseFormat"]
    assert audit["fallbackExecuted"] is False
    assert audit["effective"] == "compact"
    assert result.entity_map["DOMICILIO"].get("NOMBRE_LOCALIDAD") is None
    assert result.entity_map["DOMICILIO"]["MUNICIPIO"] == "AGUASCALIENTES"
    first, second = result.entity_map["ACTIVIDADES"]
    assert first.get("FECHA_FIN") is None and first["PORCENTAJE"] == 50
    assert second["ACTIVIDAD"] == "Trabajos de albañilería"


def test_common_no_se_pide_al_modelo_y_lo_llena_el_pipeline(monkeypatch):
    calls = install(monkeypatch, [(
        {"s": {}, "t": {"ACTIVIDADES": [
            ["Instalacioneseléctricasen construcciones", 50, "04/05/2011", None, 0.96],
            ["Trabajos de albañilería", 5, "01/04/2025", None, 0.9],
        ]}},
        {"usage": {}},
    )])

    result = text_extractor.extract_document(CONSTANCIA_PAGES, constancia_entry())

    schema = calls[0]["tool_schema"]["properties"]["t"]["properties"]["ACTIVIDADES"]
    assert schema["items"]["minItems"] == schema["items"]["maxItems"] == 5
    assert "VALOR_CRUDO" not in calls[0]["system"] .split("PROTOCOLO COMPACTO")[1]
    first, second = result.entity_map["ACTIVIDADES"]
    assert first["ORDEN"] == 1 and second["ORDEN"] == 2
    # El modelo pegó palabras; la fila se encuentra igual, sin espacios.
    assert first["PAGINA"] == 2
    assert first["VALOR_CRUDO"] == "1    Instalaciones eléctricas en construcciones    50    04/05/2011"
    assert second["VALOR_CRUDO"].startswith("2    Trabajos de albañilería")
    # La confianza de la fila sigue encontrándose con la fila ya completada.
    from pipeline.confidence import row_key
    assert result.row_confidences[row_key(first)]["ACTIVIDAD"] == 0.96


def test_tabla_irrecuperable_reintenta_solo_esa_rama(monkeypatch):
    calls = install(monkeypatch, [
        ({
            "s": {"DOMICILIO.MUNICIPIO": ["AGUASCALIENTES", 0.98]},
            "t": {"ACTIVIDADES": [["solo una celda"]]},
        }, {"usage": {"inputTokens": 100, "outputTokens": 30}}),
        ({"ACTIVIDADES": [{"ACTIVIDAD": "Trabajos de albañilería", "PORCENTAJE": 5, "confidence": 0.9}]},
         {"usage": {"inputTokens": 80, "outputTokens": 20}}),
    ])

    result = text_extractor.extract_document(CONSTANCIA_PAGES, constancia_entry())

    assert len(calls) == 2
    retry_props = calls[1]["tool_schema"]["properties"]
    assert set(retry_props) == {"ACTIVIDADES"}
    assert result.entity_map["DOMICILIO"]["MUNICIPIO"] == "AGUASCALIENTES"
    assert result.entity_map["ACTIVIDADES"][0]["ACTIVIDAD"] == "Trabajos de albañilería"
    audit = result.meta["aiResponseFormat"]
    assert audit["fallbackExecuted"] is True
    assert audit["retriedPaths"] == ["ACTIVIDADES"]
    assert result.meta["usage"] == {"inputTokens": 180, "outputTokens": 50}


def test_fila_sin_celda_numerica_final_sigue_siendo_error():
    columns = [("A", FieldSpec("A", "string")), ("B", FieldSpec("B", "number"))]
    try:
        text_extractor._repair_compact_row(["x", 0.9], columns, "T[0]")
    except text_extractor.CompactResponseError:
        return
    raise AssertionError("una celda numérica faltante no debe adivinarse")


def test_respuesta_real_minimax_tipo_por_nombre_y_vacios_como_cero(monkeypatch):
    # Forma exacta de la respuesta de minimax.minimax-m2.5 a la constancia de
    # persona física (2026-10-14): `d` con el nombre del tipo en vez de true,
    # escalares en blanco como [0] y filas sin su FECHA_FIN vacía. Antes se
    # descartaba completa y se pagaba otra vez en legacy (~30 s).
    entry = _parse_entry("CONSTANCIA_PRUEBA", {
        "description": "Constancia de prueba",
        "fields": {
            "NOMBRES": {"type": "string"},
            "NUMERO_INTERIOR": {"type": "string"},
            "OBLIGACIONES": {"type": "array", "item_fields": {
                "DESCRIPCION": {"type": "string", "required": True},
                "VENCIMIENTO": {"type": "string"},
                "FECHA_INICIO": {"type": "date"},
                "FECHA_FIN": {"type": "date"},
            }},
        },
    })
    calls = install(monkeypatch, [(
        {
            "s": {"NOMBRES": ["JUAN DANIEL", 1.0], "NUMERO_INTERIOR": [0]},
            "t": {"OBLIGACIONES": [
                ["Pago definitivo mensual de IVA.", "A más tardar el día 17", "04/05/2011", 1.0],
            ]},
            "d": ["CONSTANCIA_PRUEBA", 1.0, "Constancia de Situación Fiscal - Persona física"],
        },
        {"usage": {"inputTokens": 100, "outputTokens": 40}},
    )])

    result = text_extractor.extract_document(PAGES, entry)

    assert len(calls) == 1
    assert result.meta["aiResponseFormat"]["fallbackExecuted"] is False
    assert result.entity_map["NOMBRES"] == "JUAN DANIEL"
    assert result.entity_map.get("NUMERO_INTERIOR") is None
    assert result.entity_map["OBLIGACIONES"][0]["FECHA_INICIO"] == "04/05/2011"
    assert result.meta["typeSignal"]["corresponde"] is True


def test_cero_suelto_en_campo_numerico_reintenta_solo_ese_campo(monkeypatch):
    entry = _parse_entry("PRUEBA_NUM", {
        "description": "Prueba",
        "fields": {"TOTAL": {"type": "number"}, "FOLIO": {"type": "string"}},
    })
    calls = install(monkeypatch, [
        ({"s": {"TOTAL": [0], "FOLIO": ["A-1", 0.9]}, "t": {}}, {"usage": {"inputTokens": 10, "outputTokens": 4}}),
        ({"TOTAL": {"value": 0, "confidence": 0.9}}, {"usage": {"inputTokens": 8, "outputTokens": 3}}),
    ])

    result = text_extractor.extract_document(PAGES, entry)

    assert len(calls) == 2
    assert set(calls[1]["tool_schema"]["properties"]) == {"TOTAL"}
    assert result.entity_map["FOLIO"] == "A-1" and result.entity_map["TOTAL"] == 0


def test_ruta_no_pedida_junto_a_rutas_validas_se_ignora(monkeypatch):
    # En una llamada híbrida el modelo devolvió además un campo que ya había
    # leído el mapper; antes eso descartaba toda la respuesta.
    calls = install(monkeypatch, [(
        {"s": {"POLIZA": ["ABC", 0.98], "YA_RESUELTO_POR_CODIGO": ["614", 1.0]}, "t": {}},
        {"usage": {"inputTokens": 10, "outputTokens": 4}},
    )])

    result = text_extractor.extract_document(PAGES, entry())

    assert len(calls) == 1
    assert result.entity_map["POLIZA"] == "ABC"
    assert "YA_RESUELTO_POR_CODIGO" not in result.entity_map
    assert result.meta["aiResponseFormat"]["fallbackExecuted"] is False
