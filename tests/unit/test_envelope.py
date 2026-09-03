"""Envelope v2: un documento = un elemento de `documents[]`.

Lo que estas pruebas fijan es el CONTRATO, no la implementación: qué campo
sobrevive, cuál se omite, dónde vive cada dato y qué pasa cuando el envelope no
cabe. Cada caso corresponde a una incoherencia real que v2 elimina — ver
`pipeline/envelope.py`.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "document-processing" / "resources" / "enrichment-lambda"))

from pipeline import envelope  # noqa: E402


def _node(value, *, confidence=0.9, valid=True, reason=None, source="MODEL_EXTRACTION"):
    return {"value": value, "confidence": confidence, "valid": valid,
            "reason": reason, "source": source}


def _doc(doc_type="FACTURA_FLETE_EXTRANJERO", **kw):
    doc = {
        "documentType": doc_type,
        "pageStart": 1,
        "pageEnd": 1,
        "status": "APPROVED",
        "confidence": 0.92,
        "fieldTreeShape": True,
        "extractedEntities": {},
    }
    doc.update(kw)
    return doc


def _result(documents, **kw):
    result = {
        "processingId": "p-1",
        "sourceFile": "raw/X/id/f.pdf",
        "sourceFormat": "pdf",
        "pages": {"totalPages": 2},
        "status": "APPROVED",
        "reviewRequired": False,
        "documents": documents,
        "processedAt": "2026-09-03T00:00:00+00:00",
    }
    result.update(kw)
    return result


def _build(result, **kw):
    args = {
        "document_id": "doc-1",
        "result": result,
        "tracking": {"createdAt": "2026-09-03T00:00:00+00:00"},
        "metadata": None,
        "result_location": {"bucket": "b", "key": "results/doc-1.json"},
        "fallback_classification": "OTRO",
    }
    args.update(kw)
    return envelope.build(**args)


# --------------------------------------------------- regla de omisión

def test_un_campo_vacio_y_valido_se_omite():
    """"No venía en el documento y nadie lo exigía" no necesita 120 bytes para
    decirse. Era el 20% de la respuesta."""
    fields = envelope.compact_fields({"OPCIONAL": _node(None, confidence=0.0)})
    assert fields == {}


def test_un_obligatorio_ausente_se_queda_con_su_razon():
    """Si se omitiera junto con los vacíos, la respuesta escondería justo el
    caso que alguien tiene que atender."""
    fields = envelope.compact_fields({
        "NUMERO_FACTURA": _node(None, confidence=0.0, valid=False,
                                reason="required field 'NUMERO_FACTURA' missing or empty"),
    })
    assert fields["NUMERO_FACTURA"]["value"] is None
    assert fields["NUMERO_FACTURA"]["valid"] is False
    assert "missing or empty" in fields["NUMERO_FACTURA"]["reason"]


def test_un_valor_descartado_por_confianza_baja_se_queda():
    """`value: null` porque la confianza no alcanzó NO es lo mismo que "no
    estaba": la confianza baja es la explicación y tiene que viajar."""
    fields = envelope.compact_fields({
        "TOTAL": _node(None, confidence=0.31, valid=False,
                       reason="value discarded: confidence 0.31 below discard floor 0.5"),
    })
    assert fields["TOTAL"]["confidence"] == 0.31
    assert "discarded" in fields["TOTAL"]["reason"]


def test_reason_y_source_solo_cuando_dicen_algo():
    """`reason: null` y `source: MODEL_EXTRACTION` son el default en casi todos
    los campos: repetirlos es ruido."""
    normal = envelope.compact_fields({"A": _node("x")})["A"]
    assert normal == {"value": "x", "confidence": 0.9, "valid": True}

    recuperado = envelope.compact_fields({"A": _node("x", source="QR_RECOVERY")})["A"]
    assert recuperado["source"] == "QR_RECOVERY"


def test_los_arreglos_conservan_todas_sus_filas():
    """`len(items)` es la señal de cuántos renglones traía el documento;
    perderla sería peor que una fila con pocas celdas."""
    node = envelope.compact_node({
        "items": [
            {"valid": True, "reason": None, "fields": {"IMPORTE": _node(10), "VACIO": _node(None)}},
            {"valid": False, "reason": "fila ilegible", "fields": {"IMPORTE": _node(None)}},
        ]
    })
    assert len(node["items"]) == 2
    assert node["items"][0]["fields"] == {"IMPORTE": {"value": 10, "confidence": 0.9, "valid": True}}
    assert node["items"][1]["valid"] is False and node["items"][1]["reason"] == "fila ilegible"


def test_objetos_anidados_vacios_desaparecen_enteros():
    assert envelope.compact_node({"valid": True, "fields": {"A": _node(None)}}) is None
    kept = envelope.compact_node({"valid": True, "fields": {"A": _node("x")}})
    assert kept == {"fields": {"A": {"value": "x", "confidence": 0.9, "valid": True}}}


# --------------------------------------------- documents[] siempre

def test_un_solo_documento_tambien_es_un_arreglo():
    """Quien consume no puede saber de antemano si un PDF trae uno o varios; una
    forma que dependa del resultado obliga a dos caminos y a equivocarse el día
    que llegan dos."""
    env = _build(_result([_doc(extractedEntities={"NUMERO_FACTURA": _node("A1")})]))
    assert isinstance(env["documents"], list) and len(env["documents"]) == 1
    assert env["documentCount"] == 1
    assert env["documents"][0]["fields"]["NUMERO_FACTURA"]["value"] == "A1"


def test_cada_documento_trae_sus_propios_renglones():
    """El bug que originó v2: los CONCEPTOS solo viajaban para el primero."""
    conceptos = {"items": [{"valid": True, "fields": {"IMPORTE": _node(5)}}]}
    env = _build(_result([
        _doc(extractedEntities={"NUMERO_FACTURA": _node("A1"), "CONCEPTOS": conceptos}),
        _doc(extractedEntities={"NUMERO_FACTURA": _node("A2"), "CONCEPTOS": conceptos},
             pageStart=2, pageEnd=2),
    ]))
    for i, numero in enumerate(("A1", "A2")):
        fields = env["documents"][i]["fields"]
        assert fields["NUMERO_FACTURA"]["value"] == numero
        assert len(fields["CONCEPTOS"]["items"]) == 1


def test_ningun_dato_aparece_dos_veces():
    env = _build(_result([_doc(extractedEntities={"NUMERO_FACTURA": _node("A1")})]))
    assert "extractedEntities" not in env
    assert "expediente" not in env
    # El tracking se duplicaba verbatim dentro de `expediente`.
    assert json.dumps(env).count('"createdAt"') == 1


# ------------------------------------------------ clasificación

def test_varios_documentos_del_mismo_tipo_se_clasifican_con_ese_tipo():
    """Un PDF con dos facturas es un archivo de facturas. Etiquetarlo con el
    contenedor lo escondía de `docType=FACTURA_...` y de una app restringida a
    ese tipo."""
    env = _build(_result([_doc(), _doc(pageStart=2, pageEnd=2)]))
    assert env["documentClassification"] == "FACTURA_FLETE_EXTRANJERO"
    assert env["documentCount"] == 2


def test_tipos_mixtos_siguen_siendo_el_contenedor():
    env = _build(_result([_doc(), _doc("PEDIMENTO_ADUANAL", pageStart=2, pageEnd=2)]))
    assert env["documentClassification"] == envelope.CONTAINER_TYPE


def test_los_documentos_OTRO_no_cuentan_pero_no_se_borran():
    """Las copias duplicadas se marcan OTRO en vez de eliminarse, que es lo que
    hace auditable que las páginas están todas cubiertas."""
    env = _build(_result([
        _doc(),
        _doc("OTRO", documentSubtype="COPIA_DUPLICADA", pageStart=2, pageEnd=2),
    ]))
    assert env["documentCount"] == 1
    assert len(env["documents"]) == 2
    assert env["documentClassification"] == "FACTURA_FLETE_EXTRANJERO"
    assert env["documents"][1]["documentSubtype"] == "COPIA_DUPLICADA"


def test_sin_documentos_principales_manda_la_clasificacion_de_entrada():
    env = _build(_result([_doc("OTRO")]), fallback_classification="PEDIMENTO_ADUANAL")
    assert env["documentClassification"] == "PEDIMENTO_ADUANAL"


# ------------------------------------------------------ presupuesto

def _big_result(rows=40, docs=6):
    """Grande para 20 KB, holgado para el presupuesto real de 180 KB: así el
    recorte lo dispara la prueba y no `build()` por su cuenta."""
    conceptos = {
        "items": [
            {"valid": True, "fields": {f"C{j}": _node(f"valor largo numero {i}-{j}") for j in range(8)}}
            for i in range(rows)
        ]
    }
    return _result([
        _doc(extractedEntities={"NUMERO_FACTURA": _node(f"A{i}"), "CONCEPTOS": conceptos},
             pageStart=i + 1, pageEnd=i + 1)
        for i in range(docs)
    ])


def test_lo_que_cabe_no_se_toca():
    env = _build(_result([_doc(extractedEntities={"A": _node("x")})]))
    assert "omitted" not in env


def test_suelta_primero_el_arreglo_mas_grande():
    """De mayor a menor: una factura con tres conceptos no puede perder sus
    renglones por culpa de un pedimento con cuatrocientas partidas."""
    env = _build(_big_result())
    chico = {"items": [{"valid": True, "fields": {"IMPORTE": _node(1)}}]}
    env["documents"].append({"index": 99, "documentType": "F", "fields": {"CHICO": chico}})
    env, report = envelope.fit_to_budget(env, 20_000)
    assert report["reason"] == "size"
    assert env["documents"][-1]["fields"]["CHICO"]["items"], "el arreglo chico sobrevivió"
    assert all("CONCEPTOS" in path for path in report["fields"])
    # Empate entre arreglos del mismo tamaño: gana el primero, no el último.
    assert report["fields"][0] == "documents[0].fields.CONCEPTOS"


def test_un_arreglo_soltado_conserva_su_conteo_y_se_anuncia():
    """Nunca en silencio: quien lee tiene que poder saber que hay más y dónde."""
    env, report = envelope.fit_to_budget(_build(_big_result()), 20_000)
    soltado = env["documents"][0]["fields"]["CONCEPTOS"]
    assert soltado == {"count": 40, "omitted": "size"}
    assert report["limitBytes"] == 20_000
    assert "documents[0].fields.CONCEPTOS" in report["fields"]


def test_el_recorte_es_determinista():
    a, _ = envelope.fit_to_budget(_build(_big_result()), 20_000)
    b, _ = envelope.fit_to_budget(_build(_big_result()), 20_000)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_ultimo_recurso_suelta_campos_pero_conserva_el_encabezado():
    """Aun sin campos, el documento sigue siendo localizable: tipo, páginas,
    status, y el detalle completo en `resultLocation`."""
    env, report = envelope.fit_to_budget(_build(_big_result()), 1_500)
    assert any(d.get("fieldsOmitted") == "size" for d in env["documents"])
    assert all(d["documentType"] and d["status"] for d in env["documents"])
    assert env["resultLocation"]["key"] == "results/doc-1.json"
    assert report["bytes"] > 0


def test_el_presupuesto_se_mide_en_bytes_utf8():
    """Los acentos ocupan dos bytes y son justo lo que abunda en estos
    documentos: medir caracteres subestimaría el payload real."""
    acentos = {"A": _node("ñáéíóú" * 100)}
    env = _build(_result([_doc(extractedEntities=acentos)]))
    serialized = json.dumps(env, ensure_ascii=False)
    assert envelope._size(env) > len(serialized)


# ------------------------------------------------ flujo estándar

class _Check:
    def __init__(self, field, rule, valid, message):
        self.field, self.rule, self.valid, self.message = field, rule, valid, message


def test_el_flujo_estandar_emite_la_misma_forma():
    """Un solo parser para todo el sistema: el flujo estándar tampoco expone un
    `extractedEntities` plano."""
    env = envelope.build_standard(
        document_id="doc-1",
        classification="INE",
        entity_map={"CURP": "ABCD000000HDFXXX00", "VIGENCIA": ""},
        checks=[_Check("CURP", "regex", False, "no cumple el formato")],
        status="RECHAZADO",
        tracking={"createdAt": "2026-09-03T00:00:00+00:00"},
        metadata=None,
        summary="1 de 1 checks fallaron",
    )
    assert "extractedEntities" not in env
    assert env["envelopeVersion"] == 2 and env["documentCount"] == 1
    doc = env["documents"][0]
    assert doc["documentType"] == "INE" and doc["status"] == "REJECTED"
    assert doc["fields"]["CURP"] == {
        "value": "ABCD000000HDFXXX00", "valid": False, "reason": "no cumple el formato",
    }
    # Vacío y sin queja: se omite, igual que en el otro flujo.
    assert "VIGENCIA" not in doc["fields"]
    # Sin confianza inventada: ese flujo no calcula ninguna.
    assert "confidence" not in doc["fields"]["CURP"]


# ------------------------------------------------ DSL legado

def test_un_documento_del_dsl_legado_se_ve_igual_que_uno_field_centric():
    doc = {
        "documentType": "VIEJO",
        "pageStart": 1, "pageEnd": 1, "status": "APPROVED", "confidence": 0.8,
        "fields": {
            "FOLIO": {"value": "F1", "confidence": 0.9, "valid": True, "reason": None,
                      "extractedValue": "F1", "normalizedValue": "F1", "checks": []},
            "VACIO": {"value": None, "confidence": 0.0, "valid": True, "reason": None},
        },
        "extractedEntities": {"PARTIDAS": [{"DESC": "algo"}]},
    }
    view = envelope.document_view(doc, 1)
    assert view["fields"]["FOLIO"] == {"value": "F1", "confidence": 0.9, "valid": True}
    assert "VACIO" not in view["fields"]
    assert view["fields"]["PARTIDAS"]["items"] == [{"fields": {"DESC": {"value": "algo"}}}]
