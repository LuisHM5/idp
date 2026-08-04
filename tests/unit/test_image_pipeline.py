"""Tests for standalone-image support (JPG/JPEG/PNG) in the expediente
pipeline — a field-centric type (e.g. INE) isn't necessarily backed by a
PDF; a photographed/scanned ID is routinely uploaded as a raw image, and
`pdf_pages.load_pages` (pypdf) cannot parse those bytes at all (that
mismatch used to surface as "unreadable PDF: Stream has ended
unexpectedly" for a plain .jpeg upload).

No real Textract/Bedrock calls: `_textract_client`/`load_image_page`/
`load_pages` are monkeypatched.
"""

import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline import expediente, pdf_pages  # noqa: E402
from pipeline.result_common import error_result  # noqa: E402


def _textract_response(lines):
    return {
        "Blocks": [
            {
                "BlockType": "LINE",
                "Text": text,
                "Confidence": 99.0,
                "Geometry": {"BoundingBox": {"Top": top, "Left": 0.1}},
            }
            for text, top in lines
        ]
    }


def test_blocks_to_page_sorts_by_position_not_response_order():
    # Textract can return blocks out of visual order; Top is authoritative,
    # broken by Left when two blocks share (near enough) the same row.
    response = _textract_response([("segunda", 0.5), ("primera", 0.1)])
    page = pdf_pages._blocks_to_page(response, 1)
    assert page.text == "primera\nsegunda"
    assert page.number == 1
    assert page.source == "textract"
    assert page.confidence == 0.99


def test_blocks_to_page_empty_response_yields_empty_source():
    page = pdf_pages._blocks_to_page({"Blocks": []}, 1)
    assert page.text == ""
    assert page.source == "empty"
    assert page.confidence == 0.0


def test_load_image_page_calls_textract_directly_no_pypdf(monkeypatch):
    calls = []

    class FakeClient:
        def detect_document_text(self, Document):
            calls.append(Document)
            return _textract_response([("INE", 0.1), ("HERNANDEZ LARA LUIS", 0.2)])

    monkeypatch.setattr(pdf_pages, "_textract_client", lambda: FakeClient())
    pages = pdf_pages.load_image_page(b"\xff\xd8\xff\xe0fake-jpeg-bytes")

    assert len(pages) == 1
    assert pages[0].number == 1
    assert pages[0].source == "textract"
    assert pages[0].text == "INE\nHERNANDEZ LARA LUIS"
    assert calls == [{"Bytes": b"\xff\xd8\xff\xe0fake-jpeg-bytes"}]  # raw bytes, no PDF wrapping


def test_process_pdf_routes_jpeg_to_load_image_page_not_load_pages(monkeypatch):
    calls = {"image": 0, "pdf": 0}

    def _fake_load_image_page(data):
        calls["image"] += 1
        return []

    def _fake_load_pages(data):
        calls["pdf"] += 1
        return []

    monkeypatch.setattr(expediente, "load_image_page", _fake_load_image_page)
    monkeypatch.setattr(expediente, "load_pages", _fake_load_pages)

    # Mocked loader returns no pages, so process_pdf fails fast right after
    # page-loading — enough to prove which loader ran without also having to
    # mock the segmenter/Bedrock extraction beyond this point.
    result = expediente.process_pdf(b"fake-jpeg-bytes", declared_type="INE", source_format="jpeg")

    assert calls == {"image": 1, "pdf": 0}
    assert result["documents"] == []
    assert result["error"]["stage"] == "page_load"
    assert result["error"]["sourceFormat"] == "jpeg"
    assert "jpeg" in result["warnings"][0]


def test_process_pdf_still_routes_real_pdf_to_load_pages(monkeypatch):
    calls = {"image": 0, "pdf": 0}

    def _fake_load_image_page(data):
        calls["image"] += 1
        return []

    def _fake_load_pages(data):
        calls["pdf"] += 1
        return []

    monkeypatch.setattr(expediente, "load_image_page", _fake_load_image_page)
    monkeypatch.setattr(expediente, "load_pages", _fake_load_pages)
    monkeypatch.setattr(expediente, "_truncate_pdf", lambda data, max_pages: (data, None))

    result = expediente.process_pdf(b"%PDF-1.4 fake", declared_type="PEDIMENTO_ADUANAL")  # source_format defaults to "pdf"

    assert calls == {"image": 0, "pdf": 1}
    assert result["error"]["sourceFormat"] == "pdf"


def test_process_pdf_reports_exception_type_and_format_aware_message(monkeypatch):
    def _boom(data):
        raise ValueError("corrupted image data")

    monkeypatch.setattr(expediente, "load_image_page", _boom)

    result = expediente.process_pdf(b"garbage", declared_type="INE", source_format="png")

    assert result["error"]["type"] == "ValueError"
    assert result["error"]["stage"] == "page_load"
    assert result["error"]["sourceFormat"] == "png"
    assert result["warnings"][0] == "unreadable png image: corrupted image data"
    # No more "unreadable PDF" for an image failure — that was the confusing
    # part of the original bug report (a .jpeg failing with a PDF-flavored
    # message and no clear indication of what actually went wrong).
    assert "PDF" not in result["warnings"][0]


def test_error_result_structured_fields_are_optional_and_backward_compatible():
    # Old call shape (no stage/exception/source_format) must still work —
    # every pre-existing caller of error_result().
    plain = error_result("pid", "src", "boom")
    assert plain["warnings"] == ["boom"]
    assert plain["error"] == {"message": "boom"}
    assert "sourceFormat" not in plain

    detailed = error_result(
        "pid", "src", "boom", stage="parse", exception=ValueError("x"), source_format="csv",
    )
    assert detailed["error"] == {
        "message": "boom", "stage": "parse", "type": "ValueError", "sourceFormat": "csv",
    }
    assert detailed["sourceFormat"] == "csv"
