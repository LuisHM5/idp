"""Fallback locator: ask the model which pages belong to a document type.

`page_selector` decides by regex markers, which is free and exact — but only
for types whose pages announce themselves with stable phrases. Some documents
have no such phrase, or vary so much in length that no `expected_pages` is
honest. For those, a type declares `expected_pages: "auto"` and this module
takes over WHEN THE MARKERS FAIL: one cheap Bedrock call reads a compact
digest of each page and answers "which page ranges are this type?".

Why this is worth a model call at all: failing open means sending every
page's FULL text to extraction. This call sends ~450 characters per page
instead, so on a real 86-page expediente it costs roughly $0.012 against the
~$0.09 of extracting everything — and it only ever runs when the free path
already gave up.

It reuses `segmenter._page_digest` deliberately: that digest already carries
the deterministic marker tags the segmenter relies on, and a second, subtly
different digest format would drift from it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bedrock import converse, parse_json_response, resolve_model
from .page_selector import Selection

logger = logging.getLogger(__name__)

# Same budget the segmenter uses for its own page digests.
_MAX_PAGES_IN_PROMPT = 400


def wants_locator(spec: Optional[Dict[str, Any]]) -> bool:
    """True when the type declared a VARIABLE page count instead of a fixed one.

    `expected_pages: "auto"` (or "variable") is how a catalog says "I can't
    give you a page range — work it out". Any other value is a fixed size and
    is handled deterministically by `page_selector`, with no model call.
    """
    expected = (spec or {}).get("expected_pages")
    return isinstance(expected, str) and expected.strip().lower() in ("auto", "variable")


def _build_prompt(pages: Sequence[Any], type_entry: Any) -> str:
    from .segmenter import _page_digest

    type_id = getattr(type_entry, "type_id", "") or ""
    description = (getattr(type_entry, "description", "") or "")[:400]
    field_tree = getattr(type_entry, "field_tree", None) or {}
    fields = list(field_tree.keys()) or list(getattr(type_entry, "extraction_fields", []) or [])

    page_lines = [
        f"[pag {p.number}] {_page_digest(p) or '(pagina sin texto / imagen)'}"
        for p in pages[:_MAX_PAGES_IN_PROMPT]
    ]
    return (
        f"Un PDF contiene varios documentos concatenados. Localiza UNICAMENTE las "
        f"paginas que pertenecen a un documento de tipo {type_id}.\n\n"
        f"{type_id}: {description}\n"
        f"Campos que se extraen de este tipo: {', '.join(fields[:40])}\n\n"
        "Te doy el inicio del texto de cada pagina. Devuelve los rangos de paginas "
        "que son de ese tipo, INCLUYENDO sus paginas de continuacion (anexos, "
        "partidas, renglones que siguen sin repetir el encabezado). Si el mismo "
        "documento aparece repetido (copias), incluye TODOS los rangos.\n"
        "Si NINGUNA pagina es de ese tipo, devuelve una lista vacia.\n\n"
        "Paginas:\n" + "\n".join(page_lines) + "\n\n"
        "Responde SOLO un JSON: {\"ranges\": [{\"page_start\": 4, \"page_end\": 18, "
        "\"confidence\": 0.9}]}. confidence entre 0 y 1."
    )


def locate(
    pages: Sequence[Any],
    type_entry: Any,
    model_id: Optional[str] = None,
) -> Tuple[Optional[Selection], Dict[str, Any]]:
    """Ask the model which pages are this type. Returns (selection, usage_meta).

    `selection` is None whenever the answer can't be trusted — unparseable
    output, no ranges, or a range covering (nearly) everything, which carries
    no information and would only add cost. The caller then keeps whatever
    `page_selector` decided, which is always "process every page". A locator
    that isn't sure must never be the reason a page gets dropped.
    """
    all_numbers = [p.number for p in pages]
    if not all_numbers:
        return None, {}

    used_model_id = model_id or resolve_model("locator", type_entry)
    try:
        text, meta = converse(
            _build_prompt(pages, type_entry),
            model_id=used_model_id,
            max_tokens=2048,
            stage="locator",
        )
    except Exception as e:
        logger.warning("page_locator: la llamada falló (%s); se procesan todas las páginas", e)
        return None, {}

    usage_meta = {"modelId": meta["modelId"], "usage": meta.get("usage") or {}}
    try:
        parsed = parse_json_response(text)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("page_locator: salida no parseable (%s); se procesan todas", e)
        return None, usage_meta

    valid = set(all_numbers)
    keep: set = set()
    confidences: List[float] = []
    for r in parsed.get("ranges", []):
        if not isinstance(r, dict):
            continue
        try:
            start, end = int(r.get("page_start", 0)), int(r.get("page_end", 0))
            confidences.append(float(r.get("confidence", 0.5)))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        keep |= {n for n in range(start, end + 1) if n in valid}

    if not keep:
        logger.info("page_locator: el modelo no ubicó ninguna página del tipo; se procesan todas")
        return None, usage_meta

    skipped = [n for n in all_numbers if n not in keep]
    if not skipped:
        # "Todo es del tipo" no aporta nada sobre procesar todo, y fingir que
        # hubo selección ensuciaría el reporte de ahorro con un cero.
        return None, usage_meta

    confidence = min(confidences) if confidences else 0.0
    selected = sorted(keep)
    logger.info(
        "page_locator: %d/%d páginas ubicadas por el modelo (confianza %.2f)",
        len(selected), len(all_numbers), confidence,
    )
    return Selection(
        selected, skipped, False,
        f"{len(selected)}/{len(all_numbers)} páginas ubicadas por el modelo "
        f"(expected_pages='auto', confianza {confidence:.2f})",
    ), usage_meta
