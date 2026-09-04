"""Expediente segmentation: split a multi-document PDF into typed page ranges.

One Bedrock call receives a compact digest of every page (page number +
leading text) plus the customs catalog's type descriptions, and returns the
detected documents with page ranges and confidence. Deterministic post-
processing guarantees full coverage, valid ordering and no overlaps.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import ClientError

from document_catalog import (
    TypeRegistryEntry,
    get_all_types,
    get_type_entry,
    is_segmentation_candidate,
    normalize_declared_types,
    resolve_declared_counts,
)

from . import page_selector
from .bedrock import (
    converse,
    note_used_model,
    parse_json_response,
    record_stage_degraded,
    resolve_model,
)
from .model_fallback import get_fallback_models
# The container label, imported rather than repeated: declaring it is the
# existing escape hatch that forces the model path even WITH a declared type,
# and a copy of the string here could drift from the one the envelope emits.
# `envelope` pulls in nothing from `pipeline`, so this cannot cycle.
from .envelope import CONTAINER_TYPE
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


# Disambiguation rules for the classifier prompt, each tagged with the type it
# is ABOUT so only the ones whose type is actually a candidate get printed.
# `None` tags a rule that always applies.
#
# They used to be one hardcoded block, which had gone quietly stale: five of the
# types it argued about (CFDI_GASTO_TERCERO, FACTURA_AGENCIA_ADUANAL,
# FACTURA_LOGISTICA, PERMISO_CERTIFICADO_SANITARIO, COMPROBANTE_PAGO_ADUANAL)
# now live in catalogs/save/ and are not in the catalog at all, so the model was
# being told to pick between options it was never offered. Tagging also earns
# its keep on the declared path: a caller asking for two types should not pay
# for paragraphs about five others the classifier is forbidden to choose.
_DISAMBIGUATION_RULES: Tuple[Tuple[Optional[str], str], ...] = (
    (
        "CFDI_GASTO_TERCERO",
        "- Una pagina con senal CFDI o UUID es una FACTURA (CFDI_GASTO_TERCERO, "
        "FACTURA_AGENCIA_ADUANAL, FACTURA_LOGISTICA...), NUNCA "
        "PERMISO_CERTIFICADO_SANITARIO, aunque el concepto facturado mencione "
        "'certificado fitosanitario' o derechos: eso es un CFDI por pago de derechos "
        "(CFDI_GASTO_TERCERO).",
    ),
    (
        "PERMISO_CERTIFICADO_SANITARIO",
        "- PERMISO_CERTIFICADO_SANITARIO es solo el permiso/certificado oficial emitido "
        "por la autoridad (SENASICA/COFEPRIS), sin UUID ni sellos CFDI.",
    ),
    (
        "FACTURA_AGENCIA_ADUANAL",
        "- FACTURA_AGENCIA_ADUANAL: CFDI emitido por la agencia/agente aduanal; "
        "factura HONORARIOS del despacho (y suele incluir CAAAREM, COVE, precinto, "
        "UVA o desglose de gastos por cuenta de terceros).",
    ),
    (
        "FACTURA_LOGISTICA",
        "- FACTURA_LOGISTICA: CFDI cuyos conceptos son maniobras, almacenaje, "
        "acceso de vehiculos, muestreo, refrigerado, fletes o transporte. Si NO "
        "factura honorarios de agente aduanal, NO es FACTURA_AGENCIA_ADUANAL "
        "aunque mencione un pedimento.",
    ),
    (
        "CFDI_GASTO_TERCERO",
        "- CFDI_GASTO_TERCERO: CFDI de otros terceros ligados al pedimento "
        "(derechos, muestreo, inspeccion).",
    ),
    (
        "FACTURA_HONORARIOS",
        "- FACTURA_HONORARIOS: CFDI mexicano timbrado (UUID, Folio Fiscal, Sello "
        "Digital del SAT) cuyos conceptos son honorarios por servicios "
        "profesionales o por despacho aduanero, con retenciones de ISR/IVA. Si "
        "esta en ingles y no tiene UUID no es este tipo.",
    ),
    (
        "FACTURA_FLETE_EXTRANJERO",
        "- Una factura EXTRANJERA (en ingles, emisor de EUA, con INVOICE / "
        "Invoice # / Bill To / Sold To / Balance Due y SIN UUID ni sello CFDI) "
        "es una factura de proveedor extranjero, cubra mercancia, flete o "
        "servicios (almacenaje, maniobras, labels). NUNCA es un CFDI aunque "
        "venga junto a uno, ni un packing list aunque describa la mercancia.",
    ),
    (
        "FACTURA_FLETE_EXTRANJERO",
        "- Cada INVOICE con su PROPIO numero de factura es un documento "
        "SEPARADO, aunque vayan en paginas consecutivas y sean del mismo "
        "proveedor y del mismo pedimento. No las juntes en un solo rango.",
    ),
    (
        "PEDIMENTO_ADUANAL",
        "- El pedimento (PEDIMENTO_ADUANAL) tiene encabezado NUM. PEDIMENTO / "
        "T. OPER / CVE. PEDIM y cuadro de liquidacion; no tiene UUID.",
    ),
    (
        "COMPROBANTE_PAGO_ADUANAL",
        "- COMPROBANTE_PAGO_ADUANAL requiere linea de captura, desglose de "
        "contribuciones o pago electronico de comercio exterior. Una transferencia "
        "SPEI entre cuentas de empresas SIN linea de captura es OTRO.",
    ),
    (
        None,
        "- Paginas de COVE, DODA, SPEI, manifestacion de valor, hoja de calculo y "
        "solicitudes internas clasifícalas como OTRO salvo que exista un tipo "
        "especifico en la lista.",
    ),
)


def _env_int(name: str, default: int) -> int:
    """Read an int env var, tolerating an unset/invalid value the same way
    `model_fallback.get_max_fallback_retries` does: fall back to `default`
    and warn, never raise. Read at call time (not import time) so tests and
    hot-reload-by-redeploy both see changes without a code change."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("segmenter: invalid %s=%r, using default %d", name, raw, default)
        return default


# ---- output-token budget --------------------------------------------------
#
# The classifier prompt used to always request `max_tokens=4096` — BELOW
# converse()'s own default of 8192 — regardless of `len(pages)`. For a large
# expediente whose classification lists many documents (FACTURA_FLETE_EXTRANJERO
# alone requires one entry per invoice, never merged), that budget can run out
# before the model writes a single character of its JSON answer: Bedrock still
# reports a full `outputTokens` usage and a 200 OK, so nothing in
# `bedrock.py`'s ClientError-based retry/fallback machinery ever sees this as
# a failure — it has to be caught here, from the (empty/truncated) text and
# `stopReason` `converse()` already returns.
_SEGMENTER_MIN_TOKENS = 8192
_SEGMENTER_TOKENS_PER_PAGE = 90
_SEGMENTER_TOKENS_OVERHEAD = 512
_SEGMENTER_MAX_TOKENS_CEILING_DEFAULT = 32000
# At most one caller-level retry per page range once a response degrades —
# a fixed safety cap, not an operational knob, so no env var for it.
_MAX_DEGRADED_RETRIES = 1


def _segmenter_max_tokens_ceiling() -> int:
    return _env_int("SEGMENTER_MAX_TOKENS_CEILING", _SEGMENTER_MAX_TOKENS_CEILING_DEFAULT)


def _segmenter_max_tokens(total_pages: int) -> int:
    """Output-token budget for classifying `total_pages` in one call. Never
    below `converse()`'s own default (8192, itself already higher than the
    old fixed 4096), scaled up for larger page ranges, capped at a
    configurable ceiling. `bedrock._clamp_max_tokens`/`_MODEL_MAX_TOKENS`
    still clamp this down if Bedrock rejects it for exceeding a model's real
    ceiling — this is only ever the FIRST request, not a promise."""
    raw = _SEGMENTER_TOKENS_OVERHEAD + total_pages * _SEGMENTER_TOKENS_PER_PAGE
    return min(_segmenter_max_tokens_ceiling(), max(_SEGMENTER_MIN_TOKENS, raw))


def _retry_model_id(current_model_id: str) -> Optional[str]:
    """The most reliable fallback model, for retrying a degraded response —
    or `None` if that IS the model that just degraded (retrying the same
    model is exactly what produced the erratic behaviour in the first
    place). The LAST entry of `model_fallback.get_fallback_models()` is, by
    that module's own design (see its docstring), the best-verified/most
    compatible model — read from there (hot-reloadable via DynamoDB) instead
    of hardcoding a model id here, so this never drifts from that list."""
    models = get_fallback_models()
    candidate = models[-1] if models else None
    return candidate if candidate and candidate != current_model_id else None


def _classify_once(
    pages: List[PageText],
    allowed: Optional[Sequence[str]],
    model_id: str,
    max_tokens: int,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[str]]:
    """One classification attempt: build the prompt, call the model, parse
    the response. Returns `(parsed_or_None, meta, reason)` — `reason` is
    `None` on a clean success, else one of `"empty_response"` /
    `"unparseable_output"` / `"output_truncated"` (the last: the JSON DID
    parse, but Bedrock's own `stopReason` says the model ran out of budget
    mid-answer, so the parsed `documents` list may be missing entries off
    the end even though `json.loads` didn't complain)."""
    prompt = _build_prompt(pages, allowed=allowed)
    text, meta = converse(prompt, model_id=model_id, max_tokens=max_tokens, stage="segmenter")
    if not text:
        return None, meta, "empty_response"
    try:
        parsed = parse_json_response(text)
    except (ValueError, json.JSONDecodeError):
        return None, meta, "unparseable_output"
    if meta.get("stopReason") == "max_tokens":
        return parsed, meta, "output_truncated"
    return parsed, meta, None


def _attempt_summary(meta: Dict[str, Any], max_tokens: int, reason: Optional[str]) -> Dict[str, Any]:
    return {
        "modelId": meta.get("modelId"),
        "maxTokens": max_tokens,
        "stopReason": meta.get("stopReason"),
        "outputTokens": (meta.get("usage") or {}).get("outputTokens"),
        "reason": reason,
    }


def _aggregate_usage(
    entries: Sequence[Tuple[Optional[str], Optional[Dict[str, Any]]]]
) -> Dict[str, Any]:
    """Combine several real `(modelId, usage)` Bedrock-call pairs into one
    `usage.segmentation`-shaped entry. `modelId` stays a single string —
    unchanged shape for the common case — when every call used the SAME
    model; otherwise `perModel` carries the true per-model breakdown, so
    `cost_audit.py` can price each call at ITS OWN model's rate instead of
    one model's tokens getting priced at another's (the exact incident
    `documentacion/COSTOS.md` already documents once, from a different
    cause: a single-`modelId` assumption meeting a model switch it didn't
    account for)."""
    from .pricing import merge_bedrock_usage  # deferred: no reason to import pricing at module load for every segmenter call

    entries = [(model_id, usage) for model_id, usage in entries if model_id]
    model_ids = sorted({model_id for model_id, _usage in entries})
    out: Dict[str, Any] = {
        "modelId": model_ids[0] if len(model_ids) <= 1 else ",".join(model_ids),
        "usage": merge_bedrock_usage(*(usage for _model_id, usage in entries if usage)),
    }
    if len(model_ids) > 1:
        out["perModel"] = [
            {
                "modelId": mid,
                "usage": merge_bedrock_usage(*(
                    usage for model_id, usage in entries if model_id == mid and usage
                )),
            }
            for mid in model_ids
        ]
    return out


def _usage_entries(usage_meta: Dict[str, Any]) -> List[Tuple[Optional[str], Optional[Dict[str, Any]]]]:
    """Flatten one `usage_meta` (as `_classify_pages`/`_classify_window`
    return it) back into `(modelId, usage)` pairs, expanding `perModel` when
    present — so a second level of aggregation (windows aggregating
    already-aggregated per-window usage) never joins model ids that are
    themselves already a comma-joined string into something even less
    meaningful."""
    if usage_meta.get("perModel"):
        return [(m.get("modelId"), m.get("usage")) for m in usage_meta["perModel"]]
    if usage_meta.get("modelId"):
        return [(usage_meta.get("modelId"), usage_meta.get("usage"))]
    return []


def _classify_pages(
    pages: List[PageText],
    allowed: Optional[Sequence[str]],
    model_id: Optional[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Classify one page range (the whole document, or one window — see
    `_classify_windowed`), with up to `_MAX_DEGRADED_RETRIES` retries on a
    degraded (empty/unparseable/truncated) response before giving up.

    Returns `(documents, usage_meta)` — `documents` is `[]` only when even
    the retry produced nothing usable. Records a `segmenter` degradation via
    `bedrock.record_stage_degraded` whenever the FIRST attempt was not clean,
    regardless of whether the retry then recovered, so both outcomes are
    visible in `tracking.segmentationDegraded` (`resolution` distinguishes
    them). A retry that itself raises (the fallback model's own ClientError
    chain exhausted) is treated as just another degraded attempt rather than
    propagated — the original attempt's data (if any) is used instead, so a
    broken retry can never make this WORSE than not retrying at all. The
    very first attempt is NOT guarded this way: a total failure there
    propagates unchanged from today's behaviour (see `_classify_window` for
    the extra, window-level safety net windowed segmentation needs instead).
    """
    requested_model_id = model_id or resolve_model("segmenter")
    max_tokens = _segmenter_max_tokens(len(pages))

    parsed, meta, reason = _classify_once(pages, allowed, requested_model_id, max_tokens)
    attempts = [_attempt_summary(meta, max_tokens, reason)]
    # Every call that actually reached Bedrock billed tokens regardless of
    # whether its answer was usable — both must be costed, not just whichever
    # one's documents got returned.
    call_usages: List[Tuple[Optional[str], Optional[Dict[str, Any]]]] = [(meta.get("modelId"), meta.get("usage"))]
    if reason is None:
        return (parsed or {}).get("documents", []), _aggregate_usage(call_usages)

    logger.warning(
        "segmenter: respuesta degradada (%s, stopReason=%s) de %s en paginas %d-%d",
        reason, meta.get("stopReason"), meta["modelId"], pages[0].number, pages[-1].number,
    )

    best_parsed = parsed
    retry_model = _retry_model_id(meta["modelId"]) if _MAX_DEGRADED_RETRIES > 0 else None
    if retry_model is not None:
        try:
            parsed2, meta2, reason2 = _classify_once(pages, allowed, retry_model, max_tokens)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            attempts.append({"modelId": retry_model, "maxTokens": max_tokens, "reason": "client_error", "error": code})
        else:
            attempts.append(_attempt_summary(meta2, max_tokens, reason2))
            call_usages.append((meta2.get("modelId"), meta2.get("usage")))
            if reason2 is None:
                note_used_model("segmenter", meta2["modelId"])
                record_stage_degraded("segmenter", {
                    "pageStart": pages[0].number, "pageEnd": pages[-1].number,
                    "reason": reason, "resolution": "recovered_on_retry", "attempts": attempts,
                })
                return parsed2.get("documents", []), _aggregate_usage(call_usages)
            if parsed2 is not None:
                best_parsed = parsed2

    record_stage_degraded("segmenter", {
        "pageStart": pages[0].number, "pageEnd": pages[-1].number,
        "reason": reason, "resolution": "marked_otro", "attempts": attempts,
    })
    documents = (best_parsed or {}).get("documents", []) if best_parsed else []
    return documents, _aggregate_usage(call_usages)


def _segments_from_documents(
    documents: List[Dict[str, Any]], allowed_set: Optional[set]
) -> List[Segment]:
    """Cast the model's `documents` list to `Segment`s, demoting anything
    outside `allowed_set` to OTRO (what makes a declared type list CLOSED)
    and dropping malformed entries. Shared by the direct and windowed paths."""
    segments: List[Segment] = []
    for d in documents:
        if not isinstance(d, dict):
            continue
        try:
            doc_type = str(d.get("type", "OTRO")).strip().upper()
            if allowed_set and doc_type != "OTRO" and doc_type not in allowed_set:
                logger.info(
                    "segmenter: '%s' fuera de los tipos declarados %s; paginas %s-%s -> OTRO",
                    doc_type, sorted(allowed_set), d.get("page_start"), d.get("page_end"),
                )
                doc_type = "OTRO"
            segments.append(
                Segment(
                    doc_type=doc_type,
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
    return segments


# ---- page-windowed classification -----------------------------------------
#
# A single `converse()` call, however generous its `max_tokens`, still has a
# ceiling — and a large enough expediente (86-page expedientes already run in
# production; a type declaring `page_selection` raises the page cap from 50
# to `expediente._MAX_PAGES_WITH_SELECTION` = 500) can always in principle
# produce more documents than fit in one answer. Above a page-count
# threshold, split the classification into overlapping windows, classify
# each independently (optionally in parallel — segmentation latency is
# dominated by the slowest single call, not the sum, per
# documentacion/COSTOS.md's per-model latency table), and merge the results.
_SEGMENTER_WINDOW_THRESHOLD_DEFAULT = 40
_SEGMENTER_WINDOW_SIZE_DEFAULT = 25
_SEGMENTER_WINDOW_OVERLAP_DEFAULT = 3
_SEGMENTER_WINDOW_WORKERS_DEFAULT = 4
_SEGMENTER_MAX_WINDOWS_DEFAULT = 20


def _segmentation_window_threshold() -> int:
    return _env_int("SEGMENTER_WINDOW_THRESHOLD", _SEGMENTER_WINDOW_THRESHOLD_DEFAULT)


def _segmentation_window_size() -> int:
    return max(1, _env_int("SEGMENTER_WINDOW_SIZE", _SEGMENTER_WINDOW_SIZE_DEFAULT))


def _segmentation_window_overlap() -> int:
    return max(0, _env_int("SEGMENTER_WINDOW_OVERLAP", _SEGMENTER_WINDOW_OVERLAP_DEFAULT))


def _segmentation_window_workers() -> int:
    return max(1, _env_int("SEGMENTER_WINDOW_WORKERS", _SEGMENTER_WINDOW_WORKERS_DEFAULT))


def _segmentation_max_windows() -> int:
    return max(1, _env_int("SEGMENTER_MAX_WINDOWS", _SEGMENTER_MAX_WINDOWS_DEFAULT))


def _should_window(total_pages: int) -> bool:
    return total_pages > _segmentation_window_threshold()


def _build_windows(
    pages: List[PageText], window_size: int, overlap: int
) -> List[List[PageText]]:
    """Contiguous windows sharing `overlap` pages with their neighbour, so a
    document whose real boundary falls near a window edge appears WHOLE in
    at least one of the two — `_merge_window_segments` reconciles the
    resulting duplicate coverage in the overlap. A hard cap
    (`SEGMENTER_MAX_WINDOWS`) grows `window_size` instead of the window count
    when the naive geometry would need more calls than that, keeping the
    number of Bedrock calls bounded even for an exceptionally large file."""
    total = len(pages)
    if total <= window_size:
        return [pages]
    max_windows = _segmentation_max_windows()
    step = max(1, window_size - overlap)
    if (total + step - 1) // step > max_windows:
        # Bound the window COUNT by bounding the STEP, not the window size
        # directly: the step (window_size - overlap), not the window size
        # itself, is what determines how many windows a walk over `total`
        # pages needs. Growing window_size alone without growing the step by
        # the same amount would leave the step — and so the window count —
        # unchanged.
        step = max(1, (total + max_windows - 1) // max_windows)
        window_size = max(window_size, step + overlap)
    windows: List[List[PageText]] = []
    start = 0
    while start < total:
        end = min(start + window_size, total)
        windows.append(pages[start:end])
        if end == total:
            break
        start += step
    return windows


def _classify_window(
    window_pages: List[PageText],
    allowed: Optional[Sequence[str]],
    model_id: Optional[str],
) -> Tuple[List[Segment], Dict[str, Any]]:
    """`_classify_pages` for one window, plus an extra safety net: if
    `converse()` exhausts its OWN internal model-fallback chain and raises,
    only THIS window is marked OTRO — the other windows, already resolved or
    in flight, are kept. Deliberately NOT applied to the non-windowed path
    (`segment_pages` calling `_classify_pages` directly): there, a total
    failure still propagates exactly as before this change, since there are
    no sibling windows worth salvaging behind a silently degraded result."""
    try:
        documents, usage_meta = _classify_pages(window_pages, allowed, model_id)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        logger.error(
            "segmenter: ventana %d-%d fallo por completo (%s); marcada OTRO",
            window_pages[0].number, window_pages[-1].number, code,
        )
        record_stage_degraded("segmenter", {
            "pageStart": window_pages[0].number, "pageEnd": window_pages[-1].number,
            "reason": "client_error", "resolution": "marked_otro",
            "attempts": [{"modelId": model_id or resolve_model("segmenter"), "reason": "client_error", "error": code}],
        })
        return [Segment("OTRO", window_pages[0].number, window_pages[-1].number, 0.0)], {}
    allowed_set = set(allowed) if allowed else None
    return _segments_from_documents(documents, allowed_set), usage_meta


def _merge_window_segments(
    window_segments: List[List[Segment]], windows: List[List[PageText]]
) -> List[Segment]:
    """Reconcile the duplicate coverage the overlap between windows creates.

    A document whose real boundary falls inside an overlap gets reported
    TWICE — once by each window, each with a partial view. Only the LAST
    segment accumulated so far and the FIRST segment of the NEXT window are
    ever compared — segments within the same window are never compared to
    each other. That is deliberate: it is what preserves rules like
    FACTURA_FLETE_EXTRANJERO's "each invoice is its own document even
    back-to-back with an identical one" — two such segments from the SAME
    model call are never candidates for merging here.

    They merge when they share a `doc_type` AND touch across the seam
    without a gap (the accumulated segment reaches into the next window's
    overlap region, and the next window's first segment starts at or before
    that segment's own end + 1). A type disagreement on overlapping pages is
    a genuine disagreement between windows, not a cut artefact — it is left
    alone; `_sanitize`, which every caller already runs right after this,
    already resolves any remaining overlap by trimming the later segment in
    page order, so that rule lives in exactly one place. Chains naturally
    for a document spanning 3+ windows, since `merged[-1]` reflects the
    extended range by the time the next window is considered.
    """
    if not window_segments:
        return []
    merged: List[Segment] = list(window_segments[0])
    for i in range(1, len(window_segments)):
        next_segments = sorted(window_segments[i], key=lambda s: (s.page_start, s.page_end))
        if merged and next_segments:
            overlap_start = windows[i][0].number
            prev_last, next_first = merged[-1], next_segments[0]
            touches_seam = (
                prev_last.page_end >= overlap_start
                and next_first.page_start <= prev_last.page_end + 1
            )
            if touches_seam and prev_last.doc_type == next_first.doc_type:
                merged[-1] = Segment(
                    doc_type=prev_last.doc_type,
                    page_start=min(prev_last.page_start, next_first.page_start),
                    page_end=max(prev_last.page_end, next_first.page_end),
                    confidence=max(prev_last.confidence, next_first.confidence),
                    subtype=prev_last.subtype or next_first.subtype,
                    pedimento_reference=prev_last.pedimento_reference or next_first.pedimento_reference,
                    raw={**prev_last.raw, **next_first.raw, "windowMerged": True},
                )
                next_segments = next_segments[1:]
        merged.extend(next_segments)
    return merged


def _merge_window_usage(
    results: List[Tuple[List[Segment], Dict[str, Any]]]
) -> Dict[str, Any]:
    """Aggregate every window's already-aggregated usage into one
    `usage.segmentation` entry, via the same `_aggregate_usage` a single
    (non-windowed) call's own retry uses — see its docstring for the
    `perModel` shape."""
    entries: List[Tuple[Optional[str], Optional[Dict[str, Any]]]] = []
    for _segments, usage_meta in results:
        entries.extend(_usage_entries(usage_meta))
    return _aggregate_usage(entries)


def _classify_windowed(
    pages: List[PageText],
    allowed: Optional[Sequence[str]],
    model_id: Optional[str],
) -> Tuple[List[Segment], Dict[str, Any]]:
    windows = _build_windows(pages, _segmentation_window_size(), _segmentation_window_overlap())
    logger.info(
        "segmenter: %d paginas > umbral de ventaneo (%d); %d ventana(s)",
        len(pages), _segmentation_window_threshold(), len(windows),
    )
    workers = min(_segmentation_window_workers(), len(windows)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda w: _classify_window(w, allowed, model_id), windows))
    window_segments = [segs for segs, _usage in results]
    merged = _merge_window_segments(window_segments, windows)
    return merged, _merge_window_usage(results)


def _candidate_types(allowed: Optional[Sequence[str]] = None) -> Dict[str, TypeRegistryEntry]:
    """The types the classifier may choose from.

    With `allowed` (the caller declared several types in `documentType`), the
    list is exactly those, in the order given, with NO further gate: naming a
    type explicitly is a stronger signal than any catalog default, so a type
    that would not qualify on its own — because it lacks `pipeline:
    "expediente"`, or opted out of auto-candidacy — still enters. That is the
    whole point of declaring a list.

    Without it (nothing declared), candidacy is decided by catalog SHAPE via
    `is_segmentation_candidate`.
    """
    all_types = get_all_types()
    if allowed:
        return {tid: all_types[tid] for tid in allowed if tid in all_types}
    return {tid: e for tid, e in all_types.items() if is_segmentation_candidate(tid)}


def _build_prompt(pages: List[PageText], allowed: Optional[Sequence[str]] = None) -> str:
    candidates = _candidate_types(allowed)
    # 400, not 220: a type's description is where its discriminators live, and
    # cutting mid-sentence threw away exactly the "...but NOT to be confused
    # with X" half that resolves the hard cases. Candidate types number in the
    # single digits, so the extra characters are noise against the page digests.
    type_lines = [
        f"- {tid}: {entry.description[:400]}"
        for tid, entry in candidates.items()
        if tid != CONTAINER_TYPE
    ]
    # Short files get generous digests (concepts often live past the header);
    # long expedientes stay compact to keep the prompt cheap.
    digest_chars = 1600 if len(pages) <= 4 else _DIGEST_CHARS
    page_lines = []
    for p in pages:
        digest = _page_digest(p, digest_chars)
        page_lines.append(f"[pag {p.number}] {digest if digest else '(pagina sin texto / imagen)'}")

    rule_lines = [text for tid, text in _DISAMBIGUATION_RULES if tid is None or tid in candidates]
    # When the caller declared the candidate list, say so: the model's job stops
    # being "what is this?" and becomes "which of THESE, if any?". Everything
    # else stays OTRO, which `_sanitize` uses to keep page coverage complete.
    closed_list = (
        "La lista de tipos de arriba es CERRADA: si un documento no es ninguno "
        "de ellos, clasificalo como OTRO. No inventes tipos.\n\n"
        if allowed
        else ""
    )

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
        "REGLAS DE DESAMBIGUACION (importantes):\n" + "\n".join(rule_lines) + "\n\n"
        + closed_list
        + "Paginas:\n" + "\n".join(page_lines) + "\n\n"
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


def _declared_segments(
    pages: List[PageText], declared_type: str, total: int, *, deduplicate: bool = True
) -> List[Segment]:
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

    split = page_selector.split_instances(pages, spec, deduplicate=deduplicate)
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
    segments: List[Segment], pages: List[PageText], counts: Optional[Dict[str, Any]] = None
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
        requested = (counts or {}).get(seg.doc_type, 1)
        # `multiple`, `:all` and `:N>1` refer to physical occurrences. A
        # reprint therefore remains a document of the requested type; collapsing
        # it to OTRO would contradict the requested cardinality. Deduplication is
        # only part of the conservative `single` path, where it helps choose the
        # most complete printing before the first distinct document is kept.
        if requested == "all" or (
            isinstance(requested, int)
            and not isinstance(requested, bool)
            and requested > 1
        ):
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


def _split_typed_instances(
    segments: List[Segment],
    pages: List[PageText],
    counts: Optional[Dict[str, Any]] = None,
) -> List[Segment]:
    """Re-split typed model ranges using catalog document boundaries.

    Guided/automatic classification can correctly recognize a type but still
    merge consecutive documents into one range (two invoices on adjacent
    pages).  A type that declares `page_selection.multi_instance` already has
    deterministic boundaries, so apply them inside every classified range
    before extraction. One-page/uncertain ranges fail open unchanged.

    The caller's cardinality controls physical-copy handling exactly as in the
    manual path: single deduplicates before choosing the first; multiple/N/all
    keeps every occurrence. `_sanitize` runs again afterwards to restore OTRO
    coverage for any non-type pages that were inside an over-wide model range.
    """
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
        if not page_selector.enabled_for(entry):
            result.append(seg)
            continue
        spec = page_selector.spec_for(entry)
        if not page_selector.multi_instance_enabled(spec):
            result.append(seg)
            continue

        requested = (counts or {}).get(seg.doc_type, 1)
        seg_pages = [
            page for page in pages
            if seg.page_start <= page.number <= seg.page_end
        ]
        split = page_selector.split_instances(
            seg_pages, spec, deduplicate=requested == 1
        )
        if split.fail_open or len(split.spans) <= 1:
            result.append(seg)
            continue

        logger.info(
            "segmenter: rango %s %d-%d repartido determinísticamente en %s",
            seg.doc_type,
            seg.page_start,
            seg.page_end,
            split.ranges,
        )
        result.extend(
            Segment(
                seg.doc_type,
                start,
                end,
                seg.confidence,
                seg.subtype,
                seg.pedimento_reference,
                {**seg.raw, "deterministic_instance_split": True},
            )
            for start, end in split.ranges
        )
    return result


def _span_len(seg: Segment) -> int:
    return seg.page_end - seg.page_start + 1


def _apply_type_limits(
    segments: List[Segment], counts: Optional[Dict[str, Any]]
) -> Tuple[List[Segment], Dict[str, int]]:
    """Keep only the first N documents of each type the caller asked for.

    `counts` arrives resolved as TYPE -> `int >= 1` or `"all"`: per-type
    suffix, POST `occurrenceMode`, catalog `occurrence_mode`, then the global
    one-type/many-types default. `_apply_type_limits` still defaults an absent
    entry to one as a last-resort guard for direct/legacy callers.

    The excess is relabelled `OTRO` with subtype `LIMITE_ALCANZADO` so the
    segmenter's internal page map stays complete. On a closed request,
    `expediente._only_requested_segments` removes those audit-only segments
    before extraction and response assembly.

    "The first N" means the first in PAGE order, which is the order `_sanitize`
    already guarantees. Note that the winner among duplicate copies was already
    decided before this runs (`_collapse_duplicate_segments` keeps the LONGEST
    printing of a given identity, not the earliest), so "first" here means first
    distinct document, not first occurrence of any ink.

    Returns the segments plus how many were dropped per type, so the caller can
    report it instead of the caller having to re-derive it.
    """
    counts = counts or {}
    seen: Dict[str, int] = {}
    dropped: Dict[str, int] = {}
    result: List[Segment] = []
    for seg in segments:
        limit = counts.get(seg.doc_type, 1)
        if seg.doc_type == "OTRO" or limit == "all":
            result.append(seg)
            continue
        seen[seg.doc_type] = seen.get(seg.doc_type, 0) + 1
        if seen[seg.doc_type] <= limit:
            result.append(seg)
            continue
        dropped[seg.doc_type] = dropped.get(seg.doc_type, 0) + 1
        logger.info(
            "segmenter: %s #%d en paginas %d-%d excede el limite declarado (%s) -> OTRO",
            seg.doc_type, seen[seg.doc_type], seg.page_start, seg.page_end, limit,
        )
        result.append(
            Segment("OTRO", seg.page_start, seg.page_end, 0.0, "LIMITE_ALCANZADO")
        )
    return result, dropped


def segment_pages(
    pages: List[PageText],
    declared_type: Optional[str] = None,
    model_id: Optional[str] = None,
    declared_types: Optional[Sequence[str]] = None,
    declared_counts: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Segment], Dict[str, Any]]:
    """Split pages into typed segments.

    Three modes, decided by how many types the uploader declared:

    - **One** (`declared_type`, or a one-element `declared_types`) — no model
      call at all: the file is one segment, or, for a type that declares
      `page_selection.multi_instance`, one segment per instance found
      deterministically from its catalog boundaries. See `_declared_segments`.
    - **Several** — the model DOES run, but it may only choose among those
      types; anything else is forced to `OTRO`. This is the point of declaring
      a list: the caller knows what the bundle should contain, and saying so is
      both cheaper (a shorter prompt) and more accurate (no look-alike from the
      rest of the catalog to confuse it with).
    - **None** — the model chooses from every type that qualifies by catalog
      shape (`is_segmentation_candidate`).

    `CONTAINER_TYPE` is a DIRECTIVE, never a candidate: declaring it is the
    documented way to force multi-document segmentation without restricting
    anything. It is dropped from the candidate list, and when nothing else was
    declared alongside it the classification is left open — otherwise it would
    produce an EMPTY candidate list, and the strict guard below would then demote
    every document in the file to OTRO.

    `declared_types` is added at the end of the signature, so every existing
    caller passing `declared_type` keeps its exact behaviour.

    Returns `(segments, usage_meta)` — `usage_meta` is `{}` whenever no
    Bedrock call was made (single declared type, empty input), or
    `{"modelId": ..., "usage": {...}}` when one was, so callers can cost this
    step the same way `text_extractor` already exposes extraction usage
    (previously this call's tokens were only logged, never returned — a real
    gap against Cost Explorer, see `documentacion/COSTOS.md`).
    """
    total = len(pages)
    if total == 0:
        return [], {}
    types = normalize_declared_types(declared_types, declared_type)
    declared_counts = resolve_declared_counts(types, declared_counts)
    if len(types) == 1 and types[0] != CONTAINER_TYPE:
        requested = (declared_counts or {}).get(types[0], 1)
        deduplicate = requested == 1
        segments, _ = _apply_type_limits(
            _declared_segments(
                pages, types[0], total, deduplicate=deduplicate
            ),
            declared_counts,
        )
        return segments, {}

    allowed = [t for t in types if t != CONTAINER_TYPE] or None
    if allowed:
        logger.info("segmenter: clasificacion guiada, candidatos %s", allowed)

    if _should_window(total):
        segments, usage_meta = _classify_windowed(pages, allowed, model_id)
    else:
        documents, usage_meta = _classify_pages(pages, allowed, model_id)
        allowed_set = set(allowed) if allowed else None
        segments = _segments_from_documents(documents, allowed_set)

    if not segments:
        return [Segment("OTRO", 1, total, 0.0)], usage_meta
    sanitized = _sanitize(segments, total)
    split_segments = _split_typed_instances(sanitized, pages, declared_counts)
    result, _ = _apply_type_limits(
        _collapse_duplicate_segments(
            _sanitize(split_segments, total), pages, declared_counts
        ),
        declared_counts,
    )
    logger.info(
        "segmenter: %d segments over %d pages (usage=%s)",
        len(result), total, usage_meta.get("usage"),
    )
    return result, usage_meta
