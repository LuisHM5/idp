"""EnrichmentLambda handler - Config-driven validation engine.

This handler is invoked by the Step Functions workflow after classification
and extraction. It:
1. Reads classificationResult and processingResult from the event
2. Loads document type config from JSON file or DynamoDB (no code changes needed)
3. Runs deterministic validation checks via the appropriate validator module
4. Optionally runs custom Node.js validators for advanced logic
5. Returns a Canonical_Result with status and detailed checks

EXTENSIBILITY:
- Add new document types: edit config/document_types.json OR insert in DynamoDB
- Add custom validation logic: create a .mjs file in custom_validators/
- No code changes required for either path.
"""

import importlib
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from document_catalog import CheckResult, get_type_entry, reload_catalog

logger = logging.getLogger(__name__)

_CUSTOM_VALIDATORS_DIR = Path(__file__).parent / "custom_validators"
_FORCE_RELOAD = os.environ.get("CATALOG_FORCE_RELOAD", "false").lower() == "true"


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
    """Run Python-based validation via the catalog's validator_module."""
    type_entry = get_type_entry(classification)

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


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Lambda handler entry point.

    Expected event structure from Step Functions:
    {
        "documentId": "...",
        "classificationResult": {"documentClassification": "INE"},
        "processingResult": {"result": {"entities": [...]}}
    }
    """
    try:
        # Force reload catalog if configured (useful for DynamoDB hot-reload)
        if _FORCE_RELOAD:
            reload_catalog()

        document_id = event.get("documentId", "unknown")
        classification_result = event.get("classificationResult", {})
        processing_result = event.get("processingResult", {})

        classification = classification_result.get("documentClassification", "OTRO")
        entities = processing_result.get("result", {}).get("entities", [])

        # Convert entities list to map
        entity_map = entities_to_map(entities)

        # 1. Run Python-based validation (regex, dates, required fields)
        checks = run_python_validation(entity_map, classification)

        # 2. Run custom Node.js validator if configured
        type_entry = get_type_entry(classification)
        if type_entry.custom_validator:
            custom_checks = run_custom_node_validator(
                type_entry.custom_validator, entity_map, document_id, classification
            )
            checks.extend(custom_checks)

        # Determine status
        status = determine_status(checks)

        # Build and return canonical result
        return build_canonical_result(
            document_id, classification, entity_map, checks, status
        )

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
