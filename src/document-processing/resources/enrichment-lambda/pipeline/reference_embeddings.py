"""Image embeddings for registered references — the RETRIEVAL half of the
reference comparison.

An embedding answers one question only: "which registered references are worth
showing next to this crop?". It never decides whether a signature, stamp or
logo corresponds — that stays with the vision model (`vision_inspector`), and
what a mismatch does to the document stays with the catalog (`on_fail`).

Everything that knows a concrete model sits behind `EmbeddingProvider`. Today
there is one, Amazon Titan Multimodal Embeddings G1. Changing model is:

  1. a class with `name` / `model_id` / `dimension` / `embed_image`;
  2. an entry in `PROVIDERS`;
  3. its rate in `pipeline/pricing.py` (`_EMBEDDING_PRICING`);
  4. `pnpm refs:reindex` — vectors of different models are NOT comparable, so
     every stored vector carries the model that made it and is only ever
     compared against one made by the same model.

Nothing else in the stage, the API or the CLI names a model.

What is deliberately NOT inside a provider is `prepare`: the crop is trimmed
to its ink and resized the same way when a reference is registered and when a
document is processed. A different preparation on each side is a different
picture of the same stamp — exactly the mismatch the ink fingerprint has
(`ink="all"` at registration, `dark`/`colour` on the document).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional, Protocol, Tuple

from . import page_raster, pixel_forensics

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "titan-multimodal"

_PREPARE_MAX_EDGE = 512     # the model resizes anyway; more pixels are only bytes
_PREPARE_MARGIN = 0.06      # paper kept around the ink box, as a fraction of it


class EmbeddingProvider(Protocol):
    """One embedding model. `embed_image` raises on failure — `embed` is the
    one that turns a failure into "no vector"."""

    name: str
    model_id: str
    dimension: int

    def embed_image(self, jpeg: bytes) -> Any:  # -> numpy float32 array
        ...


class TitanMultimodalProvider:
    """Amazon Titan Multimodal Embeddings G1 (`amazon.titan-embed-image-v1`).
    The only place that knows its request and response shape."""

    name = "titan-multimodal"
    model_id = "amazon.titan-embed-image-v1"
    dimension = 1024            # the model also offers 384 and 256
    _client: Any = None

    def __init__(self, client: Any = None):
        self._client = client

    def request_body(self, jpeg: bytes) -> Dict[str, Any]:
        return {
            "inputImage": base64.b64encode(jpeg).decode("ascii"),
            "embeddingConfig": {"outputEmbeddingLength": self.dimension},
        }

    def embed_image(self, jpeg: bytes) -> Any:
        import numpy as np

        if self._client is None:
            import boto3  # deferred: the Lambda runtime provides it

            self._client = boto3.client("bedrock-runtime")
        response = self._client.invoke_model(
            modelId=self.model_id, body=json.dumps(self.request_body(jpeg)),
            accept="application/json", contentType="application/json",
        )
        payload = json.loads(response["body"].read())
        vector = np.asarray(payload.get("embedding") or [], dtype=np.float32)
        if vector.size != self.dimension:
            raise ValueError(f"{self.model_id} returned {vector.size} dimensions, expected {self.dimension}")
        return vector


# name -> class. The registry IS the extension point.
PROVIDERS: Dict[str, Any] = {TitanMultimodalProvider.name: TitanMultimodalProvider}
_INSTANCES: Dict[str, Any] = {}


def enabled() -> bool:
    """`REFERENCE_EMBEDDINGS_ENABLED=false` turns retrieval by embedding off
    everywhere (no Bedrock access, a cost freeze): ranking falls back to the
    ink fingerprint and the search without a key simply does not run."""
    return os.environ.get("REFERENCE_EMBEDDINGS_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def provider_for(config: Optional[Dict[str, Any]] = None) -> Optional[EmbeddingProvider]:
    """The provider a `references` block asks for: `references.embedding.provider`
    in the catalog → `REFERENCE_EMBEDDING_PROVIDER` → Titan. An unknown name
    is a warning and no embeddings at all — never a crash, never a silent
    switch to a model whose vectors are not the ones in the index."""
    if not enabled():
        return None
    declared = (config or {}).get("embedding") if isinstance(config, dict) else None
    name = str((declared or {}).get("provider") if isinstance(declared, dict) else "") \
        or os.environ.get("REFERENCE_EMBEDDING_PROVIDER") or DEFAULT_PROVIDER
    if name not in PROVIDERS:
        logger.warning("reference_embeddings: unknown provider '%s' (known: %s); embeddings off",
                       name, ", ".join(sorted(PROVIDERS)))
        return None
    if name not in _INSTANCES:
        _INSTANCES[name] = PROVIDERS[name]()
    return _INSTANCES[name]


def prepare(image: Any) -> Optional[bytes]:
    """A crop (RGB array) or stored image bytes -> the JPEG that is embedded.
    Trimmed to the ink with a fixed margin so the same element cropped with
    more paper around it gives the same picture; an image with no
    distinguishable ink (a pale logo) is embedded whole."""
    if isinstance(image, (bytes, bytearray)):
        image = page_raster.decode_image(bytes(image))
    if image is None or getattr(image, "size", 0) == 0:
        return None
    box = pixel_forensics.tight_ink_bbox(pixel_forensics.ink_mask(image))
    if box is not None:
        import numpy as np

        x0, y0, x1, y1 = box
        if x1 - x0 >= 8 and y1 - y0 >= 8:
            # The margin is ADDED in the crop's own paper tone, not taken from
            # whatever happens to surround the ink: a crop cut flush to the
            # stamp and one with half a page around it end up framed alike.
            paper = np.percentile(image.reshape(-1, image.shape[-1]), 90, axis=0) if image.ndim == 3 \
                else np.percentile(image, 90)
            pad_x, pad_y = int((x1 - x0) * _PREPARE_MARGIN) + 1, int((y1 - y0) * _PREPARE_MARGIN) + 1
            ink = image[y0:y1, x0:x1]
            framed = np.empty((ink.shape[0] + 2 * pad_y, ink.shape[1] + 2 * pad_x) + ink.shape[2:], dtype=np.uint8)
            framed[...] = np.asarray(paper, dtype=np.uint8)
            framed[pad_y:pad_y + ink.shape[0], pad_x:pad_x + ink.shape[1]] = ink
            image = framed
    return page_raster.encode_jpeg(image, max_edge=_PREPARE_MAX_EDGE, quality=90)


def embed(image: Any, provider: Optional[EmbeddingProvider]) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """(unit-length float32 vector | None, billing meta | None). Never raises:
    no embedding means "rank some other way", not a failed document."""
    if provider is None:
        return None, None
    jpeg = prepare(image)
    if not jpeg:
        return None, None
    try:
        vector = normalize(provider.embed_image(jpeg))
    except Exception as e:
        logger.warning("reference_embeddings: %s failed: %s", provider.model_id, e)
        return None, None
    if vector is None:
        return None, None
    return vector, {"provider": provider.name, "modelId": provider.model_id,
                    "dimension": int(vector.size), "images": 1}


def normalize(vector: Any) -> Any:
    import numpy as np

    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(array))
    if array.size == 0 or norm <= 0:
        return None
    return array / norm


def to_bytes(vector: Any) -> bytes:
    """How a vector is stored on the reference's DynamoDB row (Binary)."""
    import numpy as np

    return np.asarray(vector, dtype="<f4").tobytes()


def from_bytes(data: Any) -> Any:
    import numpy as np

    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        return np.frombuffer(bytes(data), dtype="<f4")
    return np.asarray(data, dtype=np.float32).reshape(-1)


def cosine(a: Any, b: Any) -> float:
    """Cosine SIMILARITY (1.0 = same direction) between two vectors, each an
    array or its stored bytes. 0.0 when either is missing or their sizes
    differ — vectors of different models never score as alike by accident."""
    import numpy as np

    va, vb = from_bytes(a), from_bytes(b)
    if va is None or vb is None or va.size == 0 or va.size != vb.size:
        return 0.0
    denominator = float(np.linalg.norm(va)) * float(np.linalg.norm(vb))
    if denominator <= 0:
        return 0.0
    return round(float(np.dot(va, vb)) / denominator, 4)
