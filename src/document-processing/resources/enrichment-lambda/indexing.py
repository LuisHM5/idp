"""Indexed DynamoDB columns for a processed document.

The Step Functions workflow only stores `EnrichmentResult` (the whole envelope
as a JSON string), so any query by document type used to mean a full-table
Scan plus JSON parsing in the query Lambda. This module promotes the fields
worth querying to real top-level attributes so `GET /documents?docType=` can
run a DynamoDB Query against the `DocumentTypeIndex` GSI:

    DocumentType            (S)  final classification, GSI partition key
    ProcessedAt             (S)  ISO-8601 timestamp, GSI sort key
    DocumentTypeName        (S)  human-readable name ("Pedimento Aduanal")
    ValidationStatus        (S)  APROBADO | RECHAZADO | REQUIERE_REVISION
    ContainedDocumentTypes  (SS) every type found inside the file
    DocumentName            (S)  source file name, when known
    IndexedAt               (S)  when these columns were written
    Metadata                (M)  the caller's own `metadata` from
                                 `POST /documents` (see handler.py's
                                 _upload_metadata), promoted so it is readable
                                 from the item/console without parsing the
                                 `EnrichmentResult` blob. The query API
                                 filters it from the envelope, not from here
                                 — this column is for operators, not the
                                 hot path
    Tracking                (M)  {CreatedAt, UpdatedAt?, ProcessingStartedAt?,
                                 ProcessingCompletedAt?, ProcessingSeconds?,
                                 ProcessingTime?, Schema?, AppOrigin?, ModelIds?,
                                 ModelFallbacks?} — every field the envelope's
                                 `tracking.*` has (see _TRACKING_FIELD_MAP),
                                 promoted so it's on the item itself
                                 (Scan/console), not only inside the
                                 `EnrichmentResult` JSON blob

`DocumentType` is the classification the enrichment Lambda settled on (the
segmenter's verdict for expedientes), NOT the upstream vision guess — which is
why these columns are written here and not by the workflow.

The write is best-effort: an indexing failure must never fail validation.
"""

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from document_catalog import get_type_entry
from pipeline.envelope import index_type_of

logger = logging.getLogger(__name__)

_DOCUMENT_TABLE = os.environ.get("DOCUMENT_TABLE")
_DEFAULT_TYPE = "OTRO"
_DEFAULT_STATUS = "REQUIERE_REVISION"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_name_for(type_id: str) -> str:
    """Human-readable name for a type id.

    Uses the catalog's optional `display_name`, else title-cases the id
    (`PEDIMENTO_ADUANAL` → `Pedimento Aduanal`).
    """
    entry = get_type_entry(type_id)
    if entry.display_name:
        return entry.display_name
    return " ".join(part.capitalize() for part in type_id.split("_") if part)


def _document_name(result: Dict[str, Any], source_key: Optional[str]) -> str:
    """Original file name: the S3 key's basename, or the envelope's source."""
    source = source_key or result.get("sourceFile") or ""
    return str(source).rsplit("/", 1)[-1]


def _contained_types(index_type: str, result: Dict[str, Any]) -> Set[str]:
    """Every document type present in the result, including the indexed one.

    Un archivo con documentos de tipos DISTINTOS se INDEXA como
    EXPEDIENTE_ADUANAL_MULTIDOCUMENTO, así que buscar PEDIMENTO_ADUANAL se
    perdería el pedimento de adentro sin este conjunto, filtrable del lado del
    GSI (`contains(ContainedDocumentTypes, :t)`). Cuando todos los documentos
    son del mismo tipo, ese tipo YA es la clasificación (ver
    `pipeline/envelope.py::classification_of`) y esto solo lo confirma.

    Recibe el tipo INDEXADO (`envelope.index_type_of`), no la clasificación que
    ve quien llama: ésa puede ser "A,B", y ese string aquí ensuciaría el set con
    un valor que ninguna búsqueda puede pedir — `contains` compara elementos
    completos — y que además crecería con una entrada por cada combinación de
    tipos que llegara a existir.
    """
    types: Set[str] = {index_type}
    for doc in result.get("documents", []) or []:
        if (
            "pageStart" in doc
            and doc.get("pageStart") is None
            and "pageEnd" in doc
            and doc.get("pageEnd") is None
        ):
            continue
        doc_type = str(doc.get("documentType") or "").strip().upper()
        if doc_type:
            types.add(doc_type)
    types.discard("")
    return types


def _decimalize(value: Any) -> Any:
    """Recursively convert every float to Decimal — boto3's DynamoDB
    *resource* API (unlike the low-level client) rejects a raw Python float
    outright ("Float types are not supported. Use Decimal types instead."),
    anywhere in the structure, not just at the top level."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


# camelCase (envelope's `tracking`) -> PascalCase (this module's DynamoDB
# column naming convention) — every field build_tracking() sets (see
# pipeline/result_common.py) plus modelIds/appOrigin/modelFallbacks/
# segmentationDegraded, so this column never lags behind el `tracking` del
# envelope (que a partir de v2 es uno solo, ya no duplicado dentro de
# `expediente`). modelFallbacks (pipeline/bedrock.py::fallback_events) is
# present only for stages that actually switched models this run — expected
# vs. actual model and retry count, see catalogs/README.md's fallback
# section. segmentationDegraded (pipeline/bedrock.py::stage_degradations,
# populated by pipeline/segmenter.py) is present only when a page range's
# classification had to settle for a best-effort result.
_TRACKING_FIELD_MAP = {
    "createdAt": "CreatedAt",
    "updatedAt": "UpdatedAt",
    "processingStartedAt": "ProcessingStartedAt",
    "processingCompletedAt": "ProcessingCompletedAt",
    "processingSeconds": "ProcessingSeconds",
    "processingMs": "ProcessingTime",
    "uploadedAt": "UploadedAt",
    "uploadToResultSeconds": "UploadToResultSeconds",
    "uploadToResultMs": "UploadToResultMs",
    "timings": "Timings",
    "schema": "Schema",
    "appOrigin": "AppOrigin",
    "modelIds": "ModelIds",
    "modelFallbacks": "ModelFallbacks",
    "segmentationDegraded": "SegmentationDegraded",
}


def _tracking_attribute(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Top-level `Tracking` map — same fields as the envelope's own
    `tracking` object (see handler.py / _TRACKING_FIELD_MAP), just
    PascalCase-keyed to match this module's other indexed columns. None when
    there's nothing to report (defensive — `tracking.createdAt` is always
    set by the handler, so this is expected to be non-None in practice).
    """
    tracking = result.get("tracking") or {}
    if not tracking:
        return None
    out = {
        pascal_key: tracking[camel_key]
        for camel_key, pascal_key in _TRACKING_FIELD_MAP.items()
        if tracking.get(camel_key) is not None
    }
    return _decimalize(out) if out else None


def derive_index_attributes(
    result: Dict[str, Any], source_key: Optional[str] = None
) -> Dict[str, Any]:
    """Build the indexed columns from a canonical enrichment result."""
    classification = str(
        result.get("documentClassification") or _DEFAULT_TYPE
    ).strip().upper() or _DEFAULT_TYPE
    # La clasificación que ve quien llama puede traer VARIOS tipos separados por
    # coma (un expediente mixto). La columna indexada no: es la partition key de
    # un GSI y una Query busca por valor exacto. `index_type_of` la reduce al
    # contenedor, que es lo que `expandContainers` sabe consultar — ver su
    # docstring. `ContainedDocumentTypes` (abajo) sigue trayendo la lista real,
    # y es por donde `?docType=X` encuentra lo que está adentro.
    index_type = index_type_of(classification)
    validation = result.get("validation") or {}

    attributes: Dict[str, Any] = {
        "DocumentType": index_type,
        "DocumentTypeName": display_name_for(index_type),
        "ProcessedAt": str(result.get("processedAt") or _now()),
        "ValidationStatus": str(validation.get("status") or _DEFAULT_STATUS),
        "ContainedDocumentTypes": _contained_types(index_type, result),
        "IndexedAt": _now(),
    }
    name = _document_name(result, source_key)
    if name:
        attributes["DocumentName"] = name
    tracking_attr = _tracking_attribute(result)
    if tracking_attr:
        attributes["Tracking"] = tracking_attr
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and metadata:
        attributes["Metadata"] = _decimalize(metadata)
    return attributes


def write_index_attributes(
    result: Dict[str, Any], source_key: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Persist the indexed columns on the document's DynamoDB item.

    Best-effort: returns the attributes written, or None when there is nothing
    to write (no table configured, no document id) or the write failed. Uses
    UpdateItem so the attributes the workflow owns (WorkflowStatus,
    EnrichmentResult, ...) are left untouched.
    """
    document_id = result.get("documentId")
    if not _DOCUMENT_TABLE or not document_id or document_id == "unknown":
        return None

    attributes = derive_index_attributes(result, source_key)
    # Alias every name/value: several columns (Status-like names) collide with
    # DynamoDB reserved words.
    names = {f"#a{i}": key for i, key in enumerate(attributes)}
    values = {f":a{i}": attributes[key] for i, key in enumerate(attributes)}
    expression = "SET " + ", ".join(f"#a{i} = :a{i}" for i in range(len(attributes)))

    try:
        import boto3  # deferred: the Lambda runtime provides it

        boto3.resource("dynamodb").Table(_DOCUMENT_TABLE).update_item(
            Key={"DocumentId": document_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return attributes
    except Exception as e:  # noqa: BLE001 - indexing must never fail validation
        logger.error("failed to write index columns for %s: %s", document_id, e)
        return None


def index_column_names() -> List[str]:
    """Names of the columns this module owns (used by the backfill script)."""
    return [
        "DocumentType",
        "DocumentTypeName",
        "ProcessedAt",
        "ValidationStatus",
        "ContainedDocumentTypes",
        "DocumentName",
        "IndexedAt",
        "Tracking",
        "Metadata",
    ]
