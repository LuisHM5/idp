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
import math
import re
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
# Text runs are kept only to answer "was this drawn OVER that?"; a dense
# pedimento page shows thousands of strings, and past this many the geometry
# adds nothing a verdict needs (counters keep counting regardless).
_MAX_TEXT_RUNS = 4000
_DEFAULT_GLYPH_WIDTH = 500.0   # /1000 text-space units, when the font declares none

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


def _bbox_through(m: Matrix, x0: float, y0: float, x1: float, y1: float) -> Tuple[float, float, float, float]:
    """Axis-aligned bounds of a rectangle after a matrix: its four corners
    through `m`."""
    a, b, c, d, e, f = m
    xs, ys = [], []
    for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return min(xs), min(ys), max(xs), max(ys)


def _unit_square_bbox(ctm: Matrix) -> Tuple[float, float, float, float]:
    """Where an image lands in page space: images are painted into the unit
    square, so its four corners through the CTM bound the placement."""
    return _bbox_through(ctm, 0.0, 0.0, 1.0, 1.0)


def _visual_rect(
    x0: float, y0: float, x1: float, y1: float, box: Tuple[float, float, float, float], rotation: int
) -> List[float]:
    """Page-space bounds -> `[x0, y0, x1, y1]` in rendered space (0..1, origin
    top-left), the one coordinate system every layer compares boxes in."""
    left, bottom, page_w, page_h = box
    if page_w <= 0 or page_h <= 0:
        return [0.0, 0.0, 0.0, 0.0]
    corners = [_to_visual((x - left) / page_w, (y - bottom) / page_h, rotation) for x, y in ((x0, y0), (x1, y1))]
    vx = sorted(_clamp01(c[0]) for c in corners)
    vy = sorted(_clamp01(c[1]) for c in corners)
    return [round(vx[0], 4), round(vy[0], 4), round(vx[1], 4), round(vy[1], 4)]


def _overlap_ratio(a: Sequence[float], b: Sequence[float], relative_to: str = "a") -> float:
    """Intersection of two visual rects over the area of `a` (or `b`)."""
    if not a or not b or len(a) != 4 or len(b) != 4:
        return 0.0
    ix = max(min(a[2], b[2]) - max(a[0], b[0]), 0.0)
    iy = max(min(a[3], b[3]) - max(a[1], b[1]), 0.0)
    base = a if relative_to == "a" else b
    area = max(base[2] - base[0], 0.0) * max(base[3] - base[1], 0.0)
    return (ix * iy) / area if area > 0 else 0.0


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
    obj: Any, name: str, ctm: Matrix, box: Tuple[float, float, float, float], rotation: int,
    seq: int = 0, annotation: Optional[str] = None,
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
        "rect": _visual_rect(x0, y0, x1, y1, box, rotation),
        # Paint order on the page (text and images share one counter): what
        # was drawn AFTER what is how "typed onto a scan" is told from "a scan".
        "seq": seq,
        # Painted from an annotation's appearance stream (a /Stamp pasted with
        # an editor), not from the page content itself.
        "fromAnnotation": annotation is not None,
        **({"annotSubtype": annotation} if annotation else {}),
    }


# ------------------------------------------------------------------ the walker


class _TextState:
    """The slice of the PDF text state a run's placement depends on. Text
    space is approximate on purpose (glyph widths from `/Widths` or a default,
    a fixed ascender/descender): what is asked of it is "does this string sit
    ON that image?", a question a 10% error does not change."""

    __slots__ = ("tm", "tlm", "size", "leading", "hscale", "char_spacing", "word_spacing",
                 "widths", "default_width", "bytes_per_char", "render_mode", "font", "embedded")

    def __init__(self) -> None:
        self.tm: Matrix = _IDENTITY
        self.tlm: Matrix = _IDENTITY
        self.size = 0.0
        self.leading = 0.0
        self.hscale = 1.0
        self.char_spacing = 0.0
        self.word_spacing = 0.0
        self.widths: Dict[int, float] = {}
        self.default_width = _DEFAULT_GLYPH_WIDTH
        self.bytes_per_char = 1
        self.render_mode = 0
        self.font = ""
        self.embedded: Optional[bool] = None

    def copy(self) -> "_TextState":
        other = _TextState()
        for name in self.__slots__:
            setattr(other, name, getattr(self, name))
        return other


class _GState:
    """The slice of the graphics state that says whether a fill HIDES what is
    under it: its colour (evidence only), its opacity, and the clip that may
    shrink it. `clip` is page-space bounds, tracked for rectangular clips only
    — the ones a generator uses to confine a background to a cell."""

    __slots__ = ("fill", "fill_known", "alpha", "opaque", "clip")

    def __init__(self) -> None:
        self.fill: Optional[Tuple[float, float, float]] = (0.0, 0.0, 0.0)
        self.fill_known = True
        self.alpha = 1.0
        self.opaque = True      # Normal blend mode and no soft mask
        self.clip: Optional[Tuple[float, float, float, float]] = None

    def copy(self) -> "_GState":
        other = _GState()
        for name in self.__slots__:
            setattr(other, name, getattr(self, name))
        return other


_FILL_OPERATORS = (b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*")
_PATH_END_OPERATORS = _FILL_OPERATORS + (b"S", b"s", b"n")
_MAX_SHAPES = 3000
# Names an editor gives the optional-content group or marked-content tag it
# puts a watermark in (Acrobat, Word, LibreOffice, iLovePDF, PDF-XChange).
_WATERMARK_NAME = re.compile(r"water\s*mark|marca\s*(de)?\s*_?\s*agua|wasserzeichen|filigran", re.I)
# Acrobat tags what "Add watermark / background" paints in the form's /PieceInfo.
_ACROBAT_ADDITIONS = {"/Watermark": "MARCA_DE_AGUA_ACROBAT", "/Background": "FONDO_AGREGADO_ACROBAT"}


class _PageWalk:
    """One pass over a page's content stream (and the Form XObjects it
    paints), tracking just enough graphics state to answer: how much text is
    drawn VISIBLY vs invisibly, where each image lands, in what ORDER text,
    images and filled rectangles were painted (with where each visible string
    sits) — so "text typed over a scan", "an image dropped over text" and "a
    box painted over a value" can be read off the file — plus what each string
    was set in (font, size, angle, opacity) and whether the generator tagged
    it as a watermark."""

    def __init__(self, reader: Any, box: Tuple[float, float, float, float], rotation: int):
        self.reader = reader
        self.box = box
        self.rotation = rotation
        self.visible_chars = 0
        self.invisible_chars = 0
        self.images: List[Dict[str, Any]] = []
        self.fonts: set = set()
        self.text_runs: List[Dict[str, Any]] = []
        self.seq = 0
        # False while walking an annotation's appearance: its text is not the
        # page's text (the counters and verdict stay untouched), but the images
        # it paints ARE placed on the page and are recorded, flagged.
        self.counting = True
        self.annotation: Optional[str] = None
        # Filled axis-aligned rectangles, in paint order with text and images.
        self.shapes: List[Dict[str, Any]] = []
        # {base font: {"embedded": bool|None, "chars": visible characters}}
        self.font_usage: Dict[str, Dict[str, Any]] = {}
        # Watermark tags met (marked content, optional content, Acrobat's
        # PieceInfo) and how many of them are open at this point of the walk.
        self.watermark_tags: List[str] = []
        self._in_watermark = 0

    def walk(self, stream_source: Any, resources: Any, ctm: Matrix, depth: int,
             gs: Optional[_GState] = None) -> None:
        from pypdf.generic import ContentStream

        try:
            content = ContentStream(stream_source, self.reader)
        except Exception as e:
            logger.debug("pdf_forensics: unreadable content stream: %s", e)
            return

        fonts = _resolve(resources, "/Font") or {}
        xobjects = _resolve(resources, "/XObject") or {}
        ext_states = _resolve(resources, "/ExtGState") or {}
        properties = _resolve(resources, "/Properties") or {}
        stack: List[Tuple[Matrix, _TextState, _GState]] = []
        ts = _TextState()
        gs = gs.copy() if gs is not None else _GState()
        # The path under construction: rectangles (page-space bounds) and
        # whether anything in it is NOT a rectangle (curves, slanted lines).
        rects: List[Tuple[float, float, float, float]] = []
        points: List[Tuple[float, float]] = []
        only_rects = True
        pending_clip = False
        marks: List[bool] = []   # one per open BMC/BDC of THIS stream

        for operands, operator in content.operations:
            if operator == b"q":
                stack.append((ctm, ts.copy(), gs.copy()))
            elif operator == b"Q":
                if stack:
                    ctm, ts, gs = stack.pop()
            elif operator == b"re":
                values = _as_float_list(operands)
                if values and len(values) == 4 and _axis_aligned(ctm):
                    x, y, w, h = values
                    rects.append(_bbox_through(ctm, x, y, x + w, y + h))
                else:
                    only_rects = False
            elif operator in (b"m", b"l"):
                values = _as_float_list(operands)
                if values and len(values) == 2:
                    if operator == b"m" and points:
                        only_rects = _close_polygon(points, rects) and only_rects
                        points = []
                    a, b, c, d, e, f = ctm
                    points.append((a * values[0] + c * values[1] + e, b * values[0] + d * values[1] + f))
            elif operator in (b"c", b"v", b"y"):
                only_rects = False
            elif operator in (b"W", b"W*"):
                pending_clip = True
            elif operator in _PATH_END_OPERATORS:
                if points:
                    only_rects = _close_polygon(points, rects) and only_rects
                if operator in _FILL_OPERATORS and only_rects and gs.opaque and gs.alpha >= 0.95:
                    for rect in rects:
                        self._shape(rect, gs)
                if pending_clip and only_rects and len(rects) == 1:
                    gs.clip = _intersect(gs.clip, rects[0])
                rects, points, only_rects, pending_clip = [], [], True, False
            elif operator in (b"g", b"rg", b"k", b"sc", b"scn"):
                gs.fill, gs.fill_known = _fill_colour(operator, operands, gs.fill_known)
            elif operator == b"cs":
                name = str(operands[0]) if operands else ""
                gs.fill_known = name in ("/DeviceGray", "/DeviceRGB", "/DeviceCMYK")
                gs.fill = (0.0, 0.0, 0.0) if gs.fill_known else None
            elif operator == b"gs":
                _apply_ext_state(gs, _resolve(ext_states, str(operands[0])) if operands else None)
            elif operator in (b"BMC", b"BDC"):
                tag = _watermark_tag(operands, properties)
                marks.append(tag is not None)
                if tag is not None:
                    self._in_watermark += 1
                    if tag not in self.watermark_tags:
                        self.watermark_tags.append(tag)
            elif operator == b"EMC":
                if marks and marks.pop():
                    self._in_watermark -= 1
            elif operator == b"cm":
                values = _as_float_list(operands)
                if values and len(values) == 6:
                    ctm = _concat(tuple(values), ctm)  # type: ignore[arg-type]
            elif operator == b"BT":
                ts.tm = ts.tlm = _IDENTITY
            elif operator == b"Tm":
                values = _as_float_list(operands)
                if values and len(values) == 6:
                    ts.tm = ts.tlm = tuple(values)  # type: ignore[assignment]
            elif operator in (b"Td", b"TD"):
                values = _as_float_list(operands)
                if values and len(values) == 2:
                    if operator == b"TD":
                        ts.leading = -values[1]
                    self._next_line(ts, values[0], values[1])
            elif operator == b"T*":
                self._next_line(ts, 0.0, -ts.leading)
            elif operator == b"TL":
                values = _as_float_list(operands)
                if values:
                    ts.leading = values[0]
            elif operator == b"Tz":
                values = _as_float_list(operands)
                if values:
                    ts.hscale = values[0] / 100.0
            elif operator == b"Tc":
                values = _as_float_list(operands)
                if values:
                    ts.char_spacing = values[0]
            elif operator == b"Tw":
                values = _as_float_list(operands)
                if values:
                    ts.word_spacing = values[0]
            elif operator == b"Tr":
                values = _as_float_list(operands)
                if values:
                    ts.render_mode = int(values[0])
            elif operator == b"Tf":
                font_name = str(operands[0]) if operands else ""
                size = _as_float_list(operands[1:2]) if len(operands) > 1 else None
                ts.size = size[0] if size else ts.size
                ts.bytes_per_char = _bytes_per_char(fonts, font_name)
                ts.widths, ts.default_width = _font_widths(fonts, font_name)
                ts.font = _base_font(fonts, font_name) or font_name
                ts.embedded = _font_embedded(fonts, font_name)
                if font_name:
                    self.fonts.add(ts.font)
            elif operator == b"Tj":
                if operands:
                    self._show(ts, ctm, [operands[-1]], gs)
            elif operator == b"'":
                self._next_line(ts, 0.0, -ts.leading)
                if operands:
                    self._show(ts, ctm, [operands[-1]], gs)
            elif operator == b'"':
                values = _as_float_list(operands[:2]) if len(operands) >= 3 else None
                if values:
                    ts.word_spacing, ts.char_spacing = values[0], values[1]
                self._next_line(ts, 0.0, -ts.leading)
                if operands:
                    self._show(ts, ctm, [operands[-1]], gs)
            elif operator == b"TJ":
                if operands and isinstance(operands[0], (list, tuple)):
                    self._show(ts, ctm, list(operands[0]), gs)
            elif operator == b"Do":
                name = str(operands[0]) if operands else ""
                self._paint(xobjects, name, ctm, depth, gs)
            elif operator == b"INLINE IMAGE":
                settings = (operands or {}).get("settings") if isinstance(operands, dict) else None
                if settings is not None:
                    self.seq += 1
                    self.images.append(_inline_record(settings, operands, ctm, self.box, self.rotation,
                                                      self.seq, self.annotation))
        self._in_watermark -= sum(1 for open_mark in marks if open_mark)   # unbalanced stream

    def _shape(self, rect: Tuple[float, float, float, float], gs: _GState) -> None:
        """One opaque filled rectangle, clipped, in paint order."""
        clipped = _intersect(gs.clip, rect) if gs.clip is not None else rect
        if clipped is None or clipped[2] - clipped[0] <= 0 or clipped[3] - clipped[1] <= 0:
            return
        self.seq += 1
        if not self.counting or len(self.shapes) >= _MAX_SHAPES:
            return
        self.shapes.append({
            "rect": _visual_rect(*clipped, self.box, self.rotation),
            "colour": _colour_name(gs.fill) if gs.fill_known else "DESCONOCIDO",
            "seq": self.seq,
        })

    @staticmethod
    def _next_line(ts: _TextState, tx: float, ty: float) -> None:
        ts.tlm = _concat((1.0, 0.0, 0.0, 1.0, tx, ty), ts.tlm)
        ts.tm = ts.tlm

    def _show(self, ts: _TextState, ctm: Matrix, items: Sequence[Any], gs: Optional[_GState] = None) -> None:
        """One text-showing operator: count its characters, record where the
        string lands, and advance the text matrix past it."""
        chars = 0
        advance = 0.0   # text-space units, before Tm
        for item in items:
            if isinstance(item, (bytes, bytearray, str)):
                raw = _original_bytes(item)
                n = len(raw) // max(ts.bytes_per_char, 1)
                chars += n
                advance += _string_advance(raw, ts)
            else:
                try:
                    advance -= (float(item) / 1000.0) * ts.size * ts.hscale
                except (TypeError, ValueError):
                    pass
        visible = ts.render_mode not in (3, 7)
        if self.counting:
            # Render mode 3 = neither filled nor stroked: the OCR layer of a
            # searchable scan. Mode 7 only adds the glyphs to the clip path.
            if visible:
                self.visible_chars += chars
                usage = self.font_usage.setdefault(ts.font or "?", {"embedded": ts.embedded, "chars": 0})
                usage["chars"] += chars
            else:
                self.invisible_chars += chars
        if chars > 0:
            self.seq += 1
            if self.counting and len(self.text_runs) < _MAX_TEXT_RUNS:
                trm = _concat(ts.tm, ctm)
                x0, y0, x1, y1 = _bbox_through(trm, 0.0, -0.25 * ts.size, max(advance, 0.0), 0.8 * ts.size)
                # How far the baseline is from horizontal/vertical: 0 for
                # normal and margin (90°) text, up to 45 for a diagonal legend.
                angle = math.degrees(math.atan2(trm[1], trm[0]))
                self.text_runs.append({
                    "rect": _visual_rect(x0, y0, x1, y1, self.box, self.rotation),
                    "chars": chars, "visible": visible, "seq": self.seq,
                    "font": ts.font, "size": round(abs(ts.size) * math.hypot(trm[0], trm[1]), 1),
                    "skew": round(abs(((angle + 45.0) % 90.0) - 45.0), 1),
                    "alpha": round(gs.alpha, 2) if gs is not None else 1.0,
                    "watermarkTag": self._in_watermark > 0,
                })
        if advance:
            ts.tm = _concat((1.0, 0.0, 0.0, 1.0, advance, 0.0), ts.tm)

    def _paint(self, xobjects: Any, name: str, ctm: Matrix, depth: int, gs: Optional[_GState] = None) -> None:
        try:
            obj = xobjects[name].get_object()
        except Exception:
            return
        subtype = str(obj.get("/Subtype", ""))
        tag = _object_watermark_tag(obj)
        if tag is not None and tag not in self.watermark_tags:
            self.watermark_tags.append(tag)
        if subtype == "/Image":
            self.seq += 1
            record = _image_record(obj, name, ctm, self.box, self.rotation, self.seq, self.annotation)
            record["alpha"] = round(gs.alpha, 2) if gs is not None else 1.0
            record["watermarkTag"] = self._in_watermark > 0 or tag is not None
            self.images.append(record)
        elif subtype == "/Form" and depth < _MAX_FORM_DEPTH:
            matrix = _as_float_list(obj.get("/Matrix") or [])
            inner = _concat(tuple(matrix), ctm) if matrix and len(matrix) == 6 else ctm  # type: ignore[arg-type]
            self._in_watermark += 1 if tag is not None else 0
            try:
                self.walk(obj, obj.get("/Resources") or {}, inner, depth + 1, gs)
            finally:
                self._in_watermark -= 1 if tag is not None else 0

    def walk_annotation(self, annot: Any, subtype: str) -> bool:
        """Paint an annotation's normal appearance (`/AP /N`) onto the page, the
        way a viewer does (PDF 32000 §12.5.5: the form's `/BBox` through its
        `/Matrix`, fitted into the annotation's `/Rect`). Returns whether an
        appearance was walked. Text inside is not counted as page text."""
        rect = _as_float_list(annot.get("/Rect") or [])
        ap = _resolve(annot, "/AP")
        if not rect or len(rect) != 4 or ap is None:
            return False
        normal = _resolve(ap, "/N")
        if normal is None:
            return False
        if not hasattr(normal, "get_data") and hasattr(normal, "keys") and "/BBox" not in normal:
            # A dictionary of appearance states: the selected one, or any.
            state = annot.get("/AS")
            try:
                chosen = normal.get(state) if state is not None else None
                normal = (chosen if chosen is not None else next(iter(normal.values()))).get_object()
            except Exception:
                return False
        bbox = _as_float_list(normal.get("/BBox") or [])
        if not bbox or len(bbox) != 4:
            return False
        matrix = _as_float_list(normal.get("/Matrix") or []) or list(_IDENTITY)
        if len(matrix) != 6:
            matrix = list(_IDENTITY)
        tx0, ty0, tx1, ty1 = _bbox_through(tuple(matrix), bbox[0], bbox[1], bbox[2], bbox[3])  # type: ignore[arg-type]
        rx0, ry0, rx1, ry1 = min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3])
        sx = (rx1 - rx0) / (tx1 - tx0) if tx1 > tx0 else 1.0
        sy = (ry1 - ry0) / (ty1 - ty0) if ty1 > ty0 else 1.0
        fit: Matrix = (sx, 0.0, 0.0, sy, rx0 - tx0 * sx, ry0 - ty0 * sy)
        previous = (self.counting, self.annotation)
        self.counting, self.annotation = False, subtype
        try:
            self.walk(normal, normal.get("/Resources") or {}, _concat(tuple(matrix), fit), 1)  # type: ignore[arg-type]
        finally:
            self.counting, self.annotation = previous
        return True


def _axis_aligned(m: Matrix) -> bool:
    """A rectangle through `m` is still a rectangle with horizontal and
    vertical sides (no skew, no rotation other than a quarter turn)."""
    a, b, c, d, _, _ = m
    scale = max(abs(a), abs(b), abs(c), abs(d), 1e-9)
    return (abs(b) < 1e-3 * scale and abs(c) < 1e-3 * scale) or (abs(a) < 1e-3 * scale and abs(d) < 1e-3 * scale)


def _close_polygon(points: List[Tuple[float, float]], rects: List[Tuple[float, float, float, float]]) -> bool:
    """A `m l l l (h)` subpath: when its corners are those of an axis-aligned
    rectangle, add it to `rects` and say so. Generators draw table cells and
    redaction boxes either way (`re`, or four lines)."""
    if not 4 <= len(points) <= 5:
        return False
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    tol = 0.01 * max(x1 - x0, y1 - y0, 1.0)
    if all(min(abs(x - x0), abs(x - x1)) <= tol for x in xs) and all(min(abs(y - y0), abs(y - y1)) <= tol for y in ys):
        rects.append((x0, y0, x1, y1))
        return True
    return False


def _intersect(a: Optional[Tuple[float, float, float, float]],
               b: Tuple[float, float, float, float]) -> Optional[Tuple[float, float, float, float]]:
    if a is None:
        return b
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x0, y0, max(x1, x0), max(y1, y0))


def _fill_colour(operator: bytes, operands: Sequence[Any], known: bool) -> Tuple[Optional[Tuple[float, float, float]], bool]:
    """The non-stroking colour as RGB, and whether it could be read. A pattern
    (`scn` with a name) or a tint of a colour space this walker does not
    resolve (Separation, Indexed) is "unknown": still opaque paint, but its
    colour is not reported."""
    values = _as_float_list(operands)
    if not values:
        return None, False
    if operator == b"g" or (operator in (b"sc", b"scn") and len(values) == 1 and known):
        return (values[0],) * 3, True
    if operator == b"rg" or (operator in (b"sc", b"scn") and len(values) == 3):
        return (values[0], values[1], values[2]), True
    if operator == b"k" or (operator in (b"sc", b"scn") and len(values) == 4):
        c, m, y, k = values[:4]
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)), True
    return None, False


def _colour_name(rgb: Optional[Tuple[float, float, float]]) -> str:
    if rgb is None:
        return "DESCONOCIDO"
    if min(rgb) >= 0.92:
        return "BLANCO"
    if max(rgb) <= 0.15:
        return "NEGRO"
    return "#%02x%02x%02x" % tuple(int(round(_clamp01(v) * 255)) for v in rgb)


def _apply_ext_state(gs: _GState, state: Any) -> None:
    if state is None:
        return
    try:
        if state.get("/ca") is not None:
            gs.alpha = float(state.get("/ca"))
        blend = state.get("/BM")
        if blend is not None:
            blend = blend.get_object() if hasattr(blend, "get_object") else blend
            names = [str(b) for b in blend] if isinstance(blend, (list, tuple)) else [str(blend)]
            gs.opaque = names[0] in ("/Normal", "/Compatible")
        mask = state.get("/SMask")
        if mask is not None and str(mask) != "/None":
            gs.opaque = False
    except Exception:
        return


def _watermark_tag(operands: Sequence[Any], properties: Any) -> Optional[str]:
    """What a `BMC`/`BDC` says about the content it opens: Acrobat, Word and
    LibreOffice mark a watermark as `/Artifact <</Subtype /Watermark>>`;
    others put it in an optional-content group named after it."""
    if len(operands) < 2:
        return None
    tag, props = str(operands[0]), operands[1]
    try:
        if not hasattr(props, "keys"):
            props = _resolve(properties, str(props))
        if props is None or not hasattr(props, "get"):
            return None
        if tag == "/Artifact" and str(props.get("/Subtype", "")) == "/Watermark":
            return "ARTEFACTO_MARCA_DE_AGUA"
        if tag == "/OC" and _WATERMARK_NAME.search(str(props.get("/Name", ""))):
            return "CAPA_MARCA_DE_AGUA"
    except Exception:
        return None
    return None


def _object_watermark_tag(obj: Any) -> Optional[str]:
    """A Form/Image XObject an editor added as a watermark or background:
    Acrobat's `/PieceInfo /ADBE_CompoundType /Private`, or an `/OC` group
    named after it."""
    try:
        piece = _resolve(obj, "/PieceInfo")
        compound = _resolve(piece, "/ADBE_CompoundType") if piece is not None else None
        if compound is not None:
            kind = _ACROBAT_ADDITIONS.get(str(compound.get("/Private", "")))
            if kind:
                return kind
        group = _resolve(obj, "/OC")
        if group is not None and _WATERMARK_NAME.search(str(group.get("/Name", ""))):
            return "CAPA_MARCA_DE_AGUA"
    except Exception:
        return None
    return None


def _font_embedded(fonts: Any, name: str) -> Optional[bool]:
    """Does the file carry this font's program? Type 3 fonts ARE their
    program. None when the font cannot be read."""
    font = _font_object(fonts, name)
    if font is None:
        return None
    try:
        if str(font.get("/Subtype", "")) == "/Type3":
            return True
        if str(font.get("/Subtype", "")) == "/Type0":
            descendants = _resolve(font, "/DescendantFonts") or []
            font = descendants[0].get_object() if descendants else None
            if font is None:
                return None
        descriptor = _resolve(font, "/FontDescriptor")
        if descriptor is None:
            return False
        return any(descriptor.get(key) is not None for key in ("/FontFile", "/FontFile2", "/FontFile3"))
    except Exception:
        return None


def _original_bytes(item: Any) -> bytes:
    """The string operand as the bytes the font will look up."""
    if isinstance(item, (bytes, bytearray)):
        return bytes(item)
    getter = getattr(item, "get_original_bytes", None)
    if callable(getter):
        try:
            return bytes(getter())
        except Exception:
            pass
    return str(item).encode("latin-1", "replace")


def _string_advance(raw: bytes, ts: _TextState) -> float:
    """Horizontal displacement of one shown string in text space (PDF 32000
    §9.4.4), with the font's widths when it declares them."""
    step = max(ts.bytes_per_char, 1)
    total = 0.0
    for i in range(0, len(raw) - step + 1, step):
        code = int.from_bytes(raw[i:i + step], "big")
        w0 = ts.widths.get(code, ts.default_width) / 1000.0
        total += w0 * ts.size + ts.char_spacing
        if step == 1 and code == 32:
            total += ts.word_spacing
    return total * ts.hscale


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


def _font_widths(fonts: Any, name: str) -> Tuple[Dict[int, float], float]:
    """Glyph widths by character code (in /1000 units) and the width to
    assume for codes the font doesn't list. Simple fonts: `/Widths` from
    `/FirstChar`; composite fonts: the descendant's `/W` array and `/DW`. A
    font that declares nothing (the standard 14) gets a flat default."""
    font = _font_object(fonts, name)
    widths: Dict[int, float] = {}
    if font is None:
        return widths, _DEFAULT_GLYPH_WIDTH
    try:
        if str(font.get("/Subtype", "")) == "/Type0":
            descendants = _resolve(font, "/DescendantFonts") or []
            descendant = descendants[0].get_object() if descendants else None
            if descendant is None:
                return widths, _DEFAULT_GLYPH_WIDTH
            default = float(descendant.get("/DW", 1000) or 1000)
            array = _resolve(descendant, "/W") or []
            i = 0
            while i < len(array):
                first = int(float(array[i].get_object() if hasattr(array[i], "get_object") else array[i]))
                nxt = array[i + 1].get_object() if i + 1 < len(array) and hasattr(array[i + 1], "get_object") else (array[i + 1] if i + 1 < len(array) else None)
                if isinstance(nxt, (list, tuple)):
                    for offset, w in enumerate(nxt):
                        widths[first + offset] = float(w)
                    i += 2
                elif nxt is not None and i + 2 < len(array):
                    last = int(float(nxt))
                    w = float(array[i + 2])
                    if last - first < 65536:
                        for code in range(first, last + 1):
                            widths[code] = w
                    i += 3
                else:
                    break
            return widths, default
        array = _resolve(font, "/Widths") or []
        first = int(float(font.get("/FirstChar", 0) or 0))
        for offset, w in enumerate(array):
            try:
                widths[first + offset] = float(w)
            except (TypeError, ValueError):
                continue
        descriptor = _resolve(font, "/FontDescriptor")
        missing = float(descriptor.get("/MissingWidth", 0) or 0) if descriptor is not None else 0.0
        default = missing if missing > 0 else (
            sum(widths.values()) / len(widths) if widths else _DEFAULT_GLYPH_WIDTH
        )
        return widths, default
    except Exception:
        return {}, _DEFAULT_GLYPH_WIDTH


def _shown_length(operand: Any) -> int:
    if isinstance(operand, (bytes, bytearray, str)):
        return len(operand)
    return 0  # TJ kerning adjustments are numbers


def _inline_record(
    settings: Any, operands: Any, ctm: Matrix, box, rotation: int, seq: int = 0, annotation: Optional[str] = None
) -> Dict[str, Any]:
    class _Inline(dict):
        """Adapts an inline image's abbreviated keys to the XObject ones."""

    adapted = _Inline()
    aliases = {"/W": "/Width", "/H": "/Height", "/BPC": "/BitsPerComponent", "/F": "/Filter", "/CS": "/ColorSpace"}
    for key, value in dict(settings).items():
        adapted[aliases.get(str(key), str(key))] = value
    adapted._data = operands.get("data") or b""  # type: ignore[attr-defined]
    return _image_record(adapted, "inline", ctm, box, rotation, seq, annotation)


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


def inspect_page(reader: Any, page_index: int, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Raw structural facts of one page (0-based). Never raises: a page that
    can't be walked reports `error` and is judged INDETERMINADO downstream.

    Besides the page content, the normal appearance of every annotation is
    painted too, so an image dropped on the page as a `/Stamp` by an editor is
    seen (flagged `fromAnnotation`); the annotations themselves are listed
    under `annotations`, and `overlay` holds the paint-order facts the
    integrity layer reads (see `overlay_facts`)."""
    report: Dict[str, Any] = {
        "page": page_index + 1, "visibleTextChars": 0, "invisibleTextChars": 0,
        "fonts": [], "images": [], "annotations": [],
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
        annotations = _page_annotations(page, walk, geometry, rotation)
        report.update({
            "visibleTextChars": walk.visible_chars,
            "invisibleTextChars": walk.invisible_chars,
            "fonts": sorted(walk.fonts),
            "images": walk.images,
            "annotations": annotations,
            "rotation": rotation % 360,
            # Points, as shown (after /Rotate): what "is this page the same
            # sheet as the others?" compares.
            "size": [round(geometry[3 if rotation % 180 else 2], 1), round(geometry[2 if rotation % 180 else 3], 1)],
            "overlay": overlay_facts(walk, policy),
        })
    except Exception as e:
        logger.warning("pdf_forensics: could not inspect page %d: %s", page_index + 1, e)
        report["error"] = str(e)[:200]
    return report


def _page_annotations(page: Any, walk: _PageWalk, geometry, rotation: int) -> List[Dict[str, Any]]:
    """Every annotation on the page, as the integrity layer wants it: its
    subtype, where it sits, whether it is hidden, whether it carries an
    appearance and whether that appearance paints an image."""
    out: List[Dict[str, Any]] = []
    try:
        annots = page.get("/Annots")
        annots = annots.get_object() if hasattr(annots, "get_object") else annots
    except Exception:
        annots = None
    for ref in annots or []:
        try:
            annot = ref.get_object()
            subtype = str(annot.get("/Subtype", "") or "")
            rect = _as_float_list(annot.get("/Rect") or [])
            try:
                flags = int(annot.get("/F", 0) or 0)
            except (TypeError, ValueError):
                flags = 0
            before = len(walk.images)
            walked = walk.walk_annotation(annot, subtype)
            painted = walk.images[before:]
            out.append({
                "subtype": subtype,
                "rect": _visual_rect(min(rect[0], rect[2]), min(rect[1], rect[3]),
                                     max(rect[0], rect[2]), max(rect[1], rect[3]), geometry, rotation)
                if rect and len(rect) == 4 else None,
                "hidden": bool(flags & 2),
                "hasAppearance": walked,
                "hasImage": any(i.get("fromAnnotation") for i in painted),
                **({"fieldType": str(annot.get("/FT"))} if annot.get("/FT") is not None else {}),
                **({"action": _action_kind(annot)} if annot.get("/A") is not None else {}),
                **({"contents": str(annot.get("/Contents"))[:120]} if annot.get("/Contents") else {}),
            })
        except Exception as e:
            logger.debug("pdf_forensics: unreadable annotation: %s", e)
            out.append({"subtype": "?", "error": str(e)[:100]})
    return out


def _action_kind(annot: Any) -> str:
    try:
        action = annot.get("/A").get_object()
        return str(action.get("/S", "") or "")
    except Exception:
        return "?"


def overlay_facts(walk: _PageWalk, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Paint-order facts of one walked page, pure:

    - `textAfterFullPageImageChars`: visible characters drawn AFTER an image
      that is the page itself (a scan) — text typed onto a scan. None when
      the page holds no full-page image (nothing to be "over"). Strings that
      sit entirely in the top/bottom `overlay_margin` band are not counted:
      page numbers, Bates stamps and headers a document system adds are not
      an edit of the content.
    - `imagesOverText`: images (not decoration, not the page itself) painted
      AFTER a visible string they cover — the "white box over the amount"
      edit. None when the page has no visible text.
    - `shapesOverText`: the same for opaque filled RECTANGLES — the redaction
      bar and the "white-out, then retype" of every online editor. A
      generator paints a cell's background BEFORE its text; a box painted
      after the string it covers hides it. None when the page has no visible
      text.
    - `watermark`: what on the page looks like a watermark — content the
      generator itself tagged as one (`tags`), strings set on a diagonal
      (`diagonalRuns`), and translucent strings or images (`translucentRuns`,
      `translucentImages`). Raw candidates: the thresholds are the integrity
      policy's.
    - `fontUsage`: visible characters per font and whether the file embeds
      it, most used first.
    """
    cfg = {**DEFAULT_POLICY, **(policy or {})}
    full_coverage = float(cfg.get("full_page_coverage", DEFAULT_POLICY["full_page_coverage"]))
    smallest = float(cfg.get("ignore_image_coverage_below", DEFAULT_POLICY["ignore_image_coverage_below"]))
    margin = float(cfg.get("overlay_margin", 0.06))
    min_overlap = float(cfg.get("image_over_text_min_overlap", 0.5))

    page_images = [i for i in walk.images if not i.get("fromAnnotation")]
    full = [i for i in page_images if float(i.get("coverage") or 0) >= full_coverage]
    visible_runs = [r for r in walk.text_runs if r.get("visible")]

    after_full: Optional[int] = None
    if full:
        first_full = min(int(i.get("seq") or 0) for i in full)
        after_full = 0
        for run in visible_runs:
            if int(run["seq"]) <= first_full:
                continue
            _, y0, _, y1 = run["rect"]
            if y1 <= margin or y0 >= 1.0 - margin:
                continue
            after_full += int(run["chars"])

    over_text: Optional[List[Dict[str, Any]]] = None
    if visible_runs:
        over_text = []
        for image in page_images:
            coverage = float(image.get("coverage") or 0)
            if coverage < smallest or coverage >= full_coverage or not image.get("rect"):
                continue
            covered = [
                r for r in visible_runs
                if int(r["seq"]) < int(image.get("seq") or 0)
                and _overlap_ratio(r["rect"], image["rect"], "a") >= min_overlap
            ]
            if covered:
                over_text.append({
                    "name": image.get("name"), "rect": image["rect"], "coverage": coverage,
                    "coveredRuns": len(covered), "coveredChars": sum(int(r["chars"]) for r in covered),
                    "sampleRuns": [r["rect"] for r in covered[:3]],
                })
        over_text = over_text[:10]

    shapes_over: Optional[List[Dict[str, Any]]] = None
    if visible_runs:
        shapes_over = []
        for shape in walk.shapes:
            covered = [
                r for r in visible_runs
                if int(r["seq"]) < int(shape["seq"]) and _overlap_ratio(r["rect"], shape["rect"], "a") >= min_overlap
            ]
            if covered:
                shapes_over.append({
                    "rect": shape["rect"], "colour": shape["colour"],
                    "coveredRuns": len(covered), "coveredChars": sum(int(r["chars"]) for r in covered),
                })
        shapes_over = shapes_over[:20]

    def brief(run: Dict[str, Any]) -> Dict[str, Any]:
        return {k: run.get(k) for k in ("rect", "chars", "size", "skew", "alpha", "font")}

    return {
        "fullPageImages": len(full),
        "visibleRuns": len(visible_runs),
        "textAfterFullPageImageChars": after_full,
        "imagesOverText": over_text,
        "shapesOverText": shapes_over,
        "watermark": {
            "tags": list(walk.watermark_tags),
            "taggedChars": sum(int(r["chars"]) for r in visible_runs if r.get("watermarkTag")),
            "diagonalRuns": [brief(r) for r in visible_runs if float(r.get("skew") or 0) >= 10.0][:20],
            "translucentRuns": [brief(r) for r in visible_runs if float(r.get("alpha") or 1) < 0.9][:20],
            "translucentImages": [
                {"name": i.get("name"), "rect": i.get("rect"), "coverage": i.get("coverage"), "alpha": i.get("alpha")}
                for i in page_images if float(i.get("alpha") or 1) < 0.9
            ][:10],
        },
        "fontUsage": [
            {"font": name, **usage}
            for name, usage in sorted(walk.font_usage.items(), key=lambda kv: -int(kv[1]["chars"]))
        ][:40],
    }


def dct_stream_bytes(reader: Any, page_index: int, image_name: str) -> Optional[bytes]:
    """The stored bytes of an image XObject when — and only when — they ARE a
    JPEG file (`/DCTDecode` as the sole filter): what error-level analysis and
    the embedded-metadata check need. Looks in the page's resources and in the
    Form XObjects it paints. None otherwise."""
    try:
        page = reader.pages[page_index]
        found = _find_xobject(page.get("/Resources") or {}, image_name, 0)
        if found is None:
            return None
        filters = _filters_of(found)
        if filters != ["/DCTDecode"]:
            return None
        data = _raw_stream_bytes(found)
        return data if data[:2] == b"\xff\xd8" else None
    except Exception as e:
        logger.debug("pdf_forensics: no DCT bytes for %s: %s", image_name, e)
        return None


def _find_xobject(resources: Any, name: str, depth: int) -> Any:
    xobjects = _resolve(resources, "/XObject") or {}
    try:
        if name in xobjects:
            return xobjects[name].get_object()
    except Exception:
        pass
    if depth >= _MAX_FORM_DEPTH:
        return None
    for key in list(xobjects.keys()) if hasattr(xobjects, "keys") else []:
        try:
            obj = xobjects[key].get_object()
        except Exception:
            continue
        if str(obj.get("/Subtype", "")) == "/Form":
            found = _find_xobject(obj.get("/Resources") or {}, name, depth + 1)
            if found is not None:
                return found
    return None


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
        facts = inspect_page(reader, index, policy)
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
