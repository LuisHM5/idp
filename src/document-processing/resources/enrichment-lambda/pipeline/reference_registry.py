"""Registry of an entity's valid signatures, stamps and logos — the WRITE side
of `fingerprint_store`'s reference family (`R#<scope>#<key>` / `<KIND>#<refId>`).

One implementation for everyone who registers a reference: the operator CLI
(`scripts/manage-references.py`) and the REST API (`references_api.py`). Both
apply the same quality review, the same duplicate rules and the same
activation, so a crop the CLI would refuse cannot get in through the API.

A reference belongs to an ENTITY (`scope` + `key`: supplier 10040), never to a
document type. Which scopes exist and which elements each may hold comes from
the catalog (`authenticity.reference_scopes`).

Life of a row:

  PENDING   reserved through the API, waiting for its upload. `Active: false`,
            `ExpiresAt` set (DynamoDB TTL), image under `references-pending/`.
            Never compared against (`fingerprint_store.is_usable`).
  ACTIVE    reviewed and accepted. `ExpiresAt` REMOVED in the same write that
            activates it — otherwise the TTL would delete a live reference.
  INACTIVE  deactivated (reversible). Kept for the audit trail, out of the
            vector index.

No boto3 at import time: the table, the S3 client and the vector index are
handed in, which is also what makes this testable without AWS.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import fingerprint_store, page_raster, pixel_forensics, reference_embeddings, reference_index
from .fingerprint_store import STATUS_ACTIVE, STATUS_INACTIVE, STATUS_PENDING

logger = logging.getLogger(__name__)

MAX_BYTES = 3_500_000            # Bedrock's per-image cap, with margin
MIN_SIDE_PX = 120                # below this there is no stroke to compare
MIN_SIDE_NO_FINGERPRINT_PX = 80  # a logo (no ink fingerprint) is often wide and short
MAX_SIDE_PX = 2200               # above this it is almost certainly the whole sheet
# Checked on the image HEADER, before decoding: a 3 MB PNG can declare
# 80000x2200 pixels and take the Lambda down decoding it.
MAX_PIXELS = 12_000_000
DUPLICATE_SIMILARITY = 0.97
MAX_ACTIVE_PER_KIND = 20
PENDING_TTL_SECONDS = 24 * 3600
UPLOAD_URL_SECONDS = 900
IMAGE_URL_SECONDS = 300

PENDING_PREFIX = "references-pending"
ACTIVE_PREFIX = "references"


class RegistryError(Exception):
    """A refusal the caller should see as-is: `code` is stable (the API
    returns it as `error`), `status` is the HTTP status it maps to."""

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


# ------------------------------------------------------------------ addressing


def slug(text: str) -> str:
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


def validate_key(value: Any) -> str:
    """The key exactly as it will be looked up — or a refusal naming the form
    that would work. Never silently rewritten: `ACME S.A.` registered as
    `ACMES.A.` behind the caller's back is a reference they will not find."""
    raw = str(value if value is not None else "").strip()
    normalized = fingerprint_store.normalize_key(raw)
    if not normalized:
        raise RegistryError("INVALID_REFERENCE_KEY", "La clave está vacía o no tiene ningún carácter válido "
                            "(letras, dígitos, punto, guion y guion bajo).")
    if normalized != raw:
        raise RegistryError("INVALID_REFERENCE_KEY",
                            f"'{raw}' no es una clave válida (solo letras, dígitos, '.', '-' y '_'); usa '{normalized}'.",
                            suggested=normalized)
    return normalized


def validate_scope_and_kind(scopes: Dict[str, Dict[str, Dict[str, Any]]], scope: Any, kind: Any = None) -> Dict[str, Any]:
    """The element spec for (`scope`, `kind`) as the catalog declares it."""
    scope = str(scope or "")
    if scope not in scopes:
        raise RegistryError("INVALID_SCOPE", f"'{scope}' no es un ámbito de referencias declarado por ningún tipo "
                            f"de documento. Válidos: {', '.join(sorted(scopes)) or '(ninguno)'}.",
                            valid=sorted(scopes))
    if kind is None:
        return {}
    kind = str(kind or "")
    if kind not in scopes[scope]:
        raise RegistryError("INVALID_KIND", f"'{kind}' no es un elemento de '{scope}'. "
                            f"Válidos: {', '.join(sorted(scopes[scope]))}.", valid=sorted(scopes[scope]))
    return scopes[scope][kind]


def sort_key(kind: str, ref_id: str) -> str:
    return f"{kind}#{ref_id}"


# ------------------------------------------------------------------ review


def image_header(data: bytes) -> Optional[Dict[str, Any]]:
    """(format, width, height) read from the header only — nothing decoded."""
    real = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg" if data[:3] == b"\xff\xd8\xff" else None
    if real is None:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except Exception:
        return None
    return {"format": real, "width": int(width), "height": int(height)}


def review(data: bytes, element: Optional[Dict[str, Any]] = None, declared_format: Optional[str] = None) -> Dict[str, Any]:
    """Open a crop, fingerprint it with the SAME code the pipeline uses and
    collect what is wrong with it. `errors` block the registration, `warnings`
    do not. Each is `{"code", "message"}`."""
    element = element or {}
    fingerprinted = element.get("fingerprint", True) is not False
    card: Dict[str, Any] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "errors": [],
                            "warnings": [], "grid": None, "format": declared_format, "width": None, "height": None}

    def error(code: str, message: str) -> None:
        card["errors"].append({"code": code, "message": message})

    if len(data) > MAX_BYTES:
        error("TOO_LARGE_BYTES", f"pesa {len(data) / 1e6:.1f} MB (máx {MAX_BYTES / 1e6:.1f}); recorta o comprime")
        return card
    header = image_header(data)
    if header is None:
        error("NOT_AN_IMAGE", "no es una imagen PNG o JPEG legible")
        return card
    if declared_format and declared_format != header["format"]:
        card["warnings"].append({"code": "FORMAT_MISMATCH",
                                 "message": f"el archivo es {header['format'].upper()} aunque se declaró "
                                            f"{declared_format.upper()}: se registra como {header['format'].upper()}"})
    card.update(header)
    width, height = header["width"], header["height"]
    if width * height > MAX_PIXELS:
        error("TOO_LARGE_DIMENSIONS", f"muy grande ({width}x{height}): parece la hoja completa; recorta SOLO el elemento")
        return card
    image = page_raster.decode_image(data)
    if image is None:
        error("NOT_AN_IMAGE", "no se pudo abrir como imagen")
        return card
    height, width = image.shape[:2]  # after EXIF orientation
    card["width"], card["height"] = int(width), int(height)
    minimum = MIN_SIDE_PX if fingerprinted else MIN_SIDE_NO_FINGERPRINT_PX
    if min(height, width) < minimum:
        error("TOO_SMALL", f"muy chica ({width}x{height}); escanea a 200-300 dpi")
    if min(height, width) > MAX_SIDE_PX:
        error("TOO_LARGE_DIMENSIONS", f"muy grande ({width}x{height}): parece la hoja completa; recorta SOLO el elemento")
    if fingerprinted:
        card["grid"] = pixel_forensics.fingerprint(image)
        if card["grid"] is None:
            error("NO_INK", "no se distingue tinta sobre el fondo (¿imagen en blanco o fondo oscuro?)")
        else:
            ink = float(pixel_forensics.ink_mask(image).mean())
            if ink > 0.45:
                card["warnings"].append({"code": "TOO_MUCH_INK",
                                         "message": f"{ink:.0%} de la imagen es 'tinta': ¿trae texto o fondo de más alrededor?"})
    return card


def find_duplicate(card: Dict[str, Any], kind: str, existing: List[Dict[str, Any]],
                   own_sk: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Is this crop already registered for this entity? `{"reason": "SAME_FILE"
    | "SAME_INK", "item", "similarity"}` or None. Only usable references count;
    `own_sk` is the row being confirmed (never its own duplicate)."""
    usable = [i for i in existing if fingerprint_store.is_usable(i) and str(i.get("Sk")) != own_sk]
    for item in usable:
        if item.get("Sha256") == card["sha256"]:
            return {"reason": "SAME_FILE", "item": item, "similarity": 1.0}
    if card.get("grid"):
        for item in usable:
            if not str(item.get("Sk", "")).startswith(kind + "#"):
                continue
            similarity = pixel_forensics.grid_similarity(card["grid"], fingerprint_store._binary(item.get("InkGrid")))
            if similarity >= DUPLICATE_SIMILARITY:
                return {"reason": "SAME_INK", "item": item, "similarity": similarity}
    return None


# ------------------------------------------------------------------ views


def status_of(item: Dict[str, Any]) -> str:
    status = item.get("Status")
    if status in (STATUS_PENDING, STATUS_ACTIVE, STATUS_INACTIVE):
        return status
    return STATUS_INACTIVE if item.get("Active") is False else STATUS_ACTIVE


def is_expired(item: Dict[str, Any], now: Optional[float] = None) -> bool:
    """DynamoDB's TTL deletes lazily (hours, sometimes days): an expired
    pending row is treated as gone whether or not it still exists."""
    expires = item.get("ExpiresAt")
    return status_of(item) == STATUS_PENDING and expires is not None and float(expires) < (now or time.time())


def view(item: Dict[str, Any]) -> Dict[str, Any]:
    """What a caller gets to see of a reference: metadata. Never the ink
    fingerprint nor the embedding — they are how reuse is detected."""
    _, scope, key = str(item.get("Pk") or "R##").split("#", 2)
    kind, _, ref_id = str(item.get("Sk") or "").partition("#")
    out = {
        "scope": scope, "key": key, "kind": kind, "refId": ref_id,
        "label": item.get("Label") or "", "status": status_of(item),
        "format": item.get("Format"), "width": _int(item.get("Width")), "height": _int(item.get("Height")),
        "sha256": item.get("Sha256"), "hasInkFingerprint": bool(item.get("InkGrid")),
        "indexed": item.get("IndexStatus") == "INDEXED",
        "createdAt": item.get("CreatedAt"), "createdBy": item.get("CreatedBy"),
    }
    for source, target in (("ActivatedAt", "activatedAt"), ("DeactivatedAt", "deactivatedAt"),
                           ("DeactivatedBy", "deactivatedBy")):
        if item.get(source):
            out[target] = item[source]
    return {k: v for k, v in out.items() if v is not None}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_conditional_failure(error: Exception) -> bool:
    code = (getattr(error, "response", None) or {}).get("Error", {}).get("Code")
    return code == "ConditionalCheckFailedException"


# ------------------------------------------------------------------ registry


class Registry:
    """Reads and writes one environment's references. `index_for(provider)`
    returns the vector index for that provider (None = no vector search)."""

    def __init__(self, table: Any, s3: Any = None, bucket: str = "", provider: Any = None,
                 index_for: Any = reference_index.default_index):
        self.table, self.s3, self.bucket = table, s3, bucket
        self.provider = provider
        self._index_for = index_for

    # ---- reads

    def items(self, scope: str, key: str) -> List[Dict[str, Any]]:
        return fingerprint_store._query_all(self.table, fingerprint_store.reference_pk(scope, key), 1000)

    def get(self, scope: str, key: str, kind: str, ref_id: str) -> Optional[Dict[str, Any]]:
        item = self.table.get_item(
            Key={"Pk": fingerprint_store.reference_pk(scope, key), "Sk": sort_key(kind, ref_id)}
        ).get("Item")
        return None if not item or is_expired(item) else item

    def listing(self, scope: str, key: str, kind: Optional[str] = None,
                include_inactive: bool = False) -> List[Dict[str, Any]]:
        out = []
        for item in sorted(self.items(scope, key), key=lambda i: str(i.get("Sk"))):
            status = status_of(item)
            if status == STATUS_PENDING or (status == STATUS_INACTIVE and not include_inactive):
                continue
            if kind and not str(item.get("Sk", "")).startswith(kind + "#"):
                continue
            out.append(item)
        return out

    def image_url(self, item: Dict[str, Any]) -> Optional[str]:
        if not self.s3 or not item.get("S3Key") or status_of(item) != STATUS_ACTIVE:
            return None
        return self.s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": item["S3Key"]}, ExpiresIn=IMAGE_URL_SECONDS)

    # ---- two-step registration (API)

    def reserve(self, scope: str, key: str, kind: str, label: str, actor: str,
                source_file: Optional[str] = None) -> Dict[str, Any]:
        """Step 1: a PENDING row and where to upload the crop. `refId` is the
        slug of the label; taken by a live reference → `-2`, `-3`…; taken by a
        pending one → reused, so a retried request leaves nothing behind."""
        label = str(label or "").strip()
        base = slug(label)
        if not base:
            raise RegistryError("MISSING_FIELD", "`label` es obligatorio: quién firma o qué sucursal es el sello.")
        existing = self.items(scope, key)
        active = [i for i in existing if fingerprint_store.is_usable(i) and str(i.get("Sk", "")).startswith(kind + "#")]
        if len(active) >= MAX_ACTIVE_PER_KIND:
            raise RegistryError("REFERENCE_LIMIT_REACHED",
                                f"{scope} {key} ya tiene {len(active)} {kind} activos (máx {MAX_ACTIVE_PER_KIND}); "
                                "da de baja alguno antes de registrar otro.", status=409)
        now = time.time()
        pk = fingerprint_store.reference_pk(scope, key)
        for attempt in range(1, 21):
            ref_id = base if attempt == 1 else f"{base}-{attempt}"
            item = {
                "Pk": pk, "Sk": sort_key(kind, ref_id), "Label": label, "Active": False, "Status": STATUS_PENDING,
                "ExpiresAt": int(now + PENDING_TTL_SECONDS),
                "PendingS3Key": f"{PENDING_PREFIX}/{scope}/{key}/{kind}/{ref_id}",
                "CreatedAt": _now_iso(), "CreatedBy": actor,
                **({"SourceFile": str(source_file)[:200]} if source_file else {}),
            }
            try:
                self.table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(Pk) OR #st = :pending",
                    ExpressionAttributeNames={"#st": "Status"},
                    ExpressionAttributeValues={":pending": STATUS_PENDING},
                )
                return item
            except Exception as e:
                if not _is_conditional_failure(e):
                    raise
        raise RegistryError("REFERENCE_ID_EXHAUSTED", f"Ya hay demasiadas referencias llamadas '{base}'; usa otra etiqueta.",
                            status=409)

    def upload_url(self, item: Dict[str, Any]) -> str:
        """A presigned PUT with NO signed headers beyond `host` — same contract
        as `POST /documents`: the client sends the bytes and nothing else."""
        return self.s3.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": item["PendingS3Key"]}, ExpiresIn=UPLOAD_URL_SECONDS)

    def confirm(self, scope: str, key: str, kind: str, ref_id: str, element: Dict[str, Any],
                force: bool = False) -> Dict[str, Any]:
        """Step 3: review the uploaded crop and activate it. Idempotent — an
        already-active reference answers with itself. Returns
        `{"item", "warnings"}`; raises `RegistryError` (422 with `errors` when
        the crop does not pass, 409 on a duplicate or a missing upload)."""
        item = self.get(scope, key, kind, ref_id)
        if item is None:
            raise RegistryError("NOT_FOUND", "No existe esa referencia (o su registro pendiente expiró).", status=404)
        if status_of(item) == STATUS_ACTIVE:
            if item.get("IndexStatus") == "PENDING":
                item = self._retry_index(scope, key, kind, ref_id, item)
            return {"item": item, "warnings": []}
        if status_of(item) != STATUS_PENDING:
            raise RegistryError("NOT_PENDING", "La referencia está dada de baja; regístrala de nuevo.", status=409)

        pending_key = item["PendingS3Key"]
        data = self._read_upload(pending_key)
        card = review(data, element)
        if card["errors"]:
            self._discard(pending_key)
            raise RegistryError("REFERENCE_REJECTED", "El recorte no pasó la revisión de calidad.", status=422,
                                errors=card["errors"], warnings=card["warnings"])
        duplicate = find_duplicate(card, kind, self.items(scope, key), own_sk=sort_key(kind, ref_id))
        if duplicate and (duplicate["reason"] == "SAME_FILE" or not force):
            same_file = duplicate["reason"] == "SAME_FILE"
            other_kind, _, other_ref = str(duplicate["item"].get("Sk")).partition("#")
            raise RegistryError(
                "DUPLICATE_REFERENCE" if same_file else "SIMILAR_REFERENCE",
                (f"Ese mismo archivo ya está registrado como {other_kind}#{other_ref}." if same_file else
                 f"Misma tinta ({duplicate['similarity']:.2f}) que {other_kind}#{other_ref}: una muestra basta. "
                 "Reenvía con `force: true` para registrarla igual."),
                status=409, existing={"kind": other_kind, "refId": other_ref})

        fields = self._store(scope, key, kind, ref_id, data, card)
        try:
            self.table.update_item(
                Key={"Pk": item["Pk"], "Sk": item["Sk"]},
                # REMOVE ExpiresAt in the SAME write: an active row that kept its
                # TTL would be deleted by DynamoDB a day later.
                UpdateExpression="SET " + ", ".join(f"#{n} = :{n}" for n in fields) + " REMOVE ExpiresAt, PendingS3Key",
                ConditionExpression="#Status = :was",
                ExpressionAttributeNames={f"#{n}": n for n in fields},
                ExpressionAttributeValues={**{f":{n}": v for n, v in fields.items()}, ":was": STATUS_PENDING},
            )
        except Exception as e:
            if not _is_conditional_failure(e):
                raise
            # Lost a race with another confirm of the same reference: whoever
            # won activated it, and that is the answer.
        self._discard(pending_key)
        return {"item": self.get(scope, key, kind, ref_id) or {**item, **fields}, "warnings": card["warnings"]}

    # ---- one-step registration (CLI: the operator already has the file)

    def register(self, scope: str, key: str, kind: str, ref_id: str, label: str, data: bytes,
                 card: Dict[str, Any], actor: str, source_file: Optional[str] = None,
                 for_type: Optional[str] = None) -> Dict[str, Any]:
        fields = self._store(scope, key, kind, ref_id, data, card)
        item = {
            "Pk": fingerprint_store.reference_pk(scope, key), "Sk": sort_key(kind, ref_id), "Label": label,
            "CreatedAt": _now_iso(), "CreatedBy": actor, **fields,
            **({"SourceFile": source_file} if source_file else {}), **({"ForType": for_type} if for_type else {}),
        }
        self.table.put_item(Item=item)
        return item

    # ---- removal

    def deactivate(self, scope: str, key: str, kind: str, ref_id: str, actor: str) -> Dict[str, Any]:
        item = self.get(scope, key, kind, ref_id)
        if item is None or status_of(item) == STATUS_PENDING:
            raise RegistryError("NOT_FOUND", "No existe esa referencia.", status=404)
        changes = {"Active": False, "Status": STATUS_INACTIVE, "DeactivatedAt": _now_iso(), "DeactivatedBy": actor,
                   "IndexStatus": "NONE"}
        self.table.update_item(
            Key={"Pk": item["Pk"], "Sk": item["Sk"]},
            UpdateExpression="SET " + ", ".join(f"#{n} = :{n}" for n in changes),
            ExpressionAttributeNames={f"#{n}": n for n in changes},
            ExpressionAttributeValues={f":{n}": v for n, v in changes.items()},
        )
        self._unindex(scope, key, kind, ref_id, item)
        return {**item, **changes}

    def delete(self, scope: str, key: str, kind: str, ref_id: str) -> None:
        """Row + image + vector, irreversible. Operator-only (the CLI): fewer
        references means a weaker check, so the API can only deactivate."""
        pk = fingerprint_store.reference_pk(scope, key)
        item = self.table.get_item(Key={"Pk": pk, "Sk": sort_key(kind, ref_id)}).get("Item")
        if not item:
            raise RegistryError("NOT_FOUND", "No existe esa referencia.", status=404)
        self._unindex(scope, key, kind, ref_id, item)
        for s3_key in (item.get("S3Key"), item.get("PendingS3Key")):
            if s3_key and self.s3:
                self.s3.delete_object(Bucket=self.bucket, Key=s3_key)
        self.table.delete_item(Key={"Pk": pk, "Sk": sort_key(kind, ref_id)})

    # ---- index maintenance

    def reindex(self, item: Dict[str, Any], recompute: bool = False) -> str:
        """Bring one ACTIVE reference's embedding and index entry up to date
        with the current provider. Returns what was done: `INDEXED`,
        `EMBEDDED` (vector stored, no index configured), `SKIPPED`, `FAILED`."""
        if status_of(item) != STATUS_ACTIVE or self.provider is None:
            return "SKIPPED"
        _, scope, key = str(item["Pk"]).split("#", 2)
        kind, _, ref_id = str(item["Sk"]).partition("#")
        vector = None
        if not recompute and item.get("EmbeddingModel") == self.provider.model_id and item.get("Embedding"):
            vector = reference_embeddings.from_bytes(fingerprint_store._binary(item["Embedding"]))
        if vector is None:
            data = self._get_object(item.get("S3Key"))
            vector, _ = reference_embeddings.embed(data, self.provider) if data else (None, None)
            if vector is None:
                return "FAILED"
        fields = self._embedding_fields(scope, key, kind, ref_id, vector)
        self.table.update_item(
            Key={"Pk": item["Pk"], "Sk": item["Sk"]},
            UpdateExpression="SET " + ", ".join(f"#{n} = :{n}" for n in fields),
            ExpressionAttributeNames={f"#{n}": n for n in fields},
            ExpressionAttributeValues={f":{n}": v for n, v in fields.items()},
        )
        return {"INDEXED": "INDEXED", "NONE": "EMBEDDED"}.get(fields["IndexStatus"], "FAILED")

    # ---- internals

    def _get_object(self, s3_key: Optional[str]) -> Optional[bytes]:
        if not s3_key or not self.s3:
            return None
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=s3_key)["Body"].read()
        except Exception as e:
            logger.warning("reference_registry: could not read %s: %s", s3_key, e)
            return None

    def _read_upload(self, pending_key: str) -> bytes:
        try:
            size = int(self.s3.head_object(Bucket=self.bucket, Key=pending_key).get("ContentLength") or 0)
        except Exception:
            raise RegistryError("UPLOAD_NOT_FOUND", "Todavía no se subió la imagen a `uploadUrl` (o la URL expiró).",
                                status=409) from None
        if size > MAX_BYTES:
            # A presigned PUT cannot cap the size: refuse before downloading it.
            self._discard(pending_key)
            raise RegistryError("REFERENCE_REJECTED", "El recorte no pasó la revisión de calidad.", status=422,
                                errors=[{"code": "TOO_LARGE_BYTES",
                                         "message": f"pesa {size / 1e6:.1f} MB (máx {MAX_BYTES / 1e6:.1f}); recorta o comprime"}],
                                warnings=[])
        return self.s3.get_object(Bucket=self.bucket, Key=pending_key)["Body"].read()

    def _discard(self, s3_key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=s3_key)
        except Exception as e:  # the bucket's lifecycle rule is the backstop
            logger.warning("reference_registry: could not delete %s: %s", s3_key, e)

    def _store(self, scope: str, key: str, kind: str, ref_id: str, data: bytes, card: Dict[str, Any]) -> Dict[str, Any]:
        """Write the image where the pipeline reads it and return the row
        attributes of an ACTIVE reference (embedding and index state included)."""
        extension = "png" if card["format"] == "png" else "jpg"
        s3_key = f"{ACTIVE_PREFIX}/{scope}/{key}/{kind}/{ref_id}.{extension}"
        self.s3.put_object(Bucket=self.bucket, Key=s3_key, Body=data, ContentType=f"image/{card['format']}")
        fields: Dict[str, Any] = {
            "Active": True, "Status": STATUS_ACTIVE, "S3Key": s3_key, "Format": card["format"],
            "Sha256": card["sha256"], "Width": card["width"], "Height": card["height"], "ActivatedAt": _now_iso(),
            **({"InkGrid": card["grid"]} if card.get("grid") else {}),
        }
        vector, _ = reference_embeddings.embed(data, self.provider)
        fields.update(self._embedding_fields(scope, key, kind, ref_id, vector))
        return fields

    def _embedding_fields(self, scope: str, key: str, kind: str, ref_id: str, vector: Any) -> Dict[str, Any]:
        """`IndexStatus`: INDEXED (searchable), PENDING (has a vector or should
        have one, and the write to the index failed — `confirm` again or
        `refs:reindex` retries), NONE (no embeddings/index in this environment)."""
        if self.provider is None:
            return {"IndexStatus": "NONE"}
        if vector is None:
            return {"IndexStatus": "PENDING"}
        fields = {"Embedding": reference_embeddings.to_bytes(vector), "EmbeddingModel": self.provider.model_id,
                  "EmbeddingDim": int(vector.size)}
        index = self._index_for(self.provider) if self._index_for else None
        if index is None:
            fields["IndexStatus"] = "NONE"
        else:
            fields["IndexStatus"] = "INDEXED" if index.put(scope, key, kind, ref_id, vector, self.provider.model_id) \
                else "PENDING"
        return fields

    def _retry_index(self, scope, key, kind, ref_id, item) -> Dict[str, Any]:
        self.reindex(item)
        return self.get(scope, key, kind, ref_id) or item

    def _unindex(self, scope: str, key: str, kind: str, ref_id: str, item: Dict[str, Any]) -> None:
        if self.provider is None or not item.get("Embedding"):
            return
        index = self._index_for(self.provider) if self._index_for else None
        if index is not None:
            index.delete(scope, key, kind, ref_id)
