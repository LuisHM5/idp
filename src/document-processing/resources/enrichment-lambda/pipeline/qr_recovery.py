"""Generic QR-code recovery: rasterize a PDF page, decode any QR codes on
it, and pull declared fields out of a URL's query string.

Nothing here is specific to pedimentos or to any field name — a document
type opts in by declaring a `qr_recovery` block in its catalog JSON (see
catalogs/PEDIMENTO_ADUANAL.json for the pedimento's SOIANET QR mapping).
This module only knows how to render a page, decode a QR, parse a URL, and
slice/validate a param per the spec it's handed.

Set QR_RECOVERY_ENABLED=false to disable this globally (falls back to
whatever recovery a type already had, e.g. pipeline.expediente's
regex-based `_recover_references`) without touching any catalog.
"""

from __future__ import annotations

import io
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from .normalize import digits_exact

logger = logging.getLogger(__name__)

_SLICE_RE = re.compile(r"^(-?\d*):(-?\d*)$")


def enabled() -> bool:
    return os.environ.get("QR_RECOVERY_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def render_page_array(reader, page_index: int, dpi: int = 150) -> Optional[Any]:
    """Rasterize one page (0-based) of an already-open pypdf reader to a
    numpy BGR array — pypdfium2's native render output, fed straight into
    OpenCV with no PNG/Pillow round-trip (Pillow isn't otherwise a Lambda
    dependency here)."""
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
        logger.warning("qr_recovery: could not render page %d: %s", page_index + 1, e)
        return None


def decode_qr_texts(image: Any) -> List[str]:
    """Decode every QR code found in a page image (numpy array), returning
    their raw text."""
    try:
        import cv2

        detector = cv2.QRCodeDetector()
        ok, decoded_texts, _, _ = detector.detectAndDecodeMulti(image)
        if ok and decoded_texts:
            return [t.strip() for t in decoded_texts if t and t.strip()]
        text, _, _ = detector.detectAndDecode(image)
        return [text.strip()] if text and text.strip() else []
    except Exception as e:
        logger.warning("qr_recovery: QR decode failed: %s", e)
        return []


def parse_url_params(text: str) -> Optional[Dict[str, str]]:
    """Parse a decoded QR string as a URL query string.

    Keys are lowercased so callers can match params case-insensitively —
    the same SAT pedimento QR has been observed with both `Pa=` and `pa=`.
    """
    try:
        query = urlsplit(text).query
        if not query:
            return None
        raw = parse_qs(query, keep_blank_values=True)
        return {k.lower(): v[0] for k, v in raw.items() if v}
    except Exception:
        return None


def _apply_slice(value: str, slice_expr: Optional[str]) -> str:
    """Apply a Python-slice-style expression ("-2:", ":2") to a string."""
    if not slice_expr:
        return value
    m = _SLICE_RE.match(str(slice_expr).strip())
    if not m:
        return value
    start = int(m.group(1)) if m.group(1) else None
    stop = int(m.group(2)) if m.group(2) else None
    return value[start:stop]


def apply_spec(params: Dict[str, str], fields_spec: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Map declared catalog fields out of a parsed QR param dict.

    A field is only returned when its (optionally sliced) value has exactly
    the declared digit count — anything else is dropped rather than guessed
    at, since a QR value is only trusted when it unambiguously matches the
    shape the catalog expects.
    """
    result: Dict[str, str] = {}
    for field_name, field_spec in fields_spec.items():
        param_name = str(field_spec.get("param", "")).lower()
        raw = params.get(param_name)
        if raw is None:
            continue
        sliced = _apply_slice(raw, field_spec.get("slice"))
        expected_digits = field_spec.get("digits")
        value = digits_exact(sliced, expected_digits) if expected_digits else sliced.strip()
        if value:
            result[field_name] = value
    return result


def extract_qr_recovery(reader, page_index: int, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Render `page_index`, decode any QR(s), and map fields per `spec`.

    Never raises. Always returns a dict:
        {"qrPresent": bool, "decodedText": str | None, "fields": {name: value}}
    `qrPresent` answers requirement #1 (does the QR exist) independently of
    whether any field could be mapped from it, so callers can surface a
    "QR missing" signal even when `fields` ends up empty.
    """
    result: Dict[str, Any] = {"qrPresent": False, "decodedText": None, "fields": {}}
    if not enabled():
        return result

    fields_spec = spec.get("fields") or {}
    image = render_page_array(reader, page_index)
    if image is None:
        return result

    texts = decode_qr_texts(image)
    if not texts:
        return result

    result["qrPresent"] = True
    result["decodedText"] = texts[0]

    for text in texts:
        params = parse_url_params(text)
        if not params:
            continue
        fields = apply_spec(params, fields_spec)
        if fields:
            result["decodedText"] = text
            result["fields"] = fields
            break

    return result
