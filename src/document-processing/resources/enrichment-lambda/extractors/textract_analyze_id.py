"""Textract AnalyzeID extractor for identity documents.

AnalyzeID is Textract's ID-specialized endpoint. It returns structured
fields (LAST_NAME, DATE_OF_BIRTH, etc.) mapped to a fixed taxonomy. This
extractor maps those taxonomy names to catalog fields using per-type
aliases (with a built-in default), and builds NOMBRE_COMPLETO when needed.

Only accepts image inputs (JPEG/PNG); PDFs cause the caller to fall back
to textract_bedrock upstream.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Dict, List

import boto3

from document_catalog import TypeRegistryEntry

from .base import ExtractionResult, entities_from_dict

logger = logging.getLogger(__name__)


# Default AnalyzeID → catalog mapping. Tuned for Mexican INE/pasaporte;
# per-type overrides can be set in the catalog via `field_aliases`.
_DEFAULT_ALIASES: Dict[str, List[str]] = {
    "APELLIDO_PATERNO": ["LAST_NAME", "SURNAME", "PRIMARY_IDENTIFIER"],
    "APELLIDO_MATERNO": ["SECOND_LAST_NAME", "MOTHER_LAST_NAME"],
    "NOMBRE": ["FIRST_NAME", "GIVEN_NAME"],
    "NOMBRE_COMPLETO": ["FULL_NAME", "NAME"],
    "FECHA_NACIMIENTO": ["DATE_OF_BIRTH", "DOB"],
    "SEXO": ["SEX", "GENDER"],
    "CURP": ["CURP"],
    "NUMERO_DOCUMENTO": ["DOCUMENT_NUMBER", "ID_NUMBER", "LICENSE_NUMBER"],
    "VIGENCIA": ["EXPIRATION_DATE", "DATE_OF_EXPIRY"],
    "DOMICILIO": ["ADDRESS", "FULL_ADDRESS", "STREET_ADDRESS"],
    "TIPO_DOCUMENTO": ["DOCUMENT_TYPE"],
}

_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png"}


@lru_cache(maxsize=1)
def _textract_client(region: str):
    return boto3.client("textract", region_name=region)


def _collect_fields(identity_document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Flatten AnalyzeID IdentityDocumentFields into a `{NAME: {value, conf}}` map."""
    collected: Dict[str, Dict[str, Any]] = {}
    for field in identity_document.get("IdentityDocumentFields", []):
        field_type = field.get("Type", {}).get("Text", "")
        if not field_type:
            continue
        detection = field.get("ValueDetection", {})
        collected[field_type] = {
            "value": detection.get("Text"),
            "confidence": detection.get("Confidence"),
        }
    return collected


def _first_alias_value(
    collected: Dict[str, Dict[str, Any]],
    aliases: List[str],
) -> Dict[str, Any] | None:
    """Return the first alias that has a non-empty Textract value."""
    for name in aliases:
        slot = collected.get(name)
        if slot and slot.get("value"):
            return slot
    return None


def _resolve_aliases(
    type_entry: TypeRegistryEntry,
) -> Dict[str, List[str]]:
    """Merge default aliases with catalog-specified overrides."""
    merged: Dict[str, List[str]] = {k: list(v) for k, v in _DEFAULT_ALIASES.items()}
    for name, extra in (type_entry.field_aliases or {}).items():
        if isinstance(extra, list) and extra:
            merged[name] = list(extra)
    return merged


def _build_full_name(values: Dict[str, str]) -> str | None:
    """Compose NOMBRE_COMPLETO in INE order (paterno materno nombre) if missing."""
    parts = [
        values.get("APELLIDO_PATERNO", "").strip(),
        values.get("APELLIDO_MATERNO", "").strip(),
        values.get("NOMBRE", "").strip(),
    ]
    parts = [p for p in parts if p]
    return " ".join(parts) if parts else None


class TextractAnalyzeIdExtractor:
    """Textract AnalyzeID with catalog-driven field mapping."""

    strategy_name = "textract_analyze_id"

    def extract(
        self,
        document_bytes: bytes,
        content_type: str,
        type_entry: TypeRegistryEntry,
    ) -> ExtractionResult:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

        if content_type and content_type not in _IMAGE_CONTENT_TYPES:
            logger.info(
                "textract_analyze_id: content_type=%s unsupported, skipping",
                content_type,
            )
            return ExtractionResult(
                entities=entities_from_dict({}, type_entry.extraction_fields),
                strategy=self.strategy_name,
                raw={"skipped": "unsupported_content_type"},
            )

        logger.info(
            "textract_analyze_id: type=%s bytes=%d",
            type_entry.type_id,
            len(document_bytes),
        )

        response = _textract_client(region).analyze_id(
            DocumentPages=[{"Bytes": document_bytes}]
        )
        identity_docs = response.get("IdentityDocuments", [])
        if not identity_docs:
            return ExtractionResult(
                entities=entities_from_dict({}, type_entry.extraction_fields),
                strategy=self.strategy_name,
                raw={"identity_documents": 0},
            )

        collected = _collect_fields(identity_docs[0])
        aliases = _resolve_aliases(type_entry)

        values: Dict[str, str] = {}
        confidences: Dict[str, float] = {}
        for field_name in type_entry.extraction_fields:
            slot = _first_alias_value(collected, aliases.get(field_name, [field_name]))
            if slot and slot.get("value"):
                values[field_name] = str(slot["value"])
                if slot.get("confidence") is not None:
                    confidences[field_name] = float(slot["confidence"])

        if "NOMBRE_COMPLETO" in type_entry.extraction_fields and not values.get("NOMBRE_COMPLETO"):
            full_name = _build_full_name(values)
            if full_name:
                values["NOMBRE_COMPLETO"] = full_name

        if "TIPO_DOCUMENTO" in type_entry.extraction_fields and not values.get("TIPO_DOCUMENTO"):
            tipo = identity_docs[0].get("IdentityDocumentType")
            if tipo:
                values["TIPO_DOCUMENTO"] = str(tipo)

        logger.info(
            "textract_analyze_id: mapped %d fields for %s",
            sum(1 for v in values.values() if v),
            type_entry.type_id,
        )

        return ExtractionResult(
            entities=entities_from_dict(values, type_entry.extraction_fields),
            strategy=self.strategy_name,
            confidences=confidences,
            raw={"identity_documents": len(identity_docs)},
        )
