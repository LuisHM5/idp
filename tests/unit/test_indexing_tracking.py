"""`Tracking` is a top-level DynamoDB attribute (sibling of DocumentType/
ProcessedAt/..., not nested inside the EnrichmentResult JSON blob) so it's
visible/queryable on the item itself. See indexing.py::_tracking_attribute.
"""

import sys
from decimal import Decimal
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import indexing  # noqa: E402


def test_tracking_attribute_has_full_parity_with_the_nested_one():
    """Same fields build_tracking() sets on expediente.tracking (see
    pipeline/result_common.py) — this column must never lag behind it."""
    result = {
        "tracking": {
            "createdAt": "2026-08-05T00:00:00Z",
            "updatedAt": "2026-08-05T00:00:01Z",
            "processingStartedAt": "2026-08-05T00:00:00Z",
            "processingCompletedAt": "2026-08-05T00:00:01Z",
            "processingSeconds": 1.23,
            "processingMs": 1230.0,
            "schema": {"pipelineVersion": "1.0.0", "model": "model-x", "typeCount": 5},
            "appOrigin": "CEDIS",
            "modelIds": {"extractor": "model-x"},
        }
    }
    attr = indexing._tracking_attribute(result)
    assert attr == {
        "CreatedAt": "2026-08-05T00:00:00Z",
        "UpdatedAt": "2026-08-05T00:00:01Z",
        "ProcessingStartedAt": "2026-08-05T00:00:00Z",
        "ProcessingCompletedAt": "2026-08-05T00:00:01Z",
        "ProcessingSeconds": Decimal("1.23"),
        "ProcessingTime": Decimal("1230.0"),
        "Schema": {"pipelineVersion": "1.0.0", "model": "model-x", "typeCount": 5},
        "AppOrigin": "CEDIS",
        "ModelIds": {"extractor": "model-x"},
    }


def test_floats_become_decimal_everywhere_including_nested_schema():
    """boto3's DynamoDB *resource* API (used by write_index_attributes)
    rejects a raw Python float outright, anywhere in the structure — this
    must never regress back to one, including floats nested inside Schema."""
    attr = indexing._tracking_attribute(
        {"tracking": {"processingMs": 12.5, "processingSeconds": 0.0125,
                       "schema": {"someFutureFloat": 3.14}}}
    )
    assert isinstance(attr["ProcessingTime"], Decimal)
    assert isinstance(attr["ProcessingSeconds"], Decimal)
    assert isinstance(attr["Schema"]["someFutureFloat"], Decimal)
    assert not isinstance(attr["ProcessingTime"], float)


def test_omits_fields_that_are_absent():
    attr = indexing._tracking_attribute({"tracking": {"createdAt": "2026-08-05T00:00:00Z"}})
    assert attr == {"CreatedAt": "2026-08-05T00:00:00Z"}


def test_none_when_no_tracking_at_all():
    assert indexing._tracking_attribute({}) is None


def test_derive_index_attributes_includes_tracking():
    result = {
        "documentClassification": "PEDIMENTO_ADUANAL",
        "processedAt": "2026-08-05T00:00:00Z",
        "validation": {"status": "APROBADO"},
        "tracking": {"createdAt": "2026-08-05T00:00:00Z", "appOrigin": "ERP"},
    }
    attrs = indexing.derive_index_attributes(result)
    assert attrs["Tracking"] == {"CreatedAt": "2026-08-05T00:00:00Z", "AppOrigin": "ERP"}


def test_index_column_names_lists_tracking():
    assert "Tracking" in indexing.index_column_names()


def test_los_tiempos_por_etapa_llegan_a_la_columna_indexada():
    """`tracking.timings` es lo que permite responder "¿en qué se fueron los 56
    segundos?" desde el item mismo, sin desempacar el JSON del envelope. Sus
    valores son floats, que boto3 rechaza si no pasan por _decimalize."""
    attr = indexing._tracking_attribute(
        {"tracking": {"createdAt": "2026-09-03T00:00:00Z",
                      "timings": {"pageLoad": 3120.4, "extraction": 41200.1}}}
    )
    assert set(attr["Timings"]) == {"pageLoad", "extraction"}
    assert not any(isinstance(v, float) for v in attr["Timings"].values())


def test_build_tracking_solo_reporta_timings_cuando_los_hay():
    """Un pipeline que no los mide no debe emitir una llave vacía."""
    import time
    from datetime import datetime, timezone

    from pipeline.result_common import build_tracking

    now, mono = datetime.now(timezone.utc), time.time()
    assert "timings" not in build_tracking(now, mono, None)
    assert build_tracking(now, mono, None, timings={"pageLoad": 1.0})["timings"] == {"pageLoad": 1.0}


# --- la columna indexada vs la clasificación legible -------------------------


def test_un_expediente_mixto_se_indexa_bajo_el_contenedor():
    """`documentClassification` puede traer varios tipos separados por coma; la
    columna `DocumentType` no puede.

    Es la partition key del GSI y una Query busca por valor EXACTO: indexado
    bajo `A,B`, el expediente sería invisible para `?docType=A`, y la Lambda de
    consulta no tiene cómo enumerar particiones sin un Scan — justo lo que el
    par contenedor + `ContainedDocumentTypes` existe para evitar.
    """
    result = {
        "documentClassification": "PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO",
        "documents": [
            {"documentType": "PEDIMENTO_ERP"},
            {"documentType": "FACTURA_FLETE_EXTRANJERO"},
            {"documentType": "OTRO"},
        ],
    }
    attrs = indexing.derive_index_attributes(result)
    assert attrs["DocumentType"] == "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"
    # Y los tipos reales siguen siendo buscables por aquí, que es lo que hace
    # que `?docType=PEDIMENTO_ERP` encuentre el pedimento de adentro.
    assert {"PEDIMENTO_ERP", "FACTURA_FLETE_EXTRANJERO"} <= attrs["ContainedDocumentTypes"]
    # Y la clasificación con comas NO se cuela al set: `contains(...)` compara
    # elementos completos, así que sería un valor que nadie puede pedir, y el set
    # crecería con una entrada por cada combinación de tipos que existiera.
    assert not any("," in t for t in attrs["ContainedDocumentTypes"])


def test_un_tipo_unico_se_indexa_con_ese_tipo_como_siempre():
    result = {
        "documentClassification": "FACTURA_FLETE_EXTRANJERO",
        "documents": [{"documentType": "FACTURA_FLETE_EXTRANJERO"}],
    }
    attrs = indexing.derive_index_attributes(result)
    assert attrs["DocumentType"] == "FACTURA_FLETE_EXTRANJERO"


def test_un_tipo_solicitado_pero_no_encontrado_no_se_indexa_como_contenido():
    result = {
        "documentClassification": "FACTURA_FLETE_EXTRANJERO",
        "documents": [
            {"documentType": "FACTURA_FLETE_EXTRANJERO", "pageStart": 1, "pageEnd": 1},
            {"documentType": "PEDIMENTO_ERP", "pageStart": None, "pageEnd": None},
        ],
    }
    attrs = indexing.derive_index_attributes(result)
    assert "FACTURA_FLETE_EXTRANJERO" in attrs["ContainedDocumentTypes"]
    assert "PEDIMENTO_ERP" not in attrs["ContainedDocumentTypes"]
