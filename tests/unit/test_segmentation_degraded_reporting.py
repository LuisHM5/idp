"""The final result must say WHY a page range's classification was left as
OTRO, not just that it was: `tracking.segmentationDegraded` (list of
`{pageStart, pageEnd, reason, resolution, attempts}`), promoted to the
DynamoDB `Tracking` column exactly like `tracking.modelFallbacks` already is
(indexing.py). This is the fix for the real incident that motivated it: a
31-page PDF whose segmentation silently collapsed to
`documentClassification: "OTRO"` / `documentCount: 0` with no signal in the
API response explaining why (see `pipeline/segmenter.py`'s `_classify_pages`/
`_classify_windowed` for where the signal is produced, via
`pipeline/bedrock.py::record_stage_degraded`/`stage_degradations`).

See `tests/unit/test_segmenter_candidates.py` and
`tests/unit/test_segmenter_windowing.py` for how the signal gets produced;
this file covers only that it reaches the caller correctly.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import document_catalog  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
import handler  # noqa: E402
from indexing import _TRACKING_FIELD_MAP, _tracking_attribute  # noqa: E402
from pipeline import bedrock, expediente  # noqa: E402


def _entry(type_id: str, **extra):
    return _parse_entry(type_id, {"fields": {}, "description": "Un documento cualquiera.", **extra})


@pytest.fixture(autouse=True)
def _clean_bedrock_state():
    bedrock.reset_used_models()
    yield
    bedrock.reset_used_models()


# --- expediente.process_pdf -----------------------------------------------


def test_process_pdf_expone_segmentation_degraded_reviewRequired_y_warning(monkeypatch):
    def fake_segment_pages(*_args, **_kwargs):
        # Simula lo que _classify_pages/_classify_windowed hacen de verdad:
        # registran la degradación como efecto colateral de la llamada.
        bedrock.record_stage_degraded("segmenter", {
            "pageStart": 1, "pageEnd": 31,
            "reason": "empty_response", "resolution": "marked_otro",
            "attempts": [{"modelId": "minimax.minimax-m2.5", "reason": "empty_response"}],
        })
        return [], {}

    monkeypatch.setattr(expediente, "load_image_page", lambda _data: [
        SimpleNamespace(number=1, text="", source="ocr", confidence=0.5)
    ])
    monkeypatch.setattr(expediente, "segment_pages", fake_segment_pages)
    monkeypatch.setattr(expediente, "get_type_entry", lambda type_id: _entry(type_id))
    monkeypatch.setattr(expediente, "_run_cross_document_rules", lambda *_args: [])

    result = expediente.process_pdf(
        b"image", source_format="png", declared_types=["PEDIMENTO_ERP", "FACTURA_FLETE_EXTRANJERO"],
    )

    assert result["segmentationDegraded"] == [{
        "pageStart": 1, "pageEnd": 31,
        "reason": "empty_response", "resolution": "marked_otro",
        "attempts": [{"modelId": "minimax.minimax-m2.5", "reason": "empty_response"}],
    }]
    assert result["reviewRequired"] is True
    assert any("degradada" in w and "1-31" in w for w in result["warnings"])


def test_process_pdf_no_dice_nada_cuando_la_recuperacion_del_reintento_funciono(monkeypatch):
    """Una degradación que el reintento SÍ recuperó no debe alarmar al
    usuario final — solo queda visible en tracking para observabilidad."""
    def fake_segment_pages(*_args, **_kwargs):
        bedrock.record_stage_degraded("segmenter", {
            "pageStart": 1, "pageEnd": 5,
            "reason": "empty_response", "resolution": "recovered_on_retry",
            "attempts": [],
        })
        return [], {}

    monkeypatch.setattr(expediente, "load_image_page", lambda _data: [
        SimpleNamespace(number=1, text="", source="ocr", confidence=0.5)
    ])
    monkeypatch.setattr(expediente, "segment_pages", fake_segment_pages)
    monkeypatch.setattr(expediente, "get_type_entry", lambda type_id: _entry(type_id))
    monkeypatch.setattr(expediente, "_run_cross_document_rules", lambda *_args: [])

    result = expediente.process_pdf(b"image", source_format="png", declared_types=["A"])

    assert result["segmentationDegraded"][0]["resolution"] == "recovered_on_retry"
    assert not any("degradada" in w for w in result["warnings"])


def test_process_pdf_segmentation_degraded_vacio_cuando_no_hubo_nada(monkeypatch):
    monkeypatch.setattr(expediente, "load_image_page", lambda _data: [
        SimpleNamespace(number=1, text="", source="ocr", confidence=0.5)
    ])
    monkeypatch.setattr(expediente, "segment_pages", lambda *_a, **_k: ([], {}))
    monkeypatch.setattr(expediente, "get_type_entry", lambda type_id: _entry(type_id))
    monkeypatch.setattr(expediente, "_run_cross_document_rules", lambda *_args: [])

    result = expediente.process_pdf(b"image", source_format="png", declared_types=["A"])

    assert result["segmentationDegraded"] == []


# --- DynamoDB indexed Tracking column ---------------------------------------


def test_segmentation_degraded_promovido_a_la_columna_indexada():
    assert _TRACKING_FIELD_MAP["segmentationDegraded"] == "SegmentationDegraded"

    attr = _tracking_attribute({
        "tracking": {
            "createdAt": "2026-01-01T00:00:00Z",
            "segmentationDegraded": [
                {"pageStart": 1, "pageEnd": 31, "reason": "empty_response", "resolution": "marked_otro"}
            ],
        }
    })

    assert attr["SegmentationDegraded"][0]["reason"] == "empty_response"


def test_columna_ausente_cuando_tracking_no_trae_degradaciones():
    attr = _tracking_attribute({"tracking": {"createdAt": "2026-01-01T00:00:00Z"}})
    assert "SegmentationDegraded" not in attr


# --- handler._run_expediente: llega hasta tracking --------------------------


def test_run_expediente_expone_segmentation_degraded_en_tracking(monkeypatch):
    event = {"content": {"bucket": "test-bucket", "key": "raw/uuid-123/file"}}

    monkeypatch.setattr(handler, "_fetch_document", lambda bucket, key: (b"%PDF-bytes", "application/pdf", {}))
    monkeypatch.setattr(handler, "get_type_entry", lambda type_id: SimpleNamespace(type_id="OTRO"))
    monkeypatch.setattr(document_catalog, "get_type_entry", lambda type_id: SimpleNamespace(type_id="OTRO"))

    def fake_process_pdf(*_args, **_kwargs):
        bedrock.record_stage_degraded("segmenter", {
            "pageStart": 1, "pageEnd": 31, "reason": "empty_response", "resolution": "marked_otro",
        })
        return {
            "documents": [],
            "status": "REVIEW_REQUIRED",
            "reviewRequired": True,
            "segmentationDegraded": bedrock.stage_degradations().get("segmenter") or [],
            "tracking": {
                "createdAt": "2026-01-01T00:00:00Z",
                "processingCompletedAt": "2026-01-01T00:01:00Z",
                "processingSeconds": 60,
                "processingMs": 60000,
            },
            "crossDocumentChecks": [],
            "warnings": [],
        }

    monkeypatch.setattr(expediente, "process_pdf", fake_process_pdf)

    result = handler._run_expediente(event, "doc-1", "OTRO")

    assert result is not None
    assert result["tracking"]["segmentationDegraded"][0]["reason"] == "empty_response"


def test_run_expediente_no_agrega_la_clave_cuando_nada_degrado(monkeypatch):
    event = {"content": {"bucket": "test-bucket", "key": "raw/uuid-456/file"}}

    monkeypatch.setattr(handler, "_fetch_document", lambda bucket, key: (b"%PDF-bytes", "application/pdf", {}))
    monkeypatch.setattr(handler, "get_type_entry", lambda type_id: SimpleNamespace(type_id="OTRO"))
    monkeypatch.setattr(document_catalog, "get_type_entry", lambda type_id: SimpleNamespace(type_id="OTRO"))

    def fake_process_pdf(*_args, **_kwargs):
        return {
            "documents": [],
            "status": "APPROVED",
            "reviewRequired": False,
            "segmentationDegraded": [],
            "tracking": {
                "createdAt": "2026-01-01T00:00:00Z",
                "processingCompletedAt": "2026-01-01T00:01:00Z",
                "processingSeconds": 1,
                "processingMs": 1000,
            },
            "crossDocumentChecks": [],
            "warnings": [],
        }

    monkeypatch.setattr(expediente, "process_pdf", fake_process_pdf)

    result = handler._run_expediente(event, "doc-2", "OTRO")

    assert result is not None
    assert "segmentationDegraded" not in result["tracking"]
