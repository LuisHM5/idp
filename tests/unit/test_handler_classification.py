"""User-declared classification is honored from the S3 key layout (no AWS)."""

import document_catalog as dc
import handler


def setup_module(module):
    dc.reload_catalog()


class TestDeclaredTypeFromKey:
    def test_manual_classification_prefix_is_honored(self):
        key = "raw/PEDIMENTO_ADUANAL/4ff224be-6fbe-474f/pendimiento-scan.pdf"
        assert handler._declared_type_from_key(key) == "PEDIMENTO_ADUANAL"

    def test_auto_classified_uuid_prefix_returns_none(self):
        # raw/{uuid}/file → no type segment → segmenter decides
        key = "raw/4ff224be-6fbe-474f-8216-73d8399e50e7/pendimiento-scan.pdf"
        assert handler._declared_type_from_key(key) is None

    def test_unknown_type_segment_returns_none(self):
        assert handler._declared_type_from_key("raw/NOT_A_REAL_TYPE/uuid/x.pdf") is None

    def test_empty_or_malformed_key_returns_none(self):
        assert handler._declared_type_from_key("") is None
        assert handler._declared_type_from_key("results/x.json") is None
