"""Idempotency and recovery of the direct S3 → Enrichment ingress.

S3 delivers at least once and Lambda retries a failed asynchronous invocation
twice, so the DynamoDB claim is what keeps one upload from being processed
twice — and what must NOT leave a document stuck in PROCESSING when an attempt
dies. `FakeTable` evaluates the real Condition/Update expressions the handler
sends, so these tests fail if an expression stops meaning what the code needs.
"""

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

RESOURCES = Path(__file__).resolve().parents[2] / "src/document-processing/resources"
sys.path.insert(0, str(RESOURCES / "enrichment-lambda"))

import handler  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "async_failure_lambda", RESOURCES / "async-failure-lambda/index.py"
)
finalizer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(finalizer)


class ConditionalCheckFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """One-table DynamoDB stand-in for the subset of expressions used here."""

    def __init__(self, items=None):
        self.items = {item["DocumentId"]: dict(item) for item in (items or [])}

    def get_item(self, Key, **_):
        item = self.items.get(Key["DocumentId"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None, ConditionExpression=None, **_):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues
        item = self.items.get(Key["DocumentId"], {})

        def attr(token):
            return names.get(token, token)

        if ConditionExpression and not self._holds(ConditionExpression, item, attr, values):
            raise ConditionalCheckFailed()

        item = dict(item, DocumentId=Key["DocumentId"])
        set_part, _, remove_part = UpdateExpression.partition(" REMOVE ")
        for match in re.finditer(
            r"(\S+) = (?:if_not_exists\(\S+, (?P<default>:\w+)\)|(?P<value>:\w+))", set_part
        ):
            name = attr(match.group(1))
            if match.group("default"):
                item.setdefault(name, values[match.group("default")])
            else:
                item[name] = values[match.group("value")]
        for token in filter(None, (t.strip() for t in remove_part.split(","))):
            item.pop(attr(token), None)
        self.items[Key["DocumentId"]] = item
        return {}

    @staticmethod
    def _holds(expression, item, attr, values):
        def not_exists(token):
            return attr(token) not in item

        def is_in(token, options):
            return item.get(attr(token)) in [values[o.strip()] for o in options.split(",")]

        def compare(token, operator, value):
            name = attr(token)
            if name not in item:  # DynamoDB: a missing attribute matches nothing
                return operator == "<>"
            left, right = item[name], values[value]
            return {"=": left == right, "<>": left != right, "<": left < right}[operator]

        python = re.sub(r"attribute_not_exists\((\S+?)\)", r"not_exists('\1')", expression)
        python = re.sub(r"(\S+) IN \(([^)]*)\)", r"is_in('\1', '\2')", python)
        python = re.sub(r"([#\w]+) (=|<>|<) (:\w+)", r"compare('\1', '\2', '\3')", python)
        python = python.replace(" AND ", " and ").replace(" OR ", " or ")
        return eval(python, {"not_exists": not_exists, "is_in": is_in, "compare": compare})


class Context:
    def __init__(self, request_id, remaining_ms=900_000):
        self.aws_request_id = request_id
        self._remaining = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._remaining


def s3_record(key="source/CERTIFICADO_POLIZA/doc-1/emp_cert.pdf", sequencer="SEQ1"):
    return {
        "eventSource": "aws:s3",
        "eventName": "ObjectCreated:Put",
        "eventTime": "2026-10-13T20:01:56.298Z",
        "s3": {"bucket": {"name": "docs"}, "object": {"key": key, "sequencer": sequencer}},
    }


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable([{"DocumentId": "doc-1", "ProcessingStatus": "AWAITING_UPLOAD"}])
    monkeypatch.setattr(handler, "_document_table", lambda: fake)
    return fake


def claim(request_id="req-1", **record):
    return handler._claim_direct_event(
        handler._s3_record_to_event(s3_record(**record)), Context(request_id)
    )


def test_first_delivery_claims_the_reserved_row(table):
    assert claim() == "claimed"
    row = table.items["doc-1"]
    assert row["ProcessingStatus"] == "PROCESSING"
    assert row["SourceKey"] == "source/CERTIFICADO_POLIZA/doc-1/emp_cert.pdf"
    assert row["UploadedAt"] == "2026-10-13T20:01:56.298Z"


def test_concurrent_duplicate_delivery_is_skipped_while_the_lease_is_alive(table):
    assert claim("req-1") == "claimed"
    assert claim("req-2") == "duplicate"


def test_async_retry_of_a_crashed_attempt_takes_its_own_claim_over(table):
    # An out-of-memory kill never reaches _release_direct_claim, and the ~15
    # minute lease outlives both Lambda retries (1 and 2 minutes later).
    # Without the request-id clause both retries would be skipped as
    # duplicates and the document would stay PROCESSING forever.
    assert claim("req-1") == "claimed"
    assert claim("req-1") == "claimed"


def test_expired_lease_is_reclaimed_by_another_delivery(table):
    assert claim("req-1") == "claimed"
    table.items["doc-1"]["ProcessingLeaseUntil"] = 1
    assert claim("req-2") == "claimed"


def test_finished_document_is_never_reprocessed(table):
    table.items["doc-1"].update(
        ProcessingStatus="FINISHED", WorkflowStatus="complete", TerminalEventPublished=True,
        ProcessingRequestId="req-1",
    )
    assert claim("req-1") == "duplicate"
    assert claim("req-2", sequencer="SEQ2") == "duplicate"


def test_saved_result_with_unpublished_event_only_republishes(table):
    table.items["doc-1"].update(
        ProcessingStatus="FINISHED", WorkflowStatus="complete", TerminalEventPublished=False,
    )
    assert claim() == "publish-only"


def test_failed_attempt_releases_the_claim_and_propagates(table, monkeypatch):
    def boom(event, context=None):
        raise RuntimeError("s3 unavailable")

    monkeypatch.setattr(handler, "_handle_one", boom)
    with pytest.raises(RuntimeError):
        handler.handler({"Records": [s3_record()]}, Context("req-1"))
    row = table.items["doc-1"]
    assert row["ProcessingStatus"] == "QUEUED"
    assert row["ProcessingError"] == "s3 unavailable"
    assert "ProcessingLeaseUntil" not in row
    # ...so the retry of that same invocation can process it.
    assert claim("req-1") == "claimed"


def test_persisted_result_finishes_the_row_and_drops_the_lease(table):
    event = handler._s3_record_to_event(s3_record())
    handler._claim_direct_event(event, Context("req-1"))
    event["appOrigin"] = "portal"
    handler._persist_direct_result({"documentId": "doc-1"}, event)
    row = table.items["doc-1"]
    assert (row["ProcessingStatus"], row["WorkflowStatus"]) == ("FINISHED", "complete")
    assert row["TerminalEventPublished"] is False
    assert row["JobOwner"] == "portal"
    assert "ProcessingLeaseUntil" not in row


@pytest.mark.parametrize(
    "key", ["source/doc-1/", "source/loose.pdf", "results/doc-1.json", "raw/file.pdf"]
)
def test_objects_outside_the_document_layout_are_ignored(table, key):
    assert handler._s3_record_to_event(s3_record(key=key)) is None
    assert handler.handler({"Records": [s3_record(key=key)]}, Context("req-1")) == {
        "processed": []
    }
    assert table.items["doc-1"]["ProcessingStatus"] == "AWAITING_UPLOAD"


def test_unreadable_source_raises_on_direct_ingress_instead_of_validating_nothing(
    monkeypatch,
):
    def unreadable(bucket, key):
        raise OSError("AccessDenied")

    monkeypatch.setattr(handler, "_fetch_document", unreadable)
    event = handler._s3_record_to_event(s3_record())
    with pytest.raises(handler._SourceUnavailable):
        handler._process(event)


# --- failure finalizer -------------------------------------------------------


class FakeEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


@pytest.fixture
def finalizer_aws(monkeypatch):
    fake_table = FakeTable(
        [{"DocumentId": "doc-1", "ProcessingStatus": "PROCESSING", "ProcessingLeaseUntil": 9}]
    )
    events = FakeEvents()
    boto3 = types.SimpleNamespace(
        resource=lambda _service: types.SimpleNamespace(Table=lambda _name: fake_table),
        client=lambda _service: events,
    )
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setenv("DOCUMENT_TABLE", "documents")
    monkeypatch.setenv("EVENT_BUS_NAME", "bus")
    return fake_table, events


def destination_event(**record):
    return {
        "requestContext": {"condition": "RetriesExhausted"},
        "responseContext": {"functionError": "Unhandled"},
        "responsePayload": {"errorMessage": "Task timed out after 900.00 seconds"},
        "requestPayload": {"Records": [s3_record(**record)]},
    }


def test_finalizer_marks_exhausted_document_failed_and_publishes(finalizer_aws):
    fake_table, events = finalizer_aws
    assert finalizer.handler(destination_event()) == {"finalized": ["doc-1"]}
    row = fake_table.items["doc-1"]
    assert (row["ProcessingStatus"], row["WorkflowStatus"]) == ("FAILED", "enrichment-failure")
    assert row["ProcessingError"] == (
        "RetriesExhausted: Unhandled: Task timed out after 900.00 seconds"
    )
    assert "ProcessingLeaseUntil" not in row
    assert events.entries[0]["DetailType"] == "document-processing-failed"


def test_finalizer_never_fails_a_document_that_already_completed(finalizer_aws):
    fake_table, events = finalizer_aws
    fake_table.items["doc-1"].update(ProcessingStatus="FINISHED", WorkflowStatus="complete")
    assert finalizer.handler(destination_event()) == {"finalized": []}
    assert fake_table.items["doc-1"]["ProcessingStatus"] == "FINISHED"
    assert events.entries == []


def test_finalizer_ignores_objects_that_are_not_documents(finalizer_aws):
    fake_table, events = finalizer_aws
    assert finalizer.handler(destination_event(key="raw/file.pdf")) == {"finalized": []}
    assert set(fake_table.items) == {"doc-1"}
    assert events.entries == []
