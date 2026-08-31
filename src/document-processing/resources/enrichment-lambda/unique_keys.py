"""Unique-key lookup entries for a processed document.

A document type can declare, in its catalog schema, the field whose value
identifies ONE document of that type on its own — a pedimento number, a CFDI
UUID, a policy number. This module turns each such value into a row in the
lookup table so `GET /documents?docType=X&field=Y&value=Z` resolves with a
single indexed Query instead of walking every document of the type and
matching in the Lambda (which is what `query-lambda/index.mjs` still does as
the fallback, and what it did exclusively before this existed).

The key is ALWAYS `TIPODOC#<valor normalizado>`:

    PEDIMENTO_ADUANAL#24473456400012
    FACTURA_HONORARIOS_XML#A1B2C3D4-...-9F

Scoping the key by document type is what makes it safe to declare "unique":
two different types may legitimately use the same number space, and the type
prefix keeps them apart while still letting one Query answer "give me THE
pedimento numbered X". It is also what makes the row self-describing — the
partition key alone says which type it belongs to.

Table shape (`UNIQUE_KEY_TABLE`, see `document-validation-stack.ts`):

    UniqueKey        (S)  PK — "TIPODOC#VALOR_NORMALIZADO"
    DocumentId       (S)  SK — the upload this value was found in
    DocumentType     (S)  the type the key belongs to (the prefix, expanded)
    FieldName        (S)  which catalog field produced it
    Value            (S)  the value as extracted, verbatim
    NormalizedValue  (S)  what went into the key
    ContainerType    (S)  present only when the document was found INSIDE
                          another one (a pedimento inside an expediente)
    ProcessedAt      (S)  same value as the DocumentTypeIndex sort key
    ValidationStatus (S)  APROBADO | RECHAZADO | REQUIERE_REVISION
    DocumentName     (S)  source file name, when known
    IndexedAt        (S)  when this row was written

`DocumentId` is the SORT key, not part of the partition key, on purpose: the
same business identifier legitimately shows up in more than one upload (a
re-upload, a corrected expediente, the same pedimento referenced by two
files), and collapsing those onto one row would silently lose all but the
last. One Query on the partition key returns every upload that carries the
value; the caller decides which one it wants.

Multi-document expedientes are the reason this is a separate table rather
than another attribute on the document item: one uploaded PDF holds N
documents, each with its own type and its own unique key, and a GSI partition
key can only ever hold one value per item.

The write is best-effort, exactly like `indexing.py`: a lookup-table failure
must never fail validation, and a missing row only costs the search its fast
path — never a wrong answer, since the query Lambda falls back to the
existing per-type walk when the lookup misses.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from document_catalog import get_type_entry

logger = logging.getLogger(__name__)

_UNIQUE_KEY_TABLE = os.environ.get("UNIQUE_KEY_TABLE")
_DEFAULT_TYPE = "OTRO"
_DEFAULT_STATUS = "REQUIERE_REVISION"
# Upper bound on rows written for one upload. Only reachable by a type that
# declares several unique keys inside a large expediente; past it we stop
# writing and log, which costs those values their fast path and nothing else
# (the query Lambda still finds them through the per-type walk).
_MAX_KEYS_PER_DOCUMENT = 200

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_NON_DIGIT = re.compile(r"[^0-9]")

# The separator between the type and the value. A literal "#" cannot appear in
# a TYPE_ID (they are UPPER_SNAKE_CASE), so the split is always unambiguous.
KEY_SEPARATOR = "#"


def normalize_value(value: Any, mode: str = "upper") -> str:
    """Deterministic normalization of a raw extracted value for the key.

    Mirrored character-for-character in `query-lambda/index.mjs`
    (`normalizeKeyValue`) — the writer and the reader MUST agree or every
    lookup misses. Change one, change the other.

        upper  (default) trim + collapse internal whitespace + uppercase
        alnum            `upper`, then drop everything but A-Z0-9
        digits           keep only 0-9
        raw              trim + collapse internal whitespace, case preserved

    `alnum` is the mode to reach for when the same identifier is printed with
    different separators across documents (`24  47  3456  4000123` on the
    pedimento vs `24473456400012` in a system export) — it makes both resolve
    to the same key. `raw` is for values whose case is meaningful.
    """
    text = _WHITESPACE.sub(" ", str(value if value is not None else "").strip())
    if not text:
        return ""
    if mode == "raw":
        return text
    text = text.upper()
    if mode == "alnum":
        return _NON_ALNUM.sub("", text)
    if mode == "digits":
        return _NON_DIGIT.sub("", text)
    return text


def build_key(type_id: str, value: Any, mode: str = "upper") -> str:
    """`TIPODOC#VALOR_NORMALIZADO`, or "" when there is nothing to key on."""
    normalized = normalize_value(value, mode)
    type_id = str(type_id or "").strip().upper()
    if not normalized or not type_id:
        return ""
    return f"{type_id}{KEY_SEPARATOR}{normalized}"


def unique_key_specs(type_id: str) -> List[Dict[str, Any]]:
    """The `[{field, normalize}]` this type declares — see
    `document_catalog._parse_unique_keys`. Empty for a type that declares
    none, which is the "nothing changes" default."""
    return list(get_type_entry(type_id).unique_keys or [])


def _values(entities: Dict[str, Any]) -> Dict[str, Any]:
    """Flat `{FIELD: value}` from either `extractedEntities` shape.

    The envelope carries two: the field-centric tree at the top level
    (`{value, confidence, valid, ...}` per field, see
    `handler._entities_for_envelope`) and the already-flattened scalar map
    per document inside `expediente.documents[]`
    (`handler._scalar_entities_from_field_tree`). Reading both here means the
    caller never has to care which pipeline produced the result.
    """
    out: Dict[str, Any] = {}
    for name, node in (entities or {}).items():
        if isinstance(node, dict):
            if "value" in node:
                out[name] = node["value"]
        elif not isinstance(node, list):
            out[name] = node
    return out


def _document_sources(
    result: Dict[str, Any]
) -> List[Tuple[str, Dict[str, Any], Optional[str]]]:
    """`(document_type, values, container_type)` per document in the result.

    A single-document result yields one entry with no container. An
    expediente yields one per document it contains, each carrying the
    container's own classification so a hit can say where it was found.
    """
    classification = str(
        result.get("documentClassification") or _DEFAULT_TYPE
    ).strip().upper() or _DEFAULT_TYPE
    documents = (result.get("expediente") or {}).get("documents") or []
    if not documents:
        return [(classification, _values(result.get("extractedEntities")), None)]

    sources: List[Tuple[str, Dict[str, Any], Optional[str]]] = []
    for doc in documents:
        doc_type = str(doc.get("documentType") or "").strip().upper()
        if not doc_type or doc_type == "OTRO":
            continue
        container = classification if classification != doc_type else None
        sources.append((doc_type, _values(doc.get("extractedEntities")), container))
    return sources


def derive_unique_keys(
    result: Dict[str, Any], source_key: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Every lookup row this result should produce, deduplicated.

    Deduplication is by `UniqueKey` alone (the `DocumentId` sort key is the
    same for all of them): two fields of the same document normalizing to the
    same key, or the same pedimento segmented twice, write one row rather
    than fighting over it. Order is preserved, so the first declaration wins
    the `FieldName` attribution.
    """
    document_id = result.get("documentId")
    if not document_id or document_id == "unknown":
        return []

    expediente = result.get("expediente") or {}
    processed_at = str(
        result.get("processedAt") or expediente.get("processedAt") or _now()
    )
    status = str((result.get("validation") or {}).get("status") or _DEFAULT_STATUS)
    document_name = str(
        source_key or expediente.get("sourceFile") or ""
    ).rsplit("/", 1)[-1]
    indexed_at = _now()

    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for doc_type, values, container in _document_sources(result):
        for spec in unique_key_specs(doc_type):
            raw_value = values.get(spec["field"])
            key = build_key(doc_type, raw_value, spec.get("normalize", "upper"))
            if not key or key in seen:
                continue
            if len(rows) >= _MAX_KEYS_PER_DOCUMENT:
                logger.warning(
                    "unique-key cap (%s) reached for %s — remaining values keep "
                    "the per-type search path",
                    _MAX_KEYS_PER_DOCUMENT, document_id,
                )
                return rows
            seen.add(key)
            rows.append(
                {
                    "UniqueKey": key,
                    "DocumentId": document_id,
                    "DocumentType": doc_type,
                    "FieldName": spec["field"],
                    "Value": str(raw_value),
                    "NormalizedValue": key.split(KEY_SEPARATOR, 1)[1],
                    "ProcessedAt": processed_at,
                    "ValidationStatus": status,
                    "IndexedAt": indexed_at,
                    **({"ContainerType": container} if container else {}),
                    **({"DocumentName": document_name} if document_name else {}),
                }
            )
    return rows


def write_unique_keys(
    result: Dict[str, Any], source_key: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Persist the lookup rows for this result. Best-effort.

    Returns the rows written (empty when the table is unset, the type
    declares no unique key, or the write failed). Rows are overwritten by
    (UniqueKey, DocumentId), so reprocessing the same upload is idempotent
    and a corrected value simply adds a row — the stale one is left in place
    rather than guessed at, since only a full reindex can know it is stale.
    """
    if not _UNIQUE_KEY_TABLE:
        return []
    rows = derive_unique_keys(result, source_key)
    if not rows:
        return []

    try:
        import boto3  # deferred: the Lambda runtime provides it

        table = boto3.resource("dynamodb").Table(_UNIQUE_KEY_TABLE)
        with table.batch_writer(overwrite_by_pkeys=["UniqueKey", "DocumentId"]) as batch:
            for row in rows:
                batch.put_item(Item=row)
        return rows
    except Exception as e:  # noqa: BLE001 - lookups must never fail validation
        logger.error(
            "failed to write %s unique-key row(s) for %s: %s",
            len(rows), result.get("documentId"), e,
        )
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def key_column_names() -> Iterable[str]:
    """Attributes this module owns (used by the backfill script)."""
    return (
        "UniqueKey",
        "DocumentId",
        "DocumentType",
        "FieldName",
        "Value",
        "NormalizedValue",
        "ContainerType",
        "ProcessedAt",
        "ValidationStatus",
        "DocumentName",
        "IndexedAt",
    )
