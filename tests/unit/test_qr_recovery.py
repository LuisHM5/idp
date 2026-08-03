"""Tests for catalog-driven QR field recovery."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace


LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline import expediente, qr_recovery  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from validators.engine import ValidationContext, resolve_field_tree  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]


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
        qr_recovery={"confidence": 0.99},
        field_tree={
            "ID_CIF": SimpleNamespace(params={"qr_source": CSF_FIELDS["ID_CIF"]}),
            "RFC": SimpleNamespace(params={"qr_source": CSF_FIELDS["RFC"]}),
            "QR_CONSTANCIA": SimpleNamespace(params={"qr_source": "payload"}),
            "CONSTANCIA_QR_VALIDO": SimpleNamespace(
                params={
                    "qr_source": "valid",
                    "qr_requires": ["ID_CIF", "RFC"],
                }
            ),
        },
        extraction_fields=[],
    )
    doc = {"_entities": {}, "_confidences": {}}

    compiled = expediente._qr_spec_from_fields(entry)
    assert compiled["fields"] == CSF_FIELDS
    assert compiled["decoded_text_field"] == "QR_CONSTANCIA"
    assert compiled["validity_field"] == "CONSTANCIA_QR_VALIDO"
    assert compiled["required_fields"] == ["ID_CIF", "RFC"]

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


def test_a_payload_field_alone_enables_generic_qr_reading(monkeypatch):
    monkeypatch.setattr(
        qr_recovery,
        "extract_qr_recovery",
        lambda *_args, **_kwargs: {
            "qrPresent": True,
            "decodedText": "contenido-libre-del-qr",
            "fields": {},
            "markerPageFound": None,
            "pageChecked": 1,
        },
    )
    entry = SimpleNamespace(
        qr_recovery=None,
        field_tree={
            "QR_CONTENIDO": SimpleNamespace(params={"qr_source": "payload"}),
        },
        extraction_fields=[],
    )
    doc = {"_entities": {}, "_confidences": {}}

    expediente._recover_from_qr(
        doc, entry, SimpleNamespace(page_start=1), object(), []
    )

    assert doc["_entities"]["QR_CONTENIDO"] == "contenido-libre-del-qr"
    assert doc["qrRecovery"]["qrPresent"] is True


def _factura_honorarios_entry():
    raw = json.loads(
        (REPO_ROOT / "catalogs/FACTURA_HONORARIOS.json").read_text(encoding="utf-8")
    )["FACTURA_HONORARIOS"]
    shared = json.loads(
        (REPO_ROOT / "catalogs/_shared.json").read_text(encoding="utf-8")
    )
    return _parse_entry(
        "FACTURA_HONORARIOS",
        raw,
        common_item_fields=shared["_common_item_fields"],
    )


def test_factura_honorarios_qr_mapping_is_fully_catalog_driven():
    entry = _factura_honorarios_entry()
    spec = expediente._qr_spec_from_fields(entry)
    params = {
        "id": "00000000-0000-4000-8000-000000000001",
        "re": "XAXX010101000",
        "rr": "XEXX010101000",
        "tt": "95.330000",
        "fe": "AbCd12==",
    }

    mapped = qr_recovery.apply_spec(params, spec["fields"])

    assert set(mapped) == {
        "UUID",
        "EMISOR_RFC",
        "RECEPTOR_RFC",
        "TOTAL_CFDI",
        "QR_SELLO_ULTIMOS_8",
    }
    assert spec["decoded_text_field"] == "QR_FACTURA"
    assert spec["validity_field"] == "FACTURA_QR_VALIDA"
    assert set(spec["required_fields"]) == set(mapped)


def test_factura_honorarios_reconciles_cfdi_expenses_and_balance():
    entry = _factura_honorarios_entry()
    values = {
        "VERSION_CFDI": "4.0",
        "FOLIO": "1",
        "UUID": "00000000-0000-4000-8000-000000000001",
        "FECHA_EMISION": "2026-01-01T10:00:00",
        "FECHA_TIMBRADO": "2026-01-01T10:01:00",
        "TIPO_COMPROBANTE": "I",
        "FORMA_PAGO": "99",
        "METODO_PAGO": "PPD",
        "EMISOR_RFC": "XAXX010101000",
        "EMISOR_NOMBRE": "EMISOR PRUEBA",
        "EMISOR_REGIMEN_FISCAL": "612",
        "RECEPTOR_RFC": "XEXX010101000",
        "RECEPTOR_NOMBRE": "RECEPTOR PRUEBA",
        "RECEPTOR_REGIMEN_FISCAL": "623",
        "RECEPTOR_CODIGO_POSTAL": "00000",
        "USO_CFDI": "G03",
        "CONCEPTOS": [{
            "CANTIDAD": 1,
            "CLAVE_UNIDAD": "E48",
            "CLAVE_PRODUCTO_SERVICIO": "80151605",
            "DESCRIPCION": "SERVICIO",
            "PRECIO_UNITARIO": 100,
            "IMPORTE": 100,
        }],
        "SUBTOTAL_CFDI": 100,
        "IVA_TRASLADADO": 16,
        "SUBTOTAL_MAS_IVA": 116,
        "ISR_RETENIDO": 10,
        "IVA_RETENIDO": 10.67,
        "TOTAL_CFDI": 95.33,
        "GASTOS_CUENTA_TERCEROS": [{"CONCEPTO": "GASTO", "IMPORTE": 20}],
        "TOTAL_FACTURA_MAS_GASTOS": 115.33,
        "ANTICIPO": 0,
        "SALDO_PAGAR": 115.33,
        "FACTURA_QR_VALIDA": True,
    }
    context = ValidationContext(
        expected_values={"RECEPTOR_RFC": "XEXX010101000"}
    )

    _, checks = resolve_field_tree(values, {}, entry, context)

    assert not [check for check in checks if not check.valid]
    assert not [check for check in checks if check.skipped]


def test_cross_document_rules_use_raw_values_for_field_tree_documents():
    uuid = "00000000-0000-4000-8000-000000000001"
    documents = [
        {
            "documentType": "PEDIMENTO_ADUANAL",
            "_entities": {"NUMERO_PEDIMENTO_NORMALIZADO": "260708237654321"},
            "extractedEntities": {
                "NUMERO_PEDIMENTO_NORMALIZADO": {"value": "260708237654321"}
            },
        },
        {
            "documentType": "FACTURA_HONORARIOS",
            "_entities": {
                "PEDIMENTO_REFERENCE": "7654321",
                "RECEPTOR_RFC": "XEXX010101000",
                "UUID": uuid,
                "TOTAL_CFDI": 95.33,
            },
            "extractedEntities": {
                "PEDIMENTO_REFERENCE": {"value": "7654321"},
                "RECEPTOR_RFC": {"value": "XEXX010101000"},
                "UUID": {"value": uuid},
                "TOTAL_CFDI": {"value": 95.33},
            },
        },
    ]
    same_pedimento = expediente._cross_same_pedimento(
        {
            "documents": ["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
        },
        documents,
    )
    importer = expediente._cross_importer_rfc(
        {}, documents, {"IMPORTER_RFC": "XEXX010101000"}
    )

    assert same_pedimento["valid"] is True
    assert same_pedimento["skipped"] is False
    assert importer["valid"] is True

    duplicate = dict(documents[1])
    duplicate["_entities"] = dict(documents[1]["_entities"])
    unique_uuid = expediente._cross_unique_uuid(
        {"documents": ["FACTURA_HONORARIOS"]},
        [documents[1], duplicate],
    )

    assert unique_uuid["valid"] is True
    assert unique_uuid["severity"] == "warning"
    assert unique_uuid["details"]["copies"] == {uuid: 2}
