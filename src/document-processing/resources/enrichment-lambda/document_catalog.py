"""
Document Catalog - Config-driven registry of document types.

Loads document type definitions from:
1. Local bundled catalog (config/catalogs.json) - built recursively from the
   active catalogs/base/ and catalogs/custom/ trees (excluding any save/ subtree)
2. DynamoDB table (optional, runtime override) - set CATALOG_TABLE env var

To add a new document type WITHOUT code changes:
- Edit or add catalogs/base/**/<TYPE>.json or catalogs/custom/**/<TYPE>.json,
  then `pnpm run build` and redeploy, OR
- Insert a record in the DynamoDB catalog table (takes effect immediately)

To add custom programmatic validation:
- Create a Node.js script in custom_validators/ (see README in that folder)
- Reference it in the type config as "custom_validator": "my_script"
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pipeline import confidence as conf_policy

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
class FieldSpec:
    """One field in the field-centric DSL (schema_version 4.x, see
    catalogs/_TEMPLATE.json) — hint, requiredness and validation live TOGETHER
    on the field instead of being spread across extraction_fields/
    extraction_hints/required_fields/validation_rules like the legacy DSL.

    `type` is one of "string"|"number"|"boolean"|"date" (scalar), "object"
    (recurses into `fields`) or "array" (recurses into `item_fields` — one
    row's schema — or, for arrays of plain scalars, just `item_type`).
    `validation` rules for object/array fields never carry a `field` key in
    the JSON: the field name is already the dict key, so `ValidationRule.field`
    is synthesized as `NAME` (scalar/object-level) or `NAME.SUBFIELD` (object
    rules with an explicit `subfield`) when parsed.
    """
    name: str
    type: str
    required: bool = False
    hint: str = ""
    validation: List[ValidationRule] = field(default_factory=list)
    fields: Dict[str, "FieldSpec"] = field(default_factory=dict)       # type == "object"
    item_fields: Dict[str, "FieldSpec"] = field(default_factory=dict)  # type == "array" of rows
    item_type: Optional[str] = None                                   # type == "array" of scalars
    # True when a deterministic post-extraction step (not the model) fills
    # this field — e.g. LIQUIDACION_NORMALIZADA, built by
    # pipeline.liquidacion.build_liquidacion_normalizada from CUADRO_LIQUIDACION
    # + CONTRIBUCIONES and unconditionally overwritten after extraction. Such
    # fields are left out of the Bedrock prompt/tool-schema entirely (asking
    # the model to guess a value that gets discarded wastes output tokens)
    # and their resolved output is tagged source="NORMALIZED".
    computed: bool = False
    # Every JSON key beyond the known FieldSpec attributes, kept verbatim so
    # config-driven consumers can declare their own per-field extras without a
    # dataclass change — e.g. `pipeline.liquidacion.build_liquidacion_normalizada`
    # reads `concept_aliases`/`source`/`value_field`/`filters`/`default` off a
    # LIQUIDACION_NORMALIZADA subfield's `params` instead of a separate
    # form_mapping block (see catalogs/base/aduanas/PEDIMENTO_ADUANAL.json).
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
    # Human-readable name of the type ("Pedimento Aduanal"). Optional: when
    # absent it is derived from `type_id`. Persisted as the DocumentTypeName
    # column in DynamoDB and accepted by `GET /documents?docType=`.
    display_name: str = ""
    # Extra names the API accepts for this type ("pedimento", "expediente
    # aduanal"). Only used to resolve `docType` inputs to a canonical type_id.
    aliases: List[str] = field(default_factory=list)
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
    # Floor below which a value is DISCARDED rather than merely flagged: the
    # field comes back with `value: null`, `valid: false` and its (low)
    # confidence, because a number nobody trusts that still LOOKS like data is
    # worse downstream than an explicit "no lo sé". Strictly stricter than
    # `min_confidence`, which keeps the value for a human to judge. Policy and
    # default (0.50) live in `pipeline.confidence`.
    discard_below_confidence: float = conf_policy.DISCARD_BELOW
    # Declared schema version for THIS type (from JSON or DynamoDB). Surfaced in
    # each result's tracking so you can tell which version processed a document,
    # including hot-reloaded DynamoDB overrides.
    schema_version: Optional[str] = None
    # Default cardinality when this type is explicitly requested and the POST
    # does not send `occurrenceMode` nor a per-type `:N`/`:all` override.
    # None means "use the global request default" (single for one declared
    # type, multiple for several).
    occurrence_mode: Optional[str] = None
    # Processing pipeline for this type. "expediente" routes through the
    # multi-document customs pipeline (segmenter + cross-document rules); any
    # other value (or None) uses the standard single-document flow.
    pipeline: Optional[str] = None
    # Optional per-type Bedrock model overrides — see
    # `pipeline.bedrock.resolve_model` for the full priority chain across
    # these two, the deployment's env config, and the dev-only per-request
    # override. `model_ids` (stage -> model id, e.g. {"extractor": "...",
    # "recovery": "..."}) wins over the blanket `model_id` when both name the
    # same stage, so a type can pin one phase while leaving the rest on its
    # general override. Neither is required; an unset/absent stage just falls
    # through to whatever's configured for the deployment.
    model_id: Optional[str] = None
    model_ids: Dict[str, str] = field(default_factory=dict)
    # Optional effort level (low|medium|high|xhigh|max) for models that take
    # one (Claude Haiku 5.5) — see `pipeline.bedrock.effort_for`. Unset =
    # the deployment's `BEDROCK_<STAGE>_EFFORT` / `BEDROCK_EFFORT` / `low`.
    model_effort: Optional[str] = None
    # Internal shape requested from the extraction model. ``compact`` emits
    # sparse scalar pairs and positional table rows; ``legacy`` preserves the
    # original nested {value, confidence} response.  None inherits the
    # deployment-wide AI_RESPONSE_FORMAT setting (whose safe default is
    # compact).  This changes only the model/tool protocol, never the public
    # result envelope.
    ai_response_format: Optional[str] = None
    # Target shapes for derived/structured outputs (e.g. LIQUIDACION_NORMALIZADA
    # defaults). Consumed by post-extraction normalizers.
    structured_fields: Dict[str, Any] = field(default_factory=dict)
    # Per-form field mappings, including `cuadro_liquidacion_fields` used to
    # normalize the customs liquidation table into LIQUIDACION_NORMALIZADA.
    form_mapping: Dict[str, Any] = field(default_factory=dict)
    # Field-centric DSL (schema_version 4.x): populated ONLY when the type's
    # JSON declares a top-level "fields" key (see catalogs/_TEMPLATE.json).
    # When set, this is authoritative and extraction_fields/extraction_hints/
    # required_fields/repeatable_fields/structured_fields are left empty —
    # consumers (text_extractor, engine.resolve_fields, expediente assembly)
    # must check `field_tree` first and only fall back to the legacy flat
    # attributes when it is None. `validation_rules` is still populated (from
    # `cross_field_validation`) since cross-field rules use the exact same
    # ValidationRule shape either way.
    field_tree: Optional[Dict[str, "FieldSpec"]] = None
    # Optional deterministic-first extraction policy for field-centric PDF/
    # image types. Per-field selectors live in FieldSpec.params["text_source"];
    # this type-level block opts the document in and chooses what happens when
    # selectors leave gaps (missing_fields | full_model | review). Detection is
    # by catalog shape, never TYPE_ID; see pipeline/deterministic_text.py.
    deterministic_extraction: Optional[Dict[str, Any]] = None
    # Optional policy for acquiring PDF text before extraction.  The default
    # remains eager native-text + Textract fallback.  A type may opt in to
    # deferred OCR only when its deterministic profile can prove that the
    # document is already complete; the deployment-wide kill switch still has
    # precedence.  See pipeline/text_acquisition.py and catalogs/README.md.
    text_acquisition: Optional[Dict[str, Any]] = None
    # Optional declarative QR-code recovery (pipeline/qr_recovery.py): when
    # set, the pipeline rasterizes the first page of a matched segment,
    # decodes any QR code found, and maps its URL query params onto fields
    # per this spec — e.g. the pedimento's embedded SAT/SOIANET verification
    # QR. Shape: {"confidence": float, "fields": {NAME: {"param", "digits",
    # "slice"?}}}. None (the default) means the type has no QR to recover
    # from; nothing in the generic pipeline is coupled to which type this is.
    qr_recovery: Optional[Dict[str, Any]] = None
    # Optional declarative page selection (pipeline/page_selector.py): when
    # set, only the pages whose text matches this type's `markers` are sent to
    # the expensive stages (Textract OCR / Bedrock extraction), instead of
    # every page of the upload. Shape: {"enabled"?: bool, "markers": [regex],
    # "min_score"?: int, "max_pages"?: int, "follow_pages"?: int, ...}.
    # None (the default) means the type is never filtered — its pages take the
    # exact same path they take today. Declaring the block is what opts a type
    # in; `page_selector.enabled_for` resolves that against the global
    # PAGE_SELECTION_ENABLED kill-switch.
    page_selection: Optional[Dict[str, Any]] = None
    # Optional declarative PHYSICAL-AUTHENTICITY stage (pipeline/authenticity.py):
    # when set, the pipeline checks that the upload is a genuine capture of a
    # paper document (printed, wet-signed, stamped, scanned) rather than a
    # digital composition with a pasted signature/stamp image. Four layers, each
    # independently switchable inside the block: `structure` (PDF internals,
    # free), `pixels` (background noise, free), `vision` (a multimodal model
    # looks at the page) and `memory` (cross-document fingerprints + registered
    # references). Results land on ordinary `computed` fields that opt in with
    # a per-field `authenticity_source`, so the normal rule engine grades them
    # (`boolean_true` + `on_fail`). None (the default) means the type pays
    # nothing and takes exactly the path it takes today; nothing in the generic
    # pipeline is coupled to which type declares it. See catalogs/README.md →
    # "Autenticidad física (`authenticity`)".
    authenticity: Optional[Dict[str, Any]] = None
    # Optional tuning of the TYPE CHECK (pipeline/type_check.py): "is this
    # file really the document that was declared?" — asked of the model in
    # the same extraction call, graded like any rule. Every field-centric
    # type gets it by default; this block only adjusts it. Shape:
    # {"enabled"?: bool, "on_fail"?: "reject"|"review"|"info",
    #  "min_required_filled"?: 0.0-1.0}. None = the defaults (on, `reject`,
    # 1.0). See catalogs/README.md → "Verificación de tipo (`type_check`)".
    type_check: Optional[Dict[str, Any]] = None
    # Declarative UNIQUE KEY(s) for this type: the field(s) whose value
    # identifies one document of this type on its own (a pedimento number, a
    # CFDI UUID...). Written by `unique_keys.py` to the lookup table as
    # `TYPE_ID#<normalized value>` so `GET /documents?docType=&field=&value=`
    # resolves with a single indexed Query instead of walking every document
    # of the type. Shape here is always normalized to
    # [{"field": NAME, "normalize": "upper"|"alnum"|"digits"|"raw"}].
    #
    # Two equivalent ways to declare it in the catalog JSON, both parsed into
    # this list (see `_parse_unique_keys`):
    #   - field-centric DSL, on the field itself:  "unique_key": true
    #     (or {"normalize": "alnum"}) inside `fields[NAME]`
    #   - type level (works for the legacy DSL too): "unique_key": "CAMPO"
    #     / ["CAMPO_A", "CAMPO_B"] / [{"field": "...", "normalize": "..."}]
    # Empty (the default) means the type has no unique key and searches on it
    # take the same path they take today.
    unique_keys: List[Dict[str, Any]] = field(default_factory=list)
    # Opt-OUT of auto-classification candidacy (`is_segmentation_candidate`).
    # None (the default) means "decide by shape", which is what every type
    # wants; set `"segmentation_candidate": false` in the catalog JSON only for
    # a type the segmenter keeps confusing with a sibling, to take it out of the
    # candidate list without touching code. It never affects a type the CALLER
    # declared explicitly — naming a type in `documentType` is a stronger signal
    # than any catalog default.
    segmentation_candidate: Optional[bool] = None
    # XML namespace prefix -> URI map, used by pipeline/xml_document.py to
    # resolve each field's `xpath`/`item_xpath` (a field-centric-DSL field
    # extra, kept in FieldSpec.params like any other — see catalogs/README.md).
    # Applies to any type whose fields declare an `xpath`, not just ones named
    # with an `_XML` suffix (that suffix is a file-naming convention only,
    # detection of which pipeline runs is by the uploaded file's extension).
    namespaces: Dict[str, str] = field(default_factory=dict)


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
# Single bundled catalog, produced recursively from active catalogs by `pnpm build`
# (scripts/sync-catalogs.mjs). One base, one file.
_CONFIG_FILE = _CONFIG_DIR / "catalogs.json"
_CATALOG_TABLE = os.environ.get("CATALOG_TABLE")  # DynamoDB table name (optional)
_CATALOG_CACHE: Optional[Dict[str, TypeRegistryEntry]] = None
_CATALOG_CACHE_LOADED_AT: Optional[float] = None
# Version of each loaded catalog file: declared `_schema_version` if present,
# else a short content hash so the identifier is always deterministic and
# changes whenever the schema does. Keyed by file stem (e.g. "catalogs").
_SCHEMA_VERSIONS: Dict[str, str] = {}


_RULE_CORE_KEYS = {"field", "rule_type", "pattern", "message"}
_FIELD_SPEC_CORE_KEYS = {"type", "required", "hint", "validation", "fields", "item_fields", "item_type", "subfield", "computed"}


def _parse_occurrence_mode(value: Any) -> Optional[str]:
    """Normalize the optional catalog default without making runtime brittle.

    Build/sync rejects invalid local catalog values. DynamoDB is hot-reloadable
    and can be edited outside that tooling, so the runtime treats an invalid
    value as absent and falls back to the global request rule.
    """
    if value is None or value == "":
        return None
    normalized = str(value).strip().lower()
    if normalized in {"single", "multiple"}:
        return normalized
    logger.warning("catalog occurrence_mode inválido %r; usando default global", value)
    return None


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


def _parse_field_validation(owner_name: str, rules_data: List[Dict[str, Any]]) -> List[ValidationRule]:
    """Parse the `validation` list nested inside a field-centric DSL field.

    Unlike top-level `validation_rules`/`cross_field_validation`, these entries
    never carry a `field` key in the JSON (the field name is already the dict
    key they live under) — it's synthesized here as `owner_name`, or
    `owner_name.subfield` when the rule declares an explicit `subfield` (used
    by object-level rules that check one of the object's own subfields, e.g.
    CUADRO_LIQUIDACION's `validation` checking its `TOTAL` subfield).
    """
    rules = []
    for r in rules_data or []:
        target = f"{owner_name}.{r['subfield']}" if r.get("subfield") else owner_name
        rules.append(
            ValidationRule(
                field=target,
                rule_type=r.get("rule_type", ""),
                pattern=r.get("pattern"),
                message=r.get("message", ""),
                params={k: v for k, v in r.items() if k not in _RULE_CORE_KEYS and k != "subfield"},
            )
        )
    return rules


def _parse_field_spec(name: str, data: Dict[str, Any], common_item_fields: Dict[str, Any]) -> FieldSpec:
    ftype = data.get("type", "string")
    spec = FieldSpec(
        name=name,
        type=ftype,
        required=bool(data.get("required", False)),
        hint=data.get("hint", ""),
        validation=_parse_field_validation(name, data.get("validation", [])),
        computed=bool(data.get("computed", False)),
        params={k: v for k, v in data.items() if k not in _FIELD_SPEC_CORE_KEYS},
    )
    if ftype == "object":
        spec.fields = _parse_field_tree(data.get("fields", {}), common_item_fields)
    elif ftype == "array":
        if "item_fields" in data:
            spec.item_fields = _parse_field_tree(data["item_fields"], common_item_fields)
        else:
            spec.item_type = data.get("item_type", "string")
    return spec


def _parse_field_tree(
    fields_data: Dict[str, Any], common_item_fields: Dict[str, Any], presets: bool = False
) -> Dict[str, FieldSpec]:
    """Parse a `fields` (or `item_fields`) block, expanding `$common` and
    `$presets` refs.

    `"$common": ["PAGINA", "ORDEN", ...]` pulls those subfield definitions
    from `_shared.json`'s `_common_item_fields` instead of repeating them —
    see catalogs/_TEMPLATE.json for the convention.

    `"$presets": ["INTEGRIDAD_PDF", ...]` pulls WHOLE field sets from
    `_shared.json`'s `_field_presets` (the authenticity/integrity computed
    fields, with their rules and messages) so a type opts into a verification
    with one line. Preset fields come first; a field the type declares itself
    under the same name REPLACES the preset's — that is how a type hardens
    one signal from `review` to `reject` without copying the rest.
    """
    result: Dict[str, FieldSpec] = {}
    if "$presets" in fields_data and not presets:
        logger.warning("`$presets` only applies to a type's top-level `fields`, not to item_fields; ignored")
    for preset_name in _as_name_list(fields_data.get("$presets")) if presets else []:
        preset = _FIELD_PRESETS.get(preset_name)
        if not isinstance(preset, dict):
            logger.warning("unknown field preset '%s' (see _shared.json → _field_presets); ignored", preset_name)
            continue
        for name, raw in preset.items():
            if name.startswith("__") or not isinstance(raw, dict):
                continue
            result[name] = _parse_field_spec(name, raw, common_item_fields)
    for name, raw in fields_data.items():
        if name == "$presets":
            continue
        if name == "$common":
            for common_name in raw:
                common_raw = common_item_fields.get(common_name)
                if common_raw:
                    spec = _parse_field_spec(common_name, common_raw, common_item_fields)
                    # Row bookkeeping (page, position, printed line, row
                    # confidence) is filled by the pipeline after extraction
                    # (`pipeline.row_common`), never asked to the model: on a
                    # real constancia VALOR_CRUDO alone was ~30% of the table
                    # JSON the model generated, and ORDEN/PAGINA are derivable.
                    spec.computed = True
                    spec.params["row_common"] = common_name
                    result[common_name] = spec
            continue
        result[name] = _parse_field_spec(name, raw, common_item_fields)
    return result


def _as_name_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


# Value-normalization modes accepted by a unique key declaration. The
# implementation lives in `unique_keys.normalize_value` (mirrored in
# query-lambda/index.mjs) — this is only the allow-list used at parse time so a
# typo in the catalog degrades to the default instead of producing keys nothing
# can ever look up.
UNIQUE_KEY_NORMALIZERS = ("upper", "alnum", "digits", "raw")
_DEFAULT_UNIQUE_KEY_NORMALIZER = "upper"


def _unique_key_spec(name: str, options: Any) -> Optional[Dict[str, Any]]:
    """One normalized {"field", "normalize"} spec, or None if not a key.

    `options` is whatever the catalog put next to the field name: `True`,
    a `{"normalize": ...}` dict, or `False`/None to opt out explicitly.
    """
    if options is False or options is None:
        return None
    normalize = _DEFAULT_UNIQUE_KEY_NORMALIZER
    if isinstance(options, dict):
        candidate = str(options.get("normalize") or "").strip().lower()
        if candidate in UNIQUE_KEY_NORMALIZERS:
            normalize = candidate
        elif candidate:
            logger.warning(
                "unknown unique_key normalize '%s' on field %s — using '%s'",
                candidate, name, _DEFAULT_UNIQUE_KEY_NORMALIZER,
            )
    return {"field": name, "normalize": normalize}


def _parse_unique_keys(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every unique key declared for a type, in lookup order.

    Merges the TYPE-level declaration (`unique_key`/`unique_keys`, the only
    option for legacy-DSL types) with the FIELD-level one (`unique_key` on a
    `fields[NAME]` entry, field-centric DSL), deduplicated by field name so
    declaring it in both places is harmless rather than duplicated work at
    write time. Accepted type-level shapes:

        "unique_key": "NUMERO_PEDIMENTO"
        "unique_key": ["NUMERO_PEDIMENTO", "NUMERO_PEDIMENTO_NORMALIZADO"]
        "unique_key": [{"field": "UUID_FISCAL", "normalize": "alnum"}]
    """
    specs: List[Dict[str, Any]] = []
    seen: set = set()

    def add(spec: Optional[Dict[str, Any]]) -> None:
        if spec and spec["field"] not in seen:
            seen.add(spec["field"])
            specs.append(spec)

    raw = data.get("unique_keys", data.get("unique_key"))
    declared = raw if isinstance(raw, list) else ([raw] if raw else [])
    for item in declared:
        if isinstance(item, str):
            add(_unique_key_spec(item.strip(), True))
        elif isinstance(item, dict):
            name = str(item.get("field") or "").strip()
            if name:
                add(_unique_key_spec(name, item))

    for name, field_data in (data.get("fields") or {}).items():
        if name in ("$common", "$presets") or not isinstance(field_data, dict):
            continue
        if "unique_key" in field_data:
            add(_unique_key_spec(name, field_data["unique_key"]))

    return specs


def _parse_entry(
    type_id: str,
    data: Dict[str, Any],
    field_sets: Optional[Dict[str, List[str]]] = None,
    common_item_fields: Optional[Dict[str, Any]] = None,
) -> TypeRegistryEntry:
    """Parse a single type entry from JSON config.

    `field_sets` resolves the customs-catalog `field_sets` mechanism: named
    lists of shared fields (e.g. COMMON_CFDI_FIELDS) prepended to the type's
    own extraction_fields. Types written in the field-centric DSL (top-level
    "fields" key present, schema_version 4.x — see catalogs/_TEMPLATE.json)
    are parsed into `field_tree` instead of the legacy flat attributes;
    `common_item_fields` resolves their `$common` references.
    """
    if "fields" in data:
        return TypeRegistryEntry(
            type_id=type_id,
            extraction_fields=[],
            required_fields=[],
            validation_rules=_parse_rules(data.get("cross_field_validation", [])),
            validator_module=data.get("validator_module"),
            custom_validator=data.get("custom_validator"),
            description=data.get("description", ""),
            display_name=data.get("display_name", "") or "",
            aliases=data.get("aliases", []) or [],
            business_destination=data.get("business_destination", ""),
            min_confidence=float(data.get("min_confidence", 0.70)),
            discard_below_confidence=float(
                data.get("discard_below_confidence", conf_policy.DISCARD_BELOW)
            ),
            schema_version=data.get("schema_version"),
            occurrence_mode=_parse_occurrence_mode(data.get("occurrence_mode")),
            form_mapping=data.get("form_mapping", {}) or {},
            pipeline=data.get("pipeline"),
            model_id=data.get("model_id") or None,
            model_ids=data.get("model_ids", {}) or {},
            model_effort=data.get("model_effort") or None,
            ai_response_format=(
                str(data.get("ai_response_format")).strip().lower()
                if data.get("ai_response_format") is not None else None
            ),
            field_tree=_parse_field_tree(data["fields"], common_item_fields or {}, presets=True),
            deterministic_extraction=(
                data.get("deterministic_extraction")
                if isinstance(data.get("deterministic_extraction"), dict)
                else None
            ),
            text_acquisition=(
                data.get("text_acquisition")
                if isinstance(data.get("text_acquisition"), dict)
                else None
            ),
            unique_keys=_parse_unique_keys(data),
            qr_recovery=data.get("qr_recovery"),
            page_selection=data.get("page_selection"),
            authenticity=data.get("authenticity"),
            type_check=data.get("type_check") if isinstance(data.get("type_check"), dict) else None,
            segmentation_candidate=data.get("segmentation_candidate"),
            namespaces=data.get("namespaces", {}) or {},
        )

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
        display_name=data.get("display_name", "") or "",
        aliases=data.get("aliases", []) or [],
        extraction_hints=data.get("extraction_hints", {}) or {},
        extraction_strategy=data.get("extraction_strategy"),
        field_aliases=data.get("field_aliases", {}) or {},
        repeatable_fields=data.get("repeatable_fields", {}) or {},
        allowed_subtypes=data.get("allowed_subtypes", []) or [],
        business_destination=data.get("business_destination", ""),
        min_confidence=float(data.get("min_confidence", 0.70)),
        discard_below_confidence=float(
            data.get("discard_below_confidence", conf_policy.DISCARD_BELOW)
        ),
        schema_version=data.get("schema_version"),
        occurrence_mode=_parse_occurrence_mode(data.get("occurrence_mode")),
        structured_fields=data.get("structured_fields", {}) or {},
        form_mapping=data.get("form_mapping", {}) or {},
        pipeline=data.get("pipeline"),
        model_id=data.get("model_id") or None,
        model_ids=data.get("model_ids", {}) or {},
        model_effort=data.get("model_effort") or None,
        ai_response_format=(
            str(data.get("ai_response_format")).strip().lower()
            if data.get("ai_response_format") is not None else None
        ),
        text_acquisition=(
            data.get("text_acquisition")
            if isinstance(data.get("text_acquisition"), dict)
            else None
        ),
        qr_recovery=data.get("qr_recovery"),
        page_selection=data.get("page_selection"),
        authenticity=data.get("authenticity"),
        segmentation_candidate=data.get("segmentation_candidate"),
        unique_keys=_parse_unique_keys(data),
    )


_CROSS_DOC_RULES: List[Dict[str, Any]] = []
# _shared.json's _common_item_fields, cached at module scope so the
# DynamoDB hot-reload path (_load_from_dynamodb, which parses one item at a
# time with no access to the bundled file) can resolve `$common` refs too.
_COMMON_ITEM_FIELDS: Dict[str, Any] = {}
# Same for `_field_presets` / `$presets`. Both live in the BUNDLED file: a
# change to a preset needs `pnpm build` + deploy, while a type's own override
# of a preset field is hot-reloadable like any other field.
_FIELD_PRESETS: Dict[str, Any] = {}
# `_authenticity_elements`: what a signature, a stamp or a logo IS (how the
# vision model is told to recognize it, how alike two of them may be), defined
# once so any type asks for one by name in its `authenticity.elements`. Bundled
# like the presets; a type's own override of an element is hot-reloadable.
_AUTHENTICITY_ELEMENTS: Dict[str, Any] = {}


def shared_authenticity_elements() -> Dict[str, Any]:
    """The shared element definitions, as loaded with the bundled catalog.
    Deliberately does NOT trigger a load: whoever holds a type entry got it
    from a loaded catalog, and a tool that parses a type by hand loads
    `_shared.json` itself (`load_shared`)."""
    return _AUTHENTICITY_ELEMENTS


def load_shared(data: Dict[str, Any]) -> None:
    """Register the shared blocks of a `_shared.json`-shaped dict (presets,
    common item fields, authenticity elements). For tools and tests that parse
    catalog files straight from `catalogs/` instead of the bundle."""
    for target, key in ((_COMMON_ITEM_FIELDS, "_common_item_fields"), (_FIELD_PRESETS, "_field_presets"),
                        (_AUTHENTICITY_ELEMENTS, "_authenticity_elements")):
        target.update({k: v for k, v in (data.get(key) or {}).items()
                       if not k.startswith("__") and isinstance(v, dict)})


def _load_config_file(path: Path) -> Dict[str, TypeRegistryEntry]:
    """Load one catalog file. Supports two shapes:

    - Classic: {"types": {TYPE_ID: {...}}}
    - Customs: {"document_types": {TYPE_ID: {...}}, "_field_sets": {...},
                "_common_item_fields": {...}, "cross_document_validation_rules": [...]}
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
    common_item_fields = data.get("_common_item_fields", {}) or {}
    if common_item_fields:
        _COMMON_ITEM_FIELDS.update(
            {k: v for k, v in common_item_fields.items() if not k.startswith("__")}
        )
    field_presets = data.get("_field_presets", {}) or {}
    if field_presets:
        _FIELD_PRESETS.update(
            {k: v for k, v in field_presets.items() if not k.startswith("__") and isinstance(v, dict)}
        )

    shared_elements = data.get("_authenticity_elements", {}) or {}
    if shared_elements:
        _AUTHENTICITY_ELEMENTS.update(
            {k: v for k, v in shared_elements.items() if not k.startswith("__") and isinstance(v, dict)}
        )

    catalog: Dict[str, TypeRegistryEntry] = {}
    for type_id, type_data in types_data.items():
        catalog[type_id] = _parse_entry(type_id, type_data, field_sets, _COMMON_ITEM_FIELDS)

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
        import aws_clients  # deferred: keeps this module importable without AWS

        table = aws_clients.table(_CATALOG_TABLE)
        response = table.scan()
        items = response.get("Items", [])

        catalog: Dict[str, TypeRegistryEntry] = {}
        for item in items:
            if not item.get("enabled", True):
                continue
            type_id = item["type_id"]
            catalog[type_id] = _parse_entry(type_id, item, common_item_fields=_COMMON_ITEM_FIELDS)

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
    global _CATALOG_CACHE, _CATALOG_CACHE_LOADED_AT

    try:
        ttl = max(0.0, float(os.environ.get("CATALOG_CACHE_TTL_SECONDS", "30")))
    except ValueError:
        ttl = 30.0
    now = time.monotonic()
    if _CATALOG_CACHE is not None and (
        # Tests and local tools intentionally inject a prepared catalog by
        # assigning only _CATALOG_CACHE.  Preserve that supported seam instead
        # of unexpectedly replacing it with DynamoDB on the next read.
        _CATALOG_CACHE_LOADED_AT is None
        or ttl <= 0
        or now - _CATALOG_CACHE_LOADED_AT < ttl
    ):
        return _CATALOG_CACHE

    # Base: local JSON config (always available, deployed with Lambda)
    catalog = _load_from_json()

    # Override/extend: DynamoDB (optional, for runtime changes)
    db_catalog = _load_from_dynamodb()
    catalog.update(db_catalog)

    _CATALOG_CACHE = catalog
    _CATALOG_CACHE_LOADED_AT = now
    return catalog


def reload_catalog() -> None:
    """Force reload of the catalog (useful for testing or after DB updates)."""
    global _CATALOG_CACHE, _CATALOG_CACHE_LOADED_AT
    _CATALOG_CACHE = None
    _CATALOG_CACHE_LOADED_AT = None
    _CROSS_DOC_RULES.clear()
    _SCHEMA_VERSIONS.clear()
    _COMMON_ITEM_FIELDS.clear()
    _FIELD_PRESETS.clear()
    _AUTHENTICITY_ELEMENTS.clear()


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
    """True when the type opted into the multi-document customs pipeline.

    Declared per type with `"pipeline": "expediente"` in its catalog file.
    This gates two EXTRA behaviors on top of plain field extraction: (1)
    candidacy for auto-segmentation inside an unlabeled multi-document PDF
    (`segmenter._candidate_types`), and (2) cross-document validation rules.
    It does NOT gate whether a type's fields get extracted at all — any type
    whose catalog entry uses the field-centric DSL (`field_tree` populated
    from a top-level "fields" key) is routed through the expediente
    pipeline's extractor regardless of this flag, since that pipeline is the
    only one that understands the field-centric DSL (see handler.py's
    `_process`).
    """
    return get_type_entry(doc_type).pipeline == "expediente"


def normalize_declared_types(
    declared_types: Any = None, declared_type: Optional[str] = None
) -> List[str]:
    """The declared types as a clean, de-duplicated list, in the order given.

    Accepts either form the pipelines are called with — a bare `declared_type`
    string or a `declared_types` sequence — and, deliberately, a plain STRING in
    the `declared_types` slot too. A `str` satisfies `Sequence[str]`, so without
    this every caller that passes one would silently iterate its CHARACTERS and
    look up types called "C", "O", "N"... That is not a hypothetical: the
    pipelines are called directly by `scripts/procesar-docs.py` and by tests, not
    only through the S3 key.
    """
    if isinstance(declared_types, str):
        candidates = [declared_types]
    elif declared_types:
        candidates = list(declared_types)
    else:
        candidates = [declared_type] if declared_type else []

    out: List[str] = []
    for candidate in candidates:
        name = str(candidate).strip() if candidate else ""
        if name and name not in out:
            out.append(name)
    return out


def resolve_declared_counts(
    declared_types: Any = None,
    declared_counts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve requested cardinality with one stable precedence chain.

    `declared_counts` contains the strongest request-level decisions: an
    explicit `TIPO:N`/`:all`, or a POST `occurrenceMode` already materialized
    by Upload for every unsuffixed type. Missing types then use their catalog
    `occurrence_mode`; if that property is absent, the global default is one
    occurrence when one type was declared and all occurrences when several
    types were declared.
    """
    types = normalize_declared_types(declared_types)
    explicit = declared_counts or {}
    global_default: Any = "all" if len(types) > 1 else 1
    resolved: Dict[str, Any] = {}
    for doc_type in types:
        if doc_type in explicit:
            resolved[doc_type] = explicit[doc_type]
            continue
        try:
            catalog_mode = get_type_entry(doc_type).occurrence_mode
        except Exception:
            catalog_mode = None
        if catalog_mode == "multiple":
            resolved[doc_type] = "all"
        elif catalog_mode == "single":
            resolved[doc_type] = 1
        else:
            resolved[doc_type] = global_default
    return resolved


def _has_structured_source(specs: Optional[Dict[str, "FieldSpec"]]) -> bool:
    """Whether anything in this field tree is read from an already-structured
    source (XML via `xpath`/`item_xpath`, XLSX/CSV via `cell`/`column`) instead
    of from the text of a page.

    Detected by SHAPE, never by TYPE_ID: the `_XML`/`_XLSX` suffixes on catalog
    filenames are a human naming convention, and `handler.py` routes to those
    pipelines by the uploaded FILE's extension. Same recursive-walk pattern as
    `pipeline.derived.has_derived`.
    """
    for spec in (specs or {}).values():
        params = getattr(spec, "params", {}) or {}
        if any(params.get(k) for k in ("xpath", "item_xpath", "cell", "column", "column_letter")):
            return True
        if _has_structured_source(getattr(spec, "fields", None)):
            return True
        if _has_structured_source(getattr(spec, "item_fields", None)):
            return True
    return False


def is_segmentation_candidate(doc_type: str) -> bool:
    """True when this type may be OFFERED to the segmenter as a candidate for an
    upload that declared no type.

    This is deliberately NOT `is_customs_type`. That flag (`pipeline:
    "expediente"`) previously did double duty, so field-centric types such as
    `PEDIMENTO_ADUANAL` could be absent from the candidate list for an
    unlabelled upload.

    Candidacy is decided by SHAPE, never by TYPE_ID:
      1. the type has a field-centric tree — nothing else can be extracted from
         a page range by the expediente pipeline;
      2. its fields are readable from page text, i.e. it is not backed by an
         already-structured source (see `_has_structured_source`) — an XML type
         is routed by file extension and its xpaths mean nothing on a scan;
      3. it has a `description`, which is the discriminator the segmenter prompt
         prints for the model to choose by;
      4. it has not opted out with `"segmentation_candidate": false`.

    Only the UNDECLARED path consults this. A type the caller named explicitly
    in `documentType` is always a candidate — see `segmenter._candidate_types`.
    """
    entry = get_type_entry(doc_type)
    if entry.type_id == "OTRO" or entry.segmentation_candidate is False:
        return False
    if entry.field_tree is None or not entry.description:
        return False
    return not _has_structured_source(entry.field_tree)


# Expose DOCUMENT_CATALOG for backward compatibility
DOCUMENT_CATALOG = property(lambda self: _load_catalog())
