"""Extractor protocol and shared data shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol

from document_catalog import TypeRegistryEntry


@dataclass
class ExtractionResult:
    """Outcome of a single re-extraction pass.

    Attributes:
        entities: Field list in the canonical `[{"type": ..., "value": ...}]`
            shape used by the rest of the pipeline. Values are strings; empty
            strings replace None so downstream validators see consistent types.
        strategy: Which strategy produced this result (for observability).
        confidences: Optional per-field confidence scores returned by the
            underlying OCR/LLM. Stored for logging; validators do not use them.
        raw: Raw provider response (Textract + Bedrock payloads) kept for
            debugging. Not logged by default to avoid PII leakage.
    """

    entities: List[Dict[str, str]]
    strategy: str
    confidences: Dict[str, float] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


class Extractor(Protocol):
    """Minimal re-extraction interface.

    Implementations must be stateless (or internally cache only boto3
    clients) so the Lambda can reuse a single instance across invocations.
    """

    strategy_name: str

    def extract(
        self,
        document_bytes: bytes,
        content_type: str,
        type_entry: TypeRegistryEntry,
    ) -> ExtractionResult:
        """Re-extract fields from raw document bytes.

        Args:
            document_bytes: Raw file content fetched from S3.
            content_type: MIME type of the document (e.g., image/jpeg,
                application/pdf). Implementations may use this to choose
                request shapes (AnalyzeID requires image formats only).
            type_entry: The catalog entry for the classified type. Extractors
                MUST use its extraction_fields / extraction_hints to drive
                the prompt or field mapping. No hardcoded field lists.
        """
        ...


def entities_from_dict(
    values: Dict[str, str],
    extraction_fields: List[str],
) -> List[Dict[str, str]]:
    """Build the canonical entity list, preserving catalog field order.

    Fields not present in extraction_fields are dropped to match the shape
    the rest of the pipeline expects. Missing values become empty strings
    so downstream regex checks behave the same as an untouched bedrock
    extraction that returned no value for that field.
    """
    entities: List[Dict[str, str]] = []
    for name in extraction_fields:
        raw = values.get(name)
        value = "" if raw is None else str(raw).strip()
        entities.append({"type": name, "value": value})
    return entities
