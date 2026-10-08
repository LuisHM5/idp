"""File integrity — layer 0 of the authenticity stage.

Where `pdf_forensics` asks "is this the capture of a paper?", this module asks
the question every other type needs: **was this file touched after it was
made?** A CFDI, a pedimento, a bank statement are born digital, so "DIGITAL"
says nothing about them — but "saved twice over itself", "a FreeText
annotation over the amount", "text typed on top of the scan", "an image
dropped over the printed total", "the digital signature no longer covers the
file" do. Everything here is read off the bytes with pypdf, regex and Pillow:
no raster, no model, no OCR — free, and nothing a forged file can talk its way
past.

Sources of evidence, and why each is read the way it is:

- **Incremental updates.** A PDF editor that "saves" appends a new cross-
  reference section pointing back (`/Prev`) at the previous one, so the file
  carries its own history. pypdf follows that chain but folds it into ONE
  merged trailer (`/Prev` is consumed, never exposed), which is why the chain
  is parsed from the raw bytes here. Two traps: a *linearized* file has two
  sections by construction (first-page + main), and a *digitally signed* file
  is always ≥ 2 generations (signing IS an incremental update) — both are
  discounted, and `/L` of the linearization dictionary must still equal the
  file length. A hybrid file's `/XRefStm` is the companion stream of the same
  section, never a generation.
- **Dates.** `/ModDate` later than `/CreationDate` is what every "open and
  save" does — evidence, not an accusation. What IS inconsistent: a
  modification BEFORE the creation, or the Info dictionary and the XMP packet
  (two copies of the same fact) disagreeing beyond a tolerance.
- **Software.** Producer/Creator/XMP `CreatorTool` and the XMP history
  (`stEvt:softwareAgent`) name the tools that touched the file. An editor's
  name there is soft evidence (Acrobat's own OCR "Paper Capture" and every
  scanner app that happens to embed an editor's library are the exceptions
  the policy lists).
- **Annotations, forms, active content.** Read from the object tree; the
  annotation *appearances* are painted by `pdf_forensics.inspect_page`, so a
  `/Stamp` carrying a pasted image is seen as an image too.
- **Paint order** (`pdf_forensics.overlay_facts`): visible text drawn after
  the full-page image = typed onto a scan; an image drawn after text it
  covers = a patch over the original.
- **Boxes over text** (same paint-order walk): an opaque filled rectangle
  painted after a string it covers is a redaction bar, or the white-out an
  online editor lays down before retyping a value. A generator paints a
  cell's background BEFORE its text, so this does not fire on shaded tables.
- **Watermarks.** What the file itself says is one — content tagged
  `/Artifact /Watermark`, an optional-content group or Acrobat `/PieceInfo`
  named after it, a `/Watermark` annotation — plus what looks like one:
  a legend set on a diagonal, large translucent text, a translucent image,
  and the line a trial editor or a scanning app leaves ("Scanned with…").
  A watermark burnt into the pixels of a scan is invisible here: that is
  the vision layer's (`vision.no_watermark`), and this signal stays EMPTY
  on a page that is only a scan.
- **Foreign font.** A file whose text is set in embedded fonts, except a
  handful of characters in a font it does NOT embed: what an editor leaves
  when it retypes a value with whatever font the machine had.
- **Hidden content.** Optional-content layers switched off and annotations
  flagged hidden: content the file carries and a viewer does not show.
- **Page mix.** Scanned pages among born-digital ones, or born-digital
  pages of different sizes, inside one document: a replaced or inserted page.
- **Signatures.** Only the geometry: the latest `/ByteRange` must reach the
  end of the file, otherwise something was appended after signing. No
  cryptography (no `cryptography` in the bundle), stated plainly.
- **Images** (JPG/PNG uploads, and the JPEG a scan stores): EXIF `Software`,
  Photoshop's APP13 segment, PNG `Software`, XMP history, EXIF dates.
  Missing camera data is *information*: messaging apps strip EXIF.

EMPTY (None) is "could not be determined" and never counts as a finding.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import pdf_forensics

logger = logging.getLogger(__name__)

LAYER = "integrity"

# ------------------------------------------------------------------ policy

DEFAULT_INTEGRITY: Dict[str, Any] = {
    # Annotation subtypes that are not an edit of the content: links a viewer
    # or Acrobat creates from URLs, the pop-up companion of a note, and form
    # widgets (graded separately, by `no_form_fields`).
    "ignored_annotations": ["/Link", "/Popup", "/Widget"],
    # Substrings (lower-case) that name a PDF/image EDITOR in Producer,
    # Creator, XMP CreatorTool or the XMP history. Bare "acrobat" is NOT here:
    # "Acrobat PDFMaker" generates PDFs from Word, it does not edit them.
    "editor_patterns": [
        "acrobat pro", "acrobat dc", "acrobat 2", "pdf-xchange", "foxit", "nitro", "pdfelement",
        "wondershare", "sejda", "ilovepdf", "smallpdf", "pdfescape", "pdffiller", "photoshop",
        "gimp", "pixlr", "affinity", "inkscape", "pdf24", "soda pdf", "master pdf", "pdfgear",
        "xodo", "lumin", "pdfsam", "pdf architect", "picsart", "snapseed", "lightroom",
    ],
    # A tool string containing one of these is capture software, whatever
    # else it says ("Adobe Acrobat 11 Paper Capture Plug-in" is Acrobat's OCR).
    "capture_exceptions": ["paper capture", "scan", "pdfmaker"],
    # Seconds two timestamps of the same event may differ (scanner clocks,
    # export pipelines that stamp twice).
    "date_tolerance_s": 120,
    # Visible characters drawn after the full-page image before the page
    # counts as "typed onto a scan": a stray glyph is not an edit.
    "min_overlay_chars": 8,
    # ...and at or above this many the page is not a scan somebody typed on
    # but a digital composition over a full-page background (a Word letter on
    # an image letterhead — measured on real letters). That is the structure
    # layer's business; here the signal abstains.
    "max_overlay_chars": 200,
    # Top/bottom band (fraction of the page) where text over a scan is a
    # folio, a Bates number or a header, not an edit.
    "overlay_margin": 0.06,
    # Share of a text run an image must cover to count as painted OVER it.
    "image_over_text_min_overlap": 0.5,
    # Visible characters an opaque rectangle must cover to count as painted
    # OVER text (a check-box square over its own bullet glyph is not a patch).
    "shape_over_text_min_chars": 2,
    # A diagonal or translucent string is a watermark from this size up (pt)…
    "watermark_min_font_size": 14,
    # …"translucent" being at or below this fill opacity…
    "watermark_max_alpha": 0.6,
    # …and a translucent image from this share of the page up.
    "watermark_min_image_coverage": 0.05,
    # Lines (lower-case substrings of the page's own text) a trial editor,
    # an online converter or a scanning app prints on what it exports.
    "watermark_phrases": [
        "camscanner", "scanned with", "scanned by", "escaneado con", "escaneado por", "tinyscanner",
        "genius scan", "adobe scan", "ilovepdf", "sejda", "pdfescape", "smallpdf", "pdffiller",
        "pdfelement", "wondershare", "pdf-xchange", "foxit", "nitro pro", "soda pdf", "trial version",
        "version de prueba", "versión de prueba", "evaluation version", "evaluation copy",
        "unregistered version", "demo version", "watermark",
    ],
    # A font the file does not embed is "foreign" when the page has at least
    # `min_page_chars` visible characters, at least `min_embedded_share` of
    # them in embedded fonts, and the font shows at most `max_chars`.
    "foreign_font_min_page_chars": 200,
    "foreign_font_min_embedded_share": 0.9,
    "foreign_font_max_chars": 60,
    # Born-digital pages of one document may differ in size by this fraction.
    "page_size_tolerance": 0.05,
    # PDF/A-3 invoices legitimately carry their XML as an attachment.
    "allow_embedded_files": False,
    # Annotation actions that are active content (a /URI link is not).
    "active_actions": ["/JavaScript", "/Launch", "/SubmitForm", "/ImportData"],
    # Per-code severity override: {"ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES": "hard"}.
    "severity": {},
}

# ------------------------------------------------------------------ reasons

R_INCREMENTAL = "ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES"
R_MODIFIED_AFTER_SIGNING = "MODIFICADO_DESPUES_DE_FIRMAR"
R_SIGNED = "FIRMA_DIGITAL_PRESENTE"
R_DATES_INCONSISTENT = "FECHAS_DE_METADATOS_INCONSISTENTES"
R_MODIFIED_AFTER_CREATION = "MODIFICADO_DESPUES_DE_CREADO"
R_EDITOR = "EDITOR_DE_PDF_EN_METADATOS"
R_ANNOTATIONS = "ANOTACIONES_EN_PAGINA"
R_FORM_FIELDS = "CAMPOS_DE_FORMULARIO"
R_TEXT_OVER_SCAN = "TEXTO_ESCRITO_SOBRE_ESCANEO"
R_IMAGE_OVER_TEXT = "IMAGEN_SOBRE_TEXTO_NATIVO"
R_ACTIVE_CONTENT = "CONTENIDO_ACTIVO_EN_PDF"
R_IMAGE_EDITOR = "IMAGEN_CON_SOFTWARE_DE_EDICION"
R_EXIF_DATES = "FECHAS_EXIF_INCONSISTENTES"
R_NO_CAMERA = "SIN_METADATOS_DE_CAMARA"
R_UNDETERMINED = "INTEGRIDAD_NO_DETERMINABLE"
R_WATERMARK = "MARCA_DE_AGUA_EN_PAGINA"
R_SHAPE_OVER_TEXT = "RECUADRO_SOBRE_TEXTO_NATIVO"
R_FOREIGN_FONT = "FUENTE_AJENA_EN_PAGINA"
R_HIDDEN_CONTENT = "CONTENIDO_OCULTO_EN_PDF"
R_PAGES_MIXED = "PAGINAS_DE_ORIGEN_DISTINTO"

# Default severity of each finding. "hard" makes the verdict NO_AUTENTICO,
# "soft" SOSPECHOSO, "info" nothing — what it does to the DOCUMENT is the
# catalog field's `on_fail`, never this table.
SEVERITY: Dict[str, str] = {
    R_INCREMENTAL: "soft",
    R_MODIFIED_AFTER_SIGNING: "hard",
    R_SIGNED: "info",
    R_DATES_INCONSISTENT: "soft",
    R_MODIFIED_AFTER_CREATION: "info",
    R_EDITOR: "soft",
    R_ANNOTATIONS: "soft",
    R_FORM_FIELDS: "soft",
    R_TEXT_OVER_SCAN: "soft",
    R_IMAGE_OVER_TEXT: "soft",
    R_ACTIVE_CONTENT: "soft",
    R_IMAGE_EDITOR: "soft",
    R_EXIF_DATES: "soft",
    R_NO_CAMERA: "info",
    R_UNDETERMINED: "info",
    R_WATERMARK: "soft",
    R_SHAPE_OVER_TEXT: "soft",
    R_FOREIGN_FONT: "soft",
    R_HIDDEN_CONTENT: "soft",
    R_PAGES_MIXED: "soft",
}

MESSAGES: Dict[str, str] = {
    R_INCREMENTAL: "El archivo se guardó {n} vez/veces sobre sí mismo después de generado (actualizaciones incrementales)",
    R_MODIFIED_AFTER_SIGNING: "El archivo fue modificado después de firmarse digitalmente: la firma ya no cubre todo el contenido",
    R_SIGNED: "El archivo lleva firma digital ({n})",
    R_DATES_INCONSISTENT: "Las fechas de creación/modificación del archivo no son consistentes entre sí ({detail})",
    R_MODIFIED_AFTER_CREATION: "El archivo se modificó {delta} después de crearse (según sus metadatos)",
    R_EDITOR: "Los metadatos nombran un editor de PDF: {editor}",
    R_ANNOTATIONS: "La página tiene anotaciones agregadas encima del contenido ({kinds})",
    R_FORM_FIELDS: "El archivo tiene campos de formulario ({n}, {filled} con valor)",
    R_TEXT_OVER_SCAN: "Hay texto escrito encima del escaneo ({n} caracteres visibles sobre la imagen de la página)",
    R_IMAGE_OVER_TEXT: "Hay {n} imagen(es) colocada(s) encima del texto original (posible parche)",
    R_ACTIVE_CONTENT: "El PDF contiene contenido activo o adjuntos ({kinds})",
    R_IMAGE_EDITOR: "La imagen fue guardada por un programa de edición: {editor}",
    R_EXIF_DATES: "La fecha de modificación EXIF es posterior a la de captura ({detail})",
    R_NO_CAMERA: "La imagen no trae metadatos de cámara (pudo pasar por mensajería o ser una captura de pantalla)",
    R_UNDETERMINED: "No se pudo leer la estructura del archivo para evaluar su integridad ({detail})",
    R_WATERMARK: "La página lleva una marca de agua o leyenda sobrepuesta ({kinds})",
    R_SHAPE_OVER_TEXT: "Hay {n} recuadro(s) opaco(s) pintado(s) encima del texto original ({colours}): tachadura o parche sobre un dato",
    R_FOREIGN_FONT: "{n} carácter(es) usan una fuente que el archivo no incrusta ({fonts}) mientras el resto usa fuentes incrustadas: texto agregado por un editor",
    R_HIDDEN_CONTENT: "El PDF lleva contenido que no se muestra ({kinds})",
    R_PAGES_MIXED: "Las páginas del documento no son homogéneas ({detail}): posible página sustituida o insertada",
}

SIGNALS: List[str] = [
    "integrity.single_generation", "integrity.generations", "integrity.dates_consistent",
    "integrity.producer_consistent", "integrity.editor", "integrity.no_annotations",
    "integrity.no_form_fields", "integrity.no_text_overlay", "integrity.no_image_over_text",
    "integrity.digitally_signed", "integrity.signature_intact", "integrity.no_active_content",
    "integrity.camera_metadata",
    "integrity.no_watermark", "integrity.no_shape_over_text", "integrity.fonts_consistent",
    "integrity.no_hidden_content", "integrity.pages_consistent",
]

_MAX_DICT_SLICE = 65536
_MAX_CHAIN = 64
_HISTORY_AGENT = re.compile(rb'stEvt:softwareAgent\s*=\s*"([^"]*)"|<stEvt:softwareAgent>([^<]*)</stEvt:softwareAgent>')
_HISTORY_ACTION = re.compile(rb'stEvt:action\s*=\s*"([^"]*)"|<stEvt:action>([^<]*)</stEvt:action>')
_XMP_DATE = {
    "create": re.compile(rb'xmp:CreateDate\s*=\s*"([^"]*)"|<xmp:CreateDate>([^<]*)</xmp:CreateDate>'),
    "modify": re.compile(rb'xmp:ModifyDate\s*=\s*"([^"]*)"|<xmp:ModifyDate>([^<]*)</xmp:ModifyDate>'),
}


def policy_for(block: Any) -> Dict[str, Any]:
    cfg = dict(DEFAULT_INTEGRITY)
    if isinstance(block, dict):
        for key, value in block.items():
            if key in cfg and value is not None:
                cfg[key] = value
    return cfg


# ------------------------------------------------------------------ update chain


def update_chain(raw: bytes) -> Dict[str, Any]:
    """The file's cross-reference history, from the bytes (see module doc).

    `generations` is how many times the file was written: 1 for a file
    produced once, 2 for one incremental save on top, ... — already
    discounting the extra section linearization adds. None when the chain
    cannot be followed (a damaged file pypdf would rebuild): abstain, never
    accuse."""
    out: Dict[str, Any] = {
        "generations": None, "chain": [], "startxrefCount": 0, "eofCount": 0,
        "linearized": False, "linearizedLengthMatches": None, "hybridXrefStm": False,
        "fileLength": len(raw or b""),
    }
    if not raw:
        out["error"] = "empty"
        return out
    starts = [(m.start(), int(m.group(1))) for m in re.finditer(rb"startxref\s+(\d+)", raw)]
    out["startxrefCount"] = len(starts)
    out["eofCount"] = raw.count(b"%%EOF")

    head = raw[:1024]
    lin = re.search(rb"/Linearized\s+[\d.]+", head)
    if lin:
        out["linearized"] = True
        length = re.search(rb"/L\s+(\d+)", head[lin.start():lin.start() + 512])
        if length:
            out["linearizedLength"] = int(length.group(1))
            out["linearizedLengthMatches"] = int(length.group(1)) == len(raw)

    if not starts:
        out["error"] = "no startxref"
        return out

    chain: List[int] = []
    seen = set()
    offset: Optional[int] = starts[-1][1]
    while offset is not None and len(chain) < _MAX_CHAIN:
        if offset in seen or offset < 0 or offset >= len(raw):
            out["error"] = f"xref offset {offset} out of range or repeated"
            return out
        seen.add(offset)
        chain.append(offset)
        section = _section_dict(raw, offset)
        if section is None:
            out["error"] = f"no xref section at {offset}"
            return out
        if section.get("xrefstm") is not None:
            out["hybridXrefStm"] = True
        offset = section.get("prev")

    out["chain"] = chain
    generations = len(chain)
    if out["linearized"] and generations >= 2:
        # First-page section + main section are ONE generation of a linearized
        # file; anything appended after linearizing breaks its /L as well.
        generations -= 1
        if out["linearizedLengthMatches"] is False:
            generations = max(generations, len(chain))  # keep the extra one
    out["generations"] = generations
    return out


def _section_dict(raw: bytes, offset: int) -> Optional[Dict[str, Optional[int]]]:
    """`/Prev` and `/XRefStm` of the cross-reference section at `offset`,
    whether it is a classic table (`xref ... trailer <<...>>`) or a cross-
    reference stream (`N G obj <<...>> stream`)."""
    window = raw[offset:offset + 32].lstrip()
    if window.startswith(b"xref"):
        trailer_at = raw.find(b"trailer", offset)
        if trailer_at < 0:
            return None
        end = raw.find(b"startxref", trailer_at)
        chunk = raw[trailer_at:(end if end > 0 else trailer_at + _MAX_DICT_SLICE)][:_MAX_DICT_SLICE]
    elif re.match(rb"\d+\s+\d+\s+obj", window):
        end = raw.find(b"stream", offset)
        chunk = raw[offset:(end if end > 0 else offset + _MAX_DICT_SLICE)][:_MAX_DICT_SLICE]
        if b"/XRef" not in chunk:
            return None
    else:
        return None
    prev = re.search(rb"/Prev\s+(\d+)", chunk)
    xrefstm = re.search(rb"/XRefStm\s+(\d+)", chunk)
    return {"prev": int(prev.group(1)) if prev else None, "xrefstm": int(xrefstm.group(1)) if xrefstm else None}


# ------------------------------------------------------------------ file report


def file_report(raw: bytes, reader: Any, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything about the FILE (not a page): update chain, dates, software,
    signatures, forms, active content. Each part is independent and never
    raises; a part that fails reports `error` and its signals stay empty."""
    cfg = policy_for(policy)
    report: Dict[str, Any] = {"chain": update_chain(raw)}
    xmp_raw = _xmp_bytes(reader)
    report["dates"] = _dates(reader, xmp_raw, cfg)
    report["software"] = _software(reader, xmp_raw, cfg)
    report["signatures"], report["forms"] = _acroform(reader, len(raw or b""))
    report["activeContent"] = _active_content(reader, cfg)
    report["optionalContent"] = _optional_content(reader)
    # Measured on real files: Microsoft Word writes EVERY PDF in two passes —
    # the document, then an incremental section adding its XMP metadata — so
    # a plain Word export shows two generations with creation == modification
    # to the second. That is the generator finishing its own file, not an
    # editor coming back to it: a second section written within the date
    # tolerance of creation is folded into the first generation. An editor
    # that saves later moves /ModDate and is still counted.
    chain = report["chain"]
    dates = report["dates"]
    same_session = (
        dates.get("creation") is not None and dates.get("modification") is not None
        and not dates.get("modifiedAfterCreation") and dates.get("consistent") is not False
    )
    chain["generatorTwoPass"] = bool(chain.get("generations") == 2 and same_session)
    return report


def _xmp_bytes(reader: Any) -> bytes:
    try:
        meta = reader.trailer["/Root"].get("/Metadata")
        if meta is None:
            return b""
        return bytes(meta.get_object().get_data() or b"")[:2_000_000]
    except Exception:
        return b""


def _pdf_date(value: Any) -> Optional[datetime]:
    """A PDF date (`D:YYYYMMDDHHmmSSOHH'mm'`) or an XMP/ISO one, as an aware
    datetime (UTC when the string carries no zone)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    m = re.match(r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*(Z|[+-]\d{2}'?\d{0,2}'?)?", text)
    if m:
        parts = [int(m.group(i) or (1 if i in (2, 3) else 0)) for i in range(1, 7)]
        try:
            dt = datetime(*parts)  # type: ignore[arg-type]
        except ValueError:
            return None
        zone = m.group(7)
        if zone and zone != "Z":
            sign = 1 if zone[0] == "+" else -1
            digits = re.sub(r"[^0-9]", "", zone)
            hours, minutes = int(digits[:2] or 0), int(digits[2:4] or 0)
            from datetime import timedelta
            return (dt - sign * timedelta(hours=hours, minutes=minutes)).replace(tzinfo=timezone.utc)
        return dt.replace(tzinfo=timezone.utc)
    try:
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _dates(reader: Any, xmp_raw: bytes, cfg: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"creation": None, "modification": None, "xmpCreate": None, "xmpModify": None}
    try:
        info = reader.metadata or {}
    except Exception:
        info = {}
    for key, label in (("/CreationDate", "creation"), ("/ModDate", "modification")):
        try:
            value = info.get(key)
        except Exception:
            value = None
        out[label] = _pdf_date(value)
    for label, pattern in _XMP_DATE.items():
        m = pattern.search(xmp_raw) if xmp_raw else None
        if m:
            out["xmpCreate" if label == "create" else "xmpModify"] = _pdf_date(
                (m.group(1) or m.group(2) or b"").decode("utf-8", "replace"))

    tol = float(cfg.get("date_tolerance_s") or 0)
    creation = out["creation"] or out["xmpCreate"]
    modification = out["modification"] or out["xmpModify"]
    consistent: Optional[bool] = None
    details: List[str] = []
    if creation and modification:
        consistent = True
        if (creation - modification).total_seconds() > tol:
            consistent = False
            details.append("modificación anterior a la creación")
        out["modifiedAfterCreation"] = (modification - creation).total_seconds() > tol
        out["modifiedAfterCreationSeconds"] = round((modification - creation).total_seconds())
    for a, b, what in (("creation", "xmpCreate", "creación Info vs XMP"),
                       ("modification", "xmpModify", "modificación Info vs XMP")):
        if out[a] and out[b] and abs((out[a] - out[b]).total_seconds()) > tol:
            consistent = False
            details.append(what)
    out["consistent"] = consistent
    out["details"] = details
    for key in ("creation", "modification", "xmpCreate", "xmpModify"):
        if out[key] is not None:
            out[key] = out[key].isoformat()
    return out


def _software(reader: Any, xmp_raw: bytes, cfg: Dict[str, Any]) -> Dict[str, Any]:
    meta = pdf_forensics.document_metadata(reader)
    out: Dict[str, Any] = {
        "producer": meta.get("producer"), "creator": meta.get("creator"),
        "xmpCreatorTool": None, "historyAgents": [], "historyActions": [],
    }
    if xmp_raw:
        m = re.search(rb'xmp:CreatorTool\s*=\s*"([^"]*)"|<xmp:CreatorTool>([^<]*)</xmp:CreatorTool>', xmp_raw)
        if m:
            out["xmpCreatorTool"] = (m.group(1) or m.group(2) or b"").decode("utf-8", "replace")[:200]
        agents = []
        for m in _HISTORY_AGENT.finditer(xmp_raw):
            agent = (m.group(1) or m.group(2) or b"").decode("utf-8", "replace").strip()
            if agent and agent not in agents:
                agents.append(agent[:200])
        out["historyAgents"] = agents[:20]
        out["historyActions"] = [
            (m.group(1) or m.group(2) or b"").decode("utf-8", "replace") for m in _HISTORY_ACTION.finditer(xmp_raw)
        ][:40]
    out["producerFamily"] = pdf_forensics.classify_origin({"x": out["producer"] or ""})
    out["creatorFamily"] = pdf_forensics.classify_origin({"x": out["creator"] or out["xmpCreatorTool"] or ""})
    candidates = [("producer", out["producer"]), ("creator", out["creator"]),
                  ("xmpCreatorTool", out["xmpCreatorTool"])] + [("history", a) for a in out["historyAgents"]]
    out["editor"], out["editorSource"] = find_editor([c for c in candidates if c[1]], cfg)
    capture_then_office = (
        out["creatorFamily"] in (pdf_forensics.ORIGIN_SCANNER, pdf_forensics.ORIGIN_CAPTURE_APP)
        and out["producerFamily"] == pdf_forensics.ORIGIN_OFFICE
    )
    out["consistent"] = out["editor"] is None and not capture_then_office
    if capture_then_office:
        out["mismatch"] = "creado por software de captura, producido por una suite de oficina"
    return out


def find_editor(candidates: Sequence[Tuple[str, str]], cfg: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """The first tool string that names an editor, and where it was found."""
    patterns = [str(p).lower() for p in cfg.get("editor_patterns") or []]
    exceptions = [str(p).lower() for p in cfg.get("capture_exceptions") or []]
    for source, value in candidates:
        low = str(value or "").lower()
        if not low or any(exc in low for exc in exceptions):
            continue
        if any(p in low for p in patterns):
            return str(value)[:200], source
    return None, None


def _acroform(reader: Any, file_length: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    signatures: Dict[str, Any] = {"signed": False, "intact": None, "count": 0, "items": []}
    forms: Dict[str, Any] = {"fields": 0, "filled": 0, "kinds": [], "needAppearances": False}
    try:
        root = reader.trailer["/Root"]
        acro = root.get("/AcroForm")
        acro = acro.get_object() if acro is not None else None
    except Exception as e:
        signatures["error"] = forms["error"] = str(e)[:100]
        return signatures, forms
    if acro is None:
        return signatures, forms
    try:
        forms["needAppearances"] = bool(acro.get("/NeedAppearances", False))
        terminals: List[Tuple[Any, str]] = []
        _flatten_fields(acro.get("/Fields") or [], None, terminals, 0)
        kinds: List[str] = []
        for field, ft in terminals:
            if ft == "/Sig":
                item = _signature_item(field, file_length)
                signatures["items"].append(item)
                continue
            forms["fields"] += 1
            value = field.get("/V")
            if value is not None and str(value).strip() not in ("", "/Off"):
                forms["filled"] += 1
            if ft and ft not in kinds:
                kinds.append(ft)
        forms["kinds"] = kinds
        signed = [i for i in signatures["items"] if i.get("byteRange")]
        signatures["count"] = len(signed)
        signatures["signed"] = bool(signed)
        if signed:
            ends = [i["byteRange"][2] + i["byteRange"][3] for i in signed]
            signatures["latestEnd"] = max(ends)
            signatures["intact"] = max(ends) == file_length and all(i["byteRange"][0] == 0 for i in signed)
    except Exception as e:
        signatures["error"] = forms["error"] = str(e)[:100]
    return signatures, forms


def _flatten_fields(fields: Any, inherited_ft: Optional[str], out: List[Tuple[Any, str]], depth: int) -> None:
    if depth > 8:
        return
    try:
        fields = fields.get_object() if hasattr(fields, "get_object") else fields
    except Exception:
        return
    for ref in fields or []:
        try:
            field = ref.get_object()
        except Exception:
            continue
        ft = str(field.get("/FT")) if field.get("/FT") is not None else inherited_ft
        kids = field.get("/Kids")
        if kids:
            _flatten_fields(kids, ft, out, depth + 1)
        else:
            out.append((field, ft or ""))


def _signature_item(field: Any, file_length: int) -> Dict[str, Any]:
    item: Dict[str, Any] = {"name": str(field.get("/T") or "")[:80], "byteRange": None}
    value = field.get("/V")
    if value is None:
        return item
    try:
        sig = value.get_object()
        br = [int(x) for x in (sig.get("/ByteRange") or [])]
        if len(br) == 4:
            item["byteRange"] = br
            item["coversFile"] = br[0] == 0 and br[2] + br[3] == file_length
        for key, label in (("/SubFilter", "subFilter"), ("/Filter", "filter"), ("/M", "date"),
                           ("/Name", "signer"), ("/Type", "type"), ("/Reason", "reason")):
            if sig.get(key) is not None:
                item[label] = str(sig.get(key))[:120]
    except Exception as e:
        item["error"] = str(e)[:100]
    return item


def _active_content(reader: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"kinds": []}
    try:
        root = reader.trailer["/Root"]
        if root.get("/OpenAction") is not None:
            out["kinds"].append("OpenAction")
        if root.get("/AA") is not None:
            out["kinds"].append("AA")
        names = root.get("/Names")
        names = names.get_object() if names is not None else None
        if names is not None:
            if names.get("/JavaScript") is not None:
                out["kinds"].append("JavaScript")
            if names.get("/EmbeddedFiles") is not None:
                out["embeddedFiles"] = _embedded_file_names(names.get("/EmbeddedFiles"))
                if out["embeddedFiles"] and not cfg.get("allow_embedded_files"):
                    out["kinds"].append("EmbeddedFiles")
    except Exception as e:
        out["error"] = str(e)[:100]
    return out


def _optional_content(reader: Any) -> Dict[str, Any]:
    """The file's optional-content groups (layers) and the ones its default
    configuration switches OFF: content a viewer does not show."""
    out: Dict[str, Any] = {"groups": [], "hidden": []}
    try:
        props = reader.trailer["/Root"].get("/OCProperties")
        props = props.get_object() if props is not None else None
        if props is None:
            return out

        def names(refs: Any) -> List[str]:
            found = []
            refs = refs.get_object() if hasattr(refs, "get_object") else refs
            for ref in refs or []:
                try:
                    found.append(str(ref.get_object().get("/Name", "") or "?")[:80])
                except Exception:
                    continue
            return found[:40]

        out["groups"] = names(props.get("/OCGs"))
        default = props.get("/D")
        default = default.get_object() if default is not None else None
        if default is not None:
            out["hidden"] = names(default.get("/OFF"))
            if str(default.get("/BaseState", "/ON")) == "/OFF":
                shown = set(names(default.get("/ON")))
                out["hidden"] = [n for n in out["groups"] if n not in shown]
    except Exception as e:
        out["error"] = str(e)[:100]
    return out


def tool_phrases(reader: Any, page_index: int, policy: Optional[Dict[str, Any]] = None) -> List[str]:
    """The `watermark_phrases` found in a page's own text (pypdf, no OCR): the
    line a trial editor or a scanning app prints on its output. Never raises."""
    phrases = [str(p).lower() for p in policy_for(policy).get("watermark_phrases") or []]
    if not phrases:
        return []
    try:
        text = (reader.pages[page_index].extract_text() or "").lower()
    except Exception:
        return []
    return [p for p in phrases if p in text][:10]


def _embedded_file_names(tree: Any) -> List[str]:
    names: List[str] = []
    try:
        tree = tree.get_object()
        entries = tree.get("/Names")
        if entries:
            entries = entries.get_object()
            names.extend(str(entries[i])[:80] for i in range(0, len(entries), 2))
        for kid in tree.get("/Kids") or []:
            names.extend(_embedded_file_names(kid))
    except Exception:
        pass
    return names[:20]


# ------------------------------------------------------------------ page report


def page_report(structure_page: Dict[str, Any], policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The integrity facts of one page, from `pdf_forensics.inspect_page`'s
    report (which already painted the annotation appearances and computed
    the paint-order facts). Pure."""
    cfg = policy_for(policy)
    ignored = {str(s) for s in cfg.get("ignored_annotations") or []}
    active = {str(s) for s in cfg.get("active_actions") or []}
    out: Dict[str, Any] = {"page": structure_page.get("page")}
    if structure_page.get("error"):
        out["error"] = structure_page["error"]
        return out
    flagged = []
    actions = []
    for annot in structure_page.get("annotations") or []:
        subtype = annot.get("subtype") or "?"
        if annot.get("action") in active:
            actions.append(annot["action"])
        if subtype in ignored or annot.get("error"):
            continue
        flagged.append({k: annot.get(k) for k in ("subtype", "rect", "hidden", "hasImage", "contents") if k in annot})
    out["annotations"] = flagged[:20]
    out["annotationSubtypes"] = sorted({a["subtype"] for a in flagged})
    out["hiddenAnnotations"] = sorted({a["subtype"] for a in flagged if a.get("hidden")})
    out["actions"] = actions
    overlay = structure_page.get("overlay") or {}
    chars = overlay.get("textAfterFullPageImageChars")
    out["textAfterFullPageImageChars"] = chars
    if chars is None or chars >= int(cfg.get("max_overlay_chars") or 0):
        out["noTextOverlay"] = None
    else:
        out["noTextOverlay"] = chars < int(cfg.get("min_overlay_chars") or 0)
    over = overlay.get("imagesOverText")
    out["imagesOverText"] = over
    out["noImageOverText"] = None if over is None else not over

    shapes = overlay.get("shapesOverText")
    if shapes is not None:
        shapes = [s for s in shapes if int(s.get("coveredChars") or 0) >= int(cfg.get("shape_over_text_min_chars") or 0)]
    out["shapesOverText"] = shapes
    out["noShapeOverText"] = None if shapes is None else not shapes

    visible = int(structure_page.get("visibleTextChars") or 0)
    out["size"] = structure_page.get("size")
    # The page IS a scan (one full-page image, no text of its own) or was
    # born digital; anything in between says nothing about the page mix.
    out["kind"] = ("ESCANEO" if overlay.get("fullPageImages") and visible < 40
                   else "NATIVA" if visible >= int(cfg.get("foreign_font_min_page_chars") or 0) else None)

    mark = overlay.get("watermark")
    if mark is None:
        out["watermarkKinds"], out["noWatermark"] = [], None
    else:
        min_size = float(cfg.get("watermark_min_font_size") or 0)
        max_alpha = float(cfg.get("watermark_max_alpha") or 0)
        kinds = list(mark.get("tags") or [])
        if any(a.get("subtype") == "/Watermark" for a in flagged):
            kinds.append("ANOTACION_MARCA_DE_AGUA")
        if any(float(r.get("size") or 0) >= min_size or float(r.get("alpha") or 1) < 0.9
               for r in mark.get("diagonalRuns") or []):
            kinds.append("TEXTO_DIAGONAL")
        if any(float(r.get("alpha") or 1) <= max_alpha and float(r.get("size") or 0) >= min_size
               for r in mark.get("translucentRuns") or []):
            kinds.append("TEXTO_TRANSLUCIDO")
        if any(float(i.get("alpha") or 1) <= max_alpha
               and float(i.get("coverage") or 0) >= float(cfg.get("watermark_min_image_coverage") or 0)
               for i in mark.get("translucentImages") or []):
            kinds.append("IMAGEN_TRANSLUCIDA")
        phrases = list(structure_page.get("toolPhrases") or [])
        if phrases:
            kinds.append("LEYENDA_DE_HERRAMIENTA")
            out["toolPhrases"] = phrases
        out["watermarkKinds"] = kinds
        # A watermark burnt into a scan's pixels cannot be read off the file.
        out["noWatermark"] = None if (not kinds and out["kind"] == "ESCANEO") else not kinds

    usage = overlay.get("fontUsage")
    out["foreignFonts"], out["fontsConsistent"] = [], None
    if usage is not None and visible >= int(cfg.get("foreign_font_min_page_chars") or 0):
        total = sum(int(f.get("chars") or 0) for f in usage) or 1
        embedded = sum(int(f.get("chars") or 0) for f in usage if f.get("embedded") is True)
        if embedded / total >= float(cfg.get("foreign_font_min_embedded_share") or 0):
            out["foreignFonts"] = [
                {"font": f.get("font"), "chars": f.get("chars")} for f in usage
                if f.get("embedded") is False and 0 < int(f.get("chars") or 0) <= int(cfg.get("foreign_font_max_chars") or 0)
            ]
            out["fontsConsistent"] = not out["foreignFonts"]
    return out


def _page_mix(pages: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """Are the document's pages one kind of thing? `(consistent, detail)`;
    None with fewer than two classifiable pages. Scans are not compared by
    size: a phone crops every sheet differently."""
    kinds = [p.get("kind") for p in pages if p.get("kind")]
    if len(kinds) < 2:
        return None, ""
    if len(set(kinds)) > 1:
        scans = [str(p.get("page")) for p in pages if p.get("kind") == "ESCANEO"]
        return False, f"página(s) {', '.join(scans)} escaneada(s) entre páginas digitales de origen"
    tol = float(cfg.get("page_size_tolerance") or 0)
    native = [p for p in pages if p.get("kind") == "NATIVA" and p.get("size")]
    if len(native) >= 2:
        base = sorted(native[0]["size"])
        for page in native[1:]:
            size = sorted(page["size"])
            if any(abs(a - b) > tol * max(b, 1.0) for a, b in zip(size, base)):
                return False, f"la página {page.get('page')} mide {page['size'][0]:.0f}x{page['size'][1]:.0f} pt y la primera {native[0]['size'][0]:.0f}x{native[0]['size'][1]:.0f} pt"
    return True, ""


# ------------------------------------------------------------------ image report


def image_report(raw: bytes, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What a JPEG/PNG says about the software that wrote it and when: the
    upload itself, or the JPEG a scan embeds. Never raises."""
    cfg = policy_for(policy)
    out: Dict[str, Any] = {
        "format": None, "software": None, "photoshopApp13": False, "pngText": {}, "xmpAgents": [],
        "exif": {}, "editor": None, "editorSource": None, "cameraMetadata": None, "datesConsistent": None,
    }
    if not raw:
        out["error"] = "empty"
        return out
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            out["format"] = img.format
            out["width"], out["height"] = img.size
            info = getattr(img, "info", {}) or {}
            out["photoshopApp13"] = "photoshop" in info
            text = getattr(img, "text", None) or {}
            out["pngText"] = {str(k)[:40]: str(v)[:120] for k, v in text.items()
                              if str(k).lower() in ("software", "comment", "creation time", "source")}
            xmp = info.get("xmp") or info.get("XML:com.adobe.xmp") or b""
            if isinstance(xmp, str):
                xmp = xmp.encode("utf-8", "replace")
            if xmp:
                agents = []
                for m in _HISTORY_AGENT.finditer(xmp):
                    agent = (m.group(1) or m.group(2) or b"").decode("utf-8", "replace").strip()
                    if agent and agent not in agents:
                        agents.append(agent[:200])
                out["xmpAgents"] = agents[:20]
                m = re.search(rb'xmp:CreatorTool\s*=\s*"([^"]*)"|<xmp:CreatorTool>([^<]*)</xmp:CreatorTool>', xmp)
                if m:
                    out["xmpCreatorTool"] = (m.group(1) or m.group(2) or b"").decode("utf-8", "replace")[:200]
            try:
                exif = img.getexif()
            except Exception:
                exif = None
            if exif:
                out["software"] = _exif_str(exif.get(0x0131))
                out["exif"] = {
                    "make": _exif_str(exif.get(0x010F)), "model": _exif_str(exif.get(0x0110)),
                    "dateTime": _exif_str(exif.get(0x0132)),
                }
                try:
                    ifd = exif.get_ifd(0x8769)
                    out["exif"]["dateTimeOriginal"] = _exif_str(ifd.get(0x9003))
                    out["exif"]["dateTimeDigitized"] = _exif_str(ifd.get(0x9004))
                except Exception:
                    pass
    except Exception as e:
        out["error"] = str(e)[:120]
        return out
    if not out["photoshopApp13"] and raw[:2] == b"\xff\xd8" and b"Photoshop 3.0\x00" in raw[:65536]:
        out["photoshopApp13"] = True

    candidates: List[Tuple[str, str]] = []
    if out["software"]:
        candidates.append(("exifSoftware", out["software"]))
    if out["pngText"].get("Software") or out["pngText"].get("software"):
        candidates.append(("pngSoftware", out["pngText"].get("Software") or out["pngText"].get("software")))
    if out.get("xmpCreatorTool"):
        candidates.append(("xmpCreatorTool", out["xmpCreatorTool"]))
    candidates.extend(("history", a) for a in out["xmpAgents"])
    out["editor"], out["editorSource"] = find_editor(candidates, cfg)
    if out["editor"] is None and out["photoshopApp13"]:
        out["editor"], out["editorSource"] = "Adobe Photoshop (segmento APP13)", "app13"

    exif = out["exif"]
    out["cameraMetadata"] = bool(exif.get("make") or exif.get("model") or exif.get("dateTimeOriginal"))
    modified, original = _exif_date(exif.get("dateTime")), _exif_date(exif.get("dateTimeOriginal"))
    if modified and original:
        tol = float(cfg.get("date_tolerance_s") or 0)
        out["datesConsistent"] = (modified - original).total_seconds() <= tol
        out["modifiedAfterCaptureSeconds"] = round((modified - original).total_seconds())
    return out


def _exif_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).strip("\x00 ").strip()
    return text[:120] or None


def _exif_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return _pdf_date(value)


# ------------------------------------------------------------------ signals


def signals_from(
    file_rep: Optional[Dict[str, Any]],
    page_reps: Sequence[Dict[str, Any]],
    image_rep: Optional[Dict[str, Any]],
    policy: Optional[Dict[str, Any]] = None,
    *, is_image: bool = False,
) -> Tuple[Dict[str, Any], List[Tuple[str, str, str]]]:
    """Turn the reports into the catalog signals and the reasons to record:
    `(signals, [(code, severity, message), ...])`. Pure."""
    cfg = policy_for(policy)
    signals: Dict[str, Any] = {name: None for name in SIGNALS}
    reasons: List[Tuple[str, str, str]] = []

    def say(code: str, **fmt: Any) -> None:
        severity = str((cfg.get("severity") or {}).get(code) or SEVERITY.get(code, "soft"))
        try:
            message = MESSAGES[code].format(**fmt)
        except (KeyError, IndexError):
            message = MESSAGES[code]
        reasons.append((code, severity, message))

    if file_rep:
        chain = file_rep.get("chain") or {}
        sigs = file_rep.get("signatures") or {}
        generations = chain.get("generations")
        signed = bool(sigs.get("signed"))
        signals["integrity.generations"] = generations
        signals["integrity.digitally_signed"] = signed
        if signed:
            signals["integrity.signature_intact"] = sigs.get("intact")
            say(R_SIGNED, n=", ".join(filter(None, (i.get("signer") or i.get("name") for i in sigs.get("items") or []))) or f"{sigs.get('count')} firma(s)")
            if sigs.get("intact") is False:
                say(R_MODIFIED_AFTER_SIGNING)
                signals["integrity.single_generation"] = False
        if signals["integrity.single_generation"] is None and generations is not None:
            extra = int(generations) - 1 - (int(sigs.get("count") or 0) if signed else 0)
            if chain.get("generatorTwoPass") and extra == 1:
                extra = 0
            signals["integrity.single_generation"] = extra <= 0
            if extra > 0:
                say(R_INCREMENTAL, n=extra)

        dates = file_rep.get("dates") or {}
        signals["integrity.dates_consistent"] = dates.get("consistent")
        if dates.get("consistent") is False:
            say(R_DATES_INCONSISTENT, detail="; ".join(dates.get("details") or []) or "ver detalle")
        elif dates.get("modifiedAfterCreation"):
            say(R_MODIFIED_AFTER_CREATION, delta=_humanize(dates.get("modifiedAfterCreationSeconds") or 0))

        software = file_rep.get("software") or {}
        signals["integrity.producer_consistent"] = software.get("consistent")
        signals["integrity.editor"] = software.get("editor")
        if software.get("consistent") is False:
            say(R_EDITOR, editor=software.get("editor") or software.get("mismatch") or "desconocido")

        forms = file_rep.get("forms") or {}
        if "error" not in forms:
            signals["integrity.no_form_fields"] = int(forms.get("fields") or 0) == 0
            if int(forms.get("fields") or 0) > 0:
                say(R_FORM_FIELDS, n=forms.get("fields"), filled=forms.get("filled"))

        active = file_rep.get("activeContent") or {}
        kinds = list(active.get("kinds") or [])
        for page in page_reps:
            for action in page.get("actions") or []:
                if action not in kinds:
                    kinds.append(action)
        if "error" not in active:
            signals["integrity.no_active_content"] = not kinds
            if kinds:
                say(R_ACTIVE_CONTENT, kinds=", ".join(str(k) for k in kinds))

        layers = file_rep.get("optionalContent") or {}
        hidden = [f"capa oculta «{name}»" for name in layers.get("hidden") or []]
        for page in page_reps:
            hidden.extend(f"anotación oculta {s}" for s in page.get("hiddenAnnotations") or [])
        if "error" not in layers:
            signals["integrity.no_hidden_content"] = not hidden
            if hidden:
                say(R_HIDDEN_CONTENT, kinds=", ".join(dict.fromkeys(hidden)))

    usable_pages = [p for p in page_reps if not p.get("error")]
    if usable_pages:
        subtypes: List[str] = []
        for page in usable_pages:
            for s in page.get("annotationSubtypes") or []:
                if s not in subtypes:
                    subtypes.append(s)
        signals["integrity.no_annotations"] = not subtypes
        if subtypes:
            say(R_ANNOTATIONS, kinds=", ".join(subtypes))
        signals["integrity.no_text_overlay"] = _aggregate([p.get("noTextOverlay") for p in usable_pages])
        if signals["integrity.no_text_overlay"] is False:
            say(R_TEXT_OVER_SCAN, n=sum(int(p.get("textAfterFullPageImageChars") or 0) for p in usable_pages
                                         if p.get("noTextOverlay") is False))
        signals["integrity.no_image_over_text"] = _aggregate([p.get("noImageOverText") for p in usable_pages])
        if signals["integrity.no_image_over_text"] is False:
            say(R_IMAGE_OVER_TEXT, n=sum(len(p.get("imagesOverText") or []) for p in usable_pages))

        signals["integrity.no_shape_over_text"] = _aggregate([p.get("noShapeOverText") for p in usable_pages])
        if signals["integrity.no_shape_over_text"] is False:
            shapes = [s for p in usable_pages for s in p.get("shapesOverText") or []]
            say(R_SHAPE_OVER_TEXT, n=len(shapes), colours=", ".join(dict.fromkeys(str(s.get("colour")) for s in shapes)))

        signals["integrity.no_watermark"] = _aggregate([p.get("noWatermark") for p in usable_pages])
        if signals["integrity.no_watermark"] is False:
            kinds = list(dict.fromkeys(k for p in usable_pages for k in p.get("watermarkKinds") or []))
            phrases = list(dict.fromkeys(ph for p in usable_pages for ph in p.get("toolPhrases") or []))
            say(R_WATERMARK, kinds=", ".join(kinds) + (f": «{'», «'.join(phrases)}»" if phrases else ""))

        signals["integrity.fonts_consistent"] = _aggregate([p.get("fontsConsistent") for p in usable_pages])
        if signals["integrity.fonts_consistent"] is False:
            fonts = [f for p in usable_pages for f in p.get("foreignFonts") or []]
            say(R_FOREIGN_FONT, n=sum(int(f.get("chars") or 0) for f in fonts),
                fonts=", ".join(dict.fromkeys(str(f.get("font")) for f in fonts)))

        consistent, detail = _page_mix(usable_pages, cfg)
        signals["integrity.pages_consistent"] = consistent
        if consistent is False:
            say(R_PAGES_MIXED, detail=detail)

    if image_rep and not image_rep.get("error"):
        if image_rep.get("editor"):
            signals["integrity.producer_consistent"] = False
            signals["integrity.editor"] = image_rep["editor"]
            say(R_IMAGE_EDITOR, editor=image_rep["editor"])
        elif is_image:
            signals["integrity.producer_consistent"] = True
        if image_rep.get("datesConsistent") is not None:
            if signals["integrity.dates_consistent"] is None or image_rep["datesConsistent"] is False:
                signals["integrity.dates_consistent"] = image_rep["datesConsistent"]
            if image_rep["datesConsistent"] is False:
                say(R_EXIF_DATES, detail=_humanize(image_rep.get("modifiedAfterCaptureSeconds") or 0))
        if is_image:
            signals["integrity.camera_metadata"] = bool(image_rep.get("cameraMetadata"))
            if not image_rep.get("cameraMetadata"):
                say(R_NO_CAMERA)

    return signals, reasons


def _aggregate(values: Sequence[Optional[bool]]) -> Optional[bool]:
    if any(v is False for v in values):
        return False
    if any(v is True for v in values):
        return True
    return None


def _humanize(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} día(s)"
