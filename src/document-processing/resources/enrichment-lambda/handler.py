"""EnrichmentLambda handler - Config-driven validation engine.

The primary ingress is now an S3 ObjectCreated event for source/. The handler
also accepts the former Step Functions payload while deployments drain. It:
1. Claims each S3 object idempotently in DynamoDB
2. Loads document type config from JSON file or DynamoDB (no code changes needed)
3. Runs deterministic validation checks via the appropriate validator module
4. Optionally re-extracts from the raw S3 object using a stronger
   Textract + Bedrock strategy when the catalog asks for it or when the
   initial processingResult fails structural checks (missing required
   fields or regex mismatches). Recovers CURP/RFC/Clave de Elector
   errors caused by weak vision OCR on photos.
5. Optionally runs custom Node.js validators for advanced logic
6. Persists the result and publishes the existing terminal EventBridge event

EXTENSIBILITY:
- Add new document types under catalogs/base/** or catalogs/custom/**, then
  `pnpm sync` (DynamoDB) or `pnpm run build && deploy`
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
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus

from document_catalog import (
    CheckResult,
    TypeRegistryEntry,
    get_type_entry,
    is_customs_type,
    reload_catalog,
)
from cost_audit import write_cost_record
from extractors import get_extractor
from indexing import write_index_attributes
from unique_keys import write_unique_keys
from pipeline import envelope
from pipeline.bedrock import (
    fallback_events,
    reset_used_models,
    set_model_override,
    stage_degradations,
    used_models,
)
from pipeline.model_fallback import reload_fallback_models

logger = logging.getLogger(__name__)

_CUSTOM_VALIDATORS_DIR = Path(__file__).parent / "custom_validators"
_FORCE_RELOAD = os.environ.get("CATALOG_FORCE_RELOAD", "false").lower() == "true"
_STRUCTURAL_RULES = frozenset({"regex", "presence"})
_SYSTEM_OBJECT_METADATA = frozenset(
    {"app-origin", "model-id", "extraction-mode", "ai-response-format", "document-counts"}
)


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


# `pipeline/bedrock.py::resolve_model` records into `used_models()` under its
# own internal stage names ("extractor", "segmenter" — see `stage=` call
# sites in text_extractor.py/segmenter.py), not the phase names this
# envelope's `tracking.modelIds` otherwise uses ("extraction",
# "segmentation" — the same ones `expediente.py:347` already translates to
# for `usage.extraction.modelId`). Rename here too, or `tracking.modelIds`
# silently disagrees with `usage`/cost_audit.py about what to call the same
# phase, and any consumer keyed on the phase name (e.g. a report reading
# `Tracking.ModelIds`) finds nothing for "extraction" even though the model
# is right there under "extractor".
_STAGE_TO_PHASE = {"extractor": "extraction", "segmenter": "segmentation", "vision": "authenticity"}


def _model_ids(event: Dict[str, Any]) -> Dict[str, str]:
    """Which model ran in each phase of this invocation.

    Phases live in two Lambdas: classification and processing run in the
    model-invoke Lambda and report their model back through the Step Functions
    payload; the expediente phases run here. Only phases that actually executed
    are listed — a bypassed classification reports no model because none ran,
    and the expediente phases are absent on the standard flow.
    """
    ids: Dict[str, str] = {}
    for phase, key in (("classification", "classificationResult"),
                       ("processing", "processingResult")):
        model = (event.get(key) or {}).get("modelId")
        if model:
            ids[phase] = model
    for stage, model in used_models().items():
        ids[_STAGE_TO_PHASE.get(stage, stage)] = model
    return ids


def _upload_metadata(event: Dict[str, Any]) -> Dict[str, str]:
    """The caller's own `metadata` from `POST /documents`, as the SQS consumer
    read it back off the S3 object (`uploadMetadata` in the Step Functions
    input — see resources/sqs-consumer/index.py).

    Already lowercased, ASCII, flat and reserved-key-free by the time it gets
    here (upload-lambda's normalizeMetadata, then the consumer's own filter),
    so this only guards the shape: a malformed or absent payload yields `{}`
    rather than poisoning the envelope, because nothing about this system's
    own correctness depends on caller-supplied context.
    """
    raw = event.get("uploadMetadata")
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if k and v not in (None, "")
    }


def _apply_object_metadata(event: Dict[str, Any], metadata: Dict[str, str]) -> None:
    """Restore upload context directly from the source object's metadata.

    The removed SQS adapter used to perform this projection. Keeping it here
    makes S3 the single source of truth and preserves the public envelope.
    """
    if not isinstance(metadata, dict):
        return
    app_origin = metadata.get("app-origin")
    if app_origin and not event.get("appOrigin"):
        event["appOrigin"] = str(app_origin)
    event["uploadMetadata"] = {
        str(key): str(value)
        for key, value in metadata.items()
        if key not in _SYSTEM_OBJECT_METADATA and value not in (None, "")
    }


def _parse_tracking_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp carried by S3/Step Functions, normalized to UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _add_upload_to_result_tracking(
    result: Dict[str, Any], event: Dict[str, Any]
) -> None:
    """Add actual S3 upload → final result wall-clock metrics in place.

    ``processingSeconds`` remains the pipeline-only duration. S3's ObjectCreated
    ``eventTime`` is the first trustworthy instant after the PUT completed, so
    this metric additionally includes ingress and enrichment. ``registeredAt`` is intentionally not used: requesting the
    presigned URL can precede the real upload by many minutes.
    """
    tracking = result.get("tracking")
    if not isinstance(tracking, dict):
        return
    uploaded = _parse_tracking_timestamp(event.get("eventTime"))
    completed = _parse_tracking_timestamp(
        tracking.get("processingCompletedAt")
        or tracking.get("updatedAt")
        or tracking.get("createdAt")
    )
    if uploaded is None or completed is None:
        return
    elapsed = max(0.0, (completed - uploaded).total_seconds())
    tracking["uploadedAt"] = uploaded.isoformat()
    tracking["uploadToResultSeconds"] = round(elapsed, 2)
    tracking["uploadToResultMs"] = round(elapsed * 1000, 2)

    timings = tracking.setdefault("timings", {})
    if isinstance(timings, dict):
        consumer_started = _parse_tracking_timestamp(event.get("consumerStartedAt"))
        workflow_requested = _parse_tracking_timestamp(event.get("workflowRequestedAt"))
        processing_started = _parse_tracking_timestamp(
            tracking.get("processingStartedAt")
        )
        if consumer_started is not None:
            timings["queue"] = round(
                max(0.0, (consumer_started - uploaded).total_seconds()) * 1000,
                2,
            )
        if workflow_requested is not None and processing_started is not None:
            timings["orchestrationBeforeExtraction"] = round(
                max(0.0, (processing_started - workflow_requested).total_seconds())
                * 1000,
                2,
            )

        handler_started = _parse_tracking_timestamp(event.get("handlerStartedAt"))
        if handler_started is not None:
            timings["ingressToHandler"] = round(
                max(0.0, (handler_started - uploaded).total_seconds()) * 1000,
                2,
            )
        if handler_started is not None and processing_started is not None:
            timings["preprocessing"] = round(
                max(0.0, (processing_started - handler_started).total_seconds())
                * 1000,
                2,
            )

    route = event.get("pipelineRoute")
    if route in {"standard", "enrichment-only", "direct-s3-lambda"}:
        tracking["orchestration"] = {"route": route}


def build_canonical_result(
    document_id: str,
    classification: str,
    entity_map: Dict[str, str],
    checks: List[CheckResult],
    status: str,
    model_ids: Optional[Dict[str, str]] = None,
    app_origin: Optional[str] = None,
    model_fallbacks: Optional[Dict[str, Dict[str, Any]]] = None,
    upload_metadata: Optional[Dict[str, str]] = None,
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

    # Provenance, consolidated under one top-level object (createdAt always
    # present; modelIds/appOrigin/modelFallbacks only when there's something
    # to report) — mirrors the shape _run_expediente builds from
    # pipeline.result_common's build_tracking(), so both flows expose the
    # same `tracking.*` fields even though the standard flow doesn't compute
    # the richer schema/processing-window info the expediente pipeline does.
    tracking: Dict[str, Any] = {"createdAt": datetime.now(timezone.utc).isoformat()}
    if model_ids:
        tracking["modelIds"] = model_ids
    if app_origin:
        tracking["appOrigin"] = app_origin
    if model_fallbacks:
        tracking["modelFallbacks"] = model_fallbacks

    # Misma forma que el flujo expediente: `documents[]` en la raíz, con un
    # elemento. Ver pipeline/envelope.py — el objetivo es que un consumidor
    # tenga UN parser, no uno por flujo.
    # El `metadata` del caller viaja verbatim y nunca se interpreta (ver
    # _upload_metadata), fuera de `tracking`: son de dueños distintos, con
    # niveles de confianza distintos, y solo este es filtrable con
    # `GET /documents?meta.<clave>=<valor>`.
    return envelope.build_standard(
        document_id=document_id,
        classification=classification,
        entity_map=entity_map,
        checks=checks,
        status=status,
        tracking=tracking,
        metadata=upload_metadata,
        summary=summary,
    )


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


def _declared_file_type(event: Dict[str, Any]) -> Optional[str]:
    """Explicit file type from `content.fileType` (set by the SQS consumer —
    see `resources/sqs-consumer/index.py` — as the single source of
    truth instead of every downstream Lambda re-deriving it from the S3 key's
    extension independently). Preferred when present; the caller falls back
    to parsing the key itself for events that predate this field (already
    in-flight during a deploy) or arrive from a different ingestion path.
    """
    content = event.get("content") or {}
    file_type = content.get("fileType") if isinstance(content, dict) else None
    return str(file_type).strip().lower() if file_type else None


def _declared_types_from_key(key: str) -> List[str]:
    """The document types the uploader declared, in order, from the S3 layout.

    Manual classification places the object under `source/{TYPE}/{uuid}/file`, so
    the second path segment names the type the uploader chose explicitly. When
    SEVERAL types were declared (`POST /documents` accepts them comma-separated
    so the pipeline searches for exactly those), that same segment holds them
    all: `source/{A,B}/{uuid}/file`. An auto-classified object is under
    `source/{uuid}/file`, with no type segment at all. The former `raw/` prefix
    remains accepted so presigned URLs issued immediately before a deploy work.

    Only types the catalog actually knows are kept — a bare UUID, or a name
    nobody registered, resolves to OTRO and is dropped, which degrades to
    "nothing declared" exactly as before. That is also what makes the key a safe
    carrier during a partial deploy: an older reader that cannot parse a list
    just falls back to auto-classification instead of failing.
    """
    parts = (key or "").split("/")
    if len(parts) < 3 or parts[0] not in {"source", "raw"}:
        return []
    declared: List[str] = []
    for part in parts[1].split(","):
        candidate = part.strip().upper()
        if not candidate or candidate in declared:
            continue
        if get_type_entry(candidate).type_id != "OTRO":
            declared.append(candidate)
    return declared


def _fetch_document(bucket: str, key: str) -> Tuple[bytes, str, Dict[str, str]]:
    """Download the raw document from S3 and return its bytes, MIME type and
    the object's own metadata.

    Also applies a per-request model override when the upload carried one and
    the deployment allows it — see `_apply_model_override`.

    The metadata is returned rather than only consumed here because it is the
    channel the upload API uses for signals that only this Lambda needs:
    `model-id` (applied here), `extraction-mode` and `document-counts` (read by
    the caller — see `_extraction_mode_from_metadata` and
    `_declared_counts_from_metadata`). They are stripped from the caller's own
    `metadata` at upload time, so they cannot be forged through that bag.
    """
    import boto3  # defer boto3 import; Lambda runtime provides it

    response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    content_type = response.get("ContentType") or mimetypes.guess_type(key)[0] or ""
    s3_metadata = response.get("Metadata") or {}
    _apply_model_override(s3_metadata)
    return body, content_type, s3_metadata


def _declared_counts_from_metadata(s3_metadata: Dict[str, str]) -> Dict[str, Any]:
    """How many documents of each type the uploader asked for.

    Format written by `upload-lambda/body-parser.mjs::serializeDocumentCounts`:
    `PEDIMENTO_ERP=1,FACTURA_FLETE_EXTRANJERO=all` — flat US-ASCII, which is all
    an S3 metadata value can hold. Values are `int >= 1` or the string `"all"`.

    Travels as object metadata rather than in the S3 key (where the TYPES live)
    because three independent components parse that key and none of them needs
    this. Absent metadata yields `{}`; the expediente pipeline then resolves
    `occurrence_mode` from each catalog and finally the global default (`single`
    for one declared type, `multiple` for several).

    Never raises: a malformed value degrades that one type to the downstream
    catalog/global default rather than failing the document.
    """
    raw = (s3_metadata or {}).get("document-counts")
    if not raw:
        return {}
    counts: Dict[str, Any] = {}
    for part in str(raw).split(","):
        name, _, value = part.partition("=")
        name = name.strip().upper()
        value = value.strip().lower()
        if not name or not value:
            continue
        if value == "all":
            counts[name] = "all"
        elif value.isdigit() and int(value) >= 1:
            counts[name] = int(value)
        else:
            logger.warning("document-counts: valor ilegible '%s' para %s; usando catálogo/default", value, name)
    return counts


def _extraction_mode_from_metadata(s3_metadata: Dict[str, str]) -> str:
    """Per-upload extraction strategy, enabled only on the development stack.

    The same gate as ``model-id`` prevents a production caller from forcing a
    more expensive full-model run. Invalid or unavailable values fail open to
    the catalog, never to an accidental AI-free extraction.
    """
    raw = str((s3_metadata or {}).get("extraction-mode") or "catalog").strip().lower()
    if raw not in {"catalog", "model", "hybrid", "deterministic"}:
        logger.warning("extraction-mode inválido %r; usando catálogo", raw)
        return "catalog"
    if raw != "catalog" and os.environ.get("ALLOW_MODEL_OVERRIDE", "").lower() != "true":
        logger.warning("extraction-mode %s ignorado: override deshabilitado en este ambiente", raw)
        return "catalog"
    return raw


def _ai_response_format_from_metadata(s3_metadata: Dict[str, str]) -> Optional[str]:
    """Authorized per-upload override transported by the Upload API.

    Authorization happens before the presigned upload is created. Invalid
    object metadata fails open to catalog/environment/default compact rather
    than accidentally selecting the more verbose protocol.
    """
    raw = str((s3_metadata or {}).get("ai-response-format") or "").strip().lower()
    if not raw:
        return None
    if raw not in {"compact", "legacy"}:
        logger.warning("ai-response-format inválido %r; usando configuración heredada", raw)
        return None
    return raw


def _apply_model_override(s3_metadata: Dict[str, str]) -> None:
    """Honour a `model-id` uploaded alongside the document, in dev only.

    Lets a developer try a model per request instead of redeploying to change
    an environment variable. Gated on `ALLOW_MODEL_OVERRIDE`, which the stack
    sets only for the dev environment: the value arrives from whoever uploaded
    the file, so in prod it would let a caller pick the model — and with it the
    cost and the extraction quality — on someone else's document.
    """
    if os.environ.get("ALLOW_MODEL_OVERRIDE", "").lower() != "true":
        return
    # S3 lowercases metadata keys on the way out.
    model_id = (s3_metadata.get("model-id") or "").strip()
    if model_id:
        logger.warning("model override from upload metadata: %s (dev only)", model_id)
        set_model_override(model_id)


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
        document_bytes, content_type, _ = _fetch_document(bucket, key)
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
        document_bytes, _, s3_metadata = _fetch_document(bucket, key)
    except Exception as e:
        logger.error("failed to fetch s3://%s/%s: %s", bucket, key, e)
        return None

    _apply_object_metadata(event, s3_metadata)

    expected_values = event.get("expectedValues") or {}
    # Honor an EXPLICIT user classification. When the uploader declares a type,
    # the file is stored under raw/{TYPE}/{uuid}/... — that user intent wins and
    # the whole file is treated as that single type (no re-segmentation).
    # When the file was auto-classified (raw/{uuid}/... — no type segment),
    # declared_type stays None so the text segmenter, which re-classifies with
    # deterministic markers, is the accuracy authority (the single upstream
    # vision call regularly confuses look-alikes, e.g. fee CFDI vs. permit).
    # With exactly ONE declared type, `declared_type` is set and the whole file
    # is that type. With several, it stays None on purpose: that is what keeps
    # the xml/tabular branches below failing the way they always have (they need
    # one schema, and there is nothing to choose it with), and what lets the
    # segmenter run — restricted to `declared_types` — instead of short-circuiting.
    declared_counts = _declared_counts_from_metadata(s3_metadata)
    extraction_mode = _extraction_mode_from_metadata(s3_metadata)
    ai_response_format = _ai_response_format_from_metadata(s3_metadata)
    declared_types = _declared_types_from_key(key)
    declared_type = declared_types[0] if len(declared_types) == 1 else None
    if declared_type:
        logger.info("honoring user-declared type '%s' from key %s", declared_type, key)
    elif declared_types:
        logger.info("guided classification among %s from key %s", declared_types, key)

    # Already-structured sources (XML, XLSX/CSV) skip the PDF/OCR/segmentation
    # pipeline entirely: pipeline.xml_document/pipeline.tabular_document
    # deterministically walk the catalog's xpath-/cell-/column-mapped fields
    # instead of asking a model to read them. Routed by the uploaded file's
    # own extension, not by the declared type's name — `_XML` is a catalogs/
    # naming convention for humans, not what the code keys off. A structured
    # file with no declared type fails fast inside its pipeline (there is no
    # vision-based auto-classification for raw XML/XLSX/CSV).
    ext = _declared_file_type(event) or (key.rsplit(".", 1)[-1].lower() if "." in key else "")
    if ext == "xml":
        from pipeline.xml_document import process_xml  # deferred: rarely-hit path

        result = process_xml(
            document_bytes,
            declared_type=declared_type,
            expected_values=expected_values,
            source_name=key,
            declared_types=declared_types,
        )
    elif ext in ("xlsx", "xlsm", "csv"):
        from pipeline.tabular_document import process_tabular  # deferred: pulls openpyxl

        result = process_tabular(
            document_bytes,
            source_format=ext,
            declared_type=declared_type,
            expected_values=expected_values,
            source_name=key,
            declared_types=declared_types,
        )
    else:
        from pipeline.expediente import process_pdf  # deferred: pulls pypdf

        # A field-centric type isn't necessarily backed by a PDF — a photographed/
        # scanned ID (INE, CSF, ...) is routinely uploaded as a raw JPG/PNG.
        # process_pdf's page-loading branches internally on source_format;
        # anything else (a real .pdf, or an unrecognized extension) keeps the
        # existing pypdf path unchanged via the "pdf" default.
        result = process_pdf(
            document_bytes,
            declared_type=declared_type,
            expected_values=expected_values,
            source_name=key,
            source_format=ext if ext in ("jpg", "jpeg", "png") else "pdf",
            declared_types=declared_types,
            declared_counts=declared_counts,
            extraction_mode=extraction_mode,
            ai_response_format=ai_response_format,
            # What we know ABOUT the upload (not what's in it). Generic: a
            # catalog can name an upload-metadata key it needs, e.g. the
            # supplier whose registered signatures to compare against.
            context={
                "documentId": document_id,
                "metadata": _upload_metadata(event),
                "appOrigin": event.get("appOrigin"),
            },
        )

    # Add before persisting the full result. Otherwise the compact envelope
    # would expose the metric, but reading resultLocation from S3 would replace
    # it with an older tracking block that did not contain it.
    _add_upload_to_result_tracking(result, event)

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

    # Envelope v2: `documents[]` en la raíz, un elemento por documento
    # encontrado, sea uno o veinte. Ver pipeline/envelope.py.
    #
    # `tracking` es la misma forma que arma build_canonical_result para el flujo
    # estándar: arranca del build_tracking() del pipeline (createdAt/updatedAt/
    # processingStartedAt/processingCompletedAt/processingSeconds/processingMs/
    # schema/timings, que ponen todas las ramas — expediente.py, xml_document.py,
    # tabular_document.py) y le agrega modelIds/appOrigin/modelFallbacks encima,
    # en vez de que esos vivan como llaves sueltas del envelope.
    tracking = {
        **(result.get("tracking") or {}),
        "modelIds": _model_ids(event),
        **({"appOrigin": event.get("appOrigin")} if event.get("appOrigin") else {}),
        **({"modelFallbacks": fallback_events()} if fallback_events() else {}),
        **(
            {"segmentationDegraded": stage_degradations()["segmenter"]}
            if stage_degradations().get("segmenter")
            else {}
        ),
    }

    return envelope.build(
        document_id=document_id,
        result=result,
        tracking=tracking,
        metadata=_upload_metadata(event),
        result_location=result_location,
        # Solo se usa si el archivo no produjo ningún documento principal: sin
        # documentos que mirar, la única señal de tipo es la de clasificación.
        fallback_classification=classification,
    )


def _s3_record_to_event(record: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt one native S3 notification to the pipeline's stable input shape."""
    bucket = str(record["s3"]["bucket"]["name"])
    key = unquote_plus(str(record["s3"]["object"]["key"]))
    parts = [part for part in key.split("/") if part]
    if len(parts) < 3 or parts[0] not in {"source", "raw"}:
        raise ValueError(f"unsupported document ingress key: {key}")
    document_id = re.sub(r"[^a-zA-Z0-9-]", "-", parts[-2])
    filename = parts[-1]
    declared_types = _declared_types_from_key(key)
    requested = ",".join(declared_types) if declared_types else "AUTO"
    classification = declared_types[0] if len(declared_types) == 1 else "OTRO"
    event_time = str(record.get("eventTime") or datetime.now(timezone.utc).isoformat())
    sequencer = str((record.get("s3", {}).get("object", {}) or {}).get("sequencer") or "")
    attempt = sequencer or f"{event_time}:{key}"
    return {
        "documentId": document_id,
        "contentType": "file",
        "content": {
            "location": "s3",
            "bucket": bucket,
            "key": key,
            "filename": filename,
            "fileType": filename.rsplit(".", 1)[-1].lower() if "." in filename else "",
        },
        "eventTime": event_time,
        "eventName": record.get("eventName"),
        "source": "s3-direct",
        "requestedDocumentType": requested,
        "pipelineRoute": "direct-s3-lambda",
        "handlerStartedAt": datetime.now(timezone.utc).isoformat(),
        "processingAttempt": attempt,
        "classificationResult": {"documentClassification": classification},
        "processingResult": {"result": {"entities": []}},
        # AUTO and guided uploads must reach the page segmenter. A single
        # declared field-centric type also follows this same generic route.
        "expediente": True,
        "_directIngress": True,
    }


def _document_table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DOCUMENT_TABLE"])


def _claim_direct_event(event: Dict[str, Any], context: Any = None) -> str:
    """Claim an at-least-once S3 notification without reprocessing successes.

    A lease protects against concurrent duplicate notifications. It expires a
    few seconds before the Lambda timeout so an AWS async retry can recover a
    forced timeout instead of leaving the document permanently PROCESSING.
    """
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    remaining_ms = (
        context.get_remaining_time_in_millis()
        if context is not None and hasattr(context, "get_remaining_time_in_millis")
        else 15 * 60 * 1000
    )
    lease_seconds = max(30, int(remaining_ms / 1000) - 10)
    values = {
        ":processing": "PROCESSING",
        ":awaiting": "AWAITING_UPLOAD",
        ":queued": "QUEUED",
        ":failed": "FAILED",
        ":now": now.isoformat(),
        ":now_epoch": now_epoch,
        ":lease": now_epoch + lease_seconds,
        ":attempt": str(event["processingAttempt"]),
        ":source": str(event["content"]["key"]),
        ":uploaded": str(event.get("eventTime") or now.isoformat()),
        ":workflow": "processing",
    }
    try:
        _document_table().update_item(
            Key={"DocumentId": event["documentId"]},
            UpdateExpression=(
                "SET #status = :processing, #workflow = :workflow, "
                "ProcessingStatusUpdatedAt = :now, UploadedAt = if_not_exists(UploadedAt, :uploaded), "
                "SourceKey = :source, ProcessingAttempt = :attempt, ProcessingLeaseUntil = :lease"
            ),
            ConditionExpression=(
                "attribute_not_exists(#status) OR #status IN (:awaiting, :queued, :failed) "
                "OR ProcessingLeaseUntil < :now_epoch"
            ),
            ExpressionAttributeNames={
                "#status": "ProcessingStatus",
                "#workflow": "WorkflowStatus",
            },
            ExpressionAttributeValues=values,
        )
        return "claimed"
    except Exception as exc:  # boto3's generated exception class is client-specific
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code != "ConditionalCheckFailedException":
            raise
        item = _document_table().get_item(
            Key={"DocumentId": event["documentId"]}, ConsistentRead=True
        ).get("Item") or {}
        if item.get("WorkflowStatus") == "complete" and not item.get(
            "TerminalEventPublished", False
        ):
            return "publish-only"
        return "duplicate"


def _release_direct_claim(event: Dict[str, Any], error: Exception) -> None:
    """Make a failed, non-timeout attempt immediately retryable."""
    try:
        _document_table().update_item(
            Key={"DocumentId": event["documentId"]},
            UpdateExpression=(
                "SET #status = :queued, #workflow = :workflow, "
                "ProcessingStatusUpdatedAt = :updated, ProcessingError = :error "
                "REMOVE ProcessingLeaseUntil"
            ),
            ConditionExpression="ProcessingAttempt = :attempt AND #workflow <> :complete",
            ExpressionAttributeNames={
                "#status": "ProcessingStatus",
                "#workflow": "WorkflowStatus",
            },
            ExpressionAttributeValues={
                ":queued": "QUEUED",
                ":workflow": "retry-pending",
                ":complete": "complete",
                ":updated": datetime.now(timezone.utc).isoformat(),
                ":error": str(error)[:500],
                ":attempt": str(event["processingAttempt"]),
            },
        )
    except Exception as release_error:
        logger.error("failed to release direct claim for %s: %s", event["documentId"], release_error)


def _persist_direct_result(result: Dict[str, Any], event: Dict[str, Any]) -> None:
    """Atomically expose the terminal envelope to the existing Query API."""
    now = datetime.now(timezone.utc).isoformat()
    values: Dict[str, Any] = {
        ":complete": "complete",
        ":finished": "FINISHED",
        ":updated": now,
        ":result": json.dumps(result, ensure_ascii=False, default=str),
        ":source": str(event["content"]["key"]),
        ":content": json.dumps(event["content"], ensure_ascii=False),
        ":content_type": str(event.get("contentType") or "file"),
        ":attempt": str(event["processingAttempt"]),
        ":not_published": False,
    }
    update = (
        "SET #workflow = :complete, #status = :finished, "
        "ProcessingStatusUpdatedAt = :updated, ProcessingCompletedAt = :updated, "
        "EnrichmentResult = :result, SourceKey = :source, Content = :content, "
        "ContentType = :content_type, TerminalEventPublished = :not_published"
    )
    if event.get("appOrigin"):
        update += ", JobOwner = :owner"
        values[":owner"] = str(event["appOrigin"])
    update += " REMOVE ProcessingLeaseUntil, ProcessingError"
    _document_table().update_item(
        Key={"DocumentId": event["documentId"]},
        UpdateExpression=update,
        ConditionExpression="ProcessingAttempt = :attempt",
        ExpressionAttributeNames={
            "#workflow": "WorkflowStatus",
            "#status": "ProcessingStatus",
        },
        ExpressionAttributeValues=values,
    )


def _publish_terminal_event(result: Dict[str, Any], event: Dict[str, Any]) -> None:
    import boto3

    response = boto3.client("events").put_events(
        Entries=[
            {
                "EventBusName": os.environ["EVENT_BUS_NAME"],
                "Source": os.environ.get("EVENT_SOURCE", "document-validation-pipeline"),
                "DetailType": "document-processed-successful",
                "Detail": json.dumps(
                    {
                        "documentId": event["documentId"],
                        "contentType": event.get("contentType", "file"),
                        "content": json.dumps(event.get("content") or {}, ensure_ascii=False),
                        "classification": result.get("documentClassification", "OTRO"),
                    },
                    ensure_ascii=False,
                ),
            }
        ]
    )
    if response.get("FailedEntryCount"):
        raise RuntimeError(f"EventBridge rejected terminal event: {response.get('Entries')}")
    _document_table().update_item(
        Key={"DocumentId": event["documentId"]},
        UpdateExpression="SET TerminalEventPublished = :published",
        ExpressionAttributeValues={":published": True},
    )


def _republish_terminal_event(event: Dict[str, Any]) -> None:
    item = _document_table().get_item(
        Key={"DocumentId": event["documentId"]}, ConsistentRead=True
    ).get("Item") or {}
    raw = item.get("EnrichmentResult")
    if not raw:
        raise RuntimeError("terminal result is missing while its event is pending")
    _publish_terminal_event(json.loads(raw), event)


def _handle_one(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Run one normalized pipeline event and persist its auxiliary records.

    Runs the validation flow (`_process`) and then promotes the document type
    and friends to indexed DynamoDB columns (see `indexing.py`) so the query
    API can filter by type with a GSI Query instead of a table Scan. Writes
    one lookup row per unique key the catalog declares (see `unique_keys.py`),
    keyed `TIPODOC#valor`, so a search by that field is a single Query instead
    of a walk over every document of the type. Also writes a per-document AWS
    cost audit record (see `cost_audit.py`) to a separate table, so
    `pnpm costos` can answer "how much did processing this document actually
    cost, broken down by service" without touching Cost Explorer. All three
    writes are best-effort and never change the returned result.
    """
    # Lambda containers are reused, so the per-phase model registry must start
    # empty or a warm invocation inherits the previous document's phases.
    reset_used_models()
    started = time.time()
    result = _process(event, context)
    # Standard-flow results do not pass through `_run_expediente`; applying it
    # here makes the metric universal. It is idempotent for expediente results.
    _add_upload_to_result_tracking(result, event)
    # Proxy for billed Lambda duration: measured from inside the invocation,
    # so it excludes init and undercounts slightly (see cost_audit.py), but
    # it's the only duration available before the invocation itself finishes.
    duration_ms = (time.time() - started) * 1000
    pointer = _extract_s3_pointer(event)
    source_key = pointer[1] if pointer else None
    # These independent writes used to add their latencies serially. Running
    # them together keeps the direct path bounded by the slowest audit write.
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(write_index_attributes, result, source_key=source_key),
            executor.submit(write_unique_keys, result, source_key=source_key),
            executor.submit(
                write_cost_record,
                result,
                event=event,
                duration_ms=duration_ms,
                source_key=source_key,
            ),
        ]
        for future in futures:
            future.result()
    if event.get("_directIngress"):
        _persist_direct_result(result, event)
        _publish_terminal_event(result, event)
    return result


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Accept native S3 notifications and legacy normalized invocations."""
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list):
        return _handle_one(event, context)

    outcomes = []
    for record in records:
        if not str(record.get("eventSource") or "").endswith(":s3"):
            continue
        direct_event = _s3_record_to_event(record)
        claim = _claim_direct_event(direct_event, context)
        if claim == "duplicate":
            outcomes.append(
                {"documentId": direct_event["documentId"], "status": "duplicate-skipped"}
            )
            continue
        if claim == "publish-only":
            _republish_terminal_event(direct_event)
            outcomes.append(
                {"documentId": direct_event["documentId"], "status": "event-republished"}
            )
            continue
        try:
            result = _handle_one(direct_event, context)
            outcomes.append(
                {
                    "documentId": direct_event["documentId"],
                    "status": "finished",
                    "validationStatus": (result.get("validation") or {}).get("status"),
                }
            )
        except Exception as exc:
            _release_direct_claim(direct_event, exc)
            raise
    return {"processed": outcomes}


def _process(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Validation flow proper.

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
        # Force reload catalog + fallback-model list if configured (useful
        # for DynamoDB hot-reload — CATALOG_FORCE_RELOAD covers both tables,
        # not just the catalog one, since a warm container would otherwise
        # keep serving a stale fallback order until it cold-starts).
        if _FORCE_RELOAD:
            reload_catalog()
            reload_fallback_models()

        document_id = event.get("documentId", "unknown")
        classification_result = event.get("classificationResult", {})
        processing_result = event.get("processingResult", {})

        classification = classification_result.get("documentClassification", "OTRO")
        type_entry = get_type_entry(classification)

        # Route through the expediente pipeline (per-page text, optional
        # Textract, catalog-schema extraction, dynamic rule engine) whenever
        # EITHER is true:
        # - `is_customs_type`: the type opted into the multi-document customs
        #   pipeline (segmentation candidacy in an unlabeled expediente PDF,
        #   cross-document rules) via `"pipeline": "expediente"`.
        # - `type_entry.field_tree is not None`: the type's catalog entry uses
        #   the field-centric DSL (top-level `"fields"` key). The upstream
        #   Bedrock-native standard-flow prompt (generated at CDK synth time,
        #   src/document-catalog.ts) only understands the legacy
        #   extraction_fields/extraction_hints DSL, so it never emits a
        #   prompt for field-centric types — without this branch they would
        #   fall through to that flow and extract nothing. `pipeline:
        #   expediente` is an orthogonal, additional opt-in on top of this,
        #   not a precondition for extraction to work at all.
        if is_customs_type(classification) or event.get("expediente") or type_entry.field_tree is not None:
            expediente_result = _run_expediente(event, document_id, classification)
            if expediente_result is not None:
                return expediente_result

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
            document_id, classification, entity_map, checks, status,
            model_ids=_model_ids(event),
            app_origin=event.get("appOrigin"),
            model_fallbacks=fallback_events(),
            upload_metadata=_upload_metadata(event),
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
            app_origin=event.get("appOrigin"),
            upload_metadata=_upload_metadata(event),
        )
