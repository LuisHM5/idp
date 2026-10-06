"""Vector index of registered references — "whose stamp is this?" when the
upload did not say.

DERIVED data: the reference rows in `FingerprintTable` are the source of
truth (each one keeps its own vector), and this index can be rebuilt from
them at any time (`pnpm refs:reindex`). So every operation here is
best-effort: not configured, or any AWS error, yields None — the caller then
behaves exactly as it did before the index existed.

Behind `VectorIndex` so the store can change without touching the stage, the
API or the CLI. Today there is one implementation, Amazon S3 Vectors.

One index holds the vectors of ONE embedding model (its dimension is fixed
when it is created), and every vector also carries its `model` as filterable
metadata: a query never matches a vector made by another model, even
mid-migration.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

_KEY_SEPARATOR = "#"


def vector_key(scope: str, key: str, kind: str, ref_id: str) -> str:
    return _KEY_SEPARATOR.join((scope, key, kind, ref_id))


class VectorIndex(Protocol):
    def put(self, scope: str, key: str, kind: str, ref_id: str, vector: Any, model: str) -> bool: ...

    def delete(self, scope: str, key: str, kind: str, ref_id: str) -> bool: ...

    def search(self, vector: Any, *, scope: str, kind: str, top_k: int, model: str) -> Optional[List[Dict[str, Any]]]:
        """Nearest references of ANY key in `scope`, best first:
        `[{"key", "refId", "similarity"}]`. None = could not search."""
        ...


class S3VectorsIndex:
    """Amazon S3 Vectors. The index must be created with the COSINE distance
    metric (`src/document-validation-stack.ts`); `search` converts the distance
    it returns into a similarity in exactly one place, here."""

    def __init__(self, bucket: str, index: str, client: Any = None):
        self.bucket, self.index = bucket, index
        self._client = client

    def _s3vectors(self):
        if self._client is None:
            import boto3  # deferred: the Lambda runtime provides it

            self._client = boto3.client("s3vectors")
        return self._client

    def put(self, scope: str, key: str, kind: str, ref_id: str, vector: Any, model: str) -> bool:
        try:
            self._s3vectors().put_vectors(
                vectorBucketName=self.bucket, indexName=self.index,
                vectors=[{
                    "key": vector_key(scope, key, kind, ref_id),
                    "data": {"float32": [float(v) for v in vector]},
                    "metadata": {"scope": scope, "key": key, "kind": kind, "refId": ref_id, "model": model},
                }],
            )
            return True
        except Exception as e:
            logger.warning("reference_index: put failed: %s", e)
            return False

    def delete(self, scope: str, key: str, kind: str, ref_id: str) -> bool:
        try:
            self._s3vectors().delete_vectors(
                vectorBucketName=self.bucket, indexName=self.index,
                keys=[vector_key(scope, key, kind, ref_id)],
            )
            return True
        except Exception as e:
            logger.warning("reference_index: delete failed: %s", e)
            return False

    def search(self, vector: Any, *, scope: str, kind: str, top_k: int, model: str) -> Optional[List[Dict[str, Any]]]:
        try:
            response = self._s3vectors().query_vectors(
                vectorBucketName=self.bucket, indexName=self.index,
                topK=max(int(top_k), 1), queryVector={"float32": [float(v) for v in vector]},
                filter={"$and": [{"scope": {"$eq": scope}}, {"kind": {"$eq": kind}}, {"model": {"$eq": model}}]},
                returnMetadata=True, returnDistance=True,
            )
        except Exception as e:
            logger.warning("reference_index: search failed: %s", e)
            return None
        hits: List[Dict[str, Any]] = []
        for item in response.get("vectors") or []:
            metadata = item.get("metadata") or {}
            if not metadata.get("key") or not metadata.get("refId"):
                continue
            hits.append({"key": str(metadata["key"]), "refId": str(metadata["refId"]),
                         "similarity": similarity_from_distance(item.get("distance"))})
        hits.sort(key=lambda h: h["similarity"], reverse=True)
        return hits


def similarity_from_distance(distance: Any) -> float:
    """S3 Vectors reports cosine DISTANCE (0 = identical, 2 = opposite); the
    rest of the code compares SIMILARITIES with `>=`. Converted here and
    nowhere else, so a threshold can never be applied to the wrong one."""
    try:
        return round(1.0 - float(distance), 4)
    except (TypeError, ValueError):
        return 0.0


def index_name_for(base: str, provider: Any) -> str:
    """`<base>-<provider>-<dimension>`: an index is tied to one model, so the
    name says which — the old and the new one coexist during a migration."""
    return f"{base}-{provider.name}-{provider.dimension}"


def default_index(provider: Any = None) -> Optional[VectorIndex]:
    """The configured index for `provider`, or None when vector search is not
    set up (a local run, an environment deployed before it existed)."""
    bucket = os.environ.get("REFERENCE_VECTOR_BUCKET")
    base = os.environ.get("REFERENCE_VECTOR_INDEX")
    if not bucket or not base or provider is None:
        return None
    return S3VectorsIndex(bucket, index_name_for(base, provider))
