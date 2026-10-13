"""Fingerprint store — layer 4 of the physical-authenticity stage: memory
across documents, and the registry of each supplier's valid references.

Why a memory at all: the first three layers judge ONE document. The strongest
evidence of a forged letter is only visible across SEVERAL — the very same
signature/stamp image under the letters of three different employees. A wet
signature is never drawn twice alike; a pixel-identical one was pasted. This is
also the only layer that survives the analog hole (print the composed letter,
re-scan it): the structure and the paper noise become genuine, the ink stays
identical from one letter to the next.

One DynamoDB table (`FINGERPRINT_TABLE`), four item families by `Pk` prefix:

  E#<TYPE>#<scope>#<KIND>   one row per element SEEN on a letter: its ink
                            fingerprint (`pixel_forensics.ink_grid`).
  X#<sha256>                one row per letter that embeds that exact image
                            stream UNDER a signature/stamp (never letterheads:
                            a logo file legitimately repeats on every letter).
  L#<TYPE>#<scope>          one row per letter, to notice a re-submission.
  R#<scope>#<key>           the supplier's REGISTERED references (written by
                            `pnpm refs:*`, only read here); image bytes in S3.

For the memory families (E#, X#, L#) `Sk` is `<letterKey>#<fileSha12>`, so
re-processing the same file (a Step Functions retry, a re-upload) lands on the
same rows instead of incriminating itself. For R# it is `<KIND>#<refId>`.

Write first, then read: of three forged letters uploaded at the same moment,
whichever is processed second already finds the first. Everything here is
best-effort — no table configured, or any AWS error, yields "unknown" (the
computed fields stay empty), never a crash and never a false accusation.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from . import pixel_forensics

logger = logging.getLogger(__name__)

DEFAULT_MEMORY: Dict[str, Any] = {
    # At or above: the same ink. Set by MEASUREMENT on real letters, not taste
    # (catalogs/README.md → "Autenticidad física" → calibración): two genuine
    # signatures by the same signer scored 0.10–0.50 (max 0.69); a pasted
    # signature reused across letters scored 0.90–1.00.
    "element_similarity_reject": 0.97,
    # At or above: suspiciously alike; a person should look.
    "element_similarity_review": 0.85,
    "max_candidates": 2000,
}
# An element overrides either threshold in its own catalog entry
# (`similarity_reject` / `similarity_review`; null = never). It matters: a
# rubber stamp is a rigid object BUILT to leave the same mark, and genuine
# impressions measured 0.80–0.85 alike — so for a stamp, likeness alone must
# never reject; only the exact-image proof does.

NO_SCOPE = "_"


def table_name() -> Optional[str]:
    return os.environ.get("FINGERPRINT_TABLE") or None


def bucket_name() -> Optional[str]:
    return os.environ.get("DOCUMENT_BUCKET") or None


def configured() -> bool:
    return bool(table_name())


def read_only() -> bool:
    """`FINGERPRINT_READONLY=true`: look things up, never write. For a LOCAL run
    pointed at a deployed table (`pnpm procesar --refs-env dev`): it gets the
    registered references and sees reuse against real history, without leaving
    fingerprints of test letters behind for real uploads to collide with."""
    return os.environ.get("FINGERPRINT_READONLY", "").strip().lower() in ("1", "true", "yes")


def _table():
    import aws_clients  # deferred: the Lambda runtime provides boto3

    return aws_clients.table(table_name())


def _s3():
    import aws_clients

    return aws_clients.client("s3")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _binary(value: Any) -> Optional[bytes]:
    """DynamoDB hands Binary back wrapped; a fake table in tests hands bytes."""
    if value is None:
        return None
    return bytes(getattr(value, "value", value))


def _query_all(table: Any, pk: str, limit: int, sk_prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    condition = Key("Pk").eq(pk)
    if sk_prefix:
        condition = condition & Key("Sk").begins_with(sk_prefix)
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"KeyConditionExpression": condition}
    while len(items) < limit:
        page = table.query(**kwargs)
        items.extend(page.get("Items") or [])
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items[:limit]


# ------------------------------------------------------------------ memory


def record_and_find(
    *,
    type_id: str,
    scope_key: Optional[str],
    letter_key: str,
    file_sha256: str,
    document_id: Optional[str],
    element_grids: Dict[str, Optional[bytes]],
    exact_images: Iterable[Dict[str, Any]],
    spec: Optional[Dict[str, Any]] = None,
    element_thresholds: Optional[Dict[str, Dict[str, Any]]] = None,
    table: Any = None,
) -> Dict[str, Any]:
    """Record this letter's fingerprints and report what they collide with.

    Returns `{"evaluated", "matches": [...], "duplicateOf": [documentId...]}`.
    A match is another LETTER (different `letter_key` AND different file)
    carrying the same ink: `{"kind", "similarity", "exact", "level"
    ("reject"|"review"), "otherDocumentId", "otherLetterKey"}`.
    The same letter seen again is a duplicate, not a match.
    """
    outcome: Dict[str, Any] = {"evaluated": False, "matches": [], "duplicateOf": []}
    if table is None:
        if not configured():
            return outcome
        try:
            table = _table()
        except Exception as e:
            logger.warning("fingerprint_store: no table: %s", e)
            return outcome

    cfg = {**DEFAULT_MEMORY, **{k: v for k, v in (spec or {}).items() if k in DEFAULT_MEMORY}}
    scope = scope_key or NO_SCOPE
    sk = f"{letter_key}#{file_sha256[:12]}"
    common = {
        "Sk": sk, "LetterKey": letter_key, "FileSha256": file_sha256,
        "DocumentId": document_id or "", "Type": type_id, "ScopeKey": scope, "SeenAt": _now(),
    }

    def is_self(item: Dict[str, Any]) -> bool:
        return item.get("LetterKey") == letter_key or item.get("FileSha256") == file_sha256

    if read_only():
        outcome["readOnly"] = True
        table = _ReadOnly(table)

    try:
        letter_pk = f"L#{type_id}#{scope}"
        table.put_item(Item={"Pk": letter_pk, **common})
        for other in _query_all(table, letter_pk, 50, sk_prefix=f"{letter_key}#"):
            other_doc = other.get("DocumentId") or ""
            if other_doc and other_doc != (document_id or "") and other_doc not in outcome["duplicateOf"]:
                outcome["duplicateOf"].append(other_doc)

        for image in exact_images:
            sha, kind = image.get("sha256"), image.get("kind")
            if not sha or not kind:
                continue
            pk = f"X#{sha}"
            table.put_item(Item={"Pk": pk, "Kind": kind, **common})
            for other in _query_all(table, pk, 50):
                if is_self(other):
                    continue
                outcome["matches"].append(_match(kind, 1.0, True, "reject", other))
                break  # one proof per image is enough

        for kind, grid in element_grids.items():
            if not grid:
                continue
            pk = f"E#{type_id}#{scope}#{kind}"
            table.put_item(Item={"Pk": pk, "InkGrid": grid, **common})
            best: Optional[Dict[str, Any]] = None
            best_similarity = 0.0
            for other in _query_all(table, pk, int(cfg["max_candidates"])):
                if is_self(other):
                    continue
                similarity = pixel_forensics.grid_similarity(grid, _binary(other.get("InkGrid")))
                if similarity > best_similarity:
                    best, best_similarity = other, similarity
            reject_at, review_at = _thresholds(cfg, (element_thresholds or {}).get(kind))
            if best is None:
                continue
            if reject_at is not None and best_similarity >= reject_at:
                outcome["matches"].append(_match(kind, best_similarity, False, "reject", best))
            elif review_at is not None and best_similarity >= review_at:
                outcome["matches"].append(_match(kind, best_similarity, False, "review", best))

        outcome["evaluated"] = True
    except Exception as e:  # best-effort: never fail the document over its memory
        logger.warning("fingerprint_store: record_and_find failed: %s", e)
        outcome["error"] = str(e)[:200]
    return outcome


class _ReadOnly:
    """Same table, writes dropped — see `read_only`."""

    def __init__(self, table: Any):
        self._table = table

    def put_item(self, **_: Any) -> None:
        return None

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        return self._table.query(**kwargs)


def _thresholds(cfg: Dict[str, Any], override: Optional[Dict[str, Any]]):
    """(reject_at, review_at) for one element; None disables that level."""
    override = override or {}

    def pick(key: str, default: Any) -> Optional[float]:
        value = override[key] if key in override else default
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    return (pick("similarity_reject", cfg["element_similarity_reject"]),
            pick("similarity_review", cfg["element_similarity_review"]))


def _match(kind: str, similarity: float, exact: bool, level: str, other: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": kind, "similarity": similarity, "exact": exact, "level": level,
        "otherDocumentId": other.get("DocumentId") or None,
        "otherLetterKey": other.get("LetterKey") or None,
    }


# ------------------------------------------------------------------ references


STATUS_PENDING = "PENDING"
STATUS_ACTIVE = "ACTIVE"
STATUS_INACTIVE = "INACTIVE"

_KEY_NOT_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")


def normalize_key(value: Any) -> Optional[str]:
    """The ONE normalizer of a reference key (a supplier number, an RFC…).
    Whoever writes a reference and whoever looks it up must agree on it to the
    character, so the pipeline, the CLI and the API all go through here: a key
    registered raw and looked up sanitized is a reference nobody ever finds."""
    text = str(value).strip() if value not in (None, "") else ""
    return _KEY_NOT_ALLOWED.sub("", text) or None


def reference_pk(scope: str, key: str) -> str:
    return f"R#{scope}#{key}"


def _reference_view(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind, _, ref_id = str(item.get("Sk") or "").partition("#")
    if not kind or not ref_id:
        return None
    return {
        "kind": kind,
        "refId": ref_id,
        "label": item.get("Label") or "",
        "s3Key": item.get("S3Key"),
        "format": item.get("Format") or "png",
        "inkGrid": _binary(item.get("InkGrid")),
        "embedding": _binary(item.get("Embedding")),
        "embeddingModel": item.get("EmbeddingModel"),
    }


def is_usable(item: Dict[str, Any]) -> bool:
    """A reference a document may be compared against: not deactivated and not
    a registration still waiting for its upload. Rows written before `Status`
    existed carry none and count as active."""
    return item.get("Active") is not False and item.get("Status") in (None, STATUS_ACTIVE)


def list_references(scope: str, key: str, table: Any = None) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Active registered references of one supplier, by element KIND — metadata
    only (no image bytes). `{}` = looked and found none (the supplier simply has
    nothing registered yet); None = could not look (no table / AWS error)."""
    if table is None:
        if not configured():
            return None
        try:
            table = _table()
        except Exception as e:
            logger.warning("fingerprint_store: no table: %s", e)
            return None
    try:
        items = _query_all(table, reference_pk(scope, key), 500)
    except Exception as e:
        logger.warning("fingerprint_store: list_references failed: %s", e)
        return None
    out: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        if not is_usable(item):
            continue
        view = _reference_view(item)
        if view is not None:
            out.setdefault(view.pop("kind"), []).append(view)
    return out


def get_reference(scope: str, key: str, kind: str, ref_id: str, table: Any = None) -> Optional[Dict[str, Any]]:
    """One usable reference by its address, or None (missing, inactive,
    pending, no table, AWS error). Used when a vector search names it."""
    if table is None:
        if not configured():
            return None
        try:
            table = _table()
        except Exception as e:
            logger.warning("fingerprint_store: no table: %s", e)
            return None
    try:
        item = table.get_item(Key={"Pk": reference_pk(scope, key), "Sk": f"{kind}#{ref_id}"}).get("Item")
    except Exception as e:
        logger.warning("fingerprint_store: get_reference failed: %s", e)
        return None
    if not item or not is_usable(item):
        return None
    view = _reference_view(item)
    if view is not None:
        view.pop("kind")
    return view


def rank_references(
    references: List[Dict[str, Any]], grid: Optional[bytes], limit: int,
    vector: Any = None, model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """The `limit` references most like this crop — so a supplier with ten
    registered signers still costs three reference images per call, and the
    likeliest one is among them. Each result says how it was ranked
    (`rankedBy`):

      EMBEDDING  cosine against the crop's `vector`, when EVERY reference
                 carries an embedding made by that same `model` (vectors of
                 different models are not comparable, so it is all or nothing);
      HUELLA     the ink fingerprint, for elements that have one;
      ORDEN      nothing to rank by: the stored order, stable.
    """
    if vector is not None and model and references and all(
        r.get("embedding") and r.get("embeddingModel") == model for r in references
    ):
        from . import reference_embeddings

        scored = [{**ref, "similarity": reference_embeddings.cosine(vector, ref["embedding"]),
                   "rankedBy": "EMBEDDING"} for ref in references]
    else:
        by = "HUELLA" if grid and any(r.get("inkGrid") for r in references) else "ORDEN"
        scored = [
            {**ref, "similarity": pixel_forensics.grid_similarity(grid, ref.get("inkGrid")) if grid else 0.0,
             "rankedBy": by}
            for ref in references
        ]
    scored.sort(key=lambda r: r["similarity"], reverse=True)
    return scored[:max(limit, 0)]


def fetch_reference_bytes(reference: Dict[str, Any], s3: Any = None) -> Optional[bytes]:
    bucket, key = bucket_name(), reference.get("s3Key")
    if not bucket or not key:
        return None
    try:
        return (s3 or _s3()).get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as e:
        logger.warning("fingerprint_store: could not read reference %s: %s", key, e)
        return None
