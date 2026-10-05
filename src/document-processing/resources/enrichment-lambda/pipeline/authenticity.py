"""Physical-authenticity stage: is this upload a genuine capture of a paper
document — printed, wet-signed, stamped, scanned — or a digital composition
with a pasted signature/stamp?

Generic and catalog-driven, modelled on `qr_recovery`: a type opts in by
declaring a top-level `authenticity` block, and receives the results on
ordinary `computed` fields that each name what they want through a per-field
`authenticity_source`. Those fields are then graded by the normal rule engine
(`boolean_true` + `on_fail`), so WHAT a finding does to the document — reject,
review, just inform — is catalog config, not code. Nothing here knows any
TYPE_ID, field name or element name.

Five layers, each independently switchable in the block — and, more to the
point, each run ONLY when a field of the type asks for one of its signals
(`_layers_needed`). A type that wants nothing but "was this file edited?"
never rasterizes a page and never calls a model; a layer nobody reads is
reported `NO_SOLICITADA` and does not count against `evaluated`:

  0. integrity  `pdf_integrity`      Was the FILE touched after it was made?
                                     Incremental saves, dates, editors, annotations,
                                     forms, text over a scan, images over text,
                                     signature coverage, active content, EXIF.
                                     Deterministic, free, reads the raw bytes.
  1. structure  `pdf_forensics`      PDF internals: capture of a paper or not.
  2. pixels     `pixel_forensics`    Paper/sensor noise; error-level analysis.
  3. vision     `vision_inspector`   A multimodal model looks at the page.
  4. memory     `fingerprint_store`  Reuse across letters + registered references.

Signals a field can ask for (`authenticity_source`):

  evaluated                          every REQUESTED layer ran (bool)
  verdict | reasons | capture_kind   summary strings (capture_kind needs vision)
  integrity.single_generation        True = written once; False = saved over itself / after signing
  integrity.generations              how many times the file was written (int, info)
  integrity.dates_consistent         Info vs XMP dates agree, modification not before creation
  integrity.producer_consistent      no PDF/image editor named in the metadata
  integrity.editor                   the editor string found (info)
  integrity.no_annotations           no annotation over the content (links/popups/widgets ignored)
  integrity.no_form_fields           no AcroForm fields (signature fields excluded)
  integrity.no_text_overlay          no visible text typed AFTER the full-page scan image
  integrity.no_image_over_text       no image painted OVER visible text
  integrity.digitally_signed         info
  integrity.signature_intact         the latest signature reaches the end of the file / EMPTY if unsigned
  integrity.no_active_content        no JavaScript, OpenAction, attachments (configurable)
  integrity.camera_metadata          image uploads: EXIF camera data present (info)
  structure.physical_capture         True / False / (empty = undeterminable)
  structure.origin                   ESCANER | APP_CAPTURA | OFIMATICA | DESCONOCIDO | IMAGEN
  structure.capture_as_expected      matches the block's `expected_capture` / empty when ANY
  pixels.natural_background          bool / empty
  pixels.error_level_uniform         JPEG error-level analysis / empty when not a JPEG
  element.<KIND>.present             bool / empty (no vision)
  element.<KIND>.not_pasted          bool / empty
  element.<KIND>.text                the stamp's legend
  element.<KIND>.ink_colour          NEGRA | AZUL | OTRO_COLOR | NO_DETERMINABLE
  element.<KIND>.issuer_consistent   bool / empty
  element.<KIND>.matches_reference   bool / EMPTY when the supplier has none registered
  vision.no_edit_signs               `vision.page_review`: the model sees no edit signs on the page
  memory.elements_unique             False = same ink as ANOTHER letter (reject-level)
  memory.elements_not_similar        False = suspiciously alike (review-level or worse)
  memory.not_duplicate               False = this very letter was already received

EMPTY is a first-class answer — "could not be determined" — and the catalog
pairs these fields with `allow_empty: true` so it never counts as a "no". The
one exception is `evaluated`: a stage that could not run must send the document
to a person, never approve it silently. That includes the kill-switch
(`AUTHENTICITY_ENABLED=false`): switching the fraud check off does not turn
unchecked letters into approved ones.

The `authenticity` block itself is optional: fields carrying an
`authenticity_source` are what opt a type in (typically through the shared
`$presets` in `_shared.json`); the block only tunes thresholds and elements.

Known limit, stated plainly: someone who prints the composed letter and
re-scans it defeats layers 1 and 2 (the file IS then a genuine capture). Only
the vision judgement and cross-letter ink reuse can catch that.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from . import fingerprint_store, page_raster, pdf_forensics, pdf_integrity, pixel_forensics
from .pricing import merge_bedrock_usage

logger = logging.getLogger(__name__)

SOURCE_LABEL = "AUTHENTICITY"

VERDICT_AUTHENTIC = "AUTENTICO"
VERDICT_SUSPICIOUS = "SOSPECHOSO"
# The verdict describes the FINDING, not the document's status: what a finding
# does to the document is the catalog's call (`on_fail`), and a type running in
# observation mode reports NO_AUTENTICO on a document that only goes to review.
VERDICT_REJECTED = "NO_AUTENTICO"
VERDICT_NOT_EVALUATED = "NO_EVALUADO"

LAYER_OK, LAYER_FAIL, LAYER_NA, LAYER_ERROR, LAYER_OFF = "OK", "FALLA", "NO_APLICA", "ERROR", "DESACTIVADA"
# No field of the type asked for anything this layer produces: it did not run,
# costs nothing, and says nothing about `evaluated`.
LAYER_NOT_REQUESTED = "NO_SOLICITADA"
ALL_LAYERS = ("integrity", "structure", "pixels", "vision", "memory")

# Reasons that describe a circumstance, not a finding: they never make a
# document SOSPECHOSO on their own.
INFORMATIONAL_CODES = {
    "SIN_REFERENCIAS_REGISTRADAS", "SIN_CLAVE_DE_REFERENCIAS", "PAGINAS_SIN_ANALIZAR",
    "CARTA_YA_RECIBIDA", "VISION_OMITIDA_SIN_SOSPECHA", pdf_forensics.REASON_OFFICE_ORIGIN,
    *(code for code, severity in pdf_integrity.SEVERITY.items() if severity == "info"),
}

EXPECTED_CAPTURE_VALUES = ("FISICO", "DIGITAL", "ANY")

_DETERMINISTIC_CONFIDENCE = 0.99
_VISION_CONFIDENCE = 0.95
_MAX_STRUCTURE_PAGES = 10
_CROP_PAD = 0.12
_MAX_PASTED_COVERAGE = 0.35       # above this an image is "a scan pasted in", not a pasted element
_MAX_EMBEDDED_CANDIDATES = 8      # cost guardrail: the largest N are classified, the rest ignored
_MAX_INK_CANDIDATES = 12          # same, for ink regions proposed on a scan
_CANDIDATE_MAX_EDGE = 700         # classifying needs far fewer pixels than examining
_CROP_MAX_EDGE = 1200


def enabled() -> bool:
    return os.environ.get("AUTHENTICITY_ENABLED", "true").strip().lower() not in ("0", "false", "no")


@dataclass
class PageSource:
    """The upload as the stage needs it: an open pypdf reader for a PDF, or
    the raw bytes of a JPG/PNG. `file_sha256` identifies the FILE, so the same
    upload processed twice is recognized as itself.

    `raw_bytes`/`raw_reader` are the upload AS RECEIVED — before
    `expediente._truncate_pdf` rewrote it to the page cap. Rewriting with
    `PdfWriter` erases exactly the evidence the integrity layer reads
    (incremental updates, the Info dictionary, signatures), so that layer
    always works from the original; `reader` (the truncated one) stays what
    the other layers rasterize from. Truncation keeps a PREFIX of the pages,
    so a document's page range indexes both readers the same way.
    """

    reader: Any = None
    image_bytes: Optional[bytes] = None
    file_sha256: str = ""
    raw_bytes: Optional[bytes] = None
    raw_reader: Any = None
    _file_report: Optional[Dict[str, Any]] = field(default=None, repr=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    @property
    def is_image(self) -> bool:
        return self.reader is None and self.image_bytes is not None

    def file_report(self, policy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The file-level integrity report, computed once per upload and
        shared by every document in it (they run concurrently)."""
        if self.is_image or self.raw_reader is None:
            return None
        with self._lock:
            if self._file_report is None:
                self._file_report = pdf_integrity.file_report(self.raw_bytes or b"", self.raw_reader, policy)
            return self._file_report


def source_for(file_bytes: bytes, reader: Any, is_image: bool, raw_bytes: Optional[bytes] = None) -> PageSource:
    original = raw_bytes if raw_bytes is not None else file_bytes
    raw_reader = None
    if not is_image:
        if original is file_bytes or original == file_bytes:
            raw_reader = reader
        else:
            try:
                from pypdf import PdfReader

                raw_reader = PdfReader(io.BytesIO(original))
            except Exception as e:  # the integrity layer will report ERROR, the upload goes on
                logger.warning("authenticity: could not open the original PDF: %s", e)
                raw_reader = None
    return PageSource(
        reader=None if is_image else reader,
        image_bytes=file_bytes if is_image else None,
        file_sha256=hashlib.sha256(original or b"").hexdigest(),
        raw_bytes=original,
        raw_reader=raw_reader,
    )


# ------------------------------------------------------------------ spec


def spec_for(entry: Any) -> Optional[Dict[str, Any]]:
    """The type's `authenticity` block plus the fields that consume it, or
    None when the type doesn't take part (`enabled: false`, or no field asks
    for anything — a stage nobody reads is not worth its cost). The block is
    optional: a type whose fields carry `authenticity_source` runs with the
    defaults. `layers` is the set of layers those fields actually need."""
    block = getattr(entry, "authenticity", None)
    if block is False or (isinstance(block, dict) and block.get("enabled") is False):
        return None
    block = block if isinstance(block, dict) else {}
    outputs: Dict[str, str] = {}
    for name, field_spec in (getattr(entry, "field_tree", None) or {}).items():
        source = (getattr(field_spec, "params", None) or {}).get("authenticity_source")
        if isinstance(source, str) and source.strip():
            outputs[name] = source.strip()
    if not outputs:
        return None
    block = _plain(block)
    elements = {
        str(kind): dict(spec or {})
        for kind, spec in (block.get("elements") or {}).items()
    }
    wanted = set(outputs.values())
    # A type that reads `structure.physical_capture` is, by that very fact,
    # expecting the capture of a paper; every other type expects nothing
    # unless it says so.
    default_expected = "FISICO" if "structure.physical_capture" in wanted else "ANY"
    expected = str(block.get("expected_capture") or default_expected).upper()
    if expected not in EXPECTED_CAPTURE_VALUES:
        expected = default_expected
    return {
        **block, "elements": elements, "outputs": outputs, "wanted": wanted,
        "layers": _layers_needed(wanted, elements, block), "expected_capture": expected,
    }


def _layers_needed(wanted: Set[str], elements: Dict[str, Any], block: Optional[Dict[str, Any]] = None) -> Set[str]:
    """Which layers the requested signals need. Pure. The rule that keeps the
    stage cheap: nothing runs because it is configured, only because a field
    will read it.

      integrity.*                 -> integrity (raw bytes; never a raster)
      structure.*                 -> structure
      pixels.natural_background   -> structure + pixels (a raster)
      pixels.error_level_uniform  -> structure + pixels (the file's JPEG bytes, no raster)
      element.* | capture_kind |
      vision.*                    -> vision + structure + pixels (candidates come from
                                     the page report and the ink regions of the raster)
      memory.*                    -> memory (+ vision when there are elements to fingerprint)
      evaluated|verdict|reasons   -> nothing on their own
    """
    needed: Set[str] = set()
    block = block or {}
    for signal in wanted:
        head = signal.split(".", 1)[0]
        if head == "integrity":
            needed.add("integrity")
        elif head == "structure":
            needed.add("structure")
        elif head == "pixels":
            needed.update({"structure", "pixels"})
        elif head in ("element", "vision") or signal == "capture_kind":
            needed.update({"vision", "structure", "pixels"})
        elif head == "memory":
            needed.add("memory")
            if elements:
                needed.update({"vision", "structure", "pixels"})
    return needed


def layer_requested(spec: Dict[str, Any], name: str) -> bool:
    return name in (spec.get("layers") or set())


def _plain(value: Any) -> Any:
    """The block as plain JSON types. A catalog hot-reloaded from DynamoDB
    arrives with every number as `Decimal` (the bundled JSON one does not), and
    a Decimal threshold meeting a float measurement is a TypeError waiting for
    production — so it is normalized once, here, not at each comparison."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def applies_to(entry: Any) -> bool:
    return spec_for(entry) is not None


# ------------------------------------------------------------------ the stage


def run(doc: Dict[str, Any], entry: Any, source: PageSource, context: Optional[Dict[str, Any]] = None) -> None:
    """Evaluate one extracted document and write the results onto it:
    computed field values into `_entities`/`_confidences`, the audit block
    into `doc["authenticity"]`, billed model usage into `authenticityMeta`.
    Never raises — a failure is itself a result (`evaluated = False`)."""
    spec = spec_for(entry)
    if spec is None:
        return
    started = time.time()
    context = context or {}
    audit: Dict[str, Any] = {"layers": {}, "reasons": []}
    signals: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}

    if not enabled():
        audit["layers"] = {name: LAYER_OFF if name in spec["layers"] else LAYER_NOT_REQUESTED for name in ALL_LAYERS}
        _reason(audit, "ETAPA_DESACTIVADA", "kill-switch", False,
                "La verificación de autenticidad está desactivada (AUTHENTICITY_ENABLED=false)")
        signals["evaluated"] = False
    else:
        try:
            _evaluate(doc, entry, spec, source, context, audit, signals, confidences)
        except Exception as e:  # a bug here must cost a review, never the document
            logger.exception("authenticity: stage failed")
            audit["error"] = str(e)[:300]
            signals["evaluated"] = False

    audit["verdict"] = _verdict(audit, signals)
    signals["verdict"] = audit["verdict"]
    signals["reasons"] = ",".join(r["code"] for r in audit["reasons"]) or None
    audit["timingMs"] = round((time.time() - started) * 1000, 1)
    doc["authenticity"] = audit
    _write_fields(doc, spec["outputs"], signals, confidences)


def _evaluate(
    doc: Dict[str, Any], entry: Any, spec: Dict[str, Any], source: PageSource,
    context: Dict[str, Any], audit: Dict[str, Any], signals: Dict[str, Any],
    confidences: Dict[str, float],
) -> None:
    layers = audit["layers"]
    elements: Dict[str, Dict[str, Any]] = spec["elements"]
    needed: Set[str] = spec["layers"]
    wanted: Set[str] = spec["wanted"]
    page_indexes = _segment_pages(doc, source)
    for name in ALL_LAYERS:
        if name not in needed:
            layers[name] = LAYER_NOT_REQUESTED

    # ---- layer 1: structure (the page reports layer 0 reads too)
    structure = _run_structure(spec, source, page_indexes, audit, signals) if "structure" in needed else None
    digital = structure is not None and structure.get("physicalCapture") is False
    content_pages = [p - 1 for p in (structure or {}).get("contentPages") or []] or page_indexes
    max_pages = max(int((spec.get("pages") or {}).get("max_pages", 2)), 1)
    analysed = content_pages[:max_pages]
    audit["pagesAnalysed"] = [i + 1 for i in analysed]

    # ---- layer 0: integrity (raw bytes, no raster, no model)
    if "integrity" in needed:
        _run_integrity(spec, source, page_indexes, structure, analysed, audit, signals)

    # ---- rasters (shared by layers 2-4) — only when something will look at pixels
    rasters: Dict[int, Any] = {}
    needs_raster = "vision" in needed or "pixels.natural_background" in wanted
    if needs_raster:
        dpi = int((spec.get("pixels") or {}).get("dpi", 150))
        for index in analysed:
            raster = _raster(source, index, dpi)
            if raster is not None:
                rasters[index] = raster

    # ---- layer 2: pixels
    if "pixels" in needed:
        _run_pixels(spec, source, structure, rasters, analysed, wanted, audit, signals)

    # ---- layer 3: vision
    page_index = next(iter(rasters), None)
    located = None
    element_state: Dict[str, Dict[str, Any]] = {kind: {} for kind in elements}
    references: Dict[str, List[Dict[str, Any]]] = {}
    if "vision" in needed:
        located = _run_vision_locate(spec, entry, elements, rasters, page_index, structure, audit, signals, doc)

        # Per-element evidence that needs no model: crops, fingerprints, and — on a
        # born-digital page — whether an embedded image sits under the element.
        element_state = _element_evidence(
            elements, located, rasters.get(page_index) if page_index is not None else None,
            _page_report(structure, page_index), audit,
        )

        references = _load_references(spec, context, element_state, audit)
        _run_vision_inspect(spec, entry, elements, element_state, references, digital, structure, audit, doc)
        _element_signals(elements, element_state, references, doc, audit, signals, confidences)
        _run_vision_page_review(spec, entry, rasters, page_index, structure, audit, signals, doc)

    # ---- layer 4: memory
    if "memory" in needed:
        _run_memory(spec, entry, doc, source, context, elements, element_state, audit, signals)

    signals["evaluated"] = all(
        layers.get(name) in (LAYER_OK, LAYER_FAIL, LAYER_NA, LAYER_OFF) for name in needed
    )
    if needs_raster and len(content_pages) > len(analysed):
        _reason(audit, "PAGINAS_SIN_ANALIZAR", "pages", False,
                f"Se analizaron {len(analysed)} de {len(content_pages)} páginas con contenido "
                "(`authenticity.pages.max_pages`)")


# ------------------------------------------------------------------ layers


def _segment_pages(doc: Dict[str, Any], source: PageSource) -> List[int]:
    if source.is_image:
        return [0]
    start = int(doc.get("pageStart") or 1)
    end = int(doc.get("pageEnd") or start)
    total = len(source.reader.pages) if source.reader is not None else end
    return [i for i in range(start - 1, min(end, total))][:_MAX_STRUCTURE_PAGES]


def _run_structure(spec, source: PageSource, page_indexes, audit, signals) -> Optional[Dict[str, Any]]:
    layers = audit["layers"]
    policy = spec.get("structure")
    if policy is False or (isinstance(policy, dict) and policy.get("enabled") is False):
        layers["structure"] = LAYER_OFF
        return None
    if source.is_image or source.reader is None:
        # A photo has no PDF internals to read: layers 2-4 carry it.
        layers["structure"] = LAYER_NA
        signals["structure.origin"] = "IMAGEN"
        return None
    try:
        structure = pdf_forensics.inspect_pdf(source.reader, page_indexes, policy if isinstance(policy, dict) else {})
    except Exception as e:
        logger.warning("authenticity: structure layer failed: %s", e)
        layers["structure"] = LAYER_ERROR
        return None
    audit["structure"] = structure
    signals["structure.origin"] = structure["origin"]
    signals["structure.physical_capture"] = structure["physicalCapture"]
    expected = spec.get("expected_capture") or "ANY"
    physical = structure["physicalCapture"]
    if expected == "FISICO":
        # The classic question: the DIGITAL reasons below already carry the
        # hard evidence; this signal only names the expectation.
        signals["structure.capture_as_expected"] = physical
        layers["structure"] = LAYER_FAIL if physical is False else LAYER_OK
        for code in structure["reasons"]:
            _reason(audit, code, "structure", code != pdf_forensics.REASON_OFFICE_ORIGIN, _STRUCTURE_MESSAGES.get(code, code))
    else:
        # A type born digital (an invoice, a pedimento) is not "suspicious"
        # for being digital: the capture facts are evidence, integrity decides.
        as_expected = None if expected == "ANY" or physical is None else (physical is False)
        signals["structure.capture_as_expected"] = as_expected
        layers["structure"] = LAYER_FAIL if as_expected is False else LAYER_OK
        if as_expected is False:
            _reason(audit, REASON_CAPTURE_UNEXPECTED, "structure", False,
                    "Se esperaba un documento digital de origen y el archivo es la captura de un papel")
        if structure["origin"] == pdf_forensics.ORIGIN_OFFICE and pdf_forensics.REASON_OFFICE_ORIGIN in structure["reasons"]:
            _reason(audit, pdf_forensics.REASON_OFFICE_ORIGIN, "structure", False,
                    _STRUCTURE_MESSAGES[pdf_forensics.REASON_OFFICE_ORIGIN])
    return structure


REASON_CAPTURE_UNEXPECTED = "CAPTURA_DISTINTA_DE_LA_ESPERADA"

_STRUCTURE_MESSAGES = {
    pdf_forensics.REASON_PASTED_IMAGE: "El PDF tiene texto nativo y una imagen insertada: no es la captura de un papel",
    pdf_forensics.REASON_NO_CAPTURE: "El PDF es un documento digital, no la captura de un papel",
    pdf_forensics.REASON_TRANSPARENCY: "Una imagen insertada tiene transparencia (recorte pegado)",
    pdf_forensics.REASON_OFFICE_ORIGIN: "El archivo fue generado por una suite de oficina",
}


def _run_integrity(spec, source: PageSource, page_indexes, structure, analysed, audit, signals) -> None:
    """Layer 0: file-level facts once per upload (cached on the source),
    page-level facts for THIS document's pages only, and the metadata of the
    image the upload is — or the JPEG each analysed scan page embeds."""
    layers = audit["layers"]
    policy = spec.get("integrity")
    if policy is False or (isinstance(policy, dict) and policy.get("enabled") is False):
        layers["integrity"] = LAYER_OFF
        return
    policy = pdf_integrity.policy_for(policy if isinstance(policy, dict) else {})
    try:
        file_rep = source.file_report(policy)
        page_reps: List[Dict[str, Any]] = []
        image_rep: Optional[Dict[str, Any]] = None
        if source.is_image:
            image_rep = pdf_integrity.image_report(source.image_bytes or b"", policy)
        else:
            if source.raw_reader is None:
                raise RuntimeError("original PDF unreadable")
            if page_indexes and max(page_indexes) >= len(source.raw_reader.pages):
                raise RuntimeError("document page range beyond the original file")
            structure_pages = {p.get("page"): p for p in (structure or {}).get("perPage") or []}
            for index in page_indexes:
                facts = structure_pages.get(index + 1)
                if facts is None or "overlay" not in facts:
                    # Structure was not requested (or ran on other pages):
                    # walk the page here — pypdf only, still free.
                    facts = pdf_forensics.inspect_page(source.raw_reader, index, policy)
                page_reps.append(pdf_integrity.page_report(facts, policy))
            # The JPEG a scan stores keeps the metadata of whatever wrote it —
            # an image edited in Photoshop and "printed" to PDF included.
            for index in analysed:
                facts = structure_pages.get(index + 1) or {}
                full = [i for i in facts.get("images") or [] if float(i.get("coverage") or 0) >= 0.85
                        and "/DCTDecode" in (i.get("filters") or [])]
                if not full:
                    continue
                data = pdf_forensics.dct_stream_bytes(source.raw_reader, index, str(full[0].get("name")))
                if data:
                    image_rep = pdf_integrity.image_report(data, policy)
                    break
        found, reasons = pdf_integrity.signals_from(file_rep, page_reps, image_rep, policy, is_image=source.is_image)
    except Exception as e:
        logger.warning("authenticity: integrity layer failed: %s", e)
        layers["integrity"] = LAYER_ERROR
        _reason(audit, pdf_integrity.R_UNDETERMINED, "integrity", False,
                pdf_integrity.MESSAGES[pdf_integrity.R_UNDETERMINED].format(detail=str(e)[:80]))
        return
    audit["integrity"] = {
        **({"file": _integrity_file_view(file_rep)} if file_rep else {}),
        **({"perPage": page_reps} if page_reps else {}),
        **({"image": {k: v for k, v in image_rep.items() if k not in ("pngText",) or v}} if image_rep else {}),
    }
    signals.update(found)
    failing = False
    for code, severity, message in reasons:
        _reason(audit, code, "integrity", severity == "hard", message)
        failing = failing or severity != "info"
    layers["integrity"] = LAYER_FAIL if failing else LAYER_OK


def _integrity_file_view(report: Dict[str, Any]) -> Dict[str, Any]:
    """The file report minus the long xref chain (offsets are noise in the
    audit; generations and counts say it all)."""
    out = dict(report)
    chain = dict(report.get("chain") or {})
    chain.pop("chain", None)
    out["chain"] = chain
    return out


def _raster(source: PageSource, index: int, dpi: int) -> Any:
    if source.is_image:
        return page_raster.decode_image(source.image_bytes or b"")
    return page_raster.render_page_rgb(source.reader, index, dpi=dpi)


def _run_pixels(spec, source: PageSource, structure, rasters, analysed, wanted, audit, signals) -> None:
    layers = audit["layers"]
    policy = spec.get("pixels")
    if policy is False or (isinstance(policy, dict) and policy.get("enabled") is False):
        layers["pixels"] = LAYER_OFF
        return
    policy = policy if isinstance(policy, dict) else {}
    pixels_audit: Dict[str, Any] = {}
    states: List[str] = []

    if "pixels.natural_background" in wanted or "vision" in spec["layers"]:
        if not rasters:
            states.append(LAYER_ERROR)
        elif structure is not None and structure.get("bilevel"):
            # A 1-bit scan is 0/255 by construction: its "perfect" background says
            # nothing about paper. Abstain rather than accuse.
            states.append(LAYER_NA)
            pixels_audit["bilevel"] = True
        else:
            per_page = {index + 1: pixel_forensics.background_stats(raster, policy)
                        for index, raster in rasters.items()}
            verdicts = [s.get("naturalBackground") for s in per_page.values()]
            natural = False if False in verdicts else True if True in verdicts else None
            pixels_audit.update({"naturalBackground": natural, "perPage": per_page})
            signals["pixels.natural_background"] = natural
            states.append(LAYER_FAIL if natural is False else LAYER_OK)
            if natural is False:
                _reason(audit, "FONDO_SIN_RUIDO_DE_CAPTURA", "pixels", False,
                        "El fondo es perfectamente uniforme: posible exportación digital o captura de pantalla")

    if "pixels.error_level_uniform" in wanted:
        # Error-level analysis runs on the JPEG bytes the FILE holds — the
        # upload itself, or the scan each page embeds — never on our raster.
        ela_pages: Dict[str, Any] = {}
        if source.is_image:
            ela_pages["1"] = pixel_forensics.error_level_stats(source.image_bytes or b"", policy)
        elif source.raw_reader is not None:
            structure_pages = {p.get("page"): p for p in (structure or {}).get("perPage") or []}
            for index in analysed:
                facts = structure_pages.get(index + 1) or {}
                full = [i for i in facts.get("images") or [] if float(i.get("coverage") or 0) >= 0.85
                        and "/DCTDecode" in (i.get("filters") or [])]
                if not full:
                    continue
                data = pdf_forensics.dct_stream_bytes(source.raw_reader, index, str(full[0].get("name")))
                if data:
                    ela_pages[str(index + 1)] = pixel_forensics.error_level_stats(data, policy)
        verdicts = [s.get("errorLevelUniform") for s in ela_pages.values()]
        uniform = False if False in verdicts else True if True in verdicts else None
        pixels_audit["errorLevel"] = {"uniform": uniform, "perPage": ela_pages}
        signals["pixels.error_level_uniform"] = uniform
        states.append(LAYER_FAIL if uniform is False else LAYER_OK if uniform is True else LAYER_NA)
        if uniform is False:
            _reason(audit, "NIVEL_DE_ERROR_NO_UNIFORME", "pixels", False,
                    "Una zona de la imagen tiene un nivel de error de compresión distinto al resto: posible parche")

    audit["pixels"] = pixels_audit
    if LAYER_ERROR in states:
        layers["pixels"] = LAYER_ERROR
    elif LAYER_FAIL in states:
        layers["pixels"] = LAYER_FAIL
    elif LAYER_OK in states:
        layers["pixels"] = LAYER_OK
    else:
        layers["pixels"] = LAYER_NA


def _vision_spec(spec) -> Optional[Dict[str, Any]]:
    vision = spec.get("vision")
    if vision is False:
        return None
    vision = vision if isinstance(vision, dict) else {}
    if not spec.get("elements") and not vision.get("page_review"):
        return None
    return None if vision.get("enabled") is False else vision


def _suspicious(audit) -> bool:
    """Did a FREE layer find anything worth paying a model to look at?"""
    return any(
        r["layer"] in ("integrity", "structure", "pixels") and r["code"] not in INFORMATIONAL_CODES
        for r in audit["reasons"]
    )


def _vision_gate(spec, vision, rasters, page_index, audit) -> Optional[bytes]:
    """The page JPEG the vision calls share, or None with the layer state
    already set: off, no raster, or `trigger: on_suspicion` with nothing to
    be suspicious about (the cost lever — a clean file pays no model)."""
    layers = audit["layers"]
    if page_index is None:
        layers["vision"] = LAYER_ERROR
        return None
    if str(vision.get("trigger") or "always").lower() == "on_suspicion" and not _suspicious(audit):
        layers["vision"] = LAYER_NA
        audit["visionSkipped"] = "SIN_SOSPECHA"
        _reason(audit, "VISION_OMITIDA_SIN_SOSPECHA", "vision", False,
                "Las capas gratuitas no encontraron nada sospechoso: no se pagó la revisión visual (`vision.trigger`)")
        return None
    page_jpeg = page_raster.encode_jpeg(rasters[page_index])
    if not page_jpeg:
        layers["vision"] = LAYER_ERROR
        return None
    return page_jpeg


def _run_vision_locate(spec, entry, elements, rasters, page_index, structure, audit, signals, doc):
    layers = audit["layers"]
    vision = _vision_spec(spec)
    if vision is None:
        layers["vision"] = LAYER_OFF
        return None
    if not elements:
        # Nothing to locate: `page_review` alone drives this layer.
        return None
    page_jpeg = _vision_gate(spec, vision, rasters, page_index, audit)
    if page_jpeg is None:
        return None
    from . import vision_inspector
    digital = structure is not None and structure.get("physicalCapture") is False
    candidates = _candidates(spec, _page_report(structure, page_index), rasters[page_index], digital)
    try:
        located, meta = vision_inspector.locate(
            page_jpeg, elements, entry, vision, monochrome=bool((structure or {}).get("bilevel")),
            embedded=[c["jpeg"] for c in candidates], embedded_are_objects=digital,
        )
    except Exception as e:
        logger.warning("authenticity: vision locate failed: %s", e)
        layers["vision"] = LAYER_ERROR
        audit["visionError"] = str(e)[:200]
        return None
    _bill(doc, meta)
    layers["vision"] = LAYER_OK
    signals["capture_kind"] = located["captureKind"]
    audit["captureKind"] = located["captureKind"]
    if located["captureKind"] in ("FOTO_DE_PANTALLA", "DOCUMENTO_DIGITAL"):
        _reason(audit, "CAPTURA_NO_ES_PAPEL", "vision", False,
                "La imagen parece una pantalla o un documento digital, no una hoja de papel")
    # Exact geometry from the PDF + the model's classification of each image.
    located["candidates"] = [
        {**{k: v for k, v in candidate.items() if k != "jpeg"},
         "kinds": (located.get("embedded") or {}).get(index, [])}
        for index, candidate in enumerate(candidates)
    ]
    return located


def _run_vision_page_review(spec, entry, rasters, page_index, structure, audit, signals, doc) -> None:
    """`vision.page_review`: one call asking the model whether the rendered
    page shows signs of editing. A judgement, so review-level at most; it runs
    under the same trigger as `locate` and is billed on the same line."""
    layers = audit["layers"]
    vision = _vision_spec(spec)
    if vision is None or not vision.get("page_review"):
        return
    if layers.get("vision") == LAYER_ERROR or audit.get("visionSkipped"):
        return
    if layers.get("vision") == LAYER_OK:
        # `locate` already ran on this page: same JPEG, no second gate.
        page_jpeg = page_raster.encode_jpeg(rasters[page_index]) if page_index is not None else None
    else:
        page_jpeg = _vision_gate(spec, vision, rasters, page_index, audit)
    if page_jpeg is None:
        return
    from . import vision_inspector

    try:
        review, meta = vision_inspector.review_page(
            page_jpeg, entry, vision, monochrome=bool((structure or {}).get("bilevel")),
        )
    except Exception as e:
        logger.warning("authenticity: vision page review failed: %s", e)
        layers["vision"] = LAYER_ERROR
        audit["visionError"] = str(e)[:200]
        return
    _bill(doc, meta)
    layers["vision"] = LAYER_OK if layers.get("vision") != LAYER_FAIL else LAYER_FAIL
    audit["pageReview"] = review
    signs = review.get("editSigns") or []
    signals["vision.no_edit_signs"] = not signs
    if signs:
        layers["vision"] = LAYER_FAIL
        _reason(audit, "INDICIOS_DE_EDICION_EN_PAGINA", "vision", False,
                f"El modelo ve indicios de edición en la página ({', '.join(signs)})")


def _candidates(spec, page_report: Dict[str, Any], raster: Any, digital: bool) -> List[Dict[str, Any]]:
    """Regions of the page the model will be asked to CLASSIFY. Their geometry
    is computed here, never by the model:

    - a digital page: the image objects the PDF places on it (`source: "PDF"`)
      — exact, and an element found in one is proven to be an inserted image;
    - anything else (a scan, a photo, a scan pasted into an office page): ink
      regions that are not body text (`source: "TINTA"`).
    """
    if digital:
        return _embedded_candidates(spec, page_report, raster)
    candidates = []
    for region in pixel_forensics.propose_regions(raster, max_regions=_MAX_INK_CANDIDATES):
        crop = page_raster.crop(raster, region["box"], pad=0.06)
        jpeg = page_raster.encode_jpeg(crop, max_edge=_CANDIDATE_MAX_EDGE, quality=88) if crop is not None else None
        if jpeg:
            candidates.append({"rect": region["box"], "coverage": region["area"], "ink": region["ink"],
                               "source": "TINTA", "jpeg": jpeg})
    return candidates


def _embedded_candidates(spec, page_report: Dict[str, Any], raster: Any) -> List[Dict[str, Any]]:
    """The image objects the PDF places on this page that could be a pasted
    element: bigger than decoration, smaller than "a scan pasted into the
    page". Their placement is EXACT (it comes from the file, not from a model),
    so they are cropped here and the model is only asked what each one is."""
    structure_policy = spec.get("structure") if isinstance(spec.get("structure"), dict) else {}
    smallest = float(structure_policy.get("ignore_image_coverage_below",
                                          pdf_forensics.DEFAULT_POLICY["ignore_image_coverage_below"]))
    images = [i for i in page_report.get("images") or []
              if smallest <= float(i.get("coverage") or 0) <= _MAX_PASTED_COVERAGE and i.get("rect")]
    images.sort(key=lambda i: float(i.get("coverage") or 0), reverse=True)
    candidates = []
    for image in images[:_MAX_EMBEDDED_CANDIDATES]:
        crop = page_raster.crop(raster, image["rect"], pad=0.04)
        jpeg = page_raster.encode_jpeg(crop, max_edge=_CROP_MAX_EDGE, quality=92) if crop is not None else None
        if jpeg:
            candidates.append({"source": "PDF", "rect": image["rect"], "sha256": image.get("sha256"),
                               "coverage": image.get("coverage"), "ppi": image.get("ppi"),
                               "transparency": image.get("transparency"), "jpeg": jpeg})
    return candidates


def _page_report(structure, page_index) -> Dict[str, Any]:
    if structure is None or page_index is None:
        return {}
    return next((p for p in structure.get("perPage") or [] if p.get("page") == page_index + 1), {})


def _element_evidence(elements, located, raster, page_report, audit) -> Dict[str, Dict[str, Any]]:
    state: Dict[str, Dict[str, Any]] = {kind: {} for kind in elements}
    if located is None:
        return state
    boxes = {kind: dict(located["elements"].get(kind) or {}) for kind in elements}
    # An embedded image the model recognised as this element WINS over the box
    # the model guessed: its placement is exact, the guess measured ~10% of the
    # page off. It also proves presence, even if the page-level answer said no.
    exact: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in located.get("candidates") or []:
        for kind in candidate.get("kinds") or []:
            exact.setdefault(kind, []).append(candidate)
    for kind, found in exact.items():
        if found[0].get("source") == "PDF":
            # Several images can be the same element (a letterhead logo on top
            # AND a brand strip at the bottom). The model's own box is too
            # coarse to crop from, but plenty to say WHICH one it meant.
            hint = boxes[kind].get("bbox")
            best = (min(found, key=lambda c: _centre_distance(c["rect"], hint)) if hint
                    else max(found, key=lambda c: float(c.get("coverage") or 0)))
        else:
            # Ink regions overlap by construction (a signature alone, and the
            # same signature with the printed name under it): the tightest one
            # that holds ONLY this element is the cleanest crop.
            alone = [c for c in found if c.get("kinds") == [kind]]
            pool = alone or found
            # Fingerprinted elements (a signature) want the tightest crop; the
            # rest (a logo) want the whole thing, not a piece of it.
            tightest = elements[kind].get("fingerprint", True) is not False
            best = (min if tightest else max)(pool, key=lambda c: float(c.get("coverage") or 0))
        boxes[kind].update({"present": True, "bbox": list(best["rect"]), "exact": True,
                            "source": best.get("source")})

    for kind, spec in elements.items():
        found = boxes[kind]
        entry = state[kind]
        entry["present"] = bool(found.get("present"))
        entry["bbox"] = found.get("bbox")
        entry["bboxSource"] = found.get("source") if found.get("exact") else "MODELO"
        entry["certainty"] = found.get("certainty")
        if not entry["present"] or raster is None:
            continue
        if not found.get("exact"):
            # The model SAW the element on the page but recognised none of the
            # computed regions as it. Its own box is not good enough to crop
            # from (measured ~20% of the page off), so presence is recorded and
            # nothing is fingerprinted, examined or compared: a wrong crop would
            # manufacture a "does not match" out of body text.
            continue

        crop = page_raster.crop(raster, entry["bbox"], pad=0.04 if found.get("exact") else _CROP_PAD)
        if crop is None:
            continue
        entry["crop"] = crop
        overlapping = any(
            other != kind and boxes[other].get("present") and _boxes_overlap(entry["bbox"], boxes[other].get("bbox"))
            for other in elements
        )
        # Two elements sharing a box (a stamp struck over the signature) are
        # told apart by ink: the catalog says which ink each one is there.
        ink = str(spec.get("ink_when_overlapping") or "all") if overlapping else "all"
        entry["inkUsed"] = ink
        if spec.get("fingerprint", True) is not False:
            entry["grid"] = pixel_forensics.fingerprint(crop, ink=ink)
        entry["inkMeasured"] = pixel_forensics.dominant_ink(crop)

        if spec.get("embedded_image_is_insertion", True) is not False:
            pasted = [c for c in exact.get(kind) or [] if c.get("source") == "PDF"]
            if pasted:
                entry["embeddedImages"] = [
                    {"sha256": i.get("sha256"), "coverage": i.get("coverage"), "ppi": i.get("ppi"),
                     "transparency": i.get("transparency")} for i in pasted
                ]
                _reason(audit, f"{kind}_ES_IMAGEN_INSERTADA", "structure", True,
                        f"{kind}: hay una imagen incrustada en el PDF justo donde está el elemento")
    return state


def _centre_distance(a, b) -> float:
    return (((a[0] + a[2]) - (b[0] + b[2])) ** 2 + ((a[1] + a[3]) - (b[1] + b[3])) ** 2) ** 0.5 / 2


def _boxes_overlap(a, b, min_iou: float = 0.1) -> bool:
    if not a or not b:
        return False
    ix = max(min(a[2], b[2]) - max(a[0], b[0]), 0.0)
    iy = max(min(a[3], b[3]) - max(a[1], b[1]), 0.0)
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return union > 0 and inter / union >= min_iou


def _reference_key(spec, context) -> Optional[str]:
    """`references.key_from` is a dotted path into the stage context — e.g.
    `metadata.proveedor`, the supplier number the uploader sent as upload
    metadata. The CALLER asserts it; nothing here can verify it."""
    path = str((spec.get("references") or {}).get("key_from") or "")
    node: Any = context
    for part in filter(None, path.split(".")):
        node = node.get(part) if isinstance(node, dict) else None
    value = str(node).strip() if node not in (None, "") else ""
    return re.sub(r"[^A-Za-z0-9._-]", "", value) or None


def _load_references(spec, context, element_state, audit) -> Dict[str, List[Dict[str, Any]]]:
    config = spec.get("references")
    audit["references"] = {"found": False}
    if not isinstance(config, dict) or config.get("enabled") is False:
        return {}
    scope = str(config.get("scope") or "DEFAULT")
    key = _reference_key(spec, context)
    audit["references"].update({"scope": scope, "key": key})
    if not key:
        _reason(audit, "SIN_CLAVE_DE_REFERENCIAS", "references", False,
                f"La carga no trae `{config.get('key_from')}`: no se puede comparar contra referencias registradas")
        return {}
    listed = fingerprint_store.list_references(scope, key)
    if not listed:
        if listed is not None:
            _reason(audit, "SIN_REFERENCIAS_REGISTRADAS", "references", False,
                    f"{scope} {key} no tiene firmas/sellos/logos registrados: solo se verificó presencia y no-inserción")
        return {}
    limit = int((spec.get("vision") or {}).get("max_references_per_kind", 3)) if isinstance(spec.get("vision"), dict) else 3
    chosen: Dict[str, List[Dict[str, Any]]] = {}
    for kind, refs in listed.items():
        if kind not in element_state:
            continue
        ranked = fingerprint_store.rank_references(refs, element_state[kind].get("grid"), limit)
        chosen[kind] = ranked
    audit["references"].update({"found": True, "registered": {k: len(v) for k, v in listed.items()}})
    return chosen


def _run_vision_inspect(spec, entry, elements, element_state, references, digital, structure, audit, doc) -> None:
    vision = _vision_spec(spec)
    if vision is None or audit["layers"].get("vision") != LAYER_OK:
        return
    when_digital = vision.get("inspect_when_digital", False)
    has_references = any(references.values())
    if digital and not (when_digital is True or (when_digital == "with_references" and has_references)):
        # Already proven a digital composition: asking a model whether the
        # signature LOOKS pasted adds cost, not information. The exception is
        # `"with_references"`: when the supplier HAS registered references, "is
        # this pasted signature at least the registered one?" is a real question
        # — and the crop it is asked about is exact (it comes from the PDF).
        audit["visionInspect"] = "OMITIDA_DOCUMENTO_DIGITAL"
        return
    if vision.get("inspect_crops", True) is False:
        return
    from . import vision_inspector

    crops: Dict[str, bytes] = {}
    for kind, state in element_state.items():
        if state.get("crop") is not None:
            jpeg = page_raster.encode_jpeg(state["crop"], max_edge=_CROP_MAX_EDGE, quality=92)
            if jpeg:
                crops[kind] = jpeg
    if not crops:
        return

    with_bytes: Dict[str, List[Dict[str, Any]]] = {}
    for kind, refs in references.items():
        loaded = []
        for ref in refs:
            data = _normalized_reference(fingerprint_store.fetch_reference_bytes(ref))
            if data:
                loaded.append({**ref, "bytes": data, "format": "jpeg"})
        if loaded:
            with_bytes[kind] = loaded
    try:
        inspected, meta = vision_inspector.inspect(
            crops, with_bytes, elements, entry, vision, monochrome=bool((structure or {}).get("bilevel")),
        )
    except Exception as e:
        logger.warning("authenticity: vision inspect failed: %s", e)
        audit["layers"]["vision"] = LAYER_ERROR
        audit["visionError"] = str(e)[:200]
        return
    _bill(doc, meta)
    for kind, result in inspected.items():
        element_state[kind]["inspected"] = result


def _normalized_reference(data: Optional[bytes]) -> Optional[bytes]:
    """A registered reference, re-encoded as a JPEG we made ourselves. Never
    forwarded as stored: a file's extension is not its format (measured: JPEGs
    saved as `firma1.png`), and Bedrock rejects the WHOLE call when the declared
    media type and the bytes disagree — one mislabelled reference would take the
    inspection of every element down with it. Decoding also flattens an alpha
    background onto white, so ink-on-transparent compares like ink on paper."""
    if not data:
        return None
    image = page_raster.decode_image(data)
    if image is None:
        return None
    return page_raster.encode_jpeg(image, max_edge=_CROP_MAX_EDGE, quality=92)


def _element_signals(elements, element_state, references, doc, audit, signals, confidences) -> None:
    from . import vision_inspector

    entities = doc.get("_entities") or {}
    audit_elements: Dict[str, Any] = {}
    for kind, spec in elements.items():
        state = element_state.get(kind) or {}
        if "present" not in state:
            continue  # vision never ran: every element signal stays empty
        prefix = f"element.{kind}"
        present = state["present"]
        signals[f"{prefix}.present"] = present
        inspected = state.get("inspected")
        summary: Dict[str, Any] = {
            "present": present, "bbox": state.get("bbox"), "bboxSource": state.get("bboxSource"),
            "certainty": state.get("certainty"),
            "inkMeasured": state.get("inkMeasured"),
        }
        if not present:
            _reason(audit, f"{kind}_AUSENTE", "vision", False, f"No se detectó {kind} en la página")
            audit_elements[kind] = summary
            continue

        if state.get("embeddedImages"):
            signals[f"{prefix}.not_pasted"] = False
            confidences[f"{prefix}.not_pasted"] = _DETERMINISTIC_CONFIDENCE
            summary["embeddedImages"] = state["embeddedImages"]
        elif inspected is not None:
            signs = inspected.get("pastedSigns") or []
            signals[f"{prefix}.not_pasted"] = not signs
            if signs:
                _reason(audit, f"{kind}_CON_INDICIOS_DE_INSERCION", "vision", False,
                        f"{kind}: indicios de inserción digital o impresión ({', '.join(signs)})")

        if inspected is not None:
            summary.update({k: inspected.get(k) for k in
                            ("pastedSigns", "inkColour", "legendText", "referencesCompared", "matchedReference")})
            signals[f"{prefix}.ink_colour"] = inspected.get("inkColour")
            if spec.get("read_text"):
                signals[f"{prefix}.text"] = inspected.get("legendText") or None
            issuer_field = spec.get("issuer_field")
            if issuer_field:
                consistent = vision_inspector.issuer_consistent(
                    inspected.get("legendText") or "", str(entities.get(issuer_field) or ""),
                )
                signals[f"{prefix}.issuer_consistent"] = consistent
                summary["issuerConsistent"] = consistent
                if consistent is False:
                    _reason(audit, f"{kind}_NO_CORRESPONDE_AL_EMISOR", "vision", False,
                            f"{kind}: la leyenda no nombra a la empresa emisora")
            if references.get(kind):
                matched = inspected.get("matchedReference") is not None
                signals[f"{prefix}.matches_reference"] = matched
                if not matched:
                    _reason(audit, f"{kind}_NO_COINCIDE_CON_REFERENCIAS", "references", False,
                            f"{kind}: no corresponde a ninguna referencia registrada")
        audit_elements[kind] = summary
    if audit_elements:
        audit["elements"] = audit_elements


def _run_memory(spec, entry, doc, source, context, elements, element_state, audit, signals) -> None:
    layers = audit["layers"]
    memory = spec.get("memory")
    if not isinstance(memory, dict) or memory.get("enabled") is False:
        layers["memory"] = LAYER_OFF
        return
    grids = {kind: state.get("grid") for kind, state in element_state.items() if state.get("grid")}
    # Kept in the full result (base64) so the table could be rebuilt from stored
    # results without re-running vision on everything. No backfill script
    # exists yet; the data it would need is here.
    audit["fingerprints"] = {kind: base64.b64encode(grid).decode("ascii") for kind, grid in grids.items()}
    if not fingerprint_store.configured():
        # A local run (`pnpm procesar`) has no table. Unknown, not a failure.
        layers["memory"] = LAYER_NA
        return

    exact = [
        {"sha256": image["sha256"], "kind": kind}
        for kind, state in element_state.items()
        for image in state.get("embeddedImages") or [] if image.get("sha256")
    ]
    letter_key = _letter_key(doc, memory, source)
    outcome = fingerprint_store.record_and_find(
        type_id=str(doc.get("documentType") or getattr(entry, "type_id", "") or ""),
        scope_key=_reference_key(spec, context),
        letter_key=letter_key,
        file_sha256=source.file_sha256,
        document_id=context.get("documentId"),
        element_grids=grids,
        exact_images=exact,
        spec=memory,
        element_thresholds=elements,
    )
    audit["memory"] = {
        "letterKey": letter_key,
        "matches": outcome["matches"],
        "duplicateOf": outcome["duplicateOf"],
        **({"error": outcome["error"]} if outcome.get("error") else {}),
    }
    if not outcome["evaluated"]:
        layers["memory"] = LAYER_ERROR
        return
    rejecting = [m for m in outcome["matches"] if m["level"] == "reject"]
    signals["memory.elements_unique"] = not rejecting
    signals["memory.elements_not_similar"] = not outcome["matches"]
    signals["memory.not_duplicate"] = not outcome["duplicateOf"]
    layers["memory"] = LAYER_FAIL if rejecting else LAYER_OK
    for match in outcome["matches"]:
        hard = match["level"] == "reject"
        what = "la misma imagen" if match["exact"] else f"tinta {match['similarity']:.0%} igual"
        _reason(audit, f"{match['kind']}_{'REUTILIZADO' if hard else 'MUY_SIMILAR'}_EN_OTRA_CARTA", "memory", hard,
                f"{match['kind']}: {what} que en otra carta ({match.get('otherLetterKey') or match.get('otherDocumentId')})")
    if outcome["duplicateOf"]:
        _reason(audit, "CARTA_YA_RECIBIDA", "memory", False,
                f"Esta misma carta ya se había recibido ({', '.join(outcome['duplicateOf'][:3])})")


def _letter_key(doc, memory, source: PageSource) -> str:
    """What makes two uploads "the same letter": the catalog's
    `identity_fields` (e.g. employee + date). Missing any of them, the file
    itself is the identity — so two unreadable letters are never merged."""
    entities = doc.get("_entities") or {}
    parts = []
    for name in memory.get("identity_fields") or []:
        value = _fold(entities.get(name))
        if not value:
            return f"FILE:{source.file_sha256[:32]}"
        parts.append(value)
    return "|".join(parts) if parts else f"FILE:{source.file_sha256[:32]}"


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


# ------------------------------------------------------------------ output


def _reason(audit, code: str, layer: str, hard: bool, message: str) -> None:
    if any(r["code"] == code for r in audit["reasons"]):
        return
    audit["reasons"].append({"code": code, "layer": layer, "hard": hard, "message": message})


def _verdict(audit, signals) -> str:
    if any(r["hard"] for r in audit["reasons"]):
        return VERDICT_REJECTED
    if not signals.get("evaluated"):
        return VERDICT_NOT_EVALUATED
    if any(r["code"] not in INFORMATIONAL_CODES for r in audit["reasons"]):
        return VERDICT_SUSPICIOUS
    return VERDICT_AUTHENTIC


def _bill(doc: Dict[str, Any], meta: Dict[str, Any]) -> None:
    """Accumulate a vision call's tokens where cost audit looks for them —
    same shape as `pageLocatorMeta`: a real, billed call gets its own line."""
    if not meta:
        return
    current = doc.setdefault("authenticityMeta", {"modelId": meta.get("modelId"), "usage": {}, "calls": 0})
    current["usage"] = merge_bedrock_usage(current.get("usage"), meta.get("usage"))
    current["calls"] = int(current.get("calls") or 0) + 1
    current["modelId"] = meta.get("modelId") or current.get("modelId")


def _write_fields(doc, outputs: Dict[str, str], signals: Dict[str, Any], confidences: Dict[str, float]) -> None:
    entities = doc.setdefault("_entities", {})
    field_confidences = doc.setdefault("_confidences", {})
    sources = doc.setdefault("_recovered_sources", {})
    for field_name, signal in outputs.items():
        value = signals.get(signal)
        if value is None:
            # Undeterminable: leave the field EMPTY on purpose (see module doc).
            entities.pop(field_name, None)
            continue
        entities[field_name] = value
        deterministic = signal.split(".", 1)[0] in (
            "integrity", "structure", "pixels", "memory", "evaluated", "verdict", "reasons",
        )
        field_confidences[field_name] = confidences.get(
            signal, _DETERMINISTIC_CONFIDENCE if deterministic else _VISION_CONFIDENCE
        )
        sources[field_name] = SOURCE_LABEL


def envelope_view(audit: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The slice of the audit block an API caller gets: what was decided and
    why, in a couple of hundred bytes. Boxes, per-page statistics, fingerprints
    and image hashes stay in the full S3 result."""
    if not isinstance(audit, dict):
        return None
    return {
        "verdict": audit.get("verdict"),
        "layers": audit.get("layers") or {},
        "reasons": [{"code": r["code"], "hard": r["hard"], "message": r["message"]}
                    for r in audit.get("reasons") or []],
        "referencesFound": bool((audit.get("references") or {}).get("found")),
    }
