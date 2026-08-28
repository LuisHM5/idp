"""Catalog-driven selection of the pages worth spending money on.

A 100-page upload whose catalog fields live on 1-3 pages currently pays
Textract OCR for every scanned page and Bedrock extraction for the full text
of every page. This module answers "which pages actually matter for this
document type" so the expensive stages can be pointed at those pages only.

Nothing here is specific to pedimentos or to any field name: a document type
opts in by declaring a `page_selection` block in its catalog JSON, exactly
the way `qr_recovery` already works. A type that does not declare one is
never filtered — the pipeline runs its current path byte for byte.

Two hard rules this module exists to guarantee:

1. **Fail-open.** If no page clears the scoring bar, EVERY page is selected
   (`fail_open=True`). A selector that found nothing must never be read as
   "nothing is relevant" — that would silently drop a document's content.
2. **Nothing is dropped silently.** `Selection` carries the per-page scores
   and the skipped page numbers so the result can surface them for a human
   to audit.

Set PAGE_SELECTION_ENABLED=false to disable this globally (emergency
rollback without a redeploy). That switch only ever turns selection OFF; it
never enables a type whose catalog left it off — see `enabled_for`.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Pattern, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Distinct markers a page must match to be treated as relevant. Mirrors
# qr_recovery's `page_marker_min_matches` default and the reasoning behind it:
# requiring two DISTINCT phrases survives OCR garbling any single one, while a
# lone coincidental match to one generic-ish phrase never clears the bar.
_DEFAULT_MIN_SCORE = 2
# Above this share of the document, the markers are not discriminating enough
# to be trusted, so the selection is DISCARDED and every page processed
# (fail-open) rather than trimmed. Trimming a selection has exactly the defect
# that blind page truncation has: it drops real content to hit a number. A
# real pedimento expediente measured 51 relevant pages out of 139 (three
# copies of the same pedimento, each with its PARTIDAS annexes) — a hard cap
# of 8 would have silently cut two thirds of its line items, while this ratio
# keeps the 63% saving. Above the ratio the saving is small anyway, so paying
# full price is the cheap side of the trade.
_DEFAULT_MAX_RATIO = 0.6
# Pages after an anchor to carry along: a document's fields routinely spill
# onto continuation pages that carry no header markers of their own (a
# pedimento's PARTIDAS being the case that motivated it).
_DEFAULT_FOLLOW_PAGES = 1


@dataclass
class PageScore:
    """How relevant one page looks, and why."""
    number: int                 # 1-based, same numbering as PageText.number
    score: int                  # count of DISTINCT markers matched
    matched: List[str] = field(default_factory=list)
    origin: str = ""            # PageText.source: "native" | "textract" | "empty"
    chars: int = 0              # length of the page's text (drives token cost)


@dataclass
class Selection:
    """Outcome of a selection pass over one document's pages."""
    selected: List[int]         # page numbers to spend money on, ascending
    skipped: List[int]          # page numbers deliberately left out, ascending
    fail_open: bool             # True => nothing matched, everything selected
    reason: str                 # human-readable why, for logs and the result
    scores: List[PageScore] = field(default_factory=list)

    @property
    def filtered(self) -> bool:
        """True only when this actually removed pages from consideration."""
        return bool(self.skipped)

    @property
    def chars_selected(self) -> int:
        keep = set(self.selected)
        return sum(s.chars for s in self.scores if s.number in keep)

    @property
    def chars_skipped(self) -> int:
        drop = set(self.skipped)
        return sum(s.chars for s in self.scores if s.number in drop)

    def savings(
        self,
        usage: Optional[Dict[str, Any]] = None,
        chunks_sent: int = 0,
        chunks_without_selection: int = 0,
    ) -> Dict[str, Any]:
        """What the filtering actually saved, priced off the REAL usage
        Bedrock billed for the pages that were sent.

        Two different derivations, because input and output scale with
        different things:

        - **Input** scales with page text. That text is the only part of the
          extraction prompt that changes between "filtered" and "unfiltered"
          (the schema/instructions prefix is byte-identical either way), so
          the tokens-per-character density of the call that DID run prices the
          characters that never went.
        - **Output** scales with CHUNKS, not characters: every chunk emits one
          full schema JSON regardless of how much text went in. So it is
          priced per avoided chunk — and that matters more than it looks,
          since output costs 5x input ($5.50 vs $1.10 per M tokens).

        Note the page saving and the token saving are NOT the same number:
        dropping 81% of the pages of a real expediente cut only ~68% of the
        input tokens, because the pages worth keeping are the text-dense ones.
        Quote the token figure for cost, the page figure for OCR volume.

        Everything prefixed `estimated` prices a call nobody made, so it is
        reported as an estimate and never mixed into the billed `usage`
        numbers, which stay exactly what Bedrock reported.
        """
        usage = usage or {}
        input_tokens = int(usage.get("inputTokens") or 0)
        output_tokens = int(usage.get("outputTokens") or 0)
        chars_sent = self.chars_selected
        chars_skipped = self.chars_skipped

        result: Dict[str, Any] = {
            "charsSent": chars_sent,
            "charsSkipped": chars_skipped,
            "billedInputTokens": input_tokens,
            "billedOutputTokens": output_tokens,
        }
        if chunks_sent:
            result["chunksSent"] = chunks_sent
        if chunks_without_selection:
            result["estimatedChunksWithoutSelection"] = chunks_without_selection

        if chars_sent > 0 and chars_skipped > 0 and input_tokens:
            saved_in = int(round(chars_skipped * (input_tokens / chars_sent)))
            result["estimatedInputTokensSaved"] = saved_in
            result["estimatedInputTokensWithoutSelection"] = input_tokens + saved_in
            result["estimatedInputSavingPct"] = round(
                saved_in / (input_tokens + saved_in) * 100, 1
            )

        if output_tokens and chunks_sent and chunks_without_selection > chunks_sent:
            saved_out = int(round(
                output_tokens * (chunks_without_selection - chunks_sent) / chunks_sent
            ))
            result["estimatedOutputTokensSaved"] = saved_out
            result["estimatedOutputTokensWithoutSelection"] = output_tokens + saved_out

        return result

    def to_json(
        self,
        usage: Optional[Dict[str, Any]] = None,
        chunks_sent: int = 0,
        chunks_without_selection: int = 0,
    ) -> Dict[str, Any]:
        """Auditable view for the pipeline result (see `pageSelection`).

        Pass the extraction call's real `usage` (and the chunk counts) to
        include the estimated token saving; without them only the page/char
        facts are reported.
        """
        payload: Dict[str, Any] = {
            "pagesConsidered": len(self.scores) or len(self.selected),
            "selected": self.selected,
            "skipped": self.skipped,
            "failOpen": self.fail_open,
            "reason": self.reason,
            # Only pages that matched something: a 100-entry list of zeroes
            # would bloat every stored result to say nothing.
            "scores": [
                {"page": s.number, "score": s.score, "matched": s.matched}
                for s in self.scores
                if s.score > 0
            ],
        }
        payload["savings"] = self.savings(usage, chunks_sent, chunks_without_selection)
        return payload


def env_enabled() -> bool:
    """Global kill-switch. False disables selection for every type."""
    return os.environ.get("PAGE_SELECTION_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def spec_for(type_entry: Any) -> Optional[Dict[str, Any]]:
    """The type's `page_selection` block, or None when it declares none."""
    spec = getattr(type_entry, "page_selection", None)
    return spec if isinstance(spec, dict) else None


def enabled_for(type_entry: Any) -> bool:
    """Whether page selection runs for this document type.

    Resolution order (most specific wins, except the kill-switch, which only
    ever turns things off):

    1. No `page_selection` in the catalog  -> off. This is the default, and
       it is what keeps every un-migrated type on its current code path.
    2. `page_selection` present            -> on (declaring it is opting in).
    3. `page_selection.enabled: false`     -> off for this type, while KEEPING
       its markers/config, so a misbehaving type can be parked without losing
       its calibration.
    4. PAGE_SELECTION_ENABLED=false        -> off everywhere, regardless.
    """
    if not env_enabled():
        return False
    spec = spec_for(type_entry)
    if spec is None:
        return False
    return bool(spec.get("enabled", True))


@lru_cache(maxsize=32)
def _compile(patterns: Tuple[str, ...]) -> Tuple[Tuple[str, Pattern[str]], ...]:
    """Compile a marker list once per distinct list (catalogs are reloaded on
    every invocation, so the same patterns are recompiled otherwise)."""
    compiled: List[Tuple[str, Pattern[str]]] = []
    for p in patterns:
        try:
            compiled.append((p, re.compile(p, re.IGNORECASE)))
        except re.error as e:
            logger.warning("page_selector: invalid marker pattern %r: %s", p, e)
    return tuple(compiled)


def score_pages(pages: Sequence[Any], markers: Sequence[str]) -> List[PageScore]:
    """Score every page by how many DISTINCT markers its text matches.

    `pages` are `PageText`-shaped (`.number`, `.text`, `.source`). Counting
    distinct patterns rather than total matches is what makes the score
    robust to OCR noise: losing one phrase to a garbled scan costs one point,
    not the whole page.
    """
    compiled = _compile(tuple(markers))
    scores: List[PageScore] = []
    for page in pages:
        text = getattr(page, "text", "") or ""
        matched = [name for name, pattern in compiled if pattern.search(text)]
        scores.append(
            PageScore(
                number=page.number,
                score=len(matched),
                matched=matched,
                origin=getattr(page, "source", "") or "",
                chars=len(text),
            )
        )
    return scores


def select(pages: Sequence[Any], spec: Optional[Dict[str, Any]]) -> Selection:
    """Pick the pages worth processing out of `pages`.

    Returns every page (with `fail_open=True`) whenever there is no usable
    spec or nothing clears `min_score` — never an empty or speculative
    selection. See the module docstring for why that is non-negotiable.
    """
    all_numbers = [p.number for p in pages]
    if not all_numbers:
        return Selection([], [], True, "documento sin páginas")

    markers = list((spec or {}).get("markers") or [])
    if not markers:
        return Selection(all_numbers, [], True, "el tipo no declara marcadores de página")

    min_score = int((spec or {}).get("min_score", _DEFAULT_MIN_SCORE))
    max_ratio = float((spec or {}).get("max_ratio", _DEFAULT_MAX_RATIO))
    max_pages = (spec or {}).get("max_pages")  # optional absolute cap, same fail-open semantics
    follow = max(0, int((spec or {}).get("follow_pages", _DEFAULT_FOLLOW_PAGES)))

    scores = score_pages(pages, markers)
    anchors = [s for s in scores if s.score >= min_score]
    if not anchors:
        return Selection(
            all_numbers, [], True,
            f"ninguna página alcanzó min_score={min_score}; se procesan todas",
            scores,
        )

    # Carry along each anchor's continuation pages: a document's fields spill
    # onto pages whose own header markers may be absent (a clean layout) or
    # garbled (a bad scan), and one extra page costs far less than a missing
    # PARTIDAS row.
    by_number = {s.number for s in scores}
    keep: Set[int] = set()
    for anchor in anchors:
        keep.add(anchor.number)
        for offset in range(1, follow + 1):
            if anchor.number + offset in by_number:
                keep.add(anchor.number + offset)

    selected = sorted(keep)
    ratio = len(selected) / len(all_numbers)
    over_ratio = ratio > max_ratio
    over_absolute = max_pages is not None and len(selected) > int(max_pages)
    if over_ratio or over_absolute:
        limit = f"max_ratio={max_ratio:.0%}" if over_ratio else f"max_pages={max_pages}"
        return Selection(
            all_numbers, [], True,
            f"selección demasiado amplia ({len(selected)}/{len(all_numbers)} = "
            f"{ratio:.0%} > {limit}); se procesan todas para no recortar contenido",
            scores,
        )

    skipped = [n for n in all_numbers if n not in keep]
    if not skipped:  # everything matched; nothing to save, and nothing dropped
        return Selection(all_numbers, [], False, "todas las páginas son relevantes", scores)

    return Selection(
        selected, skipped, False,
        f"{len(selected)}/{len(all_numbers)} páginas con score>={min_score} "
        f"(+{follow} de continuación)",
        scores,
    )


def filter_pages(pages: Sequence[Any], selection: Selection) -> List[Any]:
    """The `pages` whose numbers `selection` kept, in their original order."""
    keep = set(selection.selected)
    return [p for p in pages if p.number in keep]
