"""
Document Catalog - Config-driven registry of document types.

Loads document type definitions from:
1. Local JSON config file (config/document_types.json) - default, always available
2. DynamoDB table (optional, runtime override) - set CATALOG_TABLE env var

To add a new document type WITHOUT code changes:
- Edit config/document_types.json and redeploy, OR
- Insert a record in the DynamoDB catalog table (takes effect immediately)

To add custom programmatic validation:
- Create a Node.js script in custom_validators/ (see README in that folder)
- Reference it in the type config as "custom_validator": "my_script"
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- Data Models ---


@dataclass
class ValidationRule:
    """A single validation rule to apply to an extracted field."""
    field: str
    rule_type: str  # "regex", "date_expiry", "presence", "boolean_true"
    pattern: Optional[str] = None
    message: str = ""


@dataclass
class TypeRegistryEntry:
    """Registry entry defining a document type's extraction and validation config."""
    type_id: str
    extraction_fields: List[str]
    required_fields: List[str]
    validation_rules: List[ValidationRule] = field(default_factory=list)
    validator_module: Optional[str] = None
    custom_validator: Optional[str] = None  # Node.js custom script name
    description: str = ""
    # Per-field hints telling the extractor WHERE the data lives in the document.
    # Used by both Bedrock prompts (at synth time) and the fallback extractor.
    extraction_hints: Dict[str, str] = field(default_factory=dict)
    # Strategy for re-extracting from the raw S3 object when the default
    # Bedrock-native extraction fails validation. Options:
    #   None | "" → no fallback, trust the initial processingResult as-is
    #   "textract_bedrock" → Textract DetectDocumentText + Bedrock LLM using
    #       extraction_hints as schema (best for photos of any document type)
    #   "textract_analyze_id" → Textract AnalyzeID mapped through field aliases
    #       (only meaningful for identity documents: INE, PASAPORTE, etc.)
    extraction_strategy: Optional[str] = None
    # Optional per-type overrides for AnalyzeID → catalog field mapping.
    # If empty, extractors fall back to the built-in alias table.
    field_aliases: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class CheckResult:
    """Result of a single validation check."""
    field: str
    rule: str
    valid: bool
    message: str


# --- Regex Patterns (shared constants) ---

RFC_PATTERN_PERSONA_FISICA = r"^[A-Z&Ñ]{4}\d{6}[A-Z0-9]{3}$"
RFC_PATTERN_PERSONA_MORAL = r"^[A-Z&Ñ]{3}\d{6}[A-Z0-9]{3}$"
CURP_PATTERN = r"^[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d$"


# --- Config Loading ---

_CONFIG_DIR = Path(__file__).parent / "config"
_CONFIG_FILE = _CONFIG_DIR / "document_types.json"
_CATALOG_TABLE = os.environ.get("CATALOG_TABLE")  # DynamoDB table name (optional)
_CATALOG_CACHE: Optional[Dict[str, TypeRegistryEntry]] = None


def _parse_rules(rules_data: List[Dict[str, Any]]) -> List[ValidationRule]:
    """Parse validation rules from JSON config."""
    return [
        ValidationRule(
            field=r.get("field", ""),
            rule_type=r.get("rule_type", ""),
            pattern=r.get("pattern"),
            message=r.get("message", ""),
        )
        for r in rules_data
    ]


def _parse_entry(type_id: str, data: Dict[str, Any]) -> TypeRegistryEntry:
    """Parse a single type entry from JSON config."""
    return TypeRegistryEntry(
        type_id=type_id,
        extraction_fields=data.get("extraction_fields", []),
        required_fields=data.get("required_fields", []),
        validation_rules=_parse_rules(data.get("validation_rules", [])),
        validator_module=data.get("validator_module"),
        custom_validator=data.get("custom_validator"),
        description=data.get("description", ""),
        extraction_hints=data.get("extraction_hints", {}) or {},
        extraction_strategy=data.get("extraction_strategy"),
        field_aliases=data.get("field_aliases", {}) or {},
    )


def _load_from_json() -> Dict[str, TypeRegistryEntry]:
    """Load catalog from local JSON config file."""
    if not _CONFIG_FILE.exists():
        logger.warning(f"Config file not found: {_CONFIG_FILE}")
        return {}

    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    catalog: Dict[str, TypeRegistryEntry] = {}
    for type_id, type_data in data.get("types", {}).items():
        catalog[type_id] = _parse_entry(type_id, type_data)

    logger.info(f"Loaded {len(catalog)} document types from JSON config")
    return catalog


def _load_from_dynamodb() -> Dict[str, TypeRegistryEntry]:
    """Load catalog overrides from DynamoDB (runtime hot-reload).

    Table schema:
      PK: type_id (S)
      extraction_fields: (L of S)
      required_fields: (L of S)
      validation_rules: (L of M) - each map has field, rule_type, pattern?, message?
      validator_module: (S) - optional
      custom_validator: (S) - optional
      description: (S) - optional
      enabled: (BOOL) - if false, type is ignored
    """
    if not _CATALOG_TABLE:
        return {}

    try:
        import boto3
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(_CATALOG_TABLE)
        response = table.scan()
        items = response.get("Items", [])

        catalog: Dict[str, TypeRegistryEntry] = {}
        for item in items:
            if not item.get("enabled", True):
                continue
            type_id = item["type_id"]
            catalog[type_id] = _parse_entry(type_id, item)

        # Handle pagination
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            for item in response.get("Items", []):
                if not item.get("enabled", True):
                    continue
                type_id = item["type_id"]
                catalog[type_id] = _parse_entry(type_id, item)

        logger.info(f"Loaded {len(catalog)} document types from DynamoDB")
        return catalog

    except Exception as e:
        logger.error(f"Failed to load from DynamoDB: {e}")
        return {}


def _load_catalog() -> Dict[str, TypeRegistryEntry]:
    """Load the full catalog: JSON base + DynamoDB overrides.

    DynamoDB entries override JSON entries with the same type_id.
    This allows hot-adding types without redeployment.
    """
    global _CATALOG_CACHE

    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE

    # Base: local JSON config (always available, deployed with Lambda)
    catalog = _load_from_json()

    # Override/extend: DynamoDB (optional, for runtime changes)
    db_catalog = _load_from_dynamodb()
    catalog.update(db_catalog)

    _CATALOG_CACHE = catalog
    return catalog


def reload_catalog() -> None:
    """Force reload of the catalog (useful for testing or after DB updates)."""
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


# --- Public API ---

DEFAULT_ENTRY = TypeRegistryEntry(
    type_id="OTRO",
    extraction_fields=[],
    required_fields=[],
    validation_rules=[],
    validator_module=None,
    custom_validator=None,
    description="Documento no clasificado - requiere revision manual",
)


def get_type_entry(doc_type: str) -> TypeRegistryEntry:
    """Look up a document type in the catalog.

    Resolution order:
    1. DynamoDB (if CATALOG_TABLE is set) - highest priority
    2. Local JSON config file
    3. DEFAULT_ENTRY for unknown types
    """
    catalog = _load_catalog()
    return catalog.get(doc_type, DEFAULT_ENTRY)


def get_all_types() -> Dict[str, TypeRegistryEntry]:
    """Get the full catalog (for prompt generation or introspection)."""
    return _load_catalog()


# Expose DOCUMENT_CATALOG for backward compatibility
DOCUMENT_CATALOG = property(lambda self: _load_catalog())
