import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).parents[2]
    / "src/document-processing/resources/status-event-lambda/index.py"
)
SPEC = importlib.util.spec_from_file_location("status_event_lambda", MODULE_PATH)
status_event = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(status_event)


def test_maps_only_terminal_pipeline_events():
    assert status_event.status_for_event(
        {"detail-type": "document-processed-successful"}
    ) == "FINISHED"
    assert status_event.status_for_event(
        {"detail-type": "document-processing-failed"}
    ) == "FAILED"
    assert status_event.status_for_event({"detail-type": "something-else"}) is None


def test_updates_the_public_terminal_status(monkeypatch):
    calls = []

    class FakeTable:
        def update_item(self, **kwargs):
            calls.append(kwargs)

    fake_boto3 = SimpleNamespace(
        resource=lambda service: SimpleNamespace(Table=lambda name: FakeTable())
    )
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DOCUMENT_TABLE", "documents")

    result = status_event.handler({
        "detail-type": "document-processed-successful",
        "time": "2026-09-03T12:00:00Z",
        "detail": {"documentId": "doc-1"},
    })

    assert result == {
        "updated": True,
        "documentId": "doc-1",
        "processingStatus": "FINISHED",
    }
    assert calls[0]["Key"] == {"DocumentId": "doc-1"}
    assert calls[0]["ExpressionAttributeValues"] == {
        ":status": "FINISHED",
        ":updated": "2026-09-03T12:00:00Z",
    }

