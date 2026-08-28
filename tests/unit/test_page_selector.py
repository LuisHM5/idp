"""Tests for catalog-driven page selection (pipeline/page_selector.py).

The two invariants worth protecting here are the ones that keep this from
silently losing a document's content:

- **fail-open**: anything the selector can't confidently narrow down comes
  back as "process every page", never as an empty or partial selection;
- **no trimming**: a selection that grows past the ratio is DISCARDED, not
  cut down to size — trimming would drop real pages to hit a number, which
  is the same defect as blind page truncation.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline import page_selector  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

SPEC = {
    "markers": [r"NUM\.?\s*PEDIMENTO", r"CVE\.?\s*PEDIM", r"T\.?\s*OPER"],
    "min_score": 2,
    "follow_pages": 0,
    "max_ratio": 0.6,
}


def _page(number, text, source="native"):
    """PageText-shaped stub — the selector only reads number/text/source."""
    return SimpleNamespace(number=number, text=text, source=source)


def _doc(hits, total, hit_text="NUM. PEDIMENTO CVE. PEDIM T.OPER"):
    return [
        _page(n, hit_text if n in hits else "texto irrelevante de otro documento")
        for n in range(1, total + 1)
    ]


# --- selection ------------------------------------------------------------

def test_selects_only_the_pages_that_clear_min_score():
    selection = page_selector.select(_doc({4, 5}, 20), SPEC)
    assert selection.selected == [4, 5]
    assert selection.fail_open is False
    assert len(selection.skipped) == 18


def test_follow_pages_carries_continuation_pages():
    """A continuation page holds fields but repeats no header markers."""
    spec = {**SPEC, "follow_pages": 1}
    selection = page_selector.select(_doc({4}, 20), spec)
    assert selection.selected == [4, 5]


def test_follow_pages_never_runs_past_the_last_page():
    spec = {**SPEC, "follow_pages": 3}
    selection = page_selector.select(_doc({10}, 10), spec)
    assert selection.selected == [10]


def test_score_counts_distinct_markers_not_repeats():
    """Ten copies of ONE phrase is one point — that's what stops a single
    generic-ish marker from carrying a page over the bar on its own."""
    pages = [_page(1, "NUM. PEDIMENTO " * 10)]
    scores = page_selector.score_pages(pages, SPEC["markers"])
    assert scores[0].score == 1


# --- fail-open ------------------------------------------------------------

def test_fail_open_when_nothing_matches():
    selection = page_selector.select(_doc(set(), 30), SPEC)
    assert selection.fail_open is True
    assert selection.selected == list(range(1, 31))
    assert selection.skipped == []


def test_fail_open_without_markers():
    selection = page_selector.select(_doc({1}, 5), {"min_score": 2})
    assert selection.fail_open is True
    assert len(selection.selected) == 5


def test_fail_open_discards_an_over_wide_selection_instead_of_trimming():
    """7 of 10 pages match (70% > 60%): process everything rather than cut.
    Trimming here would drop 1 real page to satisfy a threshold."""
    selection = page_selector.select(_doc(set(range(1, 8)), 10), SPEC)
    assert selection.fail_open is True
    assert len(selection.selected) == 10
    assert "demasiado amplia" in selection.reason


def test_wide_but_within_ratio_still_saves():
    """The real 139-page expediente had 51 relevant pages (37%) — a hard
    count cap would have cut its PARTIDAS; the ratio keeps the saving."""
    selection = page_selector.select(_doc(set(range(3, 54)), 139), SPEC)
    assert selection.fail_open is False
    assert len(selection.selected) == 51


def test_optional_absolute_cap_also_fails_open_rather_than_trimming():
    spec = {**SPEC, "max_pages": 3}
    selection = page_selector.select(_doc({1, 2, 3, 4}, 100), spec)
    assert selection.fail_open is True
    assert len(selection.selected) == 100


def test_no_pages_at_all():
    selection = page_selector.select([], SPEC)
    assert selection.fail_open is True
    assert selection.selected == []


def test_invalid_regex_is_skipped_not_fatal():
    """A typo'd marker must degrade that one pattern, never crash a document."""
    spec = {**SPEC, "markers": ["NUM\\.?\\s*PEDIMENTO", "(sin cerrar", "CVE\\.?\\s*PEDIM"]}
    selection = page_selector.select(_doc({2}, 10), spec)
    assert selection.selected == [2]


# --- enablement chain -----------------------------------------------------

def test_type_without_page_selection_is_never_filtered():
    entry = _parse_entry("SIN_SELECCION", {"fields": {}})
    assert page_selector.enabled_for(entry) is False


def test_declaring_the_block_opts_the_type_in():
    entry = _parse_entry("CON_SELECCION", {"fields": {}, "page_selection": SPEC})
    assert page_selector.enabled_for(entry) is True


def test_enabled_false_parks_a_type_but_keeps_its_config():
    entry = _parse_entry("PARQUEADO", {"fields": {}, "page_selection": {**SPEC, "enabled": False}})
    assert page_selector.enabled_for(entry) is False
    assert page_selector.spec_for(entry)["markers"] == SPEC["markers"]


def test_env_kill_switch_only_ever_disables(monkeypatch):
    entry = _parse_entry("CON_SELECCION", {"fields": {}, "page_selection": SPEC})
    monkeypatch.setenv("PAGE_SELECTION_ENABLED", "false")
    assert page_selector.enabled_for(entry) is False
    # ...and it cannot switch ON a type whose catalog left it off.
    parked = _parse_entry("PARQUEADO", {"fields": {}, "page_selection": {**SPEC, "enabled": False}})
    monkeypatch.setenv("PAGE_SELECTION_ENABLED", "true")
    assert page_selector.enabled_for(parked) is False


# --- the real catalog -----------------------------------------------------

def test_pedimento_markers_hit_a_pedimento_page_and_miss_a_vucem_page():
    """Guards the calibration: with min_score=3 a VUCEM/manifestación page
    must stay under the bar, or the selector starts keeping junk pages."""
    catalog = json.loads((REPO_ROOT / "catalogs" / "PEDIMENTO_ADUANAL.json").read_text())
    spec = catalog["PEDIMENTO_ADUANAL"]["page_selection"]

    pedimento = _page(1, "NUM. PEDIMENTO: 26 07 3435 6041248 T.OPER IMP CVE. PEDIMENTO: A1 REGIMEN:IMD")
    vucem = _page(2, "Ventanilla digital mexicana de comercio exterior — Acuse de digitalización")

    scores = {s.number: s.score for s in page_selector.score_pages([pedimento, vucem], spec["markers"])}
    assert scores[1] >= spec["min_score"]
    assert scores[2] < spec["min_score"]


# --- token accounting ------------------------------------------------------

def test_savings_reports_billed_tokens_verbatim():
    """Billed figures come from Bedrock and must never be adjusted — only the
    `estimated*` fields are derived."""
    selection = page_selector.select(_doc({1}, 10), SPEC)
    savings = selection.savings({"inputTokens": 1234, "outputTokens": 56})
    assert savings["billedInputTokens"] == 1234
    assert savings["billedOutputTokens"] == 56


def test_input_saving_scales_with_characters_not_pages():
    """One dense selected page vs. nine thin skipped ones: the token saving
    must follow the TEXT, not the 90%-of-pages figure."""
    pages = [_page(1, "NUM. PEDIMENTO CVE. PEDIM " * 400)] + [
        _page(n, "x") for n in range(2, 11)
    ]
    selection = page_selector.select(pages, SPEC)
    assert selection.selected == [1]
    savings = selection.savings({"inputTokens": 10_000, "outputTokens": 500})
    # 9 pages dropped (90%) but they carry almost no text, so barely any tokens.
    assert savings["estimatedInputSavingPct"] < 1


def test_output_saving_is_priced_per_avoided_chunk():
    """Output scales with chunks (one schema JSON each), not characters."""
    selection = page_selector.select(_doc({1, 2}, 10), SPEC)
    savings = selection.savings(
        {"inputTokens": 5_000, "outputTokens": 900},
        chunks_sent=1,
        chunks_without_selection=4,
    )
    assert savings["estimatedOutputTokensSaved"] == 2700       # 3 chunks avoided
    assert savings["estimatedOutputTokensWithoutSelection"] == 3600


def test_no_estimate_without_usage():
    """Never invent a saving figure when the call's usage isn't known (e.g.
    the extraction raised) — the page facts still get reported."""
    selection = page_selector.select(_doc({1, 2}, 10), SPEC)
    payload = selection.to_json()
    assert "estimatedInputTokensSaved" not in payload["savings"]
    assert payload["savings"]["charsSkipped"] > 0


def test_estimate_tracks_the_real_counterfactual():
    """Guards the accuracy claim: the estimate is derived from the density of
    the call that ran, so it must land close to what the unfiltered call would
    genuinely have cost. Modelled with a fixed tokens-per-char rate."""
    pages = [
        _page(n, "NUM. PEDIMENTO CVE. PEDIM T.OPER " * 30 if n <= 3 else "relleno " * 200)
        for n in range(1, 11)
    ]
    selection = page_selector.select(pages, SPEC)
    assert selection.selected == [1, 2, 3]

    rate = 1 / 3.6
    sent = sum(len(p.text) for p in pages if p.number in selection.selected)
    everything = sum(len(p.text) for p in pages)
    savings = selection.savings({"inputTokens": int(sent * rate), "outputTokens": 900})

    real = int(everything * rate)
    estimate = savings["estimatedInputTokensWithoutSelection"]
    assert abs(estimate - real) / real < 0.02


# --- truncation vs selection ----------------------------------------------

def test_a_selecting_type_is_not_blindly_truncated():
    """Truncation runs BEFORE the selector, so leaving both on would let a
    68-page upload be cut to 50 before the selector ever sees pages 51-68 —
    a pedimento on page 63 would be gone. The two guards are exclusive."""
    from pipeline import expediente

    cap, selection_drives = expediente._page_cap_for("PEDIMENTO_ADUANAL")
    assert selection_drives is True
    assert cap > expediente._MAX_PAGES


def test_types_without_selection_keep_todays_truncation():
    from pipeline import expediente

    assert expediente._page_cap_for("CONSTANCIA_SITUACION_FISCAL") == (expediente._MAX_PAGES, False)


def test_undeclared_upload_keeps_todays_truncation():
    """Auto-classification: which selector applies isn't known until the
    segmenter has read the pages, so the blunt cap has to stay."""
    from pipeline import expediente

    assert expediente._page_cap_for(None) == (expediente._MAX_PAGES, False)


def test_unknown_type_falls_back_instead_of_raising():
    from pipeline import expediente

    assert expediente._page_cap_for("NO_EXISTE_ESTE_TIPO") == (expediente._MAX_PAGES, False)


def test_kill_switch_restores_truncation(monkeypatch):
    from pipeline import expediente

    monkeypatch.setenv("PAGE_SELECTION_ENABLED", "false")
    assert expediente._page_cap_for("PEDIMENTO_ADUANAL") == (expediente._MAX_PAGES, False)


def test_a_type_can_set_its_own_source_ceiling():
    from document_catalog import _parse_entry
    from pipeline import expediente

    entry = _parse_entry("CON_TOPE", {"fields": {}, "page_selection": {**SPEC, "max_source_pages": 120}})
    monkey = {"CON_TOPE": entry}
    original = expediente.get_type_entry
    expediente.get_type_entry = lambda t: monkey[t]
    try:
        assert expediente._page_cap_for("CON_TOPE") == (120, True)
    finally:
        expediente.get_type_entry = original
