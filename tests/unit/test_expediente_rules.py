"""Offline tests for segment sanitization and cross-document expediente rules."""

from pipeline.expediente import _run_cross_document_rules
from pipeline.segmenter import Segment, _sanitize


class TestSegmentSanitize:
    def test_gaps_filled_with_otro(self):
        segments = [Segment("PEDIMENTO_ADUANAL", 3, 7, 0.9)]
        result = _sanitize(segments, 10)
        assert [(s.doc_type, s.page_start, s.page_end) for s in result] == [
            ("OTRO", 1, 2),
            ("PEDIMENTO_ADUANAL", 3, 7),
            ("OTRO", 8, 10),
        ]

    def test_overlaps_trimmed(self):
        segments = [
            Segment("FACTURA_AGENCIA_ADUANAL", 1, 3, 0.9),
            Segment("PEDIMENTO_ADUANAL", 3, 7, 0.9),
        ]
        result = _sanitize(segments, 7)
        assert result[0].page_end == 3
        assert result[1].page_start == 4

    def test_out_of_range_clamped(self):
        segments = [Segment("PEDIMENTO_ADUANAL", 0, 99, 0.9)]
        result = _sanitize(segments, 5)
        assert result[0].page_start == 1 and result[0].page_end == 5


def _doc(doc_type, entities):
    return {"documentType": doc_type, "extractedEntities": entities}


class TestCrossDocumentRules:
    def test_same_pedimento_pass(self):
        docs = [
            _doc("PEDIMENTO_ADUANAL", {"NUMERO_PEDIMENTO_NORMALIZADO": "260708236001187"}),
            _doc("CFDI_GASTO_TERCERO", {"PEDIMENTO_REFERENCE": "P0823 6001187"}),
            _doc("FACTURA_LOGISTICA", {"PEDIMENTO_REFERENCE": "6001187"}),
        ]
        checks = {c["rule"]: c for c in _run_cross_document_rules(docs, {})}
        assert checks["same_pedimento"]["valid"], checks["same_pedimento"]

    def test_same_pedimento_fail(self):
        docs = [
            _doc("PEDIMENTO_ADUANAL", {"NUMERO_PEDIMENTO_NORMALIZADO": "260708236001187"}),
            _doc("CFDI_GASTO_TERCERO", {"PEDIMENTO_REFERENCE": "6041248"}),
        ]
        checks = {c["rule"]: c for c in _run_cross_document_rules(docs, {})}
        assert not checks["same_pedimento"]["valid"]

    def test_importer_rfc_against_app_config(self):
        docs = [
            _doc("PEDIMENTO_ADUANAL", {"RFC_IMPORTADOR_EXPORTADOR": "OFU910626UQ0"}),
            _doc("CFDI_GASTO_TERCERO", {"RECEPTOR_RFC": "OFU910626UQ0"}),
        ]
        ok = {c["rule"]: c for c in _run_cross_document_rules(docs, {"IMPORTER_RFC": "OFU910626UQ0"})}
        assert ok["expected_importer_rfc"]["valid"]
        bad = {c["rule"]: c for c in _run_cross_document_rules(docs, {"IMPORTER_RFC": "XAXX010101000"})}
        assert not bad["expected_importer_rfc"]["valid"]

    def test_duplicate_uuid_detected(self):
        uuid = "6B2AF82F-7E4F-4606-90CA-E7F44BF2F011"
        docs = [
            _doc("CFDI_GASTO_TERCERO", {"UUID": uuid}),
            _doc("FACTURA_LOGISTICA", {"UUID": uuid}),
        ]
        checks = {c["rule"]: c for c in _run_cross_document_rules(docs, {})}
        assert not checks["unique_cfdi_uuid"]["valid"]

    def test_missing_documents_skip_gracefully(self):
        checks = _run_cross_document_rules([], {})
        assert all(c["valid"] or c["skipped"] for c in checks)
