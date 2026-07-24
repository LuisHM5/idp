"""Indexed DynamoDB columns derived from an enrichment result (no AWS)."""

import document_catalog as dc
import indexing


def setup_module(module):
    dc.reload_catalog()


SINGLE_DOC = {
    "documentId": "INE-abc123",
    "documentClassification": "INE",
    "validation": {"status": "APROBADO"},
    "processedAt": "2026-07-24T10:00:00+00:00",
}

EXPEDIENTE = {
    "documentId": "PEDIMENTO-ADUANAL-4ff224be",
    "documentClassification": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
    "validation": {"status": "REQUIERE_REVISION"},
    "processedAt": "2026-07-24T11:30:00+00:00",
    "expediente": {
        "sourceFile": "raw/uuid/expediente-2026.pdf",
        "documents": [
            {"documentType": "PEDIMENTO_ADUANAL"},
            {"documentType": "FACTURA"},
            {"documentType": "OTRO"},
            {"documentType": "FACTURA"},
        ],
    },
}


class TestDeriveIndexAttributes:
    def test_single_document_type_and_status(self):
        attrs = indexing.derive_index_attributes(SINGLE_DOC)
        assert attrs["DocumentType"] == "INE"
        assert attrs["ValidationStatus"] == "APROBADO"
        assert attrs["ProcessedAt"] == "2026-07-24T10:00:00+00:00"
        # Sin expediente, el único tipo contenido es el propio.
        assert attrs["ContainedDocumentTypes"] == {"INE"}

    def test_display_name_defaults_to_titleized_id(self):
        attrs = indexing.derive_index_attributes(
            {"documentId": "x", "documentClassification": "PEDIMENTO_ADUANAL"}
        )
        assert attrs["DocumentTypeName"] == "Pedimento Aduanal"

    def test_expediente_exposes_every_contained_type(self):
        attrs = indexing.derive_index_attributes(EXPEDIENTE)
        assert attrs["DocumentType"] == "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"
        # Deduplicado + incluye el tipo de nivel superior: es lo que permite
        # que ?docType=FACTURA encuentre la factura dentro del expediente.
        assert attrs["ContainedDocumentTypes"] == {
            "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
            "PEDIMENTO_ADUANAL",
            "FACTURA",
            "OTRO",
        }

    def test_document_name_is_the_file_basename(self):
        attrs = indexing.derive_index_attributes(EXPEDIENTE)
        assert attrs["DocumentName"] == "expediente-2026.pdf"

    def test_source_key_wins_over_expediente_source(self):
        attrs = indexing.derive_index_attributes(
            SINGLE_DOC, source_key="raw/INE/uuid/credencial.jpg"
        )
        assert attrs["DocumentName"] == "credencial.jpg"

    def test_missing_fields_fall_back_to_safe_defaults(self):
        attrs = indexing.derive_index_attributes({"documentId": "x"})
        assert attrs["DocumentType"] == "OTRO"
        assert attrs["ValidationStatus"] == "REQUIERE_REVISION"
        # ProcessedAt es la clave de ordenamiento del GSI: nunca puede faltar.
        assert attrs["ProcessedAt"]

    def test_classification_is_normalized(self):
        attrs = indexing.derive_index_attributes(
            {"documentId": "x", "documentClassification": "  factura  "}
        )
        assert attrs["DocumentType"] == "FACTURA"


class TestWriteIndexAttributes:
    def test_no_table_configured_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(indexing, "_DOCUMENT_TABLE", None)
        assert indexing.write_index_attributes(SINGLE_DOC) is None

    def test_unknown_document_id_is_skipped(self, monkeypatch):
        monkeypatch.setattr(indexing, "_DOCUMENT_TABLE", "some-table")
        assert indexing.write_index_attributes({"documentId": "unknown"}) is None

    def test_write_failure_never_raises(self, monkeypatch):
        # boto3 no existe/falla en local: el resultado de validación no debe
        # verse afectado por un error de indexado.
        monkeypatch.setattr(indexing, "_DOCUMENT_TABLE", "some-table")
        assert indexing.write_index_attributes(SINGLE_DOC) is None
