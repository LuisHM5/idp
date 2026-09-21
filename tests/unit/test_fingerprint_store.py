"""Capa 4 de autenticidad: memoria entre cartas y referencias registradas.

Una tabla DynamoDB falsa en memoria (misma interfaz `put_item`/`query` que usa
el módulo) — cero AWS. Lo que se fija:

- la misma imagen bajo la firma de la carta de OTRO empleado: rechazo;
- la misma carta reprocesada o re-subida: duplicado, jamás se auto-incrimina;
- un sello genuino parecido (la goma es rígida) NO rechaza por parecido;
- sin tabla o con error de AWS: "desconocido", nunca un crash ni una acusación.
"""

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("boto3")

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline import fingerprint_store as store  # noqa: E402
from pipeline import pixel_forensics as px  # noqa: E402


class TablaFalsa:
    def __init__(self):
        self.items = {}
        self.fallar = False

    def put_item(self, Item):
        if self.fallar:
            raise RuntimeError("DynamoDB caído")
        self.items[(Item["Pk"], Item["Sk"])] = dict(Item)

    def query(self, KeyConditionExpression, **_):
        if self.fallar:
            raise RuntimeError("DynamoDB caído")
        pk, prefijo = _condicion(KeyConditionExpression)
        return {"Items": [i for (p, s), i in self.items.items()
                          if p == pk and (prefijo is None or s.startswith(prefijo))]}


def _condicion(expr):
    """Desarma `Key("Pk").eq(x) [& Key("Sk").begins_with(y)]` de boto3."""
    valores = getattr(expr, "_values", ())
    if getattr(expr, "expression_operator", "") == "AND":
        izq, der = valores
        return izq._values[1], der._values[1]
    return valores[1], None


def huella(semilla):
    rng = np.random.default_rng(semilla)
    mascara = np.zeros((120, 300), dtype=bool)
    y = 60.0
    for x in range(10, 290):
        y = float(np.clip(y + rng.normal(0, 5), 10, 110))
        mascara[int(y) - 2:int(y) + 2, x] = True
    return px.ink_grid(mascara)


def registrar(tabla, empleado, archivo, doc_id, firma=None, sello=None, exactas=(), umbrales=None):
    return store.record_and_find(
        type_id="CARTA_X", scope_key="10037", letter_key=empleado, file_sha256=archivo * 64,
        document_id=doc_id, element_grids={"FIRMA": firma, "SELLO": sello},
        exact_images=exactas, element_thresholds=umbrales, table=tabla,
    )


def test_la_primera_carta_no_choca_con_nada():
    resultado = registrar(TablaFalsa(), "ANA|2026-09-15", "a", "doc-1", firma=huella(1))

    assert resultado == {"evaluated": True, "matches": [], "duplicateOf": []}


def test_la_misma_imagen_bajo_la_firma_de_otro_empleado_es_rechazo():
    tabla = TablaFalsa()
    pegada = [{"sha256": "f" * 64, "kind": "FIRMA"}]
    registrar(tabla, "ANA|2026-09-15", "a", "doc-1", exactas=pegada)

    resultado = registrar(tabla, "KEVIN|2026-09-15", "b", "doc-2", exactas=pegada)

    assert resultado["matches"] == [{
        "kind": "FIRMA", "similarity": 1.0, "exact": True, "level": "reject",
        "otherDocumentId": "doc-1", "otherLetterKey": "ANA|2026-09-15",
    }]


def test_la_misma_tinta_en_otra_carta_es_rechazo_aunque_no_haya_imagen_incrustada():
    """El hueco analógico: imprimieron la carta compuesta y la re-escanearon."""
    tabla = TablaFalsa()
    registrar(tabla, "ANA|2026-09-15", "a", "doc-1", firma=huella(1))

    resultado = registrar(tabla, "KEVIN|2026-09-15", "b", "doc-2", firma=huella(1))

    assert resultado["matches"][0]["level"] == "reject"
    assert resultado["matches"][0]["exact"] is False


def test_firmas_distintas_del_mismo_firmante_no_chocan():
    tabla = TablaFalsa()
    registrar(tabla, "ANA|2026-09-15", "a", "doc-1", firma=huella(1))

    assert registrar(tabla, "KEVIN|2026-09-15", "b", "doc-2", firma=huella(2))["matches"] == []


def test_reprocesar_el_mismo_archivo_no_se_auto_incrimina():
    tabla = TablaFalsa()
    pegada = [{"sha256": "f" * 64, "kind": "FIRMA"}]
    registrar(tabla, "ANA|2026-09-15", "a", "doc-1", firma=huella(1), exactas=pegada)

    otra_vez = registrar(tabla, "ANA|2026-09-15", "a", "doc-1", firma=huella(1), exactas=pegada)

    assert otra_vez["matches"] == [] and otra_vez["duplicateOf"] == []


def test_la_misma_carta_re_subida_es_duplicado_no_fraude():
    tabla = TablaFalsa()
    registrar(tabla, "ANA|2026-09-15", "a", "doc-1", firma=huella(1))

    # Mismo empleado y fecha, otro escaneo (otro archivo, otro documentId).
    resultado = registrar(tabla, "ANA|2026-09-15", "c", "doc-9", firma=huella(1))

    assert resultado["matches"] == []
    assert resultado["duplicateOf"] == ["doc-1"]


def test_un_sello_parecido_no_rechaza_si_el_catalogo_lo_desactiva():
    tabla = TablaFalsa()
    umbrales = {"SELLO": {"similarity_reject": None, "similarity_review": 0.95}}
    registrar(tabla, "ANA|2026-09-15", "a", "doc-1", sello=huella(5), umbrales=umbrales)

    resultado = registrar(tabla, "KEVIN|2026-09-15", "b", "doc-2", sello=huella(5), umbrales=umbrales)

    assert [m["level"] for m in resultado["matches"]] == ["review"]


def test_proveedores_distintos_no_comparten_particion_de_huellas():
    tabla = TablaFalsa()
    registrar(tabla, "ANA|2026-09-15", "a", "doc-1", firma=huella(1))

    otro = store.record_and_find(
        type_id="CARTA_X", scope_key="10040", letter_key="KEVIN|2026-09-15", file_sha256="b" * 64,
        document_id="doc-2", element_grids={"FIRMA": huella(1)}, exact_images=[], table=tabla,
    )

    assert otro["matches"] == []


def test_un_error_de_aws_da_desconocido_nunca_un_crash():
    tabla = TablaFalsa()
    tabla.fallar = True

    resultado = registrar(tabla, "ANA|2026-09-15", "a", "doc-1", firma=huella(1))

    assert resultado["evaluated"] is False and resultado["matches"] == []
    assert "DynamoDB caído" in resultado["error"]


def test_sin_tabla_configurada_no_se_evalua(monkeypatch):
    monkeypatch.delenv("FINGERPRINT_TABLE", raising=False)

    resultado = store.record_and_find(
        type_id="T", scope_key=None, letter_key="x", file_sha256="a" * 64, document_id=None,
        element_grids={}, exact_images=[],
    )

    assert resultado["evaluated"] is False
    assert store.list_references("PROVEEDOR", "10021") is None


def test_referencias_se_agrupan_por_elemento_y_las_inactivas_se_ignoran():
    tabla = TablaFalsa()
    pk = store.reference_pk("PROVEEDOR", "10040")
    tabla.put_item({"Pk": pk, "Sk": "FIRMA#julio-lemus", "Label": "Lic. Julio Lemus", "S3Key": "k1", "InkGrid": huella(1)})
    tabla.put_item({"Pk": pk, "Sk": "FIRMA#omar-hernandez", "Label": "Lic. Omar Hernández", "S3Key": "k2", "InkGrid": huella(2)})
    tabla.put_item({"Pk": pk, "Sk": "SELLO#planta", "Label": "Planta Envasadora", "S3Key": "k3"})
    tabla.put_item({"Pk": pk, "Sk": "SELLO#viejo", "Label": "Sello retirado", "S3Key": "k4", "Active": False})

    referencias = store.list_references("PROVEEDOR", "10040", table=tabla)

    assert sorted(referencias) == ["FIRMA", "SELLO"]
    assert [r["refId"] for r in referencias["SELLO"]] == ["planta"]
    assert store.list_references("PROVEEDOR", "99999", table=tabla) == {}


def test_se_mandan_al_modelo_las_referencias_mas_parecidas_primero():
    referencias = [
        {"refId": "otro", "inkGrid": huella(9)},
        {"refId": "el-bueno", "inkGrid": huella(4)},
        {"refId": "sin-huella", "inkGrid": None},
    ]

    elegidas = store.rank_references(referencias, huella(4), limit=2)

    assert [r["refId"] for r in elegidas][0] == "el-bueno"
    assert len(elegidas) == 2


def test_en_solo_lectura_se_compara_pero_no_se_deja_huella(monkeypatch):
    """`pnpm procesar --refs-env dev`: una prueba local ve el historial real
    sin ensuciarlo — si no, la carta de prueba saldría luego como "duplicada"
    o "reutilizada" contra el envío de verdad."""
    tabla = TablaFalsa()
    registrar(tabla, "ANA|2026-09-15", "a", "doc-1", firma=huella(1))
    antes = dict(tabla.items)
    monkeypatch.setenv("FINGERPRINT_READONLY", "true")

    resultado = registrar(tabla, "KEVIN|2026-09-15", "b", "local-x", firma=huella(1))

    assert resultado["readOnly"] is True
    assert resultado["matches"][0]["level"] == "reject"  # sí compara contra lo real
    assert tabla.items == antes                           # y no escribió nada
