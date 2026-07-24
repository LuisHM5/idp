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
    ContainedDocumentTypes  (SS) every type found inside an expediente
    DocumentName            (S)  source file name, when known
    IndexedAt               (S)  when these columns were written

`DocumentType` is the classification the enrichment Lambda settled on (the
segmenter's verdict for expedientes), NOT the upstream vision guess — which is
why these columns are written here and not by the workflow.

The write is best-effort: an indexing failure must never fail validation.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from document_catalog import get_type_entry

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


def _document_name(
    result: Dict[str, Any], expediente: Dict[str, Any], source_key: Optional[str]
) -> str:
    """Original file name: the S3 key's basename, or the expediente source."""
    source = source_key or expediente.get("sourceFile") or ""
    return str(source).rsplit("/", 1)[-1]


def _contained_types(classification: str, expediente: Dict[str, Any]) -> Set[str]:
    """Every document type present in the result, including the top-level one.

    For a multi-document expediente the top-level type is
    EXPEDIENTE_ADUANAL_MULTIDOCUMENTO, so searching for PEDIMENTO_ADUANAL would
    otherwise miss the pedimento inside it. This set is filterable server-side
    from the GSI (`contains(ContainedDocumentTypes, :t)`).
    """
    types: Set[str] = {classification}
    for doc in expediente.get("documents", []) or []:
        doc_type = str(doc.get("documentType") or "").strip().upper()
        if doc_type:
            types.add(doc_type)
    types.discard("")
    return types


def derive_index_attributes(
    result: Dict[str, Any], source_key: Optional[str] = None
) -> Dict[str, Any]:
    """Build the indexed columns from a canonical enrichment result."""
    classification = str(
        result.get("documentClassification") or _DEFAULT_TYPE
    ).strip().upper() or _DEFAULT_TYPE
    expediente = result.get("expediente") or {}
    validation = result.get("validation") or {}

    attributes: Dict[str, Any] = {
        "DocumentType": classification,
        "DocumentTypeName": display_name_for(classification),
        "ProcessedAt": str(
            result.get("processedAt") or expediente.get("processedAt") or _now()
        ),
        "ValidationStatus": str(validation.get("status") or _DEFAULT_STATUS),
        "ContainedDocumentTypes": _contained_types(classification, expediente),
        "IndexedAt": _now(),
    }
    name = _document_name(result, expediente, source_key)
    if name:
        attributes["DocumentName"] = name
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
    ]
