"""Per-page text acquisition for PDFs, and single-page acquisition for
standalone images (JPG/JPEG/PNG — e.g. a photographed ID document).

Strategy (cost-first):
1. Native text layer via pypdf — free, exact, covers digital CFDIs/pedimentos.
2. Textract DetectDocumentText per page — only for pages whose text layer is
   empty/too small (scanned pages). Pages are re-wrapped as single-page PDFs
   so the synchronous Textract API can process them (no S3 round-trip).

A single file can mix both (e.g. digital expediente with scanned annexes).

A raw image has no PDF container to read a native layer from at all — pypdf
can't parse JPEG/PNG bytes (that's the "unreadable PDF: Stream has ended
unexpectedly" failure a raw image produces if it's fed to `load_pages`).
`load_image_page` is the equivalent entry point for that source: Textract's
DetectDocumentText already accepts JPEG/PNG bytes directly (no PDF wrapping
needed, unlike the per-PDF-page OCR fallback above), so it reuses the exact
same block-sorting/confidence logic via `_blocks_to_page`.
"""

from __future__ import annotations

import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cmp_to_key, lru_cache
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Below this many characters the page is considered scanned (no usable layer)
_MIN_NATIVE_CHARS = 30
_OCR_WORKERS = int(os.environ.get("TEXTRACT_WORKERS", "4"))


@dataclass
class PageText:
    number: int              # 1-based page number
    text: str
    source: str              # "native" | "textract" | "empty"
    confidence: float        # 0-1 (native text = 0.98; textract = mean line conf)


import re

_HSPACE_RE = re.compile(r" {5,}")

# pypdf's `layout` mode occasionally renders two adjacent numeric columns on
# the same line glued together with no separator — observed on a real
# pedimento where "VALOR ADUANA: 435730" and "PRECIO PAGADO/VALOR COMERCIAL:
# 421841" (two distinct boxes, side by side in the PDF) came out as
# "421841.00435730.00", and the extraction model then guessed the same
# number for both fields instead of picking the right one per label. A
# genuine printed amount never abuts another amount without at least a
# space/newline, so this pattern reliably flags the corruption (verified: 0
# false positives across the rest of that same document) without needing to
# know which fields are involved.
_GLUED_AMOUNTS_RE = re.compile(r"\d\.\d{2}\d{3,}\.\d{2}")


def _looks_corrupted(text: str) -> bool:
    return bool(_GLUED_AMOUNTS_RE.search(text))


def _native_text(page, index: int) -> str:
    """Native text layer for one page, layout mode preferred.

    pypdf's default extraction can scramble label/value order on CFDIs
    rendered column-by-column ("SCE130527CD1RFC:"); layout mode preserves
    the visual arrangement, which the extraction LLM parses far better.
    Horizontal whitespace runs are compressed to keep token cost down.
    """
    try:
        text = page.extract_text(extraction_mode="layout") or ""
    except Exception:
        text = ""
    if not text.strip():
        try:
            text = page.extract_text() or ""
        except Exception as e:
            logger.warning("pypdf failed on page %d: %s", index + 1, e)
            return ""
    text = _HSPACE_RE.sub("    ", text)
    return "\n".join(line.rstrip() for line in text.splitlines())


@lru_cache(maxsize=1)
def _textract_client():
    import boto3
    from botocore.config import Config

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client("textract", region_name=region, config=Config(read_timeout=120, retries={"max_attempts": 4, "mode": "adaptive"}))


def _single_page_pdf(reader, index: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_page(reader.pages[index])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _blocks_to_page(response: Dict[str, Any], page_number: int) -> PageText:
    """Turn a DetectDocumentText response into one PageText. Shared by the
    per-PDF-page OCR fallback and `load_image_page` — Textract's response
    shape is identical whichever way the bytes it analyzed got there.

    Sort LINE blocks by position (top-to-bottom, then left-to-right within a
    row) instead of trusting Textract's raw block order — this is what keeps
    multi-column layouts (tables, side-by-side boxes) from interleaving.
    Blocks within 0.006 of each other's Top are treated as the same visual
    row (tiny bounding-box jitter) and broken by Left instead; a plain
    sort-by-Top would fail exactly this case, since two blocks on the same
    row rarely share the identical Top value.
    """
    line_blocks = [b for b in response.get("Blocks", []) if b.get("BlockType") == "LINE"]

    def _compare(a: Dict[str, Any], b: Dict[str, Any]) -> float:
        box_a = a.get("Geometry", {}).get("BoundingBox", {})
        box_b = b.get("Geometry", {}).get("BoundingBox", {})
        vertical = box_a.get("Top", 0.0) - box_b.get("Top", 0.0)
        return vertical if abs(vertical) > 0.006 else box_a.get("Left", 0.0) - box_b.get("Left", 0.0)

    line_blocks.sort(key=cmp_to_key(_compare))

    lines: List[str] = []
    confs: List[float] = []
    for block in line_blocks:
        lines.append(block.get("Text", ""))
        confs.append(float(block.get("Confidence", 0.0)))
    text = "\n".join(lines)
    confidence = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return PageText(page_number, text, "textract" if text else "empty", round(confidence, 4))


def _ocr_page(reader, index: int) -> PageText:
    page_bytes = _single_page_pdf(reader, index)
    try:
        response = _textract_client().detect_document_text(Document={"Bytes": page_bytes})
    except Exception as e:
        logger.error("Textract failed on page %d: %s", index + 1, e)
        return PageText(index + 1, "", "empty", 0.0)
    return _blocks_to_page(response, index + 1)


def load_image_page(image_bytes: bytes) -> List[PageText]:
    """Single-page equivalent of `load_pages`, for a standalone JPG/JPEG/PNG
    (e.g. a photographed ID document) instead of a PDF. No pypdf involved —
    Textract's DetectDocumentText accepts image bytes directly — so this
    always yields exactly one page, sourced "textract" (there is no "native"
    text layer concept for a raw image). Raises on a genuine Textract
    failure (unlike the per-PDF-page OCR fallback, which degrades a single
    bad page to empty text so the rest of a multi-page document still
    processes) — for a single-image document there is no "rest" to salvage.
    """
    response = _textract_client().detect_document_text(Document={"Bytes": image_bytes})
    return [_blocks_to_page(response, 1)]


def load_pages(pdf_bytes: bytes, use_ocr: bool = True) -> List[PageText]:
    """Extract text for every page: native layer first, Textract fallback."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages: List[Optional[PageText]] = [None] * len(reader.pages)
    scanned_indices: List[int] = []

    for i, page in enumerate(reader.pages):
        text = _native_text(page, i)
        if len(text.strip()) >= _MIN_NATIVE_CHARS and not _looks_corrupted(text):
            pages[i] = PageText(i + 1, text, "native", 0.98)
        else:
            if text.strip():
                logger.warning(
                    "page %d: native text layer looks corrupted (glued numeric "
                    "columns), falling back to Textract", i + 1,
                )
            scanned_indices.append(i)

    if scanned_indices and use_ocr:
        logger.info("OCR fallback for %d/%d pages", len(scanned_indices), len(reader.pages))
        with ThreadPoolExecutor(max_workers=_OCR_WORKERS) as pool:
            for idx, result in zip(scanned_indices, pool.map(lambda i: _ocr_page(reader, i), scanned_indices)):
                pages[idx] = result
    else:
        for idx in scanned_indices:
            pages[idx] = PageText(idx + 1, "", "empty", 0.0)

    return [p for p in pages if p is not None]


def apply_ocr(
    pdf_bytes: bytes,
    pages: List[PageText],
    page_numbers: Optional[List[int]] = None,
) -> List[PageText]:
    """OCR selected pages that did not yield a usable native text layer.

    ``load_pages(..., use_ocr=False)`` deliberately represents those pages as
    ``source="empty"``.  This function is the second half of deferred OCR: it
    replaces only those empty entries, preserving native pages and their
    ordering.  Invalid/non-empty page numbers are ignored, which keeps the
    optimization fail-open when a catalog page scope is stale.
    """
    from pypdf import PdfReader

    wanted = set(page_numbers) if page_numbers is not None else None
    indices = [
        page.number - 1
        for page in pages
        if page.source == "empty" and (wanted is None or page.number in wanted)
    ]
    if not indices:
        return list(pages)

    reader = PdfReader(io.BytesIO(pdf_bytes))
    logger.info("deferred OCR fallback for %d/%d pages", len(indices), len(reader.pages))
    replacements: Dict[int, PageText] = {}
    with ThreadPoolExecutor(max_workers=_OCR_WORKERS) as pool:
        for idx, result in zip(indices, pool.map(lambda i: _ocr_page(reader, i), indices)):
            replacements[idx + 1] = result

    return [replacements.get(page.number, page) for page in pages]


def pages_summary(pages: List[PageText]) -> Dict[str, Any]:
    return {
        "totalPages": len(pages),
        "nativePages": sum(1 for p in pages if p.source == "native"),
        "ocrPages": sum(1 for p in pages if p.source == "textract"),
        "emptyPages": sum(1 for p in pages if p.source == "empty"),
    }
