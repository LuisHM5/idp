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
# At or below this many pages, selection is skipped entirely: there is
# essentially nothing to save (a short document is one Bedrock chunk either
# way, so dropping pages saves input tokens but not a single call), while a
# miscalibrated marker could still drop a page that mattered. Bad trade —
# don't take it.
_DEFAULT_MIN_PAGES = 5


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


# --- document boundaries ---------------------------------------------------
#
# Scoring says "this page looks like the type". Boundaries say "this document
# runs from HERE to HERE", which is strictly better information: it recovers a
# page the scoring missed (a scanned first page with no text to score) and
# drops a page the scoring wrongly kept (the unrelated document right after
# the last one, pulled in by `follow_pages`). Both were measured on real
# expedientes — see `docs` note in catalogs/README.md.
#
# `page_of_pattern` is the strongest of the three: with two capture groups
# (current, total) ANY page of a document reveals the whole span, so a
# pedimento whose "Página 1 de 5" cover page was scanned into an empty text
# layer is still recovered from its "Página 5 de 5" last page.


def _boundaries(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The declared boundary config, or {} when the type declares none."""
    declared = {
        "start": list(spec.get("start_markers") or []),
        "end": list(spec.get("end_markers") or []),
        "page_of": spec.get("page_of_pattern") or "",
    }
    return declared if any(declared.values()) else {}


def _page_of(text: str, pattern: Pattern[str]) -> Optional[Tuple[int, int]]:
    """(current, total) from a "Página 3 de 8" style label, if present."""
    m = pattern.search(text or "")
    if not m or len(m.groups()) < 2:
        return None
    try:
        current, total = int(m.group(1)), int(m.group(2))
    except (TypeError, ValueError):
        return None
    # A label that doesn't describe a sane position is worse than no label.
    if current < 1 or total < 1 or current > total:
        return None
    return current, total


def _spans_from_boundaries(
    anchors: List[PageScore],
    texts: Dict[int, str],
    bounds: Dict[str, Any],
    total_pages: int,
) -> Set[int]:
    """Grow each confirmed page into the full document it belongs to.

    Anchors come from marker scoring, so every span starts from a page already
    confirmed as this type — that is what stops an unrelated document's own
    "Página 1 de 6" (a CFDI, say) from opening a span of its own.
    """
    page_of_re = _compile((bounds["page_of"],))[0][1] if bounds["page_of"] else None
    start_res = [r for _, r in _compile(tuple(bounds["start"]))]
    end_res = [r for _, r in _compile(tuple(bounds["end"]))]

    keep: Set[int] = set()
    for anchor in anchors:
        n = anchor.number
        keep.add(n)
        span = _page_of(texts.get(n, ""), page_of_re) if page_of_re else None
        if span:
            current, count = span
            start = max(1, n - current + 1)
            keep |= set(range(start, min(total_pages, start + count - 1) + 1))

    if not keep:
        return keep

    # An explicit start/end marker inside the neighbourhood corrects a span the
    # page label got wrong (or supplies one when there is no label at all): a
    # document runs from its start marker to its end marker inclusive.
    if start_res or end_res:
        anchor_numbers = {a.number for a in anchors}
        starts = sorted(n for n in anchor_numbers if any(r.search(texts.get(n, "")) for r in start_res))
        ends = sorted(n for n in anchor_numbers if any(r.search(texts.get(n, "")) for r in end_res))
        for start in starts:
            closing = next((e for e in ends if e >= start), None)
            if closing is not None:
                keep |= set(range(start, closing + 1))
        # An end marker with no start marker before it still closes something:
        # keep everything from the previous end (exclusive) that was already
        # selected, so a document whose opening page never scored isn't cut.
        for end in ends:
            if not any(s <= end for s in starts):
                keep.add(end)
    return keep


# --- repeated copies of the SAME document ----------------------------------
#
# A real bundle prints the same pedimento more than once (agent's copy,
# importer's copy, the one-page simplified form). Measured on
# test-docs/full-dataset-pedimentos/'26 07 3435 6041353 CG.pdf': the selection
# keeps pages 3-53, which are FOUR renderings of pedimento 260734356041353
# (3-19, 20-36, 37-52, and the 1-page FORMA SIMPLIFICADA on 53). All four were
# extracted — 7 Bedrock chunks — and the row merge then deduplicated the
# identical invoice rows: paying three times to throw two away.
#
# A type opts in by declaring `duplicate_key_pattern`: a regex whose first
# group captures the identity printed on the document (the pedimento number).
# Copies sharing an identity collapse to ONE. Fail-open like everything else
# here: a span whose identity can't be read is always kept, and so is every
# span when no pattern is declared.


def _copy_spans(kept: Sequence[int], texts: Dict[int, str], bounds: Dict[str, Any]) -> List[List[int]]:
    """Split the kept pages into the individual documents they make up.

    A new document starts where its own first page does — a "Página 1 de N"
    label, or a declared `start_marker`. Pages before the first such signal
    form a span of their own rather than being attached to the next document.

    The page label WINS over the start markers when both are present: a page
    that says "Page 2 of 2" is not the start of anything, however faithfully
    it reprints its document's header. Without that precedence a vendor who
    repeats "Invoice #" on every page would have each of them opened as its
    own document.
    """
    page_of_re = _compile((bounds["page_of"],))[0][1] if bounds.get("page_of") else None
    start_res = [r for _, r in _compile(tuple(bounds.get("start") or ()))]

    spans: List[List[int]] = []
    for number in kept:
        text = texts.get(number, "")
        page_of = _page_of(text, page_of_re) if page_of_re else None
        if page_of is not None:
            opens = page_of[0] == 1
        else:
            opens = any(r.search(text) for r in start_res)
        if opens or not spans:
            spans.append([number])
        else:
            spans[-1].append(number)
    return spans


def _identity_patterns(pattern: Any) -> Tuple[Pattern[str], ...]:
    """Compile `duplicate_key_pattern`, which is a regex OR a list of them.

    A list rather than one alternation on purpose: with `(?:A(x)|B(y))` the
    captured identity lands in group 1 only for the FIRST branch, so a single
    regex cannot express "the invoice number, printed under any of these
    labels". Each pattern is tried in order and the first one that captures
    wins. An invalid pattern is skipped with a warning, never raised.
    """
    raw = pattern if isinstance(pattern, (list, tuple)) else [pattern]
    compiled: List[Pattern[str]] = []
    for p in raw:
        if not isinstance(p, str) or not p:
            continue
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logger.warning("page_selector: invalid duplicate_key_pattern %r: %s", p, e)
    return tuple(compiled)


def _identity_of(span: Sequence[int], texts: Dict[int, str], patterns: Tuple[Pattern[str], ...]) -> Optional[str]:
    """The identity printed on this document, or None when unreadable.

    Normalized to uppercase alphanumerics — NOT digits-only. For a pedimento
    (whose pattern captures digits and spaces) the result is byte-identical to
    the old `re.sub(r"\\D", "", ...)`, while an invoice number keeps the letters
    that distinguish it: `SJ191703` stays `SJ191703` instead of collapsing to
    `191703`, which another document could legitimately carry.
    """
    for number in span:
        text = texts.get(number, "")
        for identity in patterns:
            m = identity.search(text)
            if m and m.groups():
                key = re.sub(r"[^A-Z0-9]", "", (m.group(1) or "").upper())
                if key:
                    return key
    return None


def _group_by_identity(
    spans: List[List[int]], texts: Dict[int, str], patterns: Tuple[Pattern[str], ...]
) -> Tuple[List[List[int]], int]:
    """Collapse spans that are copies of the same document, keeping the
    LONGEST one per identity (ties: the earliest).

    Longest, not first, on purpose: a one-page summary form of the same
    pedimento can legitimately come before the full print, and keeping it
    while dropping the full document would lose real content — the exact
    failure this must not cause.

    Returns `(surviving spans in page order, copies dropped)`. A span whose
    identity can't be read is ALWAYS kept — fail-open, like the rest of this
    module.
    """
    winners: Dict[str, List[int]] = {}
    survivors: List[List[int]] = []
    dropped = 0
    for span in spans:
        key = _identity_of(span, texts, patterns)
        if key is None:  # identity unreadable — never drop it
            survivors.append(span)
            continue
        previous = winners.get(key)
        if previous is None:
            winners[key] = span
            survivors.append(span)
        elif len(span) > len(previous):
            survivors[survivors.index(previous)] = span
            winners[key] = span
            dropped += 1
        else:
            dropped += 1
    survivors.sort(key=lambda s: s[0] if s else 0)
    return survivors, dropped


def _drop_duplicate_copies(
    keep: Set[int], texts: Dict[int, str], bounds: Dict[str, Any], pattern: Any
) -> Tuple[Set[int], int]:
    """`_group_by_identity` over the kept pages, flattened back to a page set."""
    patterns = _identity_patterns(pattern)
    if not patterns:
        return keep, 0
    spans = _copy_spans(sorted(keep), texts, bounds)
    survivors, dropped = _group_by_identity(spans, texts, patterns)
    result: Set[int] = set()
    for span in survivors:
        result |= set(span)
    return result, dropped


def _expected_max_pages(expected: Any) -> Optional[int]:
    """Upper bound of a type's declared `expected_pages`, or None.

    Accepts a plain count (`5`) or a range (`[3, 6]`) — a range is the honest
    shape for a document whose length varies with its annexes, and only its
    top matters here: an upload no bigger than a full-size instance of this
    type is that document, not a bundle containing it.
    """
    if isinstance(expected, bool) or expected is None:
        return None
    if isinstance(expected, (int, float)):
        return int(expected)
    if isinstance(expected, (list, tuple)) and expected:
        try:
            return int(max(expected))
        except (TypeError, ValueError):
            return None
    return None


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

    # Cheap exits BEFORE any scoring: when the upload is already just this
    # document, filtering it can only lose pages, never save meaningful cost.
    total = len(all_numbers)
    min_pages = int((spec or {}).get("min_pages", _DEFAULT_MIN_PAGES))
    if total <= min_pages:
        return Selection(
            all_numbers, [], False,
            f"documento de {total} páginas (<= min_pages={min_pages}): se procesa completo",
        )

    expected_max = _expected_max_pages((spec or {}).get("expected_pages"))
    if expected_max is not None and total <= expected_max:
        return Selection(
            all_numbers, [], False,
            f"{total} páginas caben en el tamaño esperado del tipo "
            f"(expected_pages<={expected_max}): el archivo ES este documento, se procesa completo",
        )

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

    bounds = _boundaries(spec or {})
    if bounds:
        # The type tells us where its documents begin and end, so grow each
        # confirmed page into its whole document instead of guessing with a
        # fixed `follow_pages` window — exact where the window was approximate.
        texts = {getattr(pg, "number", 0): (getattr(pg, "text", "") or "") for pg in pages}
        keep = _spans_from_boundaries(anchors, texts, bounds, len(all_numbers))
        strategy = "límites declarados (inicio/fin/página X de N)"
        duplicate_key = (spec or {}).get("duplicate_key_pattern")
        if duplicate_key:
            keep, duplicates = _drop_duplicate_copies(keep, texts, bounds, duplicate_key)
            if duplicates:
                strategy += f", {duplicates} copia(s) del mismo documento descartada(s)"
    else:
        # Carry along each anchor's continuation pages: a document's fields
        # spill onto pages whose own header markers may be absent (a clean
        # layout) or garbled (a bad scan), and one extra page costs far less
        # than a missing PARTIDAS row.
        by_number = {s.number for s in scores}
        keep = set()
        for anchor in anchors:
            keep.add(anchor.number)
            for offset in range(1, follow + 1):
                if anchor.number + offset in by_number:
                    keep.add(anchor.number + offset)
        strategy = f"+{follow} de continuación"

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
        f"{len(selected)}/{len(all_numbers)} páginas con score>={min_score} ({strategy})",
        scores,
    )


def filter_pages(pages: Sequence[Any], selection: Selection) -> List[Any]:
    """The `pages` whose numbers `selection` kept, in their original order."""
    keep = set(selection.selected)
    return [p for p in pages if p.number in keep]


# --- N instances of the SAME type in one upload ----------------------------
#
# `select()` answers "which pages of this document are worth paying for" and
# returns ONE set. That is the right question for a pedimento: an upload holds
# one of them (plus reprints of it). It is the wrong question for a foreign
# invoice, where one PDF routinely holds several DIFFERENT invoices — a
# 2-page file that is two complete invoices, or an expediente carrying three
# from three vendors. Collapsing those into one document loses every number
# but the first.
#
# `split_instances` answers the other question: WHERE DOES EACH ONE START AND
# END. It reuses the same declared boundaries (`start_markers`,
# `page_of_pattern`) and the same identity (`duplicate_key_pattern`) the
# selector already uses, so a type calibrates once. It is deterministic and
# free — pure regex over text Textract/pypdf already produced, no model call.

# Above this many instances the markers are not describing documents any more
# (a generic marker matching every page would emit one segment per page, and
# each segment is a real Bedrock call). Fail open instead.
_DEFAULT_MAX_INSTANCES = 20


@dataclass
class InstanceSplit:
    """Where each instance of a type lives inside one upload."""
    spans: List[List[int]] = field(default_factory=list)  # page numbers, ascending
    duplicates_dropped: int = 0
    fail_open: bool = True    # True => "treat the whole file as one document"
    reason: str = ""

    @property
    def ranges(self) -> List[Tuple[int, int]]:
        """Each instance as an inclusive `(page_start, page_end)` pair."""
        return [(s[0], s[-1]) for s in self.spans if s]


def multi_instance_enabled(spec: Optional[Dict[str, Any]]) -> bool:
    """Whether this type says one upload may hold several of it.

    Opt-in and off by default: without `multi_instance` every type keeps
    today's "the declared type is the whole file" behaviour exactly.
    """
    return bool((spec or {}).get("multi_instance"))


def _multi_instance_options(spec: Dict[str, Any]) -> Dict[str, Any]:
    """`multi_instance` knobs. The flag is `true` (all defaults) or an object.

    They live in their own block rather than alongside `min_score`/`max_ratio`
    because `follow_pages` means something different here: in `select()` a
    declared boundary REPLACES it, while an instance genuinely needs it to
    reach a continuation page that carries neither a header marker nor a page
    label.
    """
    options = spec.get("multi_instance")
    return options if isinstance(options, dict) else {}


def identity_in(texts: Dict[int, str], span: Sequence[int], spec: Optional[Dict[str, Any]]) -> Optional[str]:
    """The identity this type prints on the document covering `span`, or None.

    The public door to `duplicate_key_pattern` for callers outside this module
    (`segmenter._collapse_duplicate_segments` reads it off the segments the
    classifier returned), so "what identifies one document of this type" has a
    single implementation and a single normalization.
    """
    patterns = _identity_patterns((spec or {}).get("duplicate_key_pattern"))
    if not patterns:
        return None
    return _identity_of(span, texts, patterns)


def _extend_spans(spans: List[List[int]], all_numbers: List[int], follow: int) -> List[List[int]]:
    """Carry each instance's continuation pages along, without crossing into
    the next instance.

    A second page that repeats no header and carries no "Page 2 of 2" label is
    invisible to both scoring and boundaries, so it would be dropped — and a
    dropped page is a lost line item. The hard stop is the next instance's own
    first page: one instance must never swallow the one after it.
    """
    if follow <= 0 or not spans:
        return spans
    order = {n: i for i, n in enumerate(all_numbers)}
    starts = [s[0] for s in spans]
    extended: List[List[int]] = []
    for i, span in enumerate(spans):
        limit = order[starts[i + 1]] if i + 1 < len(spans) else len(all_numbers)
        pages = set(span)
        cursor = order[span[-1]] + 1
        for _ in range(follow):
            if cursor >= limit:
                break
            pages.add(all_numbers[cursor])
            cursor += 1
        extended.append(sorted(pages))
    return extended


def split_instances(
    pages: Sequence[Any],
    spec: Optional[Dict[str, Any]],
    *,
    deduplicate: bool = True,
) -> InstanceSplit:
    """Find each instance of the declared type inside `pages`.

    Deliberately does NOT honour `min_pages`/`expected_pages`: those are cost
    shortcuts that say "this file is small enough that filtering can only lose
    pages", which has nothing to do with how many documents the file holds — a
    2-page upload that is two complete invoices still has to split in two.
    `max_ratio` is skipped for the same reason: a file that IS entirely
    instances of this type is the normal case here, not a warning sign.

    Fail-open, like everything in this module. Every "don't know" answer comes
    back as `fail_open=True` with a single span covering every page, which is
    byte-for-byte the behaviour of not calling this at all:

    - the type declares no `markers`, or no boundary at all (`start_markers` /
      `page_of_pattern`) — with nothing to open a document on, every page would
      land in one span anyway;
    - no page clears `min_score`;
    - only one instance was found spanning everything;
    - more than `max_instances` were found, i.e. the markers are matching
      pages rather than documents.
    """
    all_numbers = [p.number for p in pages]
    if not all_numbers:
        return InstanceSplit([], 0, True, "documento sin páginas")

    def _whole(reason: str) -> InstanceSplit:
        return InstanceSplit([list(all_numbers)], 0, True, reason)

    spec = spec or {}
    markers = list(spec.get("markers") or [])
    if not markers:
        return _whole("el tipo no declara marcadores de página")

    bounds = _boundaries(spec)
    if not bounds.get("start") and not bounds.get("page_of"):
        return _whole(
            "el tipo no declara dónde empieza cada documento "
            "(start_markers / page_of_pattern): no hay cómo separarlos"
        )

    min_score = int(spec.get("min_score", _DEFAULT_MIN_SCORE))
    scores = score_pages(pages, markers)
    anchors = [s for s in scores if s.score >= min_score]
    if not anchors:
        return _whole(f"ninguna página alcanzó min_score={min_score}")

    options = _multi_instance_options(spec)
    texts = {getattr(pg, "number", 0): (getattr(pg, "text", "") or "") for pg in pages}
    keep = _spans_from_boundaries(anchors, texts, bounds, len(all_numbers))
    spans = _copy_spans(sorted(keep), texts, bounds)

    # Deduplicate BEFORE extending, never after: "longest span wins" has to
    # compare the documents as the page markers found them. An annotated copy
    # that happens to be followed by a blank page would otherwise outgrow the
    # original by one continuation page and win, keeping the copy and dropping
    # the document it copies.
    dropped = 0
    duplicate_key = spec.get("duplicate_key_pattern")
    if deduplicate and duplicate_key:
        patterns = _identity_patterns(duplicate_key)
        if patterns:
            spans, dropped = _group_by_identity(spans, texts, patterns)

    if not spans:
        return _whole("no se pudo delimitar ningún documento")

    spans = _extend_spans(spans, all_numbers, max(0, int(options.get("follow_pages", 0))))

    max_instances = int(options.get("max_instances", _DEFAULT_MAX_INSTANCES))
    if len(spans) > max_instances:
        return _whole(
            f"{len(spans)} instancias detectadas (> max_instances={max_instances}): "
            "los marcadores están señalando páginas, no documentos"
        )

    if len(spans) == 1 and len(spans[0]) == len(all_numbers):
        return _whole("una sola instancia que ocupa todo el archivo")

    reason = f"{len(spans)} instancia(s) delimitada(s) por marcadores de inicio"
    if dropped:
        reason += f", {dropped} copia(s) del mismo documento descartada(s)"
    return InstanceSplit(spans, dropped, False, reason)
