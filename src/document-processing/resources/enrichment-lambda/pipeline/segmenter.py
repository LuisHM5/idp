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

from document_catalog import TypeRegistryEntry, get_all_types, is_customs_type

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
    type_lines = [
        f"- {tid}: {entry.description[:220]}"
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


def segment_pages(
    pages: List[PageText],
    declared_type: Optional[str] = None,
    model_id: Optional[str] = None,
) -> Tuple[List[Segment], Dict[str, Any]]:
    """Split pages into typed segments.

    When `declared_type` names a single known type (not the multi-document
    expediente), the whole file is one segment and no model call is made.

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
        return [Segment(declared_type, 1, total, 1.0)], {}

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
    result = _sanitize(segments, total)
    logger.info(
        "segmenter: %d segments over %d pages (usage=%s)",
        len(result), total, meta.get("usage"),
    )
    return result, usage_meta
