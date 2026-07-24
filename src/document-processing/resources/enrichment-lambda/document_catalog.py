"""
Document Catalog - Config-driven registry of document types.

Loads document type definitions from:
1. Local bundled catalog (config/catalogs.json) - built from catalogs/*.json
2. DynamoDB table (optional, runtime override) - set CATALOG_TABLE env var

To add a new document type WITHOUT code changes:
- Edit catalogs/<TYPE>.json then `pnpm run build` and redeploy, OR
- Insert a record in the DynamoDB catalog table (takes effect immediately)

To add custom programmatic validation:
- Create a Node.js script in custom_validators/ (see README in that folder)
- Reference it in the type config as "custom_validator": "my_script"
"""

import hashlib
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
    """A single validation rule to apply to an extracted field.

    `params` keeps every extra key from the catalog JSON (formula, tolerance,
    source, keywords, allow_empty, target_document_type, ...) so new rule
    types can be added in config without touching this dataclass.
    """
    field: str
    rule_type: str  # "regex", "custom_formula", "sum_array", "cross_document_match", ...
    pattern: Optional[str] = None
    message: str = ""
    params: Dict[str, Any] = field(default_factory=dict)


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
    # Repeatable (array) fields: name → list of sub-field names. Extracted as
    # a list of objects; validated with *_each / sum_array / minimum_items.
    repeatable_fields: Dict[str, List[str]] = field(default_factory=dict)
    # Optional closed list of subtypes (DOCUMENT_SUBTYPE must be one of these).
    allowed_subtypes: List[str] = field(default_factory=list)
    # Routing hint for consumers (which business queue receives this doc).
    business_destination: str = ""
    # Minimum extraction confidence for a field to be VALIDATED (else LOW_CONFIDENCE).
    min_confidence: float = 0.70
    # Declared schema version for THIS type (from JSON or DynamoDB). Surfaced in
    # each result's tracking so you can tell which version processed a document,
    # including hot-reloaded DynamoDB overrides.
    schema_version: Optional[str] = None
    # Processing pipeline for this type. "expediente" routes through the
    # multi-document customs pipeline (segmenter + cross-document rules); any
    # other value (or None) uses the standard single-document flow.
    pipeline: Optional[str] = None
    # Target shapes for derived/structured outputs (e.g. LIQUIDACION_NORMALIZADA
    # defaults). Consumed by post-extraction normalizers.
    structured_fields: Dict[str, Any] = field(default_factory=dict)
    # Per-form field mappings, including `cuadro_liquidacion_fields` used to
    # normalize the customs liquidation table into LIQUIDACION_NORMALIZADA.
    form_mapping: Dict[str, Any] = field(default_factory=dict)


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
# Single bundled catalog, produced from catalogs/*.json by `pnpm build`
# (scripts/sync-catalogs.mjs). One base, one file.
_CONFIG_FILE = _CONFIG_DIR / "catalogs.json"
_CATALOG_TABLE = os.environ.get("CATALOG_TABLE")  # DynamoDB table name (optional)
_CATALOG_CACHE: Optional[Dict[str, TypeRegistryEntry]] = None
# Version of each loaded catalog file: declared `_schema_version` if present,
# else a short content hash so the identifier is always deterministic and
# changes whenever the schema does. Keyed by file stem (e.g. "catalogs").
_SCHEMA_VERSIONS: Dict[str, str] = {}


_RULE_CORE_KEYS = {"field", "rule_type", "pattern", "message"}


def _parse_rules(rules_data: List[Dict[str, Any]]) -> List[ValidationRule]:
    """Parse validation rules from JSON config, keeping extra keys in params."""
    return [
        ValidationRule(
            field=r.get("field", ""),
            rule_type=r.get("rule_type", ""),
            pattern=r.get("pattern"),
            message=r.get("message", ""),
            params={k: v for k, v in r.items() if k not in _RULE_CORE_KEYS},
        )
        for r in rules_data
    ]


def _parse_entry(
    type_id: str,
    data: Dict[str, Any],
    field_sets: Optional[Dict[str, List[str]]] = None,
) -> TypeRegistryEntry:
    """Parse a single type entry from JSON config.

    `field_sets` resolves the customs-catalog `field_sets` mechanism: named
    lists of shared fields (e.g. COMMON_CFDI_FIELDS) prepended to the type's
    own extraction_fields.
    """
    fields: List[str] = []
    for set_name in data.get("field_sets", []) or []:
        for name in (field_sets or {}).get(set_name, []):
            if name not in fields:
                fields.append(name)
    for name in data.get("extraction_fields", []) or []:
        if name not in fields:
            fields.append(name)

    return TypeRegistryEntry(
        type_id=type_id,
        extraction_fields=fields,
        required_fields=data.get("required_fields", []),
        validation_rules=_parse_rules(data.get("validation_rules", [])),
        validator_module=data.get("validator_module"),
        custom_validator=data.get("custom_validator"),
        description=data.get("description", ""),
        extraction_hints=data.get("extraction_hints", {}) or {},
        extraction_strategy=data.get("extraction_strategy"),
        field_aliases=data.get("field_aliases", {}) or {},
        repeatable_fields=data.get("repeatable_fields", {}) or {},
        allowed_subtypes=data.get("allowed_subtypes", []) or [],
        business_destination=data.get("business_destination", ""),
        min_confidence=float(data.get("min_confidence", 0.70)),
        schema_version=data.get("schema_version"),
        structured_fields=data.get("structured_fields", {}) or {},
        form_mapping=data.get("form_mapping", {}) or {},
        pipeline=data.get("pipeline"),
    )


_CROSS_DOC_RULES: List[Dict[str, Any]] = []


def _load_config_file(path: Path) -> Dict[str, TypeRegistryEntry]:
    """Load one catalog file. Supports two shapes:

    - Classic: {"types": {TYPE_ID: {...}}}
    - Customs: {"document_types": {TYPE_ID: {...}}, "_field_sets": {...},
                "cross_document_validation_rules": [...]}
    """
    raw = path.read_bytes()
    data = json.loads(raw)

    # Declared version wins; otherwise a short content hash so the schema is
    # always identifiable and the id changes whenever the file does.
    declared = data.get("_schema_version") or data.get("version")
    _SCHEMA_VERSIONS[path.stem] = str(declared) if declared else (
        "sha256:" + hashlib.sha256(raw).hexdigest()[:12]
    )

    types_data = data.get("types") or data.get("document_types") or {}
    field_sets = data.get("_field_sets", {}) or {}

    catalog: Dict[str, TypeRegistryEntry] = {}
    for type_id, type_data in types_data.items():
        catalog[type_id] = _parse_entry(type_id, type_data, field_sets)

    for rule in data.get("cross_document_validation_rules", []) or []:
        if rule not in _CROSS_DOC_RULES:
            _CROSS_DOC_RULES.append(rule)

    return catalog


def _load_from_json() -> Dict[str, TypeRegistryEntry]:
    """Load the single bundled catalog (`config/catalogs.json`).

    That file is generated at build time by combining every per-type file in
    `catalogs/` plus `catalogs/_shared.json` — the one source of truth.
    """
    if not _CONFIG_FILE.exists():
        logger.warning(f"Config file not found: {_CONFIG_FILE}")
        return {}
    try:
        catalog = _load_config_file(_CONFIG_FILE)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load catalog file {_CONFIG_FILE}: {e}")
        return {}

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
    _CROSS_DOC_RULES.clear()
    _SCHEMA_VERSIONS.clear()


def get_schema_info() -> Dict[str, Any]:
    """Identify the catalog schema that processed a document.

    Returns the per-file versions (declared `_schema_version` or content hash)
    and the number of loaded types, so every result can record exactly which
    schema version produced it.
    """
    catalog = _load_catalog()
    return {
        "catalogVersions": dict(_SCHEMA_VERSIONS),
        "typeCount": len(catalog),
        "source": "dynamodb+json" if _CATALOG_TABLE else "json",
    }


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


def get_cross_document_rules() -> List[Dict[str, Any]]:
    """Cross-document validation rules declared by loaded catalogs."""
    _load_catalog()
    return list(_CROSS_DOC_RULES)


def is_customs_type(doc_type: str) -> bool:
    """True when the type is routed through the expediente (customs) pipeline.

    Declared per type with `"pipeline": "expediente"` in its catalog file.
    """
    return get_type_entry(doc_type).pipeline == "expediente"


# Expose DOCUMENT_CATALOG for backward compatibility
DOCUMENT_CATALOG = property(lambda self: _load_catalog())
