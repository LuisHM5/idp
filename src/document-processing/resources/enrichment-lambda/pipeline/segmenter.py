"""Expediente segmentation: split a multi-document PDF into typed page ranges.

One Bedrock call receives a compact digest of every page (page number +
leading text) plus the customs catalog's type descriptions, and returns the
detected documents with page ranges and confidence. Deterministic post-
processing guarantees full coverage, valid ordering and no overlaps.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from document_catalog import TypeRegistryEntry, get_all_types, get_type_entry, is_customs_type

from . import page_selector
from .bedrock import converse, parse_json_response, resolve_model
from .pdf_pages import PageText

logger = logging.getLogger(__name__)

_DIGEST_CHARS = 450
_WS_RE = re.compile(r"[ \t]+")

# Deterministic page markers appended to each digest. These are computed over
# the FULL page text (the digest itself is truncated), so the classifier sees
# hard evidence like "this page carries a CFDI UUID" even when it appears at
# the bottom of the page.
_UUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}[-‐‑–—][0-9A-Fa-f]{4}[-‐‑–—][0-9A-Fa-f]{4}[-‐‑–—][0-9A-Fa-f]{4}[-‐‑–—][0-9A-Fa-f]{12}"
)
_PAGE_MARKERS = [
    ("CFDI", re.compile(r"folio\s+fiscal|sello\s+digital\s+del\s+(cfdi|sat)|cadena\s+original\s+del\s+complemento|comprobante\s+fiscal", re.IGNORECASE)),
    ("PEDIMENTO", re.compile(r"NUM\.?\s*PEDIMENTO|CVE\.?\s*PEDIM|CUADRO\s+DE\s+LIQUIDACION|VALOR\s+ADUANA.*VALOR\s+DOLARES|T\.\s*C\.\s*:", re.IGNORECASE)),
    ("VUCEM", re.compile(r"ventanilla\s+digital|acuse\s+de|e-?document", re.IGNORECASE)),
    ("DODA", re.compile(r"\bDODA\b|documento\s+de\s+operaci[oó]n\s+para\s+despacho", re.IGNORECASE)),
    ("COVE", re.compile(r"\bCOVE\b|informaci[oó]n\s+de\s+valor\s+y\s+de\s+comercializaci[oó]n", re.IGNORECASE)),
    ("SPEI", re.compile(r"\bSPEI\b|transferencia.*mismo\s+d[ií]a", re.IGNORECASE)),
    ("MANIFESTACION_VALOR", re.compile(r"manifestaci[oó]n\s+de\s+valor", re.IGNORECASE)),
    ("HOJA_CALCULO", re.compile(r"hoja\s+de\s+c[aá]lculo.*valor\s+en\s+aduana", re.IGNORECASE)),
    ("TRANSPORTE", re.compile(r"bill\s+of\s+lading|air\s+waybill|carta\s+porte|packing\s+list|lista\s+de\s+empaque", re.IGNORECASE)),
    # A foreign (typically US) invoice. Deliberately English-only phrases: the
    # CFDIs in the same expediente are in Spanish and carry none of these, so
    # this never competes with the CFDI marker above on the same page.
    ("FACTURA_EXTRANJERA", re.compile(r"\bINVOICE\b|bill\s+to\b|sold\s+to\b|balance\s+due|remit\s+to|warehouse\s+receipt", re.IGNORECASE)),
    ("HONORARIOS_AGENCIA", re.compile(r"honorarios|caaarem|precinto|\bcove\b.*servicio|cuenta\s+de\s+terceros", re.IGNORECASE)),
    ("SERVICIOS_LOGISTICOS", re.compile(r"maniobras|almacenaje|muestreo|refrigerad|acceso\s+(por|de)\s+veh[ií]culo|flete", re.IGNORECASE)),
]


def _page_markers(text: str) -> str:
    tags = [name for name, pattern in _PAGE_MARKERS if pattern.search(text)]
    uuids = _UUID_RE.findall(text)
    if uuids:
        tags.append(f"UUID:{uuids[0][:13].upper()}")
    return f" <<señales: {', '.join(tags)}>>" if tags else ""


@dataclass
class Segment:
    doc_type: str
    page_start: int
    page_end: int
    confidence: float
    subtype: Optional[str] = None
    pedimento_reference: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def _page_digest(page: PageText, max_chars: int = _DIGEST_CHARS) -> str:
    text = _WS_RE.sub(" ", page.text.replace("\r", " "))
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return " | ".join(lines)[:max_chars] + _page_markers(page.text)


def _candidate_types() -> Dict[str, TypeRegistryEntry]:
    return {tid: e for tid, e in get_all_types().items() if is_customs_type(tid)}


def _build_prompt(pages: List[PageText]) -> str:
    candidates = _candidate_types()
    # 400, not 220: a type's description is where its discriminators live, and
    # cutting mid-sentence threw away exactly the "...but NOT to be confused
    # with X" half that resolves the hard cases. Candidate types number in the
    # single digits, so the extra characters are noise against the page digests.
    type_lines = [
        f"- {tid}: {entry.description[:400]}"
        for tid, entry in candidates.items()
        if tid != "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"
    ]
    # Short files get generous digests (concepts often live past the header);
    # long expedientes stay compact to keep the prompt cheap.
    digest_chars = 1600 if len(pages) <= 4 else _DIGEST_CHARS
    page_lines = []
    for p in pages:
        digest = _page_digest(p, digest_chars)
        page_lines.append(f"[pag {p.number}] {digest if digest else '(pagina sin texto / imagen)'}")

    return (
        "Eres un clasificador de expedientes aduanales mexicanos. Un PDF contiene "
        "varios documentos concatenados. Te doy el inicio del texto de cada pagina; "
        "identifica cada documento y su rango de paginas.\n\n"
        "Tipos posibles:\n" + "\n".join(type_lines) + "\n- OTRO: cualquier otro documento\n\n"
        "Senales de inicio de documento: cambio de emisor/diseno, 'Pagina 1 de N', "
        "nuevo folio o UUID, titulo nuevo (PEDIMENTO, ACUSE, DODA, COVE, Manifestacion "
        "de Valor, SPEI, Hoja de calculo, Solicitud). Un documento de N paginas "
        "('Pagina X de N') abarca N paginas consecutivas. Copias repetidas del mismo "
        "documento son documentos separados.\n\n"
        "REGLAS DE DESAMBIGUACION (importantes):\n"
        "- Una pagina con senal CFDI o UUID es una FACTURA (CFDI_GASTO_TERCERO, "
        "FACTURA_AGENCIA_ADUANAL, FACTURA_LOGISTICA...), NUNCA "
        "PERMISO_CERTIFICADO_SANITARIO, aunque el concepto facturado mencione "
        "'certificado fitosanitario' o derechos: eso es un CFDI por pago de derechos "
        "(CFDI_GASTO_TERCERO).\n"
        "- PERMISO_CERTIFICADO_SANITARIO es solo el permiso/certificado oficial emitido "
        "por la autoridad (SENASICA/COFEPRIS), sin UUID ni sellos CFDI.\n"
        "- FACTURA_AGENCIA_ADUANAL: CFDI emitido por la agencia/agente aduanal; "
        "factura HONORARIOS del despacho (y suele incluir CAAAREM, COVE, precinto, "
        "UVA o desglose de gastos por cuenta de terceros).\n"
        "- FACTURA_LOGISTICA: CFDI cuyos conceptos son maniobras, almacenaje, "
        "acceso de vehiculos, muestreo, refrigerado, fletes o transporte. Si NO "
        "factura honorarios de agente aduanal, NO es FACTURA_AGENCIA_ADUANAL "
        "aunque mencione un pedimento.\n"
        "- CFDI_GASTO_TERCERO: CFDI de otros terceros ligados al pedimento "
        "(derechos, muestreo, inspeccion).\n"
        "- Una factura EXTRANJERA (en ingles, emisor de EUA, con INVOICE / "
        "Invoice # / Bill To / Sold To / Balance Due y SIN UUID ni sello CFDI) "
        "es una factura de proveedor extranjero, cubra mercancia, flete o "
        "servicios (almacenaje, maniobras, labels). NUNCA es un CFDI aunque "
        "venga junto a uno, ni un packing list aunque describa la mercancia.\n"
        "- Cada INVOICE con su PROPIO numero de factura es un documento "
        "SEPARADO, aunque vayan en paginas consecutivas y sean del mismo "
        "proveedor y del mismo pedimento. No las juntes en un solo rango.\n"
        "- El pedimento (PEDIMENTO_ADUANAL) tiene encabezado NUM. PEDIMENTO / "
        "T. OPER / CVE. PEDIM y cuadro de liquidacion; no tiene UUID.\n"
        "- COMPROBANTE_PAGO_ADUANAL requiere linea de captura, desglose de "
        "contribuciones o pago electronico de comercio exterior. Una transferencia "
        "SPEI entre cuentas de empresas SIN linea de captura es OTRO.\n"
        "- Paginas de COVE, DODA, SPEI, manifestacion de valor, hoja de calculo y "
        "solicitudes internas clasifícalas como OTRO salvo que exista un tipo "
        "especifico en la lista.\n\n"
        "Paginas:\n" + "\n".join(page_lines) + "\n\n"
        "Responde SOLO un JSON: {\"documents\": [{\"type\": \"...\", \"subtype\": null, "
        "\"page_start\": 1, \"page_end\": 2, \"confidence\": 0.95, "
        "\"pedimento_reference\": \"...\"|null}]}. Todas las paginas deben quedar "
        "cubiertas por algun documento (usa OTRO si no reconoces el tipo). "
        "confidence entre 0 y 1 segun que tan seguro estas del tipo."
    )


def _sanitize(segments: List[Segment], total_pages: int) -> List[Segment]:
    """Clamp ranges, sort, drop invalid, fill gaps with OTRO segments."""
    cleaned: List[Segment] = []
    for s in segments:
        start = max(1, min(int(s.page_start), total_pages))
        end = max(start, min(int(s.page_end), total_pages))
        cleaned.append(Segment(s.doc_type, start, end, s.confidence, s.subtype, s.pedimento_reference, s.raw))
    cleaned.sort(key=lambda s: (s.page_start, s.page_end))

    result: List[Segment] = []
    cursor = 1
    for s in cleaned:
        if s.page_start > cursor:
            result.append(Segment("OTRO", cursor, s.page_start - 1, 0.0))
        if s.page_start < cursor:  # overlap: trim
            if s.page_end < cursor:
                continue
            s = Segment(s.doc_type, cursor, s.page_end, s.confidence, s.subtype, s.pedimento_reference, s.raw)
        result.append(s)
        cursor = s.page_end + 1
    if cursor <= total_pages:
        result.append(Segment("OTRO", cursor, total_pages, 0.0))
    return result


def _declared_segments(pages: List[PageText], declared_type: str, total: int) -> List[Segment]:
    """Segments for an upload whose type the uploader declared.

    Normally that is one segment covering the file: the uploader said what it
    is, so there is nothing to classify. But some types legitimately arrive
    several to a file — a PDF holding three foreign invoices from three
    vendors is three documents, not one with three invoice numbers — and those
    declare `page_selection.multi_instance`. For them the file is split
    deterministically from the catalog's own boundaries: no Bedrock call, and
    each resulting segment is extracted with only its own pages.

    Detected by catalog SHAPE, never by TYPE_ID: any future type that declares
    the flag gets this for free. Anything the splitter can't answer confidently
    comes back as the single whole-file segment this always returned.
    """
    whole = [Segment(declared_type, 1, total, 1.0)]
    try:
        entry = get_type_entry(declared_type)
    except Exception:  # unknown/unloadable type: behave exactly as before
        return whole
    if not page_selector.enabled_for(entry):
        return whole
    spec = page_selector.spec_for(entry)
    if not page_selector.multi_instance_enabled(spec):
        return whole

    split = page_selector.split_instances(pages, spec)
    if split.fail_open or len(split.spans) <= 1:
        logger.info(
            "segmenter: %s declarado, sin partir en instancias (%s)",
            declared_type, split.reason,
        )
        return whole

    logger.info(
        "segmenter: %s declarado, %d instancias %s (%s)",
        declared_type, len(split.spans), split.ranges, split.reason,
    )
    return [
        Segment(declared_type, start, end, 1.0)
        for start, end in split.ranges
    ]


def _collapse_duplicate_segments(
    segments: List[Segment], pages: List[PageText]
) -> List[Segment]:
    """Mark repeated COPIES of the same document as OTRO, per type.

    The classifier is told that a reprint is its own document, which is the
    right instruction — it has no way to know whether two similar pages are
    one document or two. Deciding which of them are the SAME document is a
    deterministic question, answered by the identity printed on them, and a
    type that declares `duplicate_key_pattern` has already said where to read
    it. Measured on a real expediente: the invoice on page 8 is reprinted with
    handwritten annotations on page 22, and without this the pipeline pays a
    full extraction for the copy and reports the same invoice twice.

    This is the same rule the declared-type path applies via
    `page_selector.split_instances`, so an upload behaves the same whether or
    not the uploader named its type.

    A duplicate is RELABELLED `OTRO` (carrying the subtype `COPIA_DUPLICADA`),
    never removed: page coverage stays complete and the copy is still visible
    in the result with its page range — it just isn't extracted again.

    Fail-open throughout: a type that declares no pattern, or a segment whose
    identity can't be read, is left exactly as the classifier returned it.
    """
    texts = {p.number: (p.text or "") for p in pages}
    seen: Dict[Tuple[str, str], Segment] = {}
    result: List[Segment] = []
    for seg in segments:
        if seg.doc_type == "OTRO":
            result.append(seg)
            continue
        try:
            entry = get_type_entry(seg.doc_type)
        except Exception:
            result.append(seg)
            continue
        spec = page_selector.spec_for(entry)
        if not page_selector.multi_instance_enabled(spec):
            result.append(seg)
            continue
        span = list(range(seg.page_start, seg.page_end + 1))
        identity = page_selector.identity_in(texts, span, spec)
        # No pattern declared, or unreadable on these pages — never collapse it.
        if identity is None:
            result.append(seg)
            continue
        key = (seg.doc_type, identity)
        previous = seen.get(key)
        if previous is None:
            seen[key] = seg
            result.append(seg)
            continue
        # Longest wins, same reasoning as page_selector: a one-page summary of
        # the same document can legitimately come first, and keeping it while
        # dropping the full print would lose real content.
        loser, winner = (previous, seg) if _span_len(seg) > _span_len(previous) else (seg, previous)
        seen[key] = winner
        logger.info(
            "segmenter: %s págs %d-%d es copia de %d-%d (%s); no se extrae",
            seg.doc_type, loser.page_start, loser.page_end,
            winner.page_start, winner.page_end, identity,
        )
        result = [
            Segment("OTRO", s.page_start, s.page_end, 0.0, "COPIA_DUPLICADA")
            if s is loser else s
            for s in result
        ]
        if seg is winner:
            result.append(seg)
        else:
            result.append(Segment("OTRO", seg.page_start, seg.page_end, 0.0, "COPIA_DUPLICADA"))
    return result


def _span_len(seg: Segment) -> int:
    return seg.page_end - seg.page_start + 1


def segment_pages(
    pages: List[PageText],
    declared_type: Optional[str] = None,
    model_id: Optional[str] = None,
) -> Tuple[List[Segment], Dict[str, Any]]:
    """Split pages into typed segments.

    When `declared_type` names a single known type (not the multi-document
    expediente), no model call is made: the file is one segment, or — for a
    type that declares `page_selection.multi_instance` — one segment per
    instance found deterministically from its catalog boundaries. See
    `_declared_segments`.

    Returns `(segments, usage_meta)` — `usage_meta` is `{}` whenever no
    Bedrock call was made (declared type, empty input), or
    `{"modelId": ..., "usage": {...}}` when one was, so callers can cost this
    step the same way `text_extractor` already exposes extraction usage
    (previously this call's tokens were only logged, never returned — a real
    gap against Cost Explorer, see `documentacion/COSTOS.md`).
    """
    total = len(pages)
    if total == 0:
        return [], {}
    if declared_type and declared_type != "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO":
        return _declared_segments(pages, declared_type, total), {}

    prompt = _build_prompt(pages)
    used_model_id = model_id or resolve_model("segmenter")
    text, meta = converse(prompt, model_id=used_model_id, max_tokens=4096, stage="segmenter")
    # `meta["modelId"]` (not the pre-call `used_model_id`) so a model-unavailable
    # fallback (pipeline.bedrock._invoke_with_fallback) is reflected here too —
    # this feeds the segmentation cost line in cost_audit.py.
    usage_meta = {"modelId": meta["modelId"], "usage": meta.get("usage") or {}}
    try:
        parsed = parse_json_response(text)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("segmenter: unparseable model output (%s); falling back to single segment", e)
        return [Segment("OTRO", 1, total, 0.0)], usage_meta

    segments: List[Segment] = []
    for d in parsed.get("documents", []):
        if not isinstance(d, dict):
            continue
        try:
            segments.append(
                Segment(
                    doc_type=str(d.get("type", "OTRO")).strip().upper(),
                    page_start=int(d.get("page_start", 0)),
                    page_end=int(d.get("page_end", 0)),
                    confidence=float(d.get("confidence", 0.5)),
                    subtype=d.get("subtype"),
                    pedimento_reference=d.get("pedimento_reference"),
                    raw=d,
                )
            )
        except (TypeError, ValueError):
            continue

    if not segments:
        return [Segment("OTRO", 1, total, 0.0)], usage_meta
    result = _collapse_duplicate_segments(_sanitize(segments, total), pages)
    logger.info(
        "segmenter: %d segments over %d pages (usage=%s)",
        len(result), total, meta.get("usage"),
    )
    return result, usage_meta
