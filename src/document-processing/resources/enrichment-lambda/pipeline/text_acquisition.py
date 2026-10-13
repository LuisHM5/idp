"""Catalog-driven PDF text acquisition with a conservative OCR fast path.

The historical path remains ``load_pages``: native text first and immediate
Textract for every unusable page.  A catalog may opt in to native-first OCR
deferral, but only a complete deterministic profile may skip Textract.  Every
uncertain state falls back to the historical path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from document_catalog import TypeRegistryEntry

from . import deterministic_text
from .pdf_pages import PageText, apply_ocr, load_pages

logger = logging.getLogger(__name__)

OCR_EAGER = "all_pages_without_native_text"
OCR_DEFERRED = "only_if_deterministic_incomplete"
SCOPE_PROFILE_THEN_REMAINING = "profile_pages_then_remaining"


@dataclass
class AcquisitionResult:
    pages: List[PageText]
    optimization: Optional[Dict[str, Any]] = None
    deterministic: Optional[deterministic_text.ExtractionResult] = None


def validate_config(raw: Any, deterministic_config: Any) -> List[str]:
    """Validate the catalog block used by this module (build/sync lint)."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["text_acquisition debe ser un objeto"]

    problems: List[str] = []
    mode = str(raw.get("ocr_mode") or OCR_EAGER).strip().lower()
    scope = str(
        raw.get("ocr_page_scope") or SCOPE_PROFILE_THEN_REMAINING
    ).strip().lower()
    if mode not in {OCR_EAGER, OCR_DEFERRED}:
        problems.append(
            "text_acquisition.ocr_mode debe ser "
            f"{OCR_EAGER} o {OCR_DEFERRED}"
        )
    if scope != SCOPE_PROFILE_THEN_REMAINING:
        problems.append(
            "text_acquisition.ocr_page_scope debe ser "
            f"{SCOPE_PROFILE_THEN_REMAINING}"
        )
    if mode == OCR_DEFERRED:
        if not isinstance(deterministic_config, dict) or not deterministic_config.get(
            "enabled", True
        ):
            problems.append(
                "OCR diferido requiere deterministic_extraction habilitado"
            )
        elif not isinstance(deterministic_config.get("profiles"), list) or not (
            deterministic_config.get("profiles")
        ):
            problems.append("OCR diferido requiere deterministic_extraction.profiles")
    return problems


def deferred_ocr_enabled() -> bool:
    """Closed global switch: only the literal ``true`` enables the feature."""
    return os.environ.get("DEFERRED_OCR_ENABLED", "") == "true"


def _spec_for(entry: TypeRegistryEntry) -> Optional[Dict[str, Any]]:
    raw = getattr(entry, "text_acquisition", None)
    return raw if isinstance(raw, dict) else None


def _is_complete(result: deterministic_text.ExtractionResult) -> bool:
    """A matched profile is complete only when every expected path resolved."""
    return bool(
        result.profile_status == "matched"
        and result.expected_paths
        and result.expected_paths.issubset(result.resolved_paths)
        and not result.warnings
    )


def _profile_page_numbers(
    policy: Dict[str, Any],
    result: deterministic_text.ExtractionResult,
    pages: Sequence[PageText],
    candidates: Set[int],
) -> Set[int]:
    """Resolve the page scope declared by the matched/possible profiles.

    Unknown or unbounded profile scopes return every candidate page.  That is
    intentionally conservative: a bad catalog must increase OCR, never drop
    information.
    """
    profiles = policy.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        return set(candidates)
    if result.profile_status == "matched" and result.profile_id:
        profiles = [
            profile
            for profile in profiles
            if isinstance(profile, dict) and profile.get("id") == result.profile_id
        ]
    if not profiles:
        return set(candidates)

    first_page = min((page.number for page in pages), default=1)
    selected: Set[int] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            return set(candidates)
        scope = profile.get("pages")
        if scope == "first":
            selected.add(first_page)
        elif isinstance(scope, list):
            selected.update(
                int(number)
                for number in scope
                if isinstance(number, (int, float, str)) and str(number).isdigit()
            )
        else:
            return set(candidates)
    return selected & candidates


def _metadata(
    *,
    global_enabled: bool,
    mode: str,
    page_scope: str,
    effective: str,
    rollback: bool,
    reason: str,
    candidates: int,
    attempted: int = 0,
    skipped: int = 0,
    result: Optional[deterministic_text.ExtractionResult] = None,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "globalEnabled": global_enabled,
        "catalogMode": mode,
        "catalogPageScope": page_scope,
        "effectiveMode": effective,
        "rollbackApplied": rollback,
        "reason": reason,
        "ocrCandidatePages": candidates,
        "ocrPagesAttempted": attempted,
        "ocrPagesSkipped": skipped,
    }
    if result is not None:
        metadata["deterministicPreflight"] = {
            "profileStatus": result.profile_status,
            "profileId": result.profile_id,
            "expectedFields": len(result.expected_paths),
            "resolvedExpectedFields": len(result.expected_paths & result.resolved_paths),
            "complete": _is_complete(result),
        }
    return metadata


def load_pdf_pages(
    pdf_bytes: bytes,
    entry: TypeRegistryEntry,
    extraction_mode: str = "catalog",
) -> AcquisitionResult:
    """Load PDF pages using eager OCR or the catalog's deferred OCR policy."""
    spec = _spec_for(entry)
    if spec is None:
        return AcquisitionResult(load_pages(pdf_bytes))

    raw_mode = str(spec.get("ocr_mode") or OCR_EAGER).strip().lower()
    page_scope = str(
        spec.get("ocr_page_scope") or SCOPE_PROFILE_THEN_REMAINING
    ).strip().lower()
    global_enabled = deferred_ocr_enabled()
    configured_deferred = raw_mode == OCR_DEFERRED

    if not configured_deferred or page_scope != SCOPE_PROFILE_THEN_REMAINING:
        pages = load_pages(pdf_bytes)
        candidates = sum(page.source != "native" for page in pages)
        return AcquisitionResult(
            pages,
            _metadata(
                global_enabled=global_enabled, mode=raw_mode, page_scope=page_scope,
                effective="eager", rollback=False,
                reason=(
                    "catalog_uses_eager_ocr"
                    if not configured_deferred
                    else "invalid_page_scope"
                ),
                candidates=candidates,
                attempted=sum(page.source == "textract" for page in pages),
            ),
        )

    if not global_enabled:
        pages = load_pages(pdf_bytes)
        candidates = sum(page.source != "native" for page in pages)
        return AcquisitionResult(
            pages,
            _metadata(
                global_enabled=False, mode=raw_mode, page_scope=page_scope,
                effective="eager", rollback=True, reason="global_kill_switch",
                candidates=candidates,
                attempted=sum(page.source == "textract" for page in pages),
            ),
        )

    requested_mode = str(extraction_mode or "catalog").strip().lower()
    if requested_mode == "model" or not deterministic_text.enabled_for(entry):
        pages = load_pages(pdf_bytes)
        candidates = sum(page.source != "native" for page in pages)
        return AcquisitionResult(
            pages,
            _metadata(
                global_enabled=True, mode=raw_mode, page_scope=page_scope,
                effective="eager", rollback=False,
                reason=(
                    "model_mode_requires_ocr"
                    if requested_mode == "model"
                    else "deterministic_mapping_unavailable"
                ),
                candidates=candidates,
                attempted=sum(page.source == "textract" for page in pages),
            ),
        )

    # Native-only is the fast path.  Pages without a usable layer are retained
    # as empty placeholders so segmentation/page numbers never change.
    pages = load_pages(pdf_bytes, use_ocr=False)
    candidates = {page.number for page in pages if page.source == "empty"}
    try:
        deterministic = deterministic_text.extract(pages, entry)
    except Exception:  # noqa: BLE001 - optimization must never block extraction
        logger.exception("deterministic OCR preflight failed for %s", entry.type_id)
        eager = apply_ocr(pdf_bytes, pages)
        return AcquisitionResult(
            eager,
            _metadata(
                global_enabled=True, mode=raw_mode, page_scope=page_scope,
                effective="eager_fallback", rollback=False,
                reason="preflight_error",
                candidates=len(candidates),
                attempted=len(candidates),
            ),
        )

    if not candidates or _is_complete(deterministic):
        return AcquisitionResult(
            pages,
            _metadata(
                global_enabled=True, mode=raw_mode, page_scope=page_scope,
                effective="deferred", rollback=False,
                reason=(
                    "no_ocr_candidates" if not candidates else "deterministic_complete"
                ),
                candidates=len(candidates),
                skipped=len(candidates),
                result=deterministic,
            ),
            deterministic,
        )

    profile_pages = _profile_page_numbers(
        deterministic_text.policy_for(entry) or {}, deterministic, pages, candidates
    )
    attempted: Set[int] = set()
    if profile_pages:
        pages = apply_ocr(pdf_bytes, pages, sorted(profile_pages))
        attempted.update(profile_pages)
        deterministic = deterministic_text.extract(pages, entry)
        if _is_complete(deterministic):
            return AcquisitionResult(
                pages,
                _metadata(
                    global_enabled=True, mode=raw_mode, page_scope=page_scope,
                    effective="deferred", rollback=False,
                    reason="complete_after_profile_ocr", candidates=len(candidates),
                    attempted=len(attempted),
                    skipped=len(candidates - attempted),
                    result=deterministic,
                ),
                deterministic,
            )

    remaining = candidates - attempted
    if remaining:
        pages = apply_ocr(pdf_bytes, pages, sorted(remaining))
        attempted.update(remaining)
    deterministic = deterministic_text.extract(pages, entry)
    return AcquisitionResult(
        pages,
        _metadata(
            global_enabled=True, mode=raw_mode, page_scope=page_scope,
            effective="eager_fallback", rollback=False,
            reason="deterministic_incomplete", candidates=len(candidates),
            attempted=len(attempted), skipped=0, result=deterministic,
        ),
        deterministic,
    )
