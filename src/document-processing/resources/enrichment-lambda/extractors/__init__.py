"""Extractors package - pluggable re-extraction strategies.

Each extractor takes raw document bytes (fetched from S3) plus the
TypeRegistryEntry for the classified type, and returns an entity list
in the canonical `[{"type": "FIELD", "value": "VALUE"}]` shape that
the rest of the validation pipeline already expects.

Strategies:
- textract_bedrock: Textract DetectDocumentText + Bedrock LLM. Uses
  extraction_fields and extraction_hints from the catalog as schema.
  Best default for photos of any supported document type.
- textract_analyze_id: Textract AnalyzeID then map via field_aliases.
  Specialized for INE/PASAPORTE; fastest when a document is in scope.

The catalog drives which strategy runs via the `extraction_strategy`
field on each type entry. If unset, no re-extraction is attempted and
the original Bedrock-extracted entities flow through unchanged.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import ExtractionResult, Extractor

logger = logging.getLogger(__name__)

# Lazy imports so the enrichment Lambda cold start does not pay the
# Textract/Bedrock client init cost unless a strategy actually runs.


def get_extractor(strategy: Optional[str]) -> Optional[Extractor]:
    """Resolve a strategy name to an Extractor instance.

    Returns None when no strategy is configured or the name is unknown,
    which signals the caller to skip re-extraction entirely.
    """
    if not strategy:
        return None

    normalized = strategy.strip().lower()

    if normalized in ("textract_bedrock", "textract+bedrock", "hybrid"):
        from .textract_bedrock import TextractBedrockExtractor
        return TextractBedrockExtractor()

    if normalized in ("textract_analyze_id", "analyze_id"):
        from .textract_analyze_id import TextractAnalyzeIdExtractor
        return TextractAnalyzeIdExtractor()

    logger.warning("Unknown extraction_strategy '%s'; skipping re-extraction", strategy)
    return None


__all__ = ["ExtractionResult", "Extractor", "get_extractor"]
