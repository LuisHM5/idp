"""REST API of the reference registry — register, list and deactivate the
valid signatures, stamps and logos of an entity (a supplier, a signer…).

    POST   /references/{scope}/{key}                           reserve + upload URL
    POST   /references/{scope}/{key}/{kind}/{refId}/confirm    review + activate
    GET    /references/{scope}/{key}                           list
    GET    /references/{scope}/{key}/{kind}/{refId}            one, with an image URL
    DELETE /references/{scope}/{key}/{kind}/{refId}            deactivate

Registration is two-step for the same reason `POST /documents` is: the image
goes straight to S3 through a presigned PUT, never through this Lambda's
payload. The crop is reviewed on `confirm`, which answers on the spot.

Runs the SAME code as the operator CLI (`pipeline/reference_registry.py`) and
ships in the enrichment Lambda's asset: reviewing a crop needs numpy, Pillow
and the pipeline's own fingerprint code.

Who may call it: registering a reference is declaring "this stamp is valid",
and listing them exposes people's signatures — so EVERY route, reads included,
requires the app's `AllowReferenceWrite` flag (AppsTable; `pnpm apps:grant --
--reference-write`). It fails CLOSED: an unregistered key or an unreachable
AppsTable is a 403, never a pass. Without an API key at all (a dev stack that
does not require keys) it is open, like the rest of that stack.

Errors use the shape of the other Lambdas: `{"error": "CODE", "message": "…"}`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote

from pipeline import authenticity, reference_embeddings, reference_registry
from pipeline.reference_registry import RegistryError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BASE = "/references/{scope}/{key}"
ONE = BASE + "/{kind}/{refId}"
CONFIRM = ONE + "/confirm"


def _json(status: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False)}


def _error(status: int, code: str, message: str, **extra: Any) -> Dict[str, Any]:
    return _json(status, {"error": code, "message": message, **extra})


# ------------------------------------------------------------------ caller


def resolve_caller(event: Dict[str, Any], apps_table: Any, key_required: bool) -> Dict[str, Any]:
    """Who is calling and whether they may touch references."""
    api_key_id = ((event.get("requestContext") or {}).get("identity") or {}).get("apiKeyId")
    if not api_key_id:
        # No key: only possible where API Gateway does not demand one (dev).
        return {"appName": "anonymous", "allowed": not key_required}
    try:
        item = apps_table.get_item(Key={"ApiKeyId": api_key_id}).get("Item") if apps_table is not None else None
    except Exception as e:
        logger.warning("references_api: AppsTable lookup failed: %s", e)
        item = None
    if not item:
        return {"appName": "unregistered", "allowed": False}
    return {"appName": str(item.get("AppName") or api_key_id), "allowed": item.get("AllowReferenceWrite") is True}


# ------------------------------------------------------------------ dependencies


class Dependencies:
    """Everything the handlers touch outside this process. Built lazily from
    the environment in the Lambda; handed in whole by the tests."""

    def __init__(self, registry: Any, apps_table: Any, scopes: Callable[[], Dict[str, Dict[str, Dict[str, Any]]]],
                 key_required: bool = True):
        self.registry, self.apps_table, self.scopes, self.key_required = registry, apps_table, scopes, key_required


def _catalog_scopes() -> Dict[str, Dict[str, Dict[str, Any]]]:
    import document_catalog

    if os.environ.get("CATALOG_FORCE_RELOAD", "false").lower() == "true":
        document_catalog.reload_catalog()  # a type synced a minute ago already counts
    return authenticity.reference_scopes(document_catalog.get_all_types().values())


_DEPENDENCIES: Optional[Dependencies] = None


def _dependencies() -> Dependencies:
    global _DEPENDENCIES
    if _DEPENDENCIES is None:
        import boto3
        from botocore.config import Config

        dynamodb = boto3.resource("dynamodb")
        registry = reference_registry.Registry(
            table=dynamodb.Table(os.environ["FINGERPRINT_TABLE"]),
            # SigV4 explicitly: the prod bucket is SSE-KMS, which rejects V2 URLs.
            s3=boto3.client("s3", config=Config(signature_version="s3v4")),
            bucket=os.environ["DOCUMENT_BUCKET"],
            provider=reference_embeddings.provider_for(None),
        )
        apps = os.environ.get("APPS_TABLE")
        _DEPENDENCIES = Dependencies(
            registry=registry, apps_table=dynamodb.Table(apps) if apps else None, scopes=_catalog_scopes,
            key_required=os.environ.get("API_KEY_REQUIRED", "true").strip().lower() not in ("0", "false", "no"),
        )
    return _DEPENDENCIES


# ------------------------------------------------------------------ handlers


def _body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise RegistryError("INVALID_JSON", "El cuerpo no es JSON válido.") from None
    if not isinstance(parsed, dict):
        raise RegistryError("INVALID_JSON", "El cuerpo debe ser un objeto JSON.")
    return parsed


def _path(event: Dict[str, Any], name: str) -> str:
    return unquote(str((event.get("pathParameters") or {}).get(name) or ""))


def _reserve(event, deps, caller, scope, key) -> Dict[str, Any]:
    body = _body(event)
    kind = body.get("kind")
    if not kind:
        raise RegistryError("MISSING_FIELD", "`kind` es obligatorio (p. ej. FIRMA, SELLO, LOGO).")
    reference_registry.validate_scope_and_kind(deps.scopes(), scope, kind)
    item = deps.registry.reserve(scope, key, str(kind), body.get("label"), caller["appName"], body.get("fileName"))
    seconds = reference_registry.UPLOAD_URL_SECONDS
    ref_id = str(item["Sk"]).partition("#")[2]
    return _json(200, {
        "scope": scope, "key": key, "kind": kind, "refId": ref_id, "label": item["Label"], "status": "PENDING",
        "uploadUrl": deps.registry.upload_url(item), "uploadMethod": "PUT",
        "uploadExpiresInSeconds": seconds,
        "uploadExpiresAt": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
        "confirmUrl": f"/references/{scope}/{key}/{kind}/{ref_id}/confirm",
    })


def _confirm(event, deps, caller, scope, key) -> Dict[str, Any]:
    kind, ref_id = _path(event, "kind"), _path(event, "refId")
    element = reference_registry.validate_scope_and_kind(deps.scopes(), scope, kind)
    outcome = deps.registry.confirm(scope, key, kind, ref_id, element, force=_body(event).get("force") is True)
    return _json(200, {**reference_registry.view(outcome["item"]), "warnings": outcome["warnings"]})


def _list(event, deps, caller, scope, key) -> Dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    kind = params.get("kind") or None
    reference_registry.validate_scope_and_kind(deps.scopes(), scope, kind)
    inactive = str(params.get("includeInactive") or "").lower() in ("1", "true", "yes")
    items = deps.registry.listing(scope, key, kind=kind, include_inactive=inactive)
    return _json(200, {"scope": scope, "key": key, "count": len(items),
                       "references": [reference_registry.view(i) for i in items]})


def _get(event, deps, caller, scope, key) -> Dict[str, Any]:
    kind, ref_id = _path(event, "kind"), _path(event, "refId")
    reference_registry.validate_scope_and_kind(deps.scopes(), scope, kind)
    item = deps.registry.get(scope, key, kind, ref_id)
    if item is None:
        raise RegistryError("NOT_FOUND", "No existe esa referencia.", status=404)
    url = deps.registry.image_url(item)
    return _json(200, {**reference_registry.view(item),
                       **({"imageUrl": url, "imageUrlExpiresInSeconds": reference_registry.IMAGE_URL_SECONDS}
                          if url else {})})


def _deactivate(event, deps, caller, scope, key) -> Dict[str, Any]:
    kind, ref_id = _path(event, "kind"), _path(event, "refId")
    reference_registry.validate_scope_and_kind(deps.scopes(), scope, kind)
    return _json(200, reference_registry.view(deps.registry.deactivate(scope, key, kind, ref_id, caller["appName"])))


_ROUTES = {
    (BASE, "POST"): _reserve,
    (BASE, "GET"): _list,
    (CONFIRM, "POST"): _confirm,
    (ONE, "GET"): _get,
    (ONE, "DELETE"): _deactivate,
}


def handle(event: Dict[str, Any], deps: Dependencies) -> Dict[str, Any]:
    route = _ROUTES.get((event.get("resource"), event.get("httpMethod")))
    if route is None:
        return _error(404, "NOT_FOUND", "Ruta no soportada.")
    try:
        caller = resolve_caller(event, deps.apps_table, deps.key_required)
        if not caller["allowed"]:
            return _error(403, "FORBIDDEN", "This app is not allowed to manage authenticity references. "
                                            "Ask for --reference-write if it's needed.")
        scope = _path(event, "scope")
        reference_registry.validate_scope_and_kind(deps.scopes(), scope)
        key = reference_registry.validate_key(_path(event, "key"))
        return route(event, deps, caller, scope, key)
    except RegistryError as e:
        return _error(e.status, e.code, e.message, **e.extra)
    except Exception:
        logger.exception("references_api: unhandled error")
        return _error(500, "INTERNAL_ERROR", "Internal error.")


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    try:
        deps = _dependencies()
    except Exception:
        logger.exception("references_api: could not initialise")
        return _error(500, "INTERNAL_ERROR", "Internal error.")
    return handle(event, deps)
