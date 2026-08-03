"""Tests for catalog-driven QR field recovery."""

import sys
from pathlib import Path
from types import SimpleNamespace


LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline import expediente, qr_recovery  # noqa: E402


CSF_FIELDS = {
    "ID_CIF": {
        "param": "d3",
        "split": "_",
        "part": 0,
        "pattern": r"^\d+$",
    },
    "RFC": {
        "param": "d3",
        "split": "_",
        "part": -1,
        "pattern": r"^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$",
    },
}


def test_maps_old_and_current_csf_d3_shapes():
    assert qr_recovery.apply_spec(
        {"d3": "14111064924_ISS6001015A3"}, CSF_FIELDS
    ) == {"ID_CIF": "14111064924", "RFC": "ISS6001015A3"}
    assert qr_recovery.apply_spec(
        {"d3": "401113583_BMA170112F13"}, CSF_FIELDS
    ) == {"ID_CIF": "401113583", "RFC": "BMA170112F13"}


def test_rejects_malformed_parts_instead_of_trusting_them():
    assert qr_recovery.apply_spec(
        {"d3": "NO-NUMERICO_RFC-INVALIDO"}, CSF_FIELDS
    ) == {}


def test_uses_catalog_declared_output_and_validity_fields(monkeypatch):
    decoded = (
        "https://siat.sat.gob.mx/app/qr/faces/pages/mobile/validadorqr.jsf"
        "?D1=26&D2=1&D3=401113583_BMA170112F13"
    )
    monkeypatch.setattr(
        qr_recovery,
        "extract_qr_recovery",
        lambda *_args, **_kwargs: {
            "qrPresent": True,
            "decodedText": decoded,
            "fields": {"ID_CIF": "401113583", "RFC": "BMA170112F13"},
            "markerPageFound": 1,
            "pageChecked": 1,
        },
    )
    entry = SimpleNamespace(
        qr_recovery={
            "confidence": 0.99,
            "decoded_text_field": "QR_CONSTANCIA",
            "validity_field": "CONSTANCIA_QR_VALIDO",
            "required_fields": ["ID_CIF", "RFC"],
        },
        field_tree={
            name: object()
            for name in ("ID_CIF", "RFC", "QR_CONSTANCIA", "CONSTANCIA_QR_VALIDO")
        },
        extraction_fields=[],
    )
    doc = {"_entities": {}, "_confidences": {}}

    expediente._recover_from_qr(
        doc, entry, SimpleNamespace(page_start=1), object(), []
    )

    assert doc["_entities"] == {
        "ID_CIF": "401113583",
        "RFC": "BMA170112F13",
        "QR_CONSTANCIA": decoded,
        "CONSTANCIA_QR_VALIDO": True,
    }
    assert doc["qrRecovery"]["valid"] is True
    assert doc["qrRecovery"]["fieldsRecovered"] == ["ID_CIF", "RFC"]


def test_preserves_pedimento_default_output_field_names(monkeypatch):
    monkeypatch.setattr(
        qr_recovery,
        "extract_qr_recovery",
        lambda *_args, **_kwargs: {
            "qrPresent": True,
            "decodedText": "https://example.test/?pa=1234",
            "fields": {"PATENTE": "1234"},
            "markerPageFound": 1,
            "pageChecked": 1,
        },
    )
    entry = SimpleNamespace(
        qr_recovery={"confidence": 0.99},
        field_tree={
            name: object()
            for name in (
                "PATENTE",
                "QR_PEDIMENTO",
                "DEPOSITO_REFERENCIADO_QR_VALIDO",
            )
        },
        extraction_fields=[],
    )
    doc = {"_entities": {}, "_confidences": {}}

    expediente._recover_from_qr(
        doc, entry, SimpleNamespace(page_start=1), object(), []
    )

    assert doc["_entities"]["QR_PEDIMENTO"] == "https://example.test/?pa=1234"
    assert doc["_entities"]["DEPOSITO_REFERENCIADO_QR_VALIDO"] is True
