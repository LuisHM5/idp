"""PDF structure forensics — layer 1 of the physical-authenticity stage.

Answers ONE question from the PDF's own internals, with no model, no OCR and
no rasterization: is this file a CAPTURE of a paper document (a scanner or a
phone app wrote one full-page raster per page), or a DIGITAL composition (an
office suite wrote real text and somebody dropped a signature/stamp image on
top)? It is deterministic and free, which is the point: there is no model here
for a forged document to talk its way past.

What was measured on real letters, and what it rules out (see
`catalogs/README.md` → "Autenticidad física"):

- A genuine scan has ~0 visible text characters and one image covering the
  page. A Word-made letter has hundreds of visible characters (800+ in every
  sample) and one or more images covering only PART of the page.
- `/SMask` (transparency) is NOT the signal. The forged letters pasted an
  opaque JPEG; not one PDF in the sample set carried a soft mask. It is kept
  as extra evidence only.
- Producer/Creator is NOT a gate either: it is trivially editable, genuine
  captures arrive with an empty or unknown producer (iText, none at all), and
  a genuine scan is sometimes pasted INTO a Word page. It is reported as
  evidence (`origin`) and never decides the verdict.
- A tiny image is not a pasted signature: CamScanner stamps an "Escaneado con
  CamScanner" watermark on every genuine capture (under 1% of the page; real
  pasted signatures measured 1-2.5%) — hence `ignore_image_coverage_below`.
  It only picks the REASON code; the verdict never hinges on it.
- A searchable scan (full-page image + an OCR text layer) is a genuine
  capture. Its text is drawn with render mode 3 (invisible), which is how it
  is told apart from born-digital text — hence visible vs invisible counts.

Only pypdf is used, so this runs anywhere the pipeline runs — including the
free offline calibration runner (`pnpm autenticidad`).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Per-page verdicts.
PAGE_PHYSICAL = "FISICO"
PAGE_PHYSICAL_OCR = "ESCANEO_CON_OCR"
PAGE_DIGITAL = "DIGITAL"
PAGE_UNDETERMINED = "INDETERMINADO"
PAGE_BLANK = "EN_BLANCO"

# Reason codes (stable identifiers — integrators branch on these).
REASON_PASTED_IMAGE = "TEXTO_NATIVO_CON_IMAGEN_PEGADA"
REASON_NO_CAPTURE = "DOCUMENTO_DIGITAL_SIN_CAPTURA"
REASON_TRANSPARENCY = "IMAGEN_CON_TRANSPARENCIA"
REASON_OFFICE_ORIGIN = "ORIGEN_OFIMATICA"

ORIGIN_SCANNER = "ESCANER"
ORIGIN_CAPTURE_APP = "APP_CAPTURA"
ORIGIN_OFFICE = "OFIMATICA"
ORIGIN_UNKNOWN = "DESCONOCIDO"

DEFAULT_ORIGIN_PATTERNS: Dict[str, List[str]] = {
    ORIGIN_SCANNER: [
        "lexmark", "ricoh", "canon", "xerox", "brother", "hardcopy", "epson",
        "kyocera", "konica", "sharp", "toshiba", "samsung", "hp scan", "hp smart",
        "officejet", "laserjet", "scansnap", "fujitsu", "naps2",
    ],
    ORIGIN_CAPTURE_APP: [
        "intsig", "camscanner", "adobe scan", "office lens", "microsoft lens",
        "genius scan", "tiny scanner", "scanner pro", "simplescan", "notes",
    ],
    ORIGIN_OFFICE: [
        "microsoft", "word", "excel", "powerpoint", "libreoffice", "openoffice",
        "writer", "pages", "google docs", "skia", "canva", "indesign",
        "illustrator", "photoshop", "print to pdf", "pdfcreator", "wps",
    ],
}

DEFAULT_POLICY: Dict[str, Any] = {
    # At or above this many VISIBLE text characters the page is born-digital:
    # nobody types a letter body onto a scan.
    "min_native_text_chars": 200,
    # At or below this many the page carries no real text layer (a stray
    # header/footer string is tolerated).
    "max_scan_text_chars": 40,
    # An image covering at least this share of the page is "the page itself".
    "full_page_coverage": 0.85,
    # An image covering less than this is decoration (a capture-app
    # watermark), not a pasted signature or stamp.
    "ignore_image_coverage_below": 0.01,
}

_BILEVEL_FILTERS = ("/CCITTFaxDecode", "/JBIG2Decode")
_MAX_FORM_DEPTH = 3
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

Matrix = Tuple[float, float, float, float, float, float]


# ------------------------------------------------------------------ geometry


def _concat(first: Matrix, then: Matrix) -> Matrix:
    """`first` applied before `then` — the PDF `cm` operator's composition."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = then
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _unit_square_bbox(ctm: Matrix) -> Tuple[float, float, float, float]:
    """Where an image lands in page space: images are painted into the unit
    square, so its four corners through the CTM bound the placement."""
    a, b, c, d, e, f = ctm
    xs = [e, a + e, c + e, a + c + e]
    ys = [f, b + f, d + f, b + d + f]
    return min(xs), min(ys), max(xs), max(ys)


def _to_visual(u: float, v: float, rotation: int) -> Tuple[float, float]:
    """Unrotated page-normalized (u right, v UP) -> what a viewer/rasterizer
    shows (x right, y DOWN), honouring the page's `/Rotate`. Needed because
    the vision layer reports boxes in rendered space, and "does this embedded
    image sit under the signature?" has to compare the two."""
    rotation %= 360
    if rotation == 90:
        return v, u
    if rotation == 180:
        return 1.0 - u, v
    if rotation == 270:
        return 1.0 - v, 1.0 - u
    return u, 1.0 - v


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _as_float_list(operands: Sequence[Any]) -> Optional[List[float]]:
    try:
        return [float(o) for o in operands]
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ image info


def _filters_of(obj: Any) -> List[str]:
    """`/Filter` is a name OR an array of names (CamScanner writes
    `['/DCTDecode']`) — always a list here."""
    raw = obj.get("/Filter")
    if raw is None:
        return []
    try:
        raw = raw.get_object()
    except AttributeError:
        pass
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    return [str(raw)]


def _raw_stream_bytes(obj: Any) -> bytes:
    """The stream exactly as stored (still encoded): two documents embedding
    the very same image file share these bytes, which is what makes a pasted
    signature block reused across letters provable."""
    data = getattr(obj, "_data", None)
    if isinstance(data, (bytes, bytearray)) and data:
        return bytes(data)
    try:
        return obj.get_data() or b""
    except Exception:  # undecodable stream: still report the image
        return b""


def _colorspace_name(obj: Any) -> str:
    raw = obj.get("/ColorSpace")
    if raw is None:
        return ""
    try:
        raw = raw.get_object()
    except AttributeError:
        pass
    if isinstance(raw, (list, tuple)):
        return str(raw[0]) if raw else ""
    return str(raw)


def _image_record(
    obj: Any, name: str, ctm: Matrix, box: Tuple[float, float, float, float], rotation: int
) -> Dict[str, Any]:
    left, bottom, page_w, page_h = box
    x0, y0, x1, y1 = _unit_square_bbox(ctm)
    width_pt, height_pt = max(x1 - x0, 0.0), max(y1 - y0, 0.0)

    # Coverage = the part of the placement that is actually ON the page.
    ix0, iy0 = max(x0, left), max(y0, bottom)
    ix1, iy1 = min(x1, left + page_w), min(y1, bottom + page_h)
    visible_area = max(ix1 - ix0, 0.0) * max(iy1 - iy0, 0.0)
    page_area = page_w * page_h
    coverage = visible_area / page_area if page_area > 0 else 0.0

    corners = [
        _to_visual((x - left) / page_w, (y - bottom) / page_h, rotation)
        for x, y in ((x0, y0), (x1, y1))
    ] if page_w > 0 and page_h > 0 else [(0.0, 0.0), (0.0, 0.0)]
    vx = sorted(_clamp01(c[0]) for c in corners)
    vy = sorted(_clamp01(c[1]) for c in corners)

    try:
        px_w, px_h = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
    except (TypeError, ValueError):
        px_w, px_h = 0, 0
    try:
        bits = int(obj.get("/BitsPerComponent", 8))
    except (TypeError, ValueError):
        bits = 8
    filters = _filters_of(obj)
    data = _raw_stream_bytes(obj)

    return {
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest() if data else None,
        "byteLength": len(data),
        "width": px_w,
        "height": px_h,
        "ppi": round(px_w / (width_pt / 72.0), 1) if width_pt > 0 and px_w else None,
        "filters": filters,
        "colorSpace": _colorspace_name(obj),
        "bilevel": bits == 1 or any(f in _BILEVEL_FILTERS for f in filters),
        "transparency": "/SMask" in obj or "/Mask" in obj,
        "coverage": round(coverage, 4),
        # [x0, y0, x1, y1] in rendered page space, 0..1, origin top-left.
        "rect": [round(vx[0], 4), round(vy[0], 4), round(vx[1], 4), round(vy[1], 4)],
    }


# ------------------------------------------------------------------ the walker


class _PageWalk:
    """One pass over a page's content stream (and the Form XObjects it
    paints), tracking just enough graphics state to answer two things: how
    much text is drawn VISIBLY vs invisibly, and where each image lands."""

    def __init__(self, reader: Any, box: Tuple[float, float, float, float], rotation: int):
        self.reader = reader
        self.box = box
        self.rotation = rotation
        self.visible_chars = 0
        self.invisible_chars = 0
        self.images: List[Dict[str, Any]] = []
        self.fonts: set = set()

    def walk(self, stream_source: Any, resources: Any, ctm: Matrix, depth: int) -> None:
        from pypdf.generic import ContentStream

        try:
            content = ContentStream(stream_source, self.reader)
        except Exception as e:
            logger.debug("pdf_forensics: unreadable content stream: %s", e)
            return

        fonts = _resolve(resources, "/Font") or {}
        xobjects = _resolve(resources, "/XObject") or {}
        stack: List[Tuple[Matrix, int]] = []
        render_mode = 0
        bytes_per_char = 1

        for operands, operator in content.operations:
            if operator == b"q":
                stack.append((ctm, render_mode))
            elif operator == b"Q":
                if stack:
                    ctm, render_mode = stack.pop()
            elif operator == b"cm":
                values = _as_float_list(operands)
                if values and len(values) == 6:
                    ctm = _concat(tuple(values), ctm)  # type: ignore[arg-type]
            elif operator == b"Tr":
                values = _as_float_list(operands)
                if values:
                    render_mode = int(values[0])
            elif operator == b"Tf":
                font_name = str(operands[0]) if operands else ""
                bytes_per_char = _bytes_per_char(fonts, font_name)
                if font_name:
                    self.fonts.add(_base_font(fonts, font_name) or font_name)
            elif operator in (b"Tj", b"'", b'"'):
                if operands:
                    self._count(_shown_length(operands[-1]), bytes_per_char, render_mode)
            elif operator == b"TJ":
                if operands and isinstance(operands[0], (list, tuple)):
                    total = sum(_shown_length(item) for item in operands[0])
                    self._count(total, bytes_per_char, render_mode)
            elif operator == b"Do":
                name = str(operands[0]) if operands else ""
                self._paint(xobjects, name, ctm, depth)
            elif operator == b"INLINE IMAGE":
                settings = (operands or {}).get("settings") if isinstance(operands, dict) else None
                if settings is not None:
                    self.images.append(_inline_record(settings, operands, ctm, self.box, self.rotation))

    def _count(self, shown_bytes: int, bytes_per_char: int, render_mode: int) -> None:
        chars = shown_bytes // max(bytes_per_char, 1)
        # Render mode 3 = neither filled nor stroked: the OCR layer of a
        # searchable scan. Mode 7 only adds the glyphs to the clip path.
        if render_mode in (3, 7):
            self.invisible_chars += chars
        else:
            self.visible_chars += chars

    def _paint(self, xobjects: Any, name: str, ctm: Matrix, depth: int) -> None:
        try:
            obj = xobjects[name].get_object()
        except Exception:
            return
        subtype = str(obj.get("/Subtype", ""))
        if subtype == "/Image":
            self.images.append(_image_record(obj, name, ctm, self.box, self.rotation))
        elif subtype == "/Form" and depth < _MAX_FORM_DEPTH:
            matrix = _as_float_list(obj.get("/Matrix") or [])
            inner = _concat(tuple(matrix), ctm) if matrix and len(matrix) == 6 else ctm  # type: ignore[arg-type]
            self.walk(obj, obj.get("/Resources") or {}, inner, depth + 1)


def _resolve(container: Any, key: str) -> Any:
    try:
        container = container.get_object()
    except AttributeError:
        pass
    try:
        value = container.get(key)
    except AttributeError:
        return None
    try:
        return value.get_object()
    except AttributeError:
        return value


def _font_object(fonts: Any, name: str) -> Any:
    try:
        return fonts[name].get_object()
    except Exception:
        return None


def _bytes_per_char(fonts: Any, name: str) -> int:
    """Composite (Type0) fonts show 2 bytes per glyph; counting raw bytes would
    double a Word letter's character count."""
    font = _font_object(fonts, name)
    if font is None:
        return 1
    if str(font.get("/Subtype", "")) == "/Type0" or "Identity" in str(font.get("/Encoding", "")):
        return 2
    return 1


def _base_font(fonts: Any, name: str) -> str:
    font = _font_object(fonts, name)
    return str(font.get("/BaseFont", "")).lstrip("/") if font is not None else ""


def _shown_length(operand: Any) -> int:
    if isinstance(operand, (bytes, bytearray, str)):
        return len(operand)
    return 0  # TJ kerning adjustments are numbers


def _inline_record(settings: Any, operands: Any, ctm: Matrix, box, rotation: int) -> Dict[str, Any]:
    class _Inline(dict):
        """Adapts an inline image's abbreviated keys to the XObject ones."""

    adapted = _Inline()
    aliases = {"/W": "/Width", "/H": "/Height", "/BPC": "/BitsPerComponent", "/F": "/Filter", "/CS": "/ColorSpace"}
    for key, value in dict(settings).items():
        adapted[aliases.get(str(key), str(key))] = value
    adapted._data = operands.get("data") or b""  # type: ignore[attr-defined]
    return _image_record(adapted, "inline", ctm, box, rotation)


# ------------------------------------------------------------------ public API


def classify_origin(metadata: Dict[str, str], patterns: Optional[Dict[str, List[str]]] = None) -> str:
    """Bucket the producing software. EVIDENCE ONLY — see the module docstring
    for why this never decides a verdict."""
    haystack = " ".join(str(v or "") for v in metadata.values()).lower()
    if not haystack.strip():
        return ORIGIN_UNKNOWN
    merged = {**DEFAULT_ORIGIN_PATTERNS, **(patterns or {})}
    for label in (ORIGIN_SCANNER, ORIGIN_CAPTURE_APP, ORIGIN_OFFICE):
        if any(str(p).lower() in haystack for p in merged.get(label) or []):
            return label
    return ORIGIN_UNKNOWN


def document_metadata(reader: Any) -> Dict[str, str]:
    try:
        info = reader.metadata or {}
    except Exception:
        info = {}
    out: Dict[str, str] = {}
    for key, label in (("/Producer", "producer"), ("/Creator", "creator"),
                       ("/Author", "author"), ("/Title", "title")):
        try:
            value = info.get(key)
        except Exception:
            value = None
        if value:
            out[label] = str(value)[:200]
    return out


def inspect_page(reader: Any, page_index: int) -> Dict[str, Any]:
    """Raw structural facts of one page (0-based). Never raises: a page that
    can't be walked reports `error` and is judged INDETERMINADO downstream."""
    report: Dict[str, Any] = {
        "page": page_index + 1, "visibleTextChars": 0, "invisibleTextChars": 0,
        "fonts": [], "images": [],
    }
    try:
        page = reader.pages[page_index]
        box = page.cropbox
        geometry = (float(box.left), float(box.bottom), float(box.width), float(box.height))
        rotation = int(page.get("/Rotate", 0) or 0)
        contents = page.get_contents()
        walk = _PageWalk(reader, geometry, rotation)
        if contents is not None:
            walk.walk(contents, page.get("/Resources") or {}, _IDENTITY, 0)
        report.update({
            "visibleTextChars": walk.visible_chars,
            "invisibleTextChars": walk.invisible_chars,
            "fonts": sorted(walk.fonts),
            "images": walk.images,
            "rotation": rotation % 360,
        })
    except Exception as e:
        logger.warning("pdf_forensics: could not inspect page %d: %s", page_index + 1, e)
        report["error"] = str(e)[:200]
    return report


def page_verdict(report: Dict[str, Any], policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Judge one page from its structural facts. Pure."""
    cfg = {**DEFAULT_POLICY, **{k: v for k, v in (policy or {}).items() if k in DEFAULT_POLICY}}
    if report.get("error"):
        return {"verdict": PAGE_UNDETERMINED, "reasons": []}

    visible = int(report.get("visibleTextChars") or 0)
    invisible = int(report.get("invisibleTextChars") or 0)
    significant = [i for i in report.get("images") or []
                   if float(i.get("coverage") or 0) >= float(cfg["ignore_image_coverage_below"])]
    full = [i for i in significant if float(i["coverage"]) >= float(cfg["full_page_coverage"])]
    partial = [i for i in significant if float(i["coverage"]) < float(cfg["full_page_coverage"])]

    reasons: List[str] = []
    if visible >= int(cfg["min_native_text_chars"]):
        # A full-page background image does not rescue it: the forged letters
        # used an EMPTY letterhead as the page background, under live text.
        reasons.append(REASON_PASTED_IMAGE if partial else REASON_NO_CAPTURE)
        if any(i.get("transparency") for i in partial):
            reasons.append(REASON_TRANSPARENCY)
        return {"verdict": PAGE_DIGITAL, "reasons": reasons}

    if not significant and visible == 0 and invisible == 0:
        return {"verdict": PAGE_BLANK, "reasons": []}

    if visible <= int(cfg["max_scan_text_chars"]) and full:
        ocr_layer = invisible >= int(cfg["min_native_text_chars"])
        return {"verdict": PAGE_PHYSICAL_OCR if ocr_layer else PAGE_PHYSICAL, "reasons": []}

    # No real text and only a partial image (a scan pasted into an office
    # page), or a little text and nothing conclusive: never a hard signal.
    return {"verdict": PAGE_UNDETERMINED, "reasons": []}


def inspect_pdf(
    reader: Any,
    page_indexes: Sequence[int],
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Layer-1 report for the given pages (0-based) of an open pypdf reader.

    `physicalCapture` is the tri-state the catalog's computed boolean takes:
    True (every content page is a capture), False (at least one page is a
    digital composition — deterministic, safe to reject on), None (can't tell
    from structure alone; the other layers decide).
    """
    policy = policy or {}
    metadata = document_metadata(reader)
    origin = classify_origin(metadata, policy.get("origin_patterns"))

    per_page: List[Dict[str, Any]] = []
    for index in page_indexes:
        facts = inspect_page(reader, index)
        facts.update(page_verdict(facts, policy))
        per_page.append(facts)

    content_pages = [p for p in per_page if p["verdict"] != PAGE_BLANK]
    verdicts = {p["verdict"] for p in content_pages}
    reasons: List[str] = []
    for p in content_pages:
        for code in p.get("reasons") or []:
            if code not in reasons:
                reasons.append(code)

    if PAGE_DIGITAL in verdicts:
        verdict, physical = PAGE_DIGITAL, False
        if origin == ORIGIN_OFFICE:
            reasons.append(REASON_OFFICE_ORIGIN)
    elif content_pages and verdicts <= {PAGE_PHYSICAL, PAGE_PHYSICAL_OCR}:
        verdict = PAGE_PHYSICAL_OCR if PAGE_PHYSICAL_OCR in verdicts else PAGE_PHYSICAL
        physical = True
    else:
        verdict, physical = PAGE_UNDETERMINED, None

    return {
        "verdict": verdict,
        "physicalCapture": physical,
        "origin": origin,
        "metadata": metadata,
        "reasons": reasons,
        "bilevel": bool(content_pages) and all(
            any(i.get("bilevel") for i in p.get("images") or []) and
            not any(not i.get("bilevel") and float(i.get("coverage") or 0) >= 0.5 for i in p.get("images") or [])
            for p in content_pages
        ),
        "contentPages": [p["page"] for p in content_pages],
        "perPage": per_page,
    }


def images_overlapping(
    page_report: Dict[str, Any],
    bbox: Sequence[float],
    min_overlap: float = 0.3,
    max_coverage: float = 0.35,
) -> List[Dict[str, Any]]:
    """Embedded images whose placement overlaps a rendered-space box — i.e.
    "is there an image OBJECT sitting where the signature/stamp is?", which on
    a born-digital page is direct proof that the element was inserted.

    `max_coverage` keeps large images out: a genuine scan is sometimes pasted
    INTO an office page (measured: one image covering 77% of it), and that
    image naturally contains the signature without being a pasted signature. A
    pasted signature block is small (measured 1–9% of the page). `min_overlap`
    is relative to the smaller of the two areas, so a small block inside a
    generous model bbox still counts.
    """
    if not bbox or len(bbox) != 4:
        return []
    bx0, by0, bx1, by1 = (float(v) for v in bbox)
    b_area = max(bx1 - bx0, 0.0) * max(by1 - by0, 0.0)
    out: List[Dict[str, Any]] = []
    for image in page_report.get("images") or []:
        if float(image.get("coverage") or 0) > max_coverage:
            continue
        ix0, iy0, ix1, iy1 = image.get("rect") or (0, 0, 0, 0)
        i_area = max(ix1 - ix0, 0.0) * max(iy1 - iy0, 0.0)
        overlap = max(min(bx1, ix1) - max(bx0, ix0), 0.0) * max(min(by1, iy1) - max(by0, iy0), 0.0)
        smaller = min(b_area, i_area)
        if smaller > 0 and overlap / smaller >= min_overlap:
            out.append(image)
    return out
