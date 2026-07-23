"""EnrichmentLambda handler - Config-driven validation engine.

This handler is invoked by the Step Functions workflow after classification
and extraction. It:
1. Reads classificationResult and processingResult from the event
2. Loads document type config from JSON file or DynamoDB (no code changes needed)
3. Runs deterministic validation checks via the appropriate validator module
4. Optionally re-extracts from the raw S3 object using a stronger
   Textract + Bedrock strategy when the catalog asks for it or when the
   initial processingResult fails structural checks (missing required
   fields or regex mismatches). Recovers CURP/RFC/Clave de Elector
   errors caused by weak vision OCR on photos.
5. Optionally runs custom Node.js validators for advanced logic
6. Returns a Canonical_Result with status and detailed checks

EXTENSIBILITY:
- Add new document types: edit config/document_types.json OR insert in DynamoDB
- Enable Textract fallback for a type: set `extraction_strategy` in its catalog
  entry (`textract_bedrock` or `textract_analyze_id`)
- Add custom validation logic: create a .mjs file in custom_validators/
- No handler edits required for any of the above.
"""

import importlib
import json
import logging
import mimetypes
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from document_catalog import (
    CheckResult,
    TypeRegistryEntry,
    get_type_entry,
    is_customs_type,
    reload_catalog,
)
from extractors import get_extractor

logger = logging.getLogger(__name__)

_CUSTOM_VALIDATORS_DIR = Path(__file__).parent / "custom_validators"
_FORCE_RELOAD = os.environ.get("CATALOG_FORCE_RELOAD", "false").lower() == "true"
_STRUCTURAL_RULES = frozenset({"regex", "presence"})


def entities_to_map(entities: List[Dict[str, str]]) -> Dict[str, str]:
    """Convert entity list from Bedrock extraction to a flat dictionary."""
    return {e.get("type", ""): e.get("value", "") for e in entities if e.get("type")}


def determine_status(checks: List[CheckResult]) -> str:
    """Determine overall validation status from check results.

    - APROBADO: all checks passed
    - RECHAZADO: at least one check failed
    - REQUIERE_REVISION: no checks were executed
    """
    if not checks:
        return "REQUIERE_REVISION"
    if any(not c.valid for c in checks):
        return "RECHAZADO"
    return "APROBADO"


def build_canonical_result(
    document_id: str,
    classification: str,
    entity_map: Dict[str, str],
    checks: List[CheckResult],
    status: str,
) -> Dict[str, Any]:
    """Assemble the Canonical_Result output JSON."""
    total = len(checks)
    passed = sum(1 for c in checks if c.valid)

    if total == 0:
        summary = "No validation rules applied - requires manual review"
    elif passed == total:
        summary = f"All {total} checks passed"
    else:
        failed = total - passed
        summary = f"{failed} of {total} checks failed"

    return {
        "documentId": document_id,
        "documentClassification": classification,
        "extractedEntities": entity_map,
        "validation": {
            "status": status,
            "checks": [
                {
                    "field": c.field,
                    "rule": c.rule,
                    "valid": c.valid,
                    "message": c.message,
                }
                for c in checks
            ],
            "summary": summary,
        },
        "processedAt": datetime.now(timezone.utc).isoformat(),
    }


def run_python_validation(
    entity_map: Dict[str, str], classification: str
) -> List[CheckResult]:
    """Back-compat wrapper: resolve the type entry and delegate."""
    return _run_validation(entity_map, get_type_entry(classification))


def _run_validation(
    entity_map: Dict[str, str], type_entry: TypeRegistryEntry
) -> List[CheckResult]:
    """Run Python-based validation via the catalog's validator_module."""
    if not type_entry.validator_module:
        return []

    try:
        module = importlib.import_module(type_entry.validator_module)
        return module.validate_document(
            entity_map, type_entry.validation_rules, type_entry.required_fields
        )
    except (ImportError, AttributeError) as e:
        logger.error(f"Failed to load validator module '{type_entry.validator_module}': {e}")
        return [
            CheckResult(
                field="_module",
                rule="module_load",
                valid=False,
                message=f"Validator module load error: {e}",
            )
        ]


def _failure_count(checks: List[CheckResult]) -> int:
    """Number of failing checks in the list."""
    return sum(1 for c in checks if not c.valid)


def _has_structural_failure(checks: List[CheckResult]) -> bool:
    """True if any regex or presence check failed.

    Structural failures (missing required field, bad CURP/RFC pattern) are
    exactly the ones Textract re-extraction can fix. Date expiry failures
    are NOT structural — re-OCR cannot make an expired document not expired,
    so we do not burn a Textract call on them.
    """
    return any(not c.valid and c.rule in _STRUCTURAL_RULES for c in checks)


def _extract_s3_pointer(event: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Pull the source bucket/key out of the Step Functions state, tolerantly.

    The Bedrock invoke Lambda (patched) reads `event['content']['bucket']`
    and `event['content']['key']`, and that same state flows into the
    enrichment step. Returns None when neither shape is present.
    """
    content = event.get("content") or {}
    if isinstance(content, dict):
        bucket = content.get("bucket")
        key = content.get("key")
        if bucket and key:
            return str(bucket), str(key)

    # Some pipelines nest under `source` or pass the uri directly.
    source = event.get("source") or {}
    if isinstance(source, dict):
        bucket = source.get("bucket") or source.get("Bucket")
        key = source.get("key") or source.get("Key")
        if bucket and key:
            return str(bucket), str(key)

    return None


def _fetch_document(bucket: str, key: str) -> Tuple[bytes, str]:
    """Download the raw document from S3 and return its bytes + MIME type."""
    import boto3  # defer boto3 import; Lambda runtime provides it

    response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    content_type = response.get("ContentType") or mimetypes.guess_type(key)[0] or ""
    return body, content_type


def _try_reextract(
    event: Dict[str, Any],
    type_entry: TypeRegistryEntry,
) -> Optional[Tuple[List[Dict[str, str]], Dict[str, Any]]]:
    """Attempt Textract-backed re-extraction for this document.

    Returns (new_entities, metadata) on success, None when anything prevents
    the fallback from running (missing S3 pointer, unsupported strategy,
    provider error). Caller decides whether to swap based on validation delta.
    """
    extractor = get_extractor(type_entry.extraction_strategy)
    if extractor is None:
        return None

    pointer = _extract_s3_pointer(event)
    if pointer is None:
        logger.info(
            "Re-extraction skipped for %s: no S3 pointer in event",
            type_entry.type_id,
        )
        return None

    bucket, key = pointer
    try:
        document_bytes, content_type = _fetch_document(bucket, key)
    except Exception as e:
        logger.error("Failed to fetch s3://%s/%s for re-extraction: %s", bucket, key, e)
        return None

    try:
        result = extractor.extract(document_bytes, content_type, type_entry)
    except Exception as e:
        logger.error(
            "Extractor '%s' failed for %s: %s",
            extractor.strategy_name,
            type_entry.type_id,
            e,
        )
        return None

    metadata = {
        "strategy": result.strategy,
        "source": {"bucket": bucket, "key": key},
        "fields_recovered": sum(1 for e in result.entities if e.get("value")),
    }
    return result.entities, metadata


def run_custom_node_validator(
    script_name: str,
    entity_map: Dict[str, str],
    document_id: str,
    classification: str,
) -> List[CheckResult]:
    """Run a custom Node.js validation script.

    The script receives entityMap and context via stdin as JSON,
    and returns an array of check results via stdout.
    """
    script_path = _CUSTOM_VALIDATORS_DIR / f"{script_name}.mjs"
    if not script_path.exists():
        logger.warning(f"Custom validator script not found: {script_path}")
        return [
            CheckResult(
                field="_custom",
                rule="custom:script_missing",
                valid=False,
                message=f"Custom validator '{script_name}' not found",
            )
        ]

    # Build the Node.js runner inline
    runner_code = f"""
import {{ validate }} from './{script_name}.mjs';
import {{ readFileSync }} from 'fs';
const input = JSON.parse(readFileSync('/dev/stdin', 'utf8'));
const result = await validate(input.entityMap, input.context);
process.stdout.write(JSON.stringify(result));
"""

    input_payload = json.dumps({
        "entityMap": entity_map,
        "context": {
            "documentId": document_id,
            "classification": classification,
        },
    })

    try:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", runner_code],
            input=input_payload,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(_CUSTOM_VALIDATORS_DIR),
        )

        if result.returncode != 0:
            logger.error(f"Custom validator '{script_name}' failed: {result.stderr}")
            return [
                CheckResult(
                    field="_custom",
                    rule=f"custom:{script_name}",
                    valid=False,
                    message=f"Custom validator error: {result.stderr[:200]}",
                )
            ]

        checks_data = json.loads(result.stdout)
        return [
            CheckResult(
                field=c.get("field", "_custom"),
                rule=c.get("rule", f"custom:{script_name}"),
                valid=c.get("valid", False),
                message=c.get("message", ""),
            )
            for c in checks_data
        ]

    except subprocess.TimeoutExpired:
        return [
            CheckResult(
                field="_custom",
                rule=f"custom:{script_name}",
                valid=False,
                message=f"Custom validator '{script_name}' timed out (10s)",
            )
        ]
    except Exception as e:
        logger.error(f"Custom validator '{script_name}' exception: {e}")
        return [
            CheckResult(
                field="_custom",
                rule=f"custom:{script_name}",
                valid=False,
                message=f"Custom validator exception: {e}",
            )
        ]


_EXPEDIENTE_STATUS_MAP = {
    "APPROVED": "APROBADO",
    "REJECTED": "RECHAZADO",
    "PARTIALLY_APPROVED": "REQUIERE_REVISION",
    "REVIEW_REQUIRED": "REQUIERE_REVISION",
    "PROCESSING_ERROR": "REQUIERE_REVISION",
}


def _run_expediente(
    event: Dict[str, Any], document_id: str, classification: str
) -> Optional[Dict[str, Any]]:
    """Run the expediente pipeline against the raw S3 object.

    Returns None when the S3 pointer is missing (caller falls back to the
    legacy single-document flow with the upstream extraction).
    """
    pointer = _extract_s3_pointer(event)
    if pointer is None:
        logger.warning("expediente pipeline skipped: no S3 pointer in event")
        return None

    bucket, key = pointer
    try:
        document_bytes, _ = _fetch_document(bucket, key)
    except Exception as e:
        logger.error("failed to fetch s3://%s/%s: %s", bucket, key, e)
        return None

    from pipeline.expediente import process_pdf  # deferred: pulls pypdf

    expected_values = event.get("expectedValues") or {}
    # declared_type=None on purpose: the upstream classification is a single
    # vision call and regularly confuses look-alikes (fee CFDI vs. the permit
    # it pays for). The text-based segmenter re-classifies with deterministic
    # markers and is the accuracy authority for customs documents.
    result = process_pdf(
        document_bytes,
        declared_type=None,
        expected_values=expected_values,
        source_name=key,
    )

    # Persist the FULL result to S3: a large expediente (86 pages, 20+
    # documents with per-field detail) exceeds Step Functions' 256KB
    # state-payload limit, so the workflow only carries a compact summary
    # plus this pointer.
    result_key = f"results/{document_id}.json"
    result_location: Optional[Dict[str, str]] = None
    try:
        import boto3

        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=result_key,
            Body=json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        result_location = {"bucket": bucket, "key": result_key}
    except Exception as e:
        logger.error("failed to persist full result to s3://%s/%s: %s", bucket, result_key, e)

    # Legacy-compatible envelope so EventBridge consumers keep working,
    # with a compact expediente summary attached.
    main_docs = [d for d in result.get("documents", []) if d.get("documentType") != "OTRO"]
    main = main_docs[0] if main_docs else None
    # Report what the segmenter actually found, not the upstream vision guess
    if len(main_docs) == 1:
        final_classification = main["documentType"]
    elif len(main_docs) > 1:
        final_classification = "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"
    else:
        final_classification = classification
    return {
        "documentId": document_id,
        "documentClassification": final_classification,
        "extractedEntities": _scalar_entities((main or {}).get("extractedEntities", {})),
        "validation": {
            "status": _EXPEDIENTE_STATUS_MAP.get(result.get("status"), "REQUIERE_REVISION"),
            "summary": f"expediente: {result.get('status')} ({len(result.get('documents', []))} documents)",
            "checks": [],
        },
        "expediente": _compact_expediente(result),
        "resultLocation": result_location,
        "processedAt": result.get("processedAt"),
    }


def _scalar_entities(entities: Dict[str, Any]) -> Dict[str, Any]:
    """Entity map without repeatable arrays (kept small for the envelope)."""
    return {k: v for k, v in entities.items() if not isinstance(v, (list, dict))}


def _compact_expediente(result: Dict[str, Any]) -> Dict[str, Any]:
    """Summary of the expediente result that fits Step Functions payloads.

    Per document: type, pages, status, confidence and scalar entities only;
    arrays become item counts and per-field detail/checks stay in the full
    S3 result (see `resultLocation`).
    """
    docs = []
    for d in result.get("documents", []):
        entities = d.get("extractedEntities", {})
        docs.append(
            {
                "documentType": d.get("documentType"),
                "documentSubtype": d.get("documentSubtype"),
                "pageStart": d.get("pageStart"),
                "pageEnd": d.get("pageEnd"),
                "status": d.get("status"),
                "confidence": d.get("confidence"),
                "businessDestination": d.get("businessDestination"),
                "extractedEntities": _scalar_entities(entities),
                "arrayCounts": {
                    k: len(v) for k, v in entities.items() if isinstance(v, list)
                },
                **({"recoveredFields": d["recoveredFields"]} if d.get("recoveredFields") else {}),
            }
        )
    return {
        "processingId": result.get("processingId"),
        "sourceFile": result.get("sourceFile"),
        "processedAt": result.get("processedAt"),
        "processingSeconds": result.get("processingSeconds"),
        "tracking": result.get("tracking"),
        "pages": result.get("pages"),
        "status": result.get("status"),
        "reviewRequired": result.get("reviewRequired"),
        "documents": docs,
        "crossDocumentChecks": result.get("crossDocumentChecks", []),
        "warnings": result.get("warnings", []),
    }


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Lambda handler entry point.

    Expected event structure from Step Functions:
    {
        "documentId": "...",
        "content": {"location": "s3", "bucket": "...", "key": "raw/..."},
        "classificationResult": {"documentClassification": "INE"},
        "processingResult": {"result": {"entities": [...]}}
    }

    The `content` block is populated upstream by the Bedrock invoke Lambda
    and is what the re-extraction fallback uses to fetch the raw document
    from S3. If it is missing, re-extraction is silently skipped.
    """
    try:
        # Force reload catalog if configured (useful for DynamoDB hot-reload)
        if _FORCE_RELOAD:
            reload_catalog()

        document_id = event.get("documentId", "unknown")
        classification_result = event.get("classificationResult", {})
        processing_result = event.get("processingResult", {})

        classification = classification_result.get("documentClassification", "OTRO")

        # Customs/pedimento types go through the full expediente pipeline:
        # per-page text (native + Textract), segmentation, catalog-schema
        # extraction with repeatable fields, and the dynamic rule engine
        # (formulas, sums, cross-document checks). The Bedrock-native
        # single-shot extraction upstream is not reliable for these documents.
        if is_customs_type(classification) or event.get("expediente"):
            expediente_result = _run_expediente(event, document_id, classification)
            if expediente_result is not None:
                return expediente_result

        type_entry = get_type_entry(classification)

        entities = processing_result.get("result", {}).get("entities", [])
        entity_map = entities_to_map(entities)

        # 1. First pass validation against the Bedrock-native extraction
        checks = _run_validation(entity_map, type_entry)
        reextraction_meta: Optional[Dict[str, Any]] = None

        # 2. Re-extract with Textract-based strategy when catalog opts in AND
        #    the initial pass has structural problems. Skips when the initial
        #    pass is already clean (saves a Textract + Bedrock roundtrip).
        if type_entry.extraction_strategy and _has_structural_failure(checks):
            reextracted = _try_reextract(event, type_entry)
            if reextracted is not None:
                new_entities, reextraction_meta = reextracted
                # Re-validate against the fresh entities
                new_entity_map = entities_to_map(new_entities)
                new_checks = _run_validation(new_entity_map, type_entry)
                # Swap only if re-extraction actually reduced failures
                if _failure_count(new_checks) < _failure_count(checks):
                    entity_map = new_entity_map
                    entities = new_entities
                    checks = new_checks
                    reextraction_meta["applied"] = True
                else:
                    reextraction_meta["applied"] = False

        # 3. Run custom Node.js validator if configured
        if type_entry.custom_validator:
            custom_checks = run_custom_node_validator(
                type_entry.custom_validator, entity_map, document_id, classification
            )
            checks.extend(custom_checks)

        status = determine_status(checks)

        result = build_canonical_result(
            document_id, classification, entity_map, checks, status
        )
        if reextraction_meta:
            result["validation"]["reextraction"] = reextraction_meta
        return result

    except Exception as e:
        logger.error(f"Unexpected error in enrichment handler: {e}")
        return build_canonical_result(
            event.get("documentId", "unknown"),
            event.get("classificationResult", {}).get("documentClassification", "OTRO"),
            {},
            [
                CheckResult(
                    field="_handler",
                    rule="exception",
                    valid=False,
                    message=f"Unexpected error: {e}",
                )
            ],
            "REQUIERE_REVISION",
        )
