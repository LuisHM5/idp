"""One boto3 client per service for the life of the Lambda container.

Building a client parses the service model and its first call opens a new TLS
connection. Doing that inline on every use (`boto3.client("s3").get_object`)
cost a document that needs no OCR and no model more time in AWS plumbing than
in its own pipeline — several hundred milliseconds on a cold start and a fresh
handshake per call on a warm one. Reusing the client keeps the connection pool
alive between invocations.

Reuse is limited to the Lambda runtime on purpose: local tools and unit tests
replace `boto3.client`/`boto3.resource` per test, and a process-wide cache
would hand one test the fake of another.

Sharing across the handler's worker threads is safe for how these are used:
low-level clients are thread-safe, and `Table.update_item/put_item/scan/
batch_writer` only issue calls through the resource's client without mutating
the resource itself.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, Tuple

_LOCK = threading.Lock()
_CACHE: Dict[Tuple[str, str], Any] = {}


def _reusable() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _shared(kind: str, name: str, build: Callable[[], Any]) -> Any:
    if not _reusable():
        return build()
    key = (kind, name)
    cached = _CACHE.get(key)
    if cached is None:
        with _LOCK:
            cached = _CACHE.get(key)
            if cached is None:
                cached = _CACHE[key] = build()
    return cached


def client(service: str) -> Any:
    """Default-configured low-level client for `service`."""

    def build() -> Any:
        import boto3  # deferred: the Lambda runtime provides it

        return boto3.client(service)

    return _shared("client", service, build)


def table(name: str) -> Any:
    """DynamoDB `Table` resource for `name`."""

    def resource() -> Any:
        import boto3

        return boto3.resource("dynamodb")

    dynamodb = _shared("resource", "dynamodb", resource)
    return _shared("table", name, lambda: dynamodb.Table(name))


def warm(table_names: Tuple[str, ...] = ()) -> None:
    """Build the clients every document uses, during init instead of on the
    first request. Best-effort: a failure here only postpones the cost."""
    for service in ("s3", "events"):
        client(service)
    for name in table_names:
        if name:
            table(name)
