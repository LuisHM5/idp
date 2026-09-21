"""Page rasters: PDF page or uploaded image -> numpy array -> JPEG bytes.

The one place the pipeline turns a page into pixels. `render_page_array` moved
here from `qr_recovery.py` (which re-imports it) once a second consumer — the
physical-authenticity stage — needed the same "give me page N as an array".

Two array conventions live here, on purpose:

- `render_page_array` keeps pypdfium2's native channel order (BGR) because
  that is what `qr_recovery` feeds zxing-cpp and has always fed it.
- Everything the authenticity stage touches is **RGB, uint8, HxWx3**
  (`render_page_rgb`, `decode_image`), so colour reasoning ("blue stamp ink vs
  black signature ink") reads naturally and Pillow round-trips need no swap.

Pillow is imported lazily: a type that declares no `authenticity` block never
pays the import, and a missing wheel degrades that stage instead of the Lambda.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Long edge Anthropic vision models downscale to anyway; sending more pixels
# only costs upload bytes, never detail the model sees.
VISION_MAX_EDGE = 1568


def render_page_array(reader, page_index: int, dpi: int = 150) -> Optional[Any]:
    """Rasterize one page (0-based) of an already-open pypdf reader to a
    numpy BGR array — pypdfium2's native render output, fed straight into
    the QR decoder with no PNG/Pillow round-trip."""
    try:
        import pypdfium2 as pdfium
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_page(reader.pages[page_index])
        buf = io.BytesIO()
        writer.write(buf)

        pdf = pdfium.PdfDocument(buf.getvalue())
        try:
            return pdf[0].render(scale=dpi / 72).to_numpy()
        finally:
            pdf.close()
    except Exception as e:
        logger.warning("page_raster: could not render page %d: %s", page_index + 1, e)
        return None


def render_page_rgb(reader, page_index: int, dpi: int = 150) -> Optional[Any]:
    """Same render, as an RGB HxWx3 uint8 array (alpha dropped, gray expanded)."""
    array = render_page_array(reader, page_index, dpi=dpi)
    if array is None:
        return None
    return _to_rgb(array, bgr=True)


def decode_image(image_bytes: bytes) -> Optional[Any]:
    """An uploaded JPG/PNG (or a registered reference crop) -> RGB array.

    Honours EXIF orientation — a phone photo is stored sideways with a flag —
    and flattens transparency onto WHITE, so a reference PNG with an alpha
    background compares like ink on paper instead of ink on black.
    """
    try:
        import numpy as np
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                img = flat
            else:
                img = img.convert("RGB")
            return np.asarray(img, dtype=np.uint8).copy()
    except Exception as e:
        logger.warning("page_raster: could not decode image: %s", e)
        return None


def encode_jpeg(array: Any, max_edge: Optional[int] = VISION_MAX_EDGE, quality: int = 90) -> Optional[bytes]:
    """RGB array -> JPEG bytes, downscaled so the long edge is `max_edge`
    (None = keep native resolution, which is what a signature/stamp CROP wants:
    the halo and halftone evidence lives in the pixels a downscale throws away)."""
    try:
        from PIL import Image

        img = Image.fromarray(_to_rgb(array))
        if max_edge and max(img.size) > max_edge:
            scale = max_edge / float(max(img.size))
            img = img.resize(
                (max(int(round(img.width * scale)), 1), max(int(round(img.height * scale)), 1)),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning("page_raster: could not encode JPEG: %s", e)
        return None


def crop(array: Any, bbox: Sequence[float], pad: float = 0.0) -> Optional[Any]:
    """Cut a normalized `[x0, y0, x1, y1]` box (0..1, origin top-left) out of
    an array, grown by `pad` (a fraction of the box's own size) on every side
    and clamped to the image. None when the box is empty or malformed."""
    box = pixel_box(array, bbox, pad)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    return array[y0:y1, x0:x1]


def pixel_box(array: Any, bbox: Sequence[float], pad: float = 0.0) -> Optional[Tuple[int, int, int, int]]:
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    grow_x, grow_y = (x1 - x0) * pad, (y1 - y0) * pad
    height, width = array.shape[0], array.shape[1]
    px0 = max(int((x0 - grow_x) * width), 0)
    py0 = max(int((y0 - grow_y) * height), 0)
    px1 = min(int(round((x1 + grow_x) * width)), width)
    py1 = min(int(round((y1 + grow_y) * height)), height)
    if px1 - px0 < 2 or py1 - py0 < 2:
        return None
    return px0, py0, px1, py1


def _to_rgb(array: Any, bgr: bool = False) -> Any:
    import numpy as np

    if array.ndim == 2:
        return np.repeat(array[:, :, None], 3, axis=2).astype(np.uint8, copy=False)
    channels = array.shape[2]
    if channels == 1:
        return np.repeat(array, 3, axis=2).astype(np.uint8, copy=False)
    rgb = array[:, :, :3]
    if bgr:
        rgb = rgb[:, :, ::-1]
    return np.ascontiguousarray(rgb, dtype=np.uint8)
