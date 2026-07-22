"""Per-page text acquisition for PDFs.

Strategy (cost-first):
1. Native text layer via pypdf — free, exact, covers digital CFDIs/pedimentos.
2. Textract DetectDocumentText per page — only for pages whose text layer is
   empty/too small (scanned pages). Pages are re-wrapped as single-page PDFs
   so the synchronous Textract API can process them (no S3 round-trip).

A single file can mix both (e.g. digital expediente with scanned annexes).
"""

from __future__ import annotations

import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
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


def _ocr_page(reader, index: int) -> PageText:
    page_bytes = _single_page_pdf(reader, index)
    try:
        response = _textract_client().detect_document_text(Document={"Bytes": page_bytes})
    except Exception as e:
        logger.error("Textract failed on page %d: %s", index + 1, e)
        return PageText(index + 1, "", "empty", 0.0)

    lines: List[str] = []
    confs: List[float] = []
    for block in response.get("Blocks", []):
        if block.get("BlockType") == "LINE":
            lines.append(block.get("Text", ""))
            confs.append(float(block.get("Confidence", 0.0)))
    text = "\n".join(lines)
    confidence = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return PageText(index + 1, text, "textract" if text else "empty", round(confidence, 4))


def load_pages(pdf_bytes: bytes, use_ocr: bool = True) -> List[PageText]:
    """Extract text for every page: native layer first, Textract fallback."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages: List[Optional[PageText]] = [None] * len(reader.pages)
    scanned_indices: List[int] = []

    for i, page in enumerate(reader.pages):
        text = _native_text(page, i)
        if len(text.strip()) >= _MIN_NATIVE_CHARS:
            pages[i] = PageText(i + 1, text, "native", 0.98)
        else:
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


def pages_summary(pages: List[PageText]) -> Dict[str, Any]:
    return {
        "totalPages": len(pages),
        "nativePages": sum(1 for p in pages if p.source == "native"),
        "ocrPages": sum(1 for p in pages if p.source == "textract"),
        "emptyPages": sum(1 for p in pages if p.source == "empty"),
    }
