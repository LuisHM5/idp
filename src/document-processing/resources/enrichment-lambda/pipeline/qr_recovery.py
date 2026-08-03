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
    their raw text.

    Uses zxing-cpp rather than cv2.QRCodeDetector: cv2's built-in detector
    proved too brittle on real degraded scans during testing — one real
    pedimento scan only decoded at exactly 250dpi and failed at
    150/200/300/400/500/600dpi, and standard preprocessing (grayscale,
    Otsu/adaptive threshold, denoising, sharpening) barely helped. zxing-cpp
    decoded that same page correctly at every DPI tried, with no
    preprocessing needed, at a fraction of opencv's installed size.
    """
    try:
        import zxingcpp

        results = zxingcpp.read_barcodes(image)
        return [r.text.strip() for r in results if r.text and r.text.strip()]
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


def find_marker_page(pages: List[Any], marker_patterns: List[str], min_matches: int = 2) -> Optional[int]:
    """Best page number (1-based, pypdf `pages`/`PageText` numbering) for the
    DEPOSITO REFERENCIADO / PAGO ELECTRONICO box, by scoring instead of a
    single regex.

    The box has several distinctive phrases around it (DEPÓSITO REFERENCIADO,
    LÍNEA DE CAPTURA, NOMBRE DE LA INSTITUCIÓN BANCARIA, NÚMERO DE
    TRANSACCIÓN SAT, MEDIO DE RECEPCIÓN/COBRO, ...). Requiring just one of
    them to match is fragile on a scanned/OCR'd page: if Textract garbles
    that one phrase, the whole page search fails even though every OTHER
    phrase in the box came through fine. Scoring how many DISTINCT patterns
    match per page and picking the highest-scoring page (ties broken by
    earliest page) survives losing any one phrase to OCR noise, as long as
    `min_matches` of the rest still land — a page with only an isolated,
    coincidental match to one generic-ish phrase never reaches that bar.

    The SAT pedimento's own layout always puts this box (and its QR) on the
    same page NUM. PEDIMENTO is printed on — but that isn't reliably the
    segment's first page: a cover page, a slightly-off segmentation call, or
    a multi-copy bundle can all shift it. The caller falls back to
    `seg.page_start` only when this returns None.
    """
    compiled = []
    for p in marker_patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logger.warning("qr_recovery: invalid page_marker pattern %r: %s", p, e)

    best_page: Optional[int] = None
    best_score = 0
    for page in pages:
        text = page.text or ""
        score = sum(1 for pattern in compiled if pattern.search(text))
        if score > best_score:
            best_score = score
            best_page = page.number

    return best_page if best_score >= min_matches else None


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

    A field may select a Python-style slice and/or one part of a delimited
    parameter before applying its declared digit count or regex. Anything
    that does not match is dropped rather than guessed at, since a QR value
    is only trusted when it unambiguously matches the catalog declaration.

    The split support covers SAT payloads such as a CSF's
    ``D3=<idCIF>_<RFC>`` without coupling this module to that document type.
    """
    result: Dict[str, str] = {}
    for field_name, field_spec in fields_spec.items():
        param_name = str(field_spec.get("param", "")).lower()
        raw = params.get(param_name)
        if raw is None:
            continue
        sliced = _apply_slice(raw, field_spec.get("slice"))
        delimiter = field_spec.get("split")
        if delimiter is not None:
            try:
                parts = sliced.split(str(delimiter))
                sliced = parts[int(field_spec.get("part", 0))]
            except (IndexError, TypeError, ValueError):
                continue
        expected_digits = field_spec.get("digits")
        value = digits_exact(sliced, expected_digits) if expected_digits else sliced.strip()
        pattern = field_spec.get("pattern")
        if value and pattern:
            try:
                if re.fullmatch(str(pattern), value) is None:
                    continue
            except re.error:
                logger.warning("qr_recovery: invalid field pattern %r", pattern)
                continue
        if value:
            result[field_name] = value
    return result


def _extract_from_page(
    reader, page_index: int, fields_spec: Dict[str, Any], url_contains: Optional[str],
    dpis: List[int],
) -> Dict[str, Any]:
    """Render+decode a single page, returning the same shape as
    `extract_qr_recovery` (never raises).

    Tries each DPI in `dpis` in order until one decodes into usable fields.
    zxing-cpp decoded every fixture tested at plain 150dpi already; the
    second DPI is a cheap safety margin, not compensation for a fragile
    decoder (an earlier cv2.QRCodeDetector-based attempt needed this far
    more — it was sensitive to exact pixel alignment, not simply "higher
    resolution is better": one real scan only decoded at exactly 250dpi and
    failed at 150/200/300/400/500/600dpi).
    """
    result: Dict[str, Any] = {"qrPresent": False, "decodedText": None, "fields": {}}
    for dpi in dpis:
        image = render_page_array(reader, page_index, dpi=dpi)
        if image is None:
            continue

        texts = decode_qr_texts(image)
        if url_contains:
            # A single page can carry more than one QR/barcode (e.g. the
            # pedimento's own CODIGO DE ACEPTACION box also renders as a
            # barcode-like graphic next to the real DEPOSITO REFERENCIADO
            # QR). Filtering by a substring every legitimate hit must
            # contain (the SAT verification host) rejects decoys by content
            # instead of hoping position/order is reliable.
            matched = [t for t in texts if url_contains.lower() in t.lower()]
            texts = matched or texts
        if not texts:
            continue

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
                return result

    return result


def extract_qr_recovery(pages: List[Any], fallback_page: int, reader, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Locate the right page, decode any QR(s) there, and map fields per `spec`.

    Page selection tries, in order, until one decodes into usable fields:
    1. Page 1 of `pages` when the segment is short (`len(pages) <=
       spec.get("short_segment_max_pages", 3)`) — for an isolated or
       near-isolated pedimento upload (the common case for a manually
       classified or single-document scan) the QR is essentially always on
       that first page, so this cheap, position-based guess is tried before
       any text scoring.
    2. The highest-scoring page from `spec["page_markers"]` (a list of
       regexes) that clears `spec.get("page_marker_min_matches", 2)` matches
       — the page carrying the DEPOSITO REFERENCIADO / LINEA DE CAPTURA box.
       The pedimento's own "NUM. PEDIMENTO" / "CVE. PEDIM" header belongs in
       this same list (declared in the catalog): a page carrying the printed
       pedimento number is, on the real SAT layout, the same page as the QR.
    3. `fallback_page` (1-based, historically the segment's first page per
       the classifier) as a last resort.
    Every candidate that doesn't decode into usable fields is tried anyway
    before giving up — a rendering hiccup or OCR noise on one candidate
    shouldn't lose the recovery when another candidate would have worked.

    Never raises. Always returns a dict:
        {"qrPresent": bool, "decodedText": str | None, "fields": {name: value},
         "markerPageFound": int | None, "pageChecked": int | None}
    `qrPresent` answers requirement #1 (does the QR exist) independently of
    whether any field could be mapped from it, so callers can surface a
    "QR missing" signal even when `fields` ends up empty.
    """
    empty: Dict[str, Any] = {
        "qrPresent": False, "decodedText": None, "fields": {},
        "markerPageFound": None, "pageChecked": None,
    }
    if not enabled():
        return empty

    fields_spec = spec.get("fields") or {}
    url_contains = spec.get("url_contains")
    # zxing-cpp (unlike the earlier cv2 attempt) decoded every real fixture
    # tested — including a badly degraded scan — correctly at plain 150dpi,
    # no escalation needed. 300dpi stays as a cheap second attempt/safety
    # margin rather than removing escalation altogether.
    dpis = spec.get("render_dpis") or [150, 300]

    marker_patterns = spec.get("page_markers") or []
    marker_min_matches = int(spec.get("page_marker_min_matches", 2))
    marker_page = find_marker_page(pages, marker_patterns, marker_min_matches) if marker_patterns else None

    candidates: List[int] = []
    short_max_pages = int(spec.get("short_segment_max_pages", 3))
    if pages and len(pages) <= short_max_pages:
        candidates.append(pages[0].number)
    if marker_page is not None and marker_page not in candidates:
        candidates.append(marker_page)
    if fallback_page not in candidates:
        candidates.append(fallback_page)

    best: Dict[str, Any] = dict(empty)
    best["markerPageFound"] = marker_page
    for page_num in candidates:
        page_result = _extract_from_page(reader, page_num - 1, fields_spec, url_contains, dpis)
        best["pageChecked"] = page_num
        if page_result["qrPresent"]:
            best["qrPresent"] = True
            best["decodedText"] = page_result["decodedText"]
        if page_result["fields"]:
            best["fields"] = page_result["fields"]
            best["decodedText"] = page_result["decodedText"]
            break

    return best
