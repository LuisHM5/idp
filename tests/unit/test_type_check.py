"""Verificación de tipo (`pipeline/type_check`): ¿el archivo ES lo que se declaró?

Lo que fijan estas pruebas:

- La señal viaja en el MISMO tool call de extracción (una llave `_DOCUMENTO`
  en el schema y una regla en el prompt), nunca en una llamada aparte, y
  solo la pide la partición de escalares.
- El merge entre chunks es fail-open: basta que uno reconozca el documento.
- La guardia gratis: el modelo no tumba solo un documento cuyos campos
  obligatorios sí se leyeron (se degrada a revisión); los marcadores de
  `page_selection`, cuando existen, deciden en los dos sentidos.
- El efecto: un check `document_type_match` bloqueante → REJECTED,
  `documents[i].confidence` ≤ 0.05, y el agregado para la raíz del envelope.
- Kill switch y bloque `type_check` del catálogo.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline import text_extractor, type_check  # noqa: E402
from pipeline.pdf_pages import PageText  # noqa: E402
from validators import engine  # noqa: E402


def entry_con(fields_extra=None, **extra):
    data = {
        "description": "Estado de cuenta bancario.",
        "display_name": "Estado de cuenta",
        "fields": {
            "BANCO": {"type": "string", "required": True, "hint": "Nombre del banco."},
            "TITULAR": {"type": "string", "required": True, "hint": "Titular."},
            "CLABE": {"type": "string", "required": False, "hint": "CLABE."},
            "MOVIMIENTOS": {
                "type": "array",
                "item_fields": {"CONCEPTO": {"type": "string"}, "IMPORTE": {"type": "number"}},
            },
            **(fields_extra or {}),
        },
    }
    data.update(extra)
    return _parse_entry("PRUEBA", data)


PAGINAS = [PageText(1, "BANCO AZTECA\nTitular: JUAN PEREZ\nCLABE 012345678901234567", "native", 1.0)]

NO_CORRESPONDE = {"corresponde": False, "confianza": 0.93, "descripcion": "Constancia fiscal del SAT"}
CORRESPONDE = {"corresponde": True, "confianza": 0.97, "descripcion": None}


# ------------------------------------------------------------------ señal


def test_read_signal_tolera_envolturas_y_booleanos_en_texto():
    assert type_check.read_signal({"_DOCUMENTO": {"corresponde": "false", "confianza": "0.8"}}) == {
        "corresponde": False, "confianza": 0.8, "descripcion": None,
    }
    envuelto = {"_DOCUMENTO": {"value": {"corresponde": True, "confianza": 2, "descripcion": " Acta "}}}
    assert type_check.read_signal(envuelto) == {"corresponde": True, "confianza": 1.0, "descripcion": "Acta"}


def test_read_signal_sin_corresponde_legible_no_adivina():
    assert type_check.read_signal({}) is None
    assert type_check.read_signal({"_DOCUMENTO": {"confianza": 0.9}}) is None
    assert type_check.read_signal({"_DOCUMENTO": "si"}) is None
    assert type_check.read_signal(None) is None


def test_merge_es_fail_open_entre_chunks():
    """Un anexo que el modelo no reconoce no tumba al documento que sí reconoció."""
    assert type_check.merge_signals([NO_CORRESPONDE, CORRESPONDE])["corresponde"] is True
    assert type_check.merge_signals([None, CORRESPONDE, None])["corresponde"] is True


def test_merge_de_puros_no_toma_la_confianza_minima_y_la_primera_descripcion():
    merged = type_check.merge_signals([
        {"corresponde": False, "confianza": 0.95, "descripcion": None},
        {"corresponde": False, "confianza": 0.6, "descripcion": "Factura"},
    ])
    assert merged["corresponde"] is False
    assert merged["confianza"] == 0.6
    assert merged["descripcion"] == "Factura"
    assert type_check.merge_signals([None, None]) is None


# ------------------------------------------------------------- evaluación


def test_corresponde_no_deja_motivo_ni_confianza_de_rechazo():
    tc = type_check.evaluate(CORRESPONDE, {"BANCO": "AZTECA"}, {"BANCO": 0.9}, entry_con())
    assert (tc.evaluated, tc.matches, tc.rejection_confidence, tc.reason) == (True, True, 0.0, None)


def test_no_corresponde_con_campos_vacios_es_rechazo_pleno():
    tc = type_check.evaluate(NO_CORRESPONDE, {}, {}, entry_con())
    assert tc.matches is False
    assert tc.reason == type_check.REASON_MISMATCH
    assert tc.rejection_confidence == 0.93
    assert tc.detected == "Constancia fiscal del SAT"
    assert tc.guard["requiredFilled"] == 0 and tc.guard["requiredTotal"] == 2


def test_guardia_degrada_cuando_los_obligatorios_si_se_leyeron():
    """2 de 2 requeridos llenos con buena confianza: el modelo no manda solo."""
    tc = type_check.evaluate(
        NO_CORRESPONDE, {"BANCO": "AZTECA", "TITULAR": "JUAN"}, {"BANCO": 0.9, "TITULAR": 0.85}, entry_con(),
    )
    assert tc.matches is False
    assert tc.reason == type_check.REASON_CONTRADICTED
    assert tc.guard["requiredRatio"] == 1.0


def test_guardia_ignora_campos_leidos_con_poca_confianza():
    tc = type_check.evaluate(
        NO_CORRESPONDE, {"BANCO": "AZTECA", "TITULAR": "JUAN"}, {"BANCO": 0.4, "TITULAR": 0.3}, entry_con(),
    )
    assert tc.reason == type_check.REASON_MISMATCH


def test_la_guardia_exige_todos_los_obligatorios_por_default():
    """Medido 2026-10-08 (MiniMax, constancia fiscal subida como estado de
    cuenta): el modelo llenó TITULAR con la razón social del contribuyente y
    PAIS con MEX — 2 de 3 obligatorios — y con "la mayoría" el archivo ajeno
    se escapaba a revisión. Un documento legítimo bien leído los tiene todos."""
    tres = entry_con(fields_extra={"PAIS": {"type": "string", "required": True, "hint": "País."}})
    dos_de_tres = {"PAIS": "MEX", "TITULAR": "RUZ IMAGINE"}
    conf = {"PAIS": 0.95, "TITULAR": 0.95}
    assert type_check.evaluate(NO_CORRESPONDE, dos_de_tres, conf, tres).reason == type_check.REASON_MISMATCH
    assert type_check.evaluate(NO_CORRESPONDE, dos_de_tres, conf, tres).guard["requiredRatio"] == 0.6667


def test_min_required_filled_del_catalogo_mueve_la_guardia():
    entidades, conf = {"BANCO": "AZTECA"}, {"BANCO": 0.9}
    assert type_check.evaluate(NO_CORRESPONDE, entidades, conf, entry_con()).reason == type_check.REASON_MISMATCH
    laxo = entry_con(type_check={"min_required_filled": 0.5})
    assert type_check.evaluate(NO_CORRESPONDE, entidades, conf, laxo).reason == type_check.REASON_CONTRADICTED


def test_marcadores_deciden_en_los_dos_sentidos():
    con_marcadores = entry_con(page_selection={"markers": ["ESTADO DE CUENTA", "CLABE"], "min_score": 2})
    llenos, conf = {"BANCO": "AZTECA", "TITULAR": "JUAN"}, {"BANCO": 0.9, "TITULAR": 0.9}
    # Campos llenos pero ningún marcador en la página: rechazo pleno.
    sin = [PageText(1, "CONSTANCIA DE SITUACION FISCAL\nRFC XAXX010101000", "native", 1.0)]
    tc = type_check.evaluate(NO_CORRESPONDE, llenos, conf, con_marcadores, sin)
    assert tc.reason == type_check.REASON_MISMATCH and tc.guard["markers"] == "ABSENT"
    # Marcadores presentes aunque no haya campos: se degrada.
    con = [PageText(1, "ESTADO DE CUENTA\nCLABE 012345678901234567", "native", 1.0)]
    tc = type_check.evaluate(NO_CORRESPONDE, {}, {}, con_marcadores, con)
    assert tc.reason == type_check.REASON_CONTRADICTED and tc.guard["markers"] == "MATCHED"


def test_sin_senal_o_apagado_no_se_evalua(monkeypatch):
    assert type_check.evaluate(None, {}, {}, entry_con()).evaluated is False
    assert type_check.evaluate(NO_CORRESPONDE, {}, {}, entry_con(type_check={"enabled": False})).evaluated is False
    monkeypatch.setenv("TYPE_CHECK_ENABLED", "false")
    assert type_check.evaluate(NO_CORRESPONDE, {}, {}, entry_con()).evaluated is False
    assert type_check.applies_to(entry_con()) is False


def test_applies_to_solo_tipos_field_centric():
    assert type_check.applies_to(entry_con()) is True
    legado = _parse_entry("LEGADO", {"extraction_fields": ["A"], "required_fields": []})
    assert type_check.applies_to(legado) is False


# ----------------------------------------------------------------- efecto


def resolver(entry, tc, entidades=None, confianzas=None):
    _, checks, efectivas = engine.resolve_field_tree(entidades or {}, confianzas or {}, entry)
    mismatch = type_check.mismatch_check(tc, entry)
    if mismatch is not None:
        checks.append(mismatch)
    status = engine.document_status_from_checks(checks)
    media = sum(c for c in efectivas.values() if c is not None) / max(1, len([c for c in efectivas.values() if c]))
    return status, checks, type_check.cap_confidence(tc, media)


def test_no_corresponde_rechaza_solo_y_cae_la_confianza():
    entry = entry_con()
    entidades = {"BANCO": "AZTECA", "TITULAR": "JUAN", "CLABE": "012345678901234567"}
    conf = {"BANCO": 0.95, "TITULAR": 0.95, "CLABE": 0.95}
    tc = type_check.evaluate(NO_CORRESPONDE, {}, {}, entry).to_json()

    status, checks, confianza = resolver(entry, tc, entidades, conf)

    assert status == engine.DOC_REJECTED
    assert confianza <= type_check.TYPE_MISMATCH_CONFIDENCE_CAP
    check = next(c for c in checks if c.rule == type_check.RULE_NAME)
    assert check.blocking and check.severity == "error" and not check.valid
    assert "Constancia fiscal del SAT" in check.message
    assert check.details["rejectionConfidence"] == 0.93
    # Los campos siguen ahí: solo cae el número del documento.
    assert all(c.valid for c in checks if c.rule != type_check.RULE_NAME)


def test_senal_contradicha_baja_a_revision():
    """Todos los obligatorios leídos: el modelo no manda solo, se revisa."""
    entry = entry_con()
    entidades, conf = {"BANCO": "AZTECA", "TITULAR": "JUAN"}, {"BANCO": 0.95, "TITULAR": 0.95}
    tc = type_check.evaluate(NO_CORRESPONDE, entidades, conf, entry).to_json()

    status, checks, confianza = resolver(entry, tc, entidades, conf)

    assert status == engine.DOC_REVIEW
    check = next(c for c in checks if c.rule == type_check.RULE_NAME)
    assert check.severity == "warning" and not check.blocking
    assert confianza <= type_check.TYPE_MISMATCH_CONFIDENCE_CAP


def test_on_fail_del_catalogo_manda():
    entidades, conf = {"BANCO": "AZTECA", "TITULAR": "JUAN"}, {"BANCO": 0.95, "TITULAR": 0.95}
    for on_fail, esperado in (("review", engine.DOC_REVIEW), ("info", engine.DOC_APPROVED)):
        entry = entry_con(type_check={"on_fail": on_fail})
        tc = type_check.evaluate(NO_CORRESPONDE, {}, {}, entry).to_json()
        status, _, _ = resolver(entry, tc, entidades, conf)
        assert status == esperado, on_fail


def test_corresponde_o_no_evaluado_no_agrega_check_ni_toca_la_confianza():
    entry = entry_con()
    entidades, conf = {"BANCO": "AZTECA", "TITULAR": "JUAN"}, {"BANCO": 0.95, "TITULAR": 0.95}
    for tc in (type_check.evaluate(CORRESPONDE, entidades, conf, entry).to_json(),
               type_check.not_evaluated("prueba").to_json(), None):
        status, checks, confianza = resolver(entry, tc, entidades, conf)
        assert status == engine.DOC_APPROVED
        assert not any(c.rule == type_check.RULE_NAME for c in checks)
        assert confianza > 0.9


def test_aggregate_resume_los_documentos_principales():
    mal = {"documentType": "A", "pageStart": 1, "typeCheck": {
        "evaluated": True, "matches": False, "rejectionConfidence": 0.8, "detected": "Factura"}}
    peor = {"documentType": "A", "pageStart": 2, "typeCheck": {
        "evaluated": True, "matches": False, "rejectionConfidence": 0.95, "detected": "Recibo"}}
    bien = {"documentType": "B", "pageStart": 3, "typeCheck": {
        "evaluated": True, "matches": True, "rejectionConfidence": 0.0, "detected": None}}
    otro = {"documentType": "OTRO", "pageStart": 4, "typeCheck": {
        "evaluated": True, "matches": False, "rejectionConfidence": 1.0, "detected": "X"}}
    faltante = {"documentType": "A", "pageStart": None, "typeCheck": mal["typeCheck"]}

    assert type_check.aggregate([bien]) == {"matches": True, "rejectionConfidence": 0.0, "detected": None}
    assert type_check.aggregate([bien, mal, peor]) == {
        "matches": False, "rejectionConfidence": 0.95, "detected": "Factura"}
    assert type_check.aggregate([otro, faltante]) == {"matches": None, "rejectionConfidence": 0.0, "detected": None}
    assert type_check.aggregate([{"documentType": "A", "pageStart": 1}])["matches"] is None


# ------------------------------------------------------------- extracción


@pytest.fixture
def modelo_falso(monkeypatch):
    """Captura cada llamada y contesta lo que diga `respuestas` (por orden)."""
    estado = {"llamadas": [], "respuestas": []}

    def fake_converse(prompt, **kwargs):
        estado["llamadas"].append({"prompt": prompt, **kwargs})
        respuesta = estado["respuestas"].pop(0) if estado["respuestas"] else {}
        return respuesta, {"usage": {"outputTokens": 10}}

    monkeypatch.setattr(text_extractor, "converse_structured", fake_converse)
    monkeypatch.setattr(text_extractor, "resolve_model", lambda *a, **k: "modelo-prueba")
    return estado


def test_la_senal_viaja_en_el_mismo_tool_call(modelo_falso):
    modelo_falso["respuestas"] = [{
        "d": [False, 0.95, "Constancia fiscal del SAT"],
        "s": {"BANCO": ["SAT", 0.5]},
        "t": {},
    }]

    result = text_extractor.extract_document(PAGINAS, entry_con())

    assert len(modelo_falso["llamadas"]) == 1, "cero llamadas extra"
    llamada = modelo_falso["llamadas"][0]
    assert "d" in llamada["tool_schema"]["properties"]
    assert "_DOCUMENTO" not in result.entity_map, "la señal no es un campo"
    assert result.entity_map["BANCO"] == "SAT"
    assert result.meta["typeSignal"]["corresponde"] is False
    assert result.meta["typeSignal"]["descripcion"] == "Constancia fiscal del SAT"


def test_el_prompt_pide_confirmar_el_tipo_declarado():
    prompt, _ = text_extractor._build_field_tree_prompts(entry_con(), PAGINAS)
    assert "DECLARADO como PRUEBA" in prompt
    assert '"_DOCUMENTO"' in prompt
    assert "SI corresponde" in prompt, "una variante del mismo documento no es otro documento"
    assert "Extrae los campos de todos modos" in prompt


def test_la_particion_de_arrays_no_pide_la_senal(monkeypatch, modelo_falso):
    monkeypatch.setattr(text_extractor, "_FIELD_TREE_SPLIT_SCHEMA_CHARS", 1)
    modelo_falso["respuestas"] = [
        {"d": [True, 0.99, None], "s": {}, "t": {}},
        {"s": {}, "t": {"MOVIMIENTOS": []}},
    ]

    result = text_extractor.extract_document(PAGINAS, entry_con())

    assert len(modelo_falso["llamadas"]) == 2
    con_senal = [c for c in modelo_falso["llamadas"] if "d" in c["tool_schema"]["properties"]]
    assert len(con_senal) == 1
    assert "MOVIMIENTOS" not in con_senal[0]["tool_schema"]["properties"]["t"]["properties"]
    sin_senal = next(c for c in modelo_falso["llamadas"] if c is not con_senal[0])
    assert '"_DOCUMENTO"' not in (sin_senal.get("system") or "") + (sin_senal.get("cache_system") or "")
    assert result.meta["typeSignal"]["corresponde"] is True


def test_varios_chunks_se_combinan_fail_open(monkeypatch, modelo_falso):
    # Una página por chunk: el default de `_chunk_pages` se fijó al definirla,
    # así que se sustituye la función y no la constante.
    monkeypatch.setattr(text_extractor, "_chunk_pages", lambda pages, *a, **k: [[p] for p in pages])
    paginas = [PageText(1, "ESTADO DE CUENTA", "native", 1.0), PageText(2, "Terminos y condiciones", "native", 1.0)]
    modelo_falso["respuestas"] = [
        {"d": [True, 0.99, None], "s": {}, "t": {}},
        {"d": [False, 0.95, "Otro"], "s": {}, "t": {}},
    ]

    result = text_extractor.extract_document(paginas, entry_con())

    assert result.meta["chunks"] == 2
    assert result.meta["typeSignal"]["corresponde"] is True
    assert result.meta["typeSignal"]["signals"] == 2


def test_sin_respuesta_del_modelo_no_hay_senal(modelo_falso):
    modelo_falso["respuestas"] = [{"s": {}, "t": {}}]
    result = text_extractor.extract_document(PAGINAS, entry_con())
    assert result.meta["typeSignal"] is None
