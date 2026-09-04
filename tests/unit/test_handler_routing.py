"""Tests for handler.py's file-source routing helpers.

`_declared_file_type` reads the explicit `content.fileType` the SQS consumer
now sets (patches/default-sqs-consumer-index.py) — the single source of
truth for which pipeline (xml/tabular/pdf) a document routes to, preferred
over re-parsing the S3 key's extension independently in every consumer.
"""

import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import handler  # noqa: E402


def test_declared_file_type_reads_content_filetype():
    event = {"content": {"bucket": "b", "key": "raw/x/file.pdf", "fileType": "pdf"}}
    assert handler._declared_file_type(event) == "pdf"


def test_declared_file_type_normalizes_case_and_whitespace():
    event = {"content": {"fileType": " XML "}}
    assert handler._declared_file_type(event) == "xml"


def test_declared_file_type_absent_returns_none():
    assert handler._declared_file_type({"content": {"bucket": "b", "key": "k"}}) is None
    assert handler._declared_file_type({}) is None
    assert handler._declared_file_type({"content": "not-a-dict"}) is None


def test_declared_file_type_is_preferred_over_key_extension_fallback():
    # The routing line in _run_expediente is `_declared_file_type(event) or
    # (key extension fallback)` — content.fileType wins whenever present,
    # even if it disagrees with the key (e.g. a future ingestion path that
    # renames/normalizes the extension differently from the declared type).
    event = {"content": {"key": "raw/x/report.dat", "fileType": "xlsx"}}
    key = event["content"]["key"]
    ext = handler._declared_file_type(event) or (key.rsplit(".", 1)[-1].lower() if "." in key else "")
    assert ext == "xlsx"


def test_key_extension_fallback_when_filetype_absent():
    event = {"content": {"key": "raw/x/report.csv"}}
    key = event["content"]["key"]
    ext = handler._declared_file_type(event) or (key.rsplit(".", 1)[-1].lower() if "." in key else "")
    assert ext == "csv"


# --- tipos declarados en la llave de S3 --------------------------------------
#
# `_declared_types_from_key` es el único lector del tipo declarado en el
# enrichment: de él salen tanto `declared_type` (un solo tipo, honrado tal cual)
# como `declared_types` (varios, clasificación guiada).


def test_declared_types_reads_one_type():
    assert handler._declared_types_from_key(
        "raw/PEDIMENTO_ADUANAL/uuid-1/file.pdf"
    ) == ["PEDIMENTO_ADUANAL"]


def test_declared_types_reads_a_comma_separated_list():
    assert handler._declared_types_from_key(
        "raw/PEDIMENTO_ADUANAL,FACTURA_HONORARIOS/uuid-1/file.pdf"
    ) == ["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"]


def test_declared_types_normalizes_spaces_case_and_duplicates():
    assert handler._declared_types_from_key(
        "raw/ pedimento_aduanal , PEDIMENTO_ADUANAL /uuid-1/file.pdf"
    ) == ["PEDIMENTO_ADUANAL"]


def test_declared_types_drops_types_the_catalog_does_not_know():
    """Un tipo inexistente se descarta en vez de tumbar la lista entera.

    Es lo que hace que la llave sea un carrier seguro durante un deploy
    parcial: lo que no se reconoce simplemente no se declara.
    """
    assert handler._declared_types_from_key(
        "raw/PEDIMENTO_ADUANAL,NO_EXISTE/uuid-1/file.pdf"
    ) == ["PEDIMENTO_ADUANAL"]


def test_declared_types_is_empty_for_an_auto_classified_upload():
    # raw/{uuid}/file — sin segmento de tipo. El uuid resuelve a OTRO.
    assert handler._declared_types_from_key(
        "raw/9f8e7d6c-1234-4321-abcd-000000000000/file.pdf"
    ) == []
    assert handler._declared_types_from_key("raw/file.pdf") == []
    assert handler._declared_types_from_key("") == []


# --- cuántos documentos de cada tipo buscar -----------------------------------
#
# Las CUENTAS viajan como metadata del objeto S3, no en la llave: la llave la
# parsean tres componentes independientes y ninguno necesita este dato.


def test_declared_counts_reads_the_metadata_written_by_the_upload():
    meta = {"document-counts": "PEDIMENTO_ERP=1,FACTURA_FLETE_EXTRANJERO=all"}
    assert handler._declared_counts_from_metadata(meta) == {
        "PEDIMENTO_ERP": 1,
        "FACTURA_FLETE_EXTRANJERO": "all",
    }


def test_declared_counts_is_empty_without_the_key():
    """Un objeto subido ANTES de que esto existiera se comporta igual que
    siempre: sin límite declarado."""
    assert handler._declared_counts_from_metadata({}) == {}
    assert handler._declared_counts_from_metadata({"model-id": "x"}) == {}
    assert handler._declared_counts_from_metadata(None) == {}


def test_declared_counts_degrades_a_bad_value_instead_of_failing_the_document():
    """El límite es una pista de costo, no un dato del documento: un valor
    ilegible deja ESE tipo sin límite en vez de tumbar el procesamiento."""
    meta = {"document-counts": "PEDIMENTO_ERP=dos,FACTURA_HONORARIOS=2,=3,SIN_VALOR="}
    assert handler._declared_counts_from_metadata(meta) == {"FACTURA_HONORARIOS": 2}
