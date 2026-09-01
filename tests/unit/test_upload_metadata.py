"""La `metadata` que el cliente manda en `POST /documents` tiene que llegar
hasta donde se puede consultar.

Recorrido completo: upload-lambda la pega al objeto S3 → el consumidor SQS la
lee de vuelta y la mete en el input de Step Functions como `uploadMetadata`
(patches/default-sqs-consumer-index.py) → aquí acaba en el envelope como
`metadata`, que es lo que filtra `GET /documents?meta.<clave>=<valor>` → y
además se promueve a la columna `Metadata` del item de DynamoDB para poder
verla sin desempaquetar el blob `EnrichmentResult`.

Va aparte de `tracking`: son dos cosas con dueño y confianza distintos —
`tracking` lo escribe este sistema, `metadata` la escribe quien llama.
"""

import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import handler  # noqa: E402
import indexing  # noqa: E402


# --- _upload_metadata -----------------------------------------------------

def test_reads_upload_metadata_from_the_step_functions_input():
    event = {"uploadMetadata": {"lote": "A123", "sucursal": "CHIH"}}
    assert handler._upload_metadata(event) == {"lote": "A123", "sucursal": "CHIH"}


def test_absent_or_malformed_metadata_is_an_empty_dict_not_a_failure():
    """Contexto del cliente: nada de la corrección de este sistema depende de
    él, así que una forma rara se descarta en vez de envenenar el envelope."""
    assert handler._upload_metadata({}) == {}
    assert handler._upload_metadata({"uploadMetadata": None}) == {}
    assert handler._upload_metadata({"uploadMetadata": "lote=A123"}) == {}
    assert handler._upload_metadata({"uploadMetadata": ["lote"]}) == {}


def test_empty_values_are_dropped():
    event = {"uploadMetadata": {"lote": "A123", "vacio": "", "nulo": None}}
    assert handler._upload_metadata(event) == {"lote": "A123"}


def test_values_are_coerced_to_strings():
    assert handler._upload_metadata({"uploadMetadata": {"piezas": 12}}) == {"piezas": "12"}


# --- el envelope ----------------------------------------------------------

def test_metadata_lands_on_the_envelope_next_to_tracking_not_inside_it():
    result = handler.build_canonical_result(
        "doc-1", "PEDIMENTO_ADUANAL", {}, [], "APROBADO",
        app_origin="ERP",
        upload_metadata={"lote": "A123"},
    )
    assert result["metadata"] == {"lote": "A123"}
    assert result["tracking"]["appOrigin"] == "ERP"
    assert "metadata" not in result["tracking"]


def test_no_metadata_means_no_key_at_all():
    """Un envelope sin metadata no gana una llave vacía: `?meta.x=` tiene que
    poder distinguir "no mandó nada" de "mandó algo distinto"."""
    result = handler.build_canonical_result("doc-1", "PEDIMENTO_ADUANAL", {}, [], "APROBADO")
    assert "metadata" not in result


# --- la columna de DynamoDB ------------------------------------------------

def test_metadata_is_promoted_to_its_own_top_level_column():
    attrs = indexing.derive_index_attributes(
        {
            "documentId": "doc-1",
            "documentClassification": "PEDIMENTO_ADUANAL",
            "processedAt": "2026-09-01T00:00:00Z",
            "validation": {"status": "APROBADO"},
            "metadata": {"lote": "A123"},
        }
    )
    assert attrs["Metadata"] == {"lote": "A123"}


def test_no_metadata_writes_no_column():
    attrs = indexing.derive_index_attributes(
        {
            "documentId": "doc-1",
            "documentClassification": "PEDIMENTO_ADUANAL",
            "processedAt": "2026-09-01T00:00:00Z",
            "validation": {"status": "APROBADO"},
        }
    )
    assert "Metadata" not in attrs


def test_the_backfill_script_knows_about_the_column():
    """index_column_names() es lo que borra/reescribe `pnpm backfill:index` —
    una columna que no esté ahí se queda huérfana en items viejos."""
    assert "Metadata" in indexing.index_column_names()
