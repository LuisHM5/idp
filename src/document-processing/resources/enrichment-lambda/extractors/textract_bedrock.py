"""Hybrid Textract + Bedrock extractor.

Runs Textract `DetectDocumentText` as the OCR engine (stronger than Bedrock
vision for character-level accuracy on ID documents) and hands the raw
OCR lines to Bedrock with the catalog's extraction_fields + extraction_hints
as the schema. The LLM produces the final structured JSON.

Why this fixes OCR failures on photos:
- Textract handles skew, glare, and low contrast better than Claude Haiku vision.
- The LLM sees ALL text lines (with confidence) and organizes them against the
  schema, so a single smudged character can be recovered from context.
- The schema is built dynamically from the catalog, so new document types get
  extractor support by editing `extraction_fields` + `extraction_hints`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Any, Dict, List

import boto3

from document_catalog import TypeRegistryEntry

from .base import ExtractionResult, entities_from_dict

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_DEFAULT_MAX_TOKENS = 1024


@lru_cache(maxsize=1)
def _textract_client(region: str):
    return boto3.client("textract", region_name=region)


@lru_cache(maxsize=1)
def _bedrock_client(region: str):
    return boto3.client("bedrock-runtime", region_name=region)


def _detect_text(document_bytes: bytes, region: str) -> Dict[str, Any]:
    """Call Textract DetectDocumentText and return the raw response."""
    return _textract_client(region).detect_document_text(
        Document={"Bytes": document_bytes}
    )


def _text_lines(textract_response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reduce the Textract response to just LINE blocks with confidence."""
    lines: List[Dict[str, Any]] = []
    for block in textract_response.get("Blocks", []):
        if block.get("BlockType") != "LINE":
            continue
        lines.append(
            {
                "text": block.get("Text", ""),
                "confidence": round(float(block.get("Confidence", 0.0)), 2),
            }
        )
    return lines


def _build_schema(type_entry: TypeRegistryEntry) -> Dict[str, Any]:
    """Build the JSON schema Bedrock must fill in, from the catalog.

    Each field entry advertises a value type hint and, when present, the
    extraction hint that tells the LLM where on the document to look.
    Required fields are marked so the model does not silently omit them.
    """
    schema: Dict[str, Any] = {}
    for name in type_entry.extraction_fields:
        entry: Dict[str, Any] = {"value": "string | null"}
        hint = type_entry.extraction_hints.get(name)
        if hint:
            entry["hint"] = hint
        if name in type_entry.required_fields:
            entry["required"] = True
        schema[name] = entry
    return schema


def _build_extraction_prompt(
    type_entry: TypeRegistryEntry,
    ocr_lines: List[Dict[str, Any]],
) -> str:
    """Assemble the prompt payload sent to Bedrock as a single user message.

    The payload is plain JSON: the LLM sees the schema (with hints), the OCR
    lines, and receives strict output instructions. No few-shot examples —
    catalog hints are authoritative.
    """
    schema = _build_schema(type_entry)
    instructions = (
        "Eres un extractor de campos. Vas a recibir lineas OCR de un documento "
        f"clasificado como {type_entry.type_id}. Mapea el contenido al schema "
        "entregado usando las pistas (hint) de cada campo para decidir de donde "
        "sale cada dato.\n\n"
        "Reglas:\n"
        "- Devuelve EXCLUSIVAMENTE un objeto JSON con la forma "
        "{\"extraction_fields\": {\"CAMPO\": {\"value\": \"...\"}, ...}}.\n"
        "- No agregues markdown ni explicaciones.\n"
        "- Si un campo no aparece en el OCR, usa value=null (no inventes).\n"
        "- Respeta el texto tal cual aparece (mayusculas, acentos, numeros). "
        "No corrijas errores de tipeo del documento.\n"
        "- Si una cadena de ancho fijo (CURP, RFC, CLAVE DE ELECTOR, UUID) parece "
        "incompleta, busca en lineas vecinas del OCR y reconstruye solo cuando la "
        "evidencia sea clara."
    )
    payload = {
        "instruction": instructions,
        "target_type": type_entry.type_id,
        "target_schema": schema,
        "ocr_lines": ocr_lines,
    }
    return json.dumps(payload, ensure_ascii=False)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_model_json(raw_text: str) -> Dict[str, Any]:
    """Extract the first JSON object the model emitted, tolerating fences."""
    text = raw_text.strip()
    if not text:
        raise ValueError("Bedrock returned empty text")

    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Bedrock response does not contain a JSON object")

    return json.loads(text[start : end + 1])


def _invoke_bedrock(
    model_id: str,
    region: str,
    extraction_prompt: str,
    max_tokens: int,
) -> str:
    """Call Bedrock Converse and return the aggregated response text."""
    response = _bedrock_client(region).converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [{"text": extraction_prompt}],
            }
        ],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    output = response.get("output", {}).get("message", {})
    texts = [
        block.get("text", "")
        for block in output.get("content", [])
        if "text" in block
    ]
    return "\n".join(texts).strip()


def _flatten_values(parsed: Dict[str, Any]) -> Dict[str, str]:
    """Convert `{FIELD: {"value": "...", ...}}` → `{FIELD: "..."}`."""
    fields = parsed.get("extraction_fields")
    if not isinstance(fields, dict):
        raise ValueError("Bedrock JSON missing 'extraction_fields' object")

    flattened: Dict[str, str] = {}
    for name, slot in fields.items():
        if isinstance(slot, dict):
            raw = slot.get("value")
        else:
            raw = slot
        if raw is None:
            continue
        flattened[name] = str(raw)
    return flattened


class TextractBedrockExtractor:
    """Textract OCR + Bedrock structuring, driven by the catalog."""

    strategy_name = "textract_bedrock"

    def extract(
        self,
        document_bytes: bytes,
        content_type: str,
        type_entry: TypeRegistryEntry,
    ) -> ExtractionResult:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        model_id = os.environ.get("BEDROCK_EXTRACTOR_MODEL_ID", _DEFAULT_MODEL_ID)
        max_tokens = int(os.environ.get("BEDROCK_EXTRACTOR_MAX_TOKENS", str(_DEFAULT_MAX_TOKENS)))

        logger.info(
            "textract_bedrock: type=%s bytes=%d content_type=%s model=%s",
            type_entry.type_id,
            len(document_bytes),
            content_type,
            model_id,
        )

        textract_response = _detect_text(document_bytes, region)
        ocr_lines = _text_lines(textract_response)
        logger.info("textract_bedrock: extracted %d OCR lines", len(ocr_lines))

        if not ocr_lines:
            return ExtractionResult(
                entities=entities_from_dict({}, type_entry.extraction_fields),
                strategy=self.strategy_name,
                raw={"textract": {"blocks": 0}},
            )

        prompt = _build_extraction_prompt(type_entry, ocr_lines)
        response_text = _invoke_bedrock(model_id, region, prompt, max_tokens)
        parsed = _parse_model_json(response_text)
        values = _flatten_values(parsed)

        logger.info(
            "textract_bedrock: extracted %d fields for %s",
            sum(1 for v in values.values() if v),
            type_entry.type_id,
        )

        return ExtractionResult(
            entities=entities_from_dict(values, type_entry.extraction_fields),
            strategy=self.strategy_name,
            raw={
                "textract_lines": len(ocr_lines),
                "bedrock_model": model_id,
            },
        )
