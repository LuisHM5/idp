import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_ROOT = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_ROOT))

from document_catalog import _parse_entry  # noqa: E402
from pipeline import text_acquisition  # noqa: E402
from pipeline.pdf_pages import PageText  # noqa: E402


def _entry():
    return _parse_entry("POLIZA_PRUEBA", {
        "description": "fixture",
        "text_acquisition": {
            "ocr_mode": "only_if_deterministic_incomplete",
            "ocr_page_scope": "profile_pages_then_remaining",
        },
        "deterministic_extraction": {
            "enabled": True,
            "fallback": "missing_fields",
            "profile_fallback": "full_model",
            "profiles": [{
                "id": "A",
                "pages": "first",
                "markers": ["FORMATO A"],
                "min_score": 1,
                "expected_fields": ["FOLIO"],
            }],
        },
        "fields": {
            "FOLIO": {
                "type": "string",
                "required": True,
                "text_source": {
                    "selectors": [{
                        "regex": r"FOLIO\s*:\s*(?P<value>[A-Z0-9-]+)",
                        "group": "value",
                    }]
                },
            }
        },
    })


def test_catalog_config_validation_is_closed_and_requires_profiles():
    assert text_acquisition.validate_config(
        {
            "ocr_mode": "only_if_deterministic_incomplete",
            "ocr_page_scope": "profile_pages_then_remaining",
        },
        {"enabled": True, "profiles": [{"id": "A"}]},
    ) == []
    problems = text_acquisition.validate_config(
        {"ocr_mode": "magic", "ocr_page_scope": "somewhere"}, None
    )
    assert len(problems) == 2

    problems = text_acquisition.validate_config(
        {"ocr_mode": "only_if_deterministic_incomplete"},
        {"enabled": True, "profiles": []},
    )
    assert problems == ["OCR diferido requiere deterministic_extraction.profiles"]


def test_complete_native_profile_skips_textract(monkeypatch):
    monkeypatch.setenv("DEFERRED_OCR_ENABLED", "true")
    native = [
        PageText(1, "FORMATO A\nFOLIO: ABC-1", "native", 0.98),
        PageText(2, "", "empty", 0.0),
    ]
    monkeypatch.setattr(text_acquisition, "load_pages", lambda _data, use_ocr=False: native)
    monkeypatch.setattr(
        text_acquisition,
        "apply_ocr",
        lambda *_args, **_kwargs: pytest.fail("Textract must not run"),
    )

    result = text_acquisition.load_pdf_pages(b"pdf", _entry())

    assert result.deterministic.entity_map["FOLIO"] == "ABC-1"
    assert result.optimization["effectiveMode"] == "deferred"
    assert result.optimization["reason"] == "deterministic_complete"
    assert result.optimization["ocrPagesAttempted"] == 0
    assert result.optimization["ocrPagesSkipped"] == 1


def test_incomplete_profile_falls_back_to_ocr(monkeypatch):
    monkeypatch.setenv("DEFERRED_OCR_ENABLED", "true")
    native = [
        PageText(1, "FORMATO A", "native", 0.98),
        PageText(2, "", "empty", 0.0),
    ]
    calls = []

    monkeypatch.setattr(text_acquisition, "load_pages", lambda _data, use_ocr=False: native)

    def _ocr(_data, pages, page_numbers=None):
        calls.append(page_numbers)
        return [pages[0], PageText(2, "FOLIO: OCR-2", "textract", 0.97)]

    monkeypatch.setattr(text_acquisition, "apply_ocr", _ocr)
    result = text_acquisition.load_pdf_pages(b"pdf", _entry())

    assert calls == [[2]]
    assert result.deterministic.entity_map["FOLIO"] == "OCR-2"
    assert result.optimization["effectiveMode"] == "eager_fallback"
    assert result.optimization["ocrPagesAttempted"] == 1
    assert result.optimization["ocrPagesSkipped"] == 0


def test_unknown_profile_uses_all_ocr_candidates(monkeypatch):
    monkeypatch.setenv("DEFERRED_OCR_ENABLED", "true")
    native = [
        PageText(1, "FORMATO DESCONOCIDO", "native", 0.98),
        PageText(2, "", "empty", 0.0),
        PageText(3, "", "empty", 0.0),
    ]
    calls = []
    monkeypatch.setattr(text_acquisition, "load_pages", lambda _data, use_ocr=False: native)

    def _ocr(_data, pages, page_numbers=None):
        calls.append(page_numbers)
        return [
            pages[0],
            PageText(2, "OTRO TEXTO", "textract", 0.97),
            PageText(3, "MÁS TEXTO", "textract", 0.97),
        ]

    monkeypatch.setattr(text_acquisition, "apply_ocr", _ocr)
    result = text_acquisition.load_pdf_pages(b"pdf", _entry())

    assert calls == [[2, 3]]
    assert result.optimization["effectiveMode"] == "eager_fallback"
    assert result.optimization["deterministicPreflight"]["profileStatus"] == "unknown"


def test_model_mode_keeps_eager_ocr(monkeypatch):
    monkeypatch.setenv("DEFERRED_OCR_ENABLED", "true")
    eager = [PageText(1, "OCR", "textract", 0.97)]
    calls = []

    def _load(_data, use_ocr=True):
        calls.append(use_ocr)
        return eager

    monkeypatch.setattr(text_acquisition, "load_pages", _load)
    result = text_acquisition.load_pdf_pages(b"pdf", _entry(), extraction_mode="model")

    assert calls == [True]
    assert result.optimization["effectiveMode"] == "eager"
    assert result.optimization["reason"] == "model_mode_requires_ocr"


@pytest.mark.parametrize("flag", [None, "false", "TRUE", "1", "invalid"])
def test_global_switch_restores_eager_ocr(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("DEFERRED_OCR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DEFERRED_OCR_ENABLED", flag)
    eager = [PageText(1, "FORMATO A\nFOLIO: OCR-1", "textract", 0.97)]
    calls = []

    def _load(_data, use_ocr=True):
        calls.append(use_ocr)
        return eager

    monkeypatch.setattr(text_acquisition, "load_pages", _load)
    result = text_acquisition.load_pdf_pages(b"pdf", _entry())

    assert calls == [True]
    assert result.pages == eager
    assert result.optimization["effectiveMode"] == "eager"
    assert result.optimization["rollbackApplied"] is True
    assert result.optimization["reason"] == "global_kill_switch"


def test_real_emp_fixture_is_complete_without_ocr(monkeypatch):
    fixture = REPO_ROOT / "test-docs/seguros/emp_cert.pdf"
    if not fixture.exists():
        pytest.skip("corpus local de seguros no disponible")
    raw = json.loads(
        (REPO_ROOT / "catalogs/base/seguros/CERTIFICADO_POLIZA.json").read_text()
    )["CERTIFICADO_POLIZA"]
    entry = _parse_entry("CERTIFICADO_POLIZA", raw)
    monkeypatch.setenv("DEFERRED_OCR_ENABLED", "true")
    monkeypatch.setattr(
        text_acquisition,
        "apply_ocr",
        lambda *_args, **_kwargs: pytest.fail("EMP completo no debe usar Textract"),
    )

    result = text_acquisition.load_pdf_pages(fixture.read_bytes(), entry)

    assert result.deterministic.profile_id == "EMP"
    assert result.optimization["effectiveMode"] == "deferred"
    assert result.optimization["ocrPagesSkipped"] == 1
    assert sum(page.source == "textract" for page in result.pages) == 0
