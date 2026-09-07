"""Unique keys: a type declares in its schema the field that identifies ONE
document of that type, and every value found is indexed as `TIPODOC#valor`
so searching by it is a single Query instead of a walk over the whole type.

Covers the three things that can silently break the feature:
  - which fields count as unique keys (catalog parsing, both DSLs),
  - what the key looks like (normalization — the writer here and the reader
    in query-lambda/index.mjs must agree character for character),
  - which rows a result produces (single documents and expedientes alike).
"""

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

import document_catalog  # noqa: E402
import unique_keys  # noqa: E402


# --------------------------------------------------------------- parsing


def test_field_level_declaration_becomes_a_spec():
    entry = document_catalog._parse_entry(
        "TIPO_X",
        {
            "fields": {
                "FOLIO": {"type": "string", "unique_key": True},
                "OTRO": {"type": "string"},
            }
        },
    )
    assert entry.unique_keys == [{"field": "FOLIO", "normalize": "upper"}]


def test_field_level_declaration_accepts_a_normalizer():
    entry = document_catalog._parse_entry(
        "TIPO_X",
        {"fields": {"UUID": {"type": "string", "unique_key": {"normalize": "alnum"}}}},
    )
    assert entry.unique_keys == [{"field": "UUID", "normalize": "alnum"}]


def test_unknown_normalizer_falls_back_to_the_default():
    """A typo must degrade to a usable key, not to keys nothing can look up."""
    entry = document_catalog._parse_entry(
        "TIPO_X",
        {"fields": {"UUID": {"type": "string", "unique_key": {"normalize": "nope"}}}},
    )
    assert entry.unique_keys == [{"field": "UUID", "normalize": "upper"}]


def test_unique_key_false_opts_out():
    entry = document_catalog._parse_entry(
        "TIPO_X", {"fields": {"FOLIO": {"type": "string", "unique_key": False}}}
    )
    assert entry.unique_keys == []


def test_type_level_declaration_works_for_the_legacy_dsl():
    """Legacy-DSL types have no per-field dict to hang the flag on, so the
    type-level form is their only way in."""
    entry = document_catalog._parse_entry(
        "TIPO_LEGACY",
        {
            "extraction_fields": ["FOLIO", "RFC"],
            "required_fields": ["FOLIO"],
            "validation_rules": [],
            "unique_key": ["FOLIO", {"field": "RFC", "normalize": "alnum"}],
        },
    )
    assert entry.unique_keys == [
        {"field": "FOLIO", "normalize": "upper"},
        {"field": "RFC", "normalize": "alnum"},
    ]


def test_declaring_in_both_places_is_deduplicated():
    entry = document_catalog._parse_entry(
        "TIPO_X",
        {
            "unique_key": {"field": "FOLIO", "normalize": "digits"},
            "fields": {"FOLIO": {"type": "string", "unique_key": True}},
        },
    )
    assert entry.unique_keys == [{"field": "FOLIO", "normalize": "digits"}]


def test_a_type_without_the_key_declares_nothing():
    entry = document_catalog._parse_entry("TIPO_X", {"fields": {"FOLIO": {"type": "string"}}})
    assert entry.unique_keys == []


def test_live_catalog_declares_the_pedimento_number():
    """The one shipped type that uses this — guards against the declaration
    being dropped from catalogs/base/aduanas/PEDIMENTO_ADUANAL.json by accident."""
    specs = unique_keys.unique_key_specs("PEDIMENTO_ADUANAL")
    assert {s["field"] for s in specs} == {
        "NUMERO_PEDIMENTO",
        "NUMERO_PEDIMENTO_NORMALIZADO",
    }
    assert {s["normalize"] for s in specs} == {"digits"}


# ---------------------------------------------------------- normalization


def test_normalizers():
    assert unique_keys.normalize_value("  ab   cd ") == "AB CD"
    assert unique_keys.normalize_value(" a1b2-c3 ", "alnum") == "A1B2C3"
    assert unique_keys.normalize_value("26 07 0823 6001630", "digits") == "260708236001630"
    assert unique_keys.normalize_value("  Ab  Cd ", "raw") == "Ab Cd"
    assert unique_keys.normalize_value(None) == ""
    assert unique_keys.normalize_value("   ") == ""


def test_key_is_always_type_then_value():
    assert (
        unique_keys.build_key("PEDIMENTO_ADUANAL", "26 07 0823 6001630", "digits")
        == "PEDIMENTO_ADUANAL#260708236001630"
    )
    # The prefix keeps two types that share a number space apart.
    assert unique_keys.build_key("TIPO_A", "1", "upper") != unique_keys.build_key(
        "TIPO_B", "1", "upper"
    )


def test_no_key_without_a_value_or_a_type():
    assert unique_keys.build_key("TIPO_X", "") == ""
    assert unique_keys.build_key("TIPO_X", None) == ""
    assert unique_keys.build_key("", "algo") == ""


def test_python_and_javascript_normalizers_are_the_same_rules():
    """`query-lambda/index.mjs` reads the keys this module writes; if the two
    normalizers drift, every lookup misses (silently — it just falls back to
    the slow path). Compare the regexes/branches rather than the prose."""
    def body(path, marker):
        """The function's body — the signature is skipped because the two
        copies spell the same default differently (a literal vs. the exported
        constant); what has to match is what runs."""
        source = (REPO / path).read_text()
        return re.sub(r"\s+", " ", source.split(marker, 1)[1].split("{", 1)[1].split("\n}", 1)[0])

    lambda_body = body(
        "src/document-processing/resources/query-lambda/index.mjs",
        "function normalizeKeyValue(",
    )
    assert 'if (mode === "raw") return text;' in lambda_body
    assert 'replace(/[^A-Z0-9]/g, "")' in lambda_body
    assert 'replace(/[^0-9]/g, "")' in lambda_body
    assert 'replace(/\\s+/g, " ")' in lambda_body
    # ...and the shared scripts/ copy (sync + backfill) agrees with it too.
    assert body("scripts/_unique_keys.mjs", "export function normalizeKeyValue(") == lambda_body


# ------------------------------------------------------------- derivation


def _v2_result():
    """Envelope v2: `documents[]` en la raíz, campos en forma de nodo."""
    return {
        "documentId": "doc-1",
        "envelopeVersion": 2,
        "documentClassification": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
        "processedAt": "2026-08-31T00:00:00Z",
        "validation": {"status": "APROBADO"},
        "sourceFile": "processed/doc-1/expediente.pdf",
        "documents": [
            {
                "index": 1,
                "documentType": "PEDIMENTO_ADUANAL",
                "fields": {
                    "NUMERO_PEDIMENTO": {"value": "26 07 0823 6001630", "valid": True},
                    "NUMERO_PEDIMENTO_NORMALIZADO": {"value": "260708236001630", "valid": True},
                    # Un arreglo no es una unique key: se ignora sin estorbar.
                    "PARTIDAS": {"items": [{"fields": {"DESC": {"value": "algo"}}}]},
                },
            },
            {"index": 2, "documentType": "OTRO", "fields": {}},
        ],
    }


def _expediente_result():
    """Envelope v1 tal como quedó GUARDADO en DynamoDB antes de v2. Se sigue
    leyendo: `pnpm backfill:keys` recorre resultados de cualquier edad."""
    return {
        "documentId": "doc-1",
        "documentClassification": "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
        "processedAt": "2026-08-31T00:00:00Z",
        "validation": {"status": "APROBADO"},
        "expediente": {
            "sourceFile": "processed/doc-1/expediente.pdf",
            "documents": [
                {
                    "documentType": "PEDIMENTO_ADUANAL",
                    "extractedEntities": {
                        "NUMERO_PEDIMENTO": "26 07 0823 6001630",
                        "NUMERO_PEDIMENTO_NORMALIZADO": "260708236001630",
                    },
                },
                {"documentType": "OTRO", "extractedEntities": {}},
            ],
        },
    }


@pytest.mark.parametrize("result", [_v2_result(), _expediente_result()], ids=["v2", "v1-guardado"])
def test_a_document_inside_an_expediente_is_keyed_by_its_own_type(result):
    """Not by the container's: the searchable identity is the pedimento's,
    and the container type is only recorded as provenance."""
    rows = unique_keys.derive_unique_keys(result)
    assert len(rows) == 1
    row = rows[0]
    assert row["UniqueKey"] == "PEDIMENTO_ADUANAL#260708236001630"
    assert row["DocumentId"] == "doc-1"
    assert row["DocumentType"] == "PEDIMENTO_ADUANAL"
    assert row["ContainerType"] == "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"
    assert row["DocumentName"] == "expediente.pdf"
    assert row["ProcessedAt"] == "2026-08-31T00:00:00Z"
    assert row["ValidationStatus"] == "APROBADO"


def test_una_clasificacion_mixta_se_reporta_como_contenedor_en_ContainerType():
    """`documentClassification` de un expediente mixto trae los tipos separados
    por coma; `ContainerType` no.

    Esa columna responde "¿venía suelto o dentro de un paquete?", así que repetir
    ahí la lista de tipos no agrega nada y rompe la comparación con las filas ya
    escritas antes de que la clasificación pasara a ser una lista.
    """
    result = _v2_result()
    result["documentClassification"] = "PEDIMENTO_ADUANAL,FACTURA_FLETE_EXTRANJERO"
    rows = unique_keys.derive_unique_keys(result)
    assert len(rows) == 1
    assert rows[0]["DocumentType"] == "PEDIMENTO_ADUANAL"
    assert rows[0]["ContainerType"] == "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"


def test_two_fields_normalizing_to_the_same_key_write_one_row():
    """The printed number ("26 07 0823 6001630") and the normalized one are
    the same identifier — one row, attributed to the first declaration."""
    rows = unique_keys.derive_unique_keys(_expediente_result())
    assert [r["FieldName"] for r in rows] == ["NUMERO_PEDIMENTO"]
    assert rows[0]["Value"] == "26 07 0823 6001630"
    assert rows[0]["NormalizedValue"] == "260708236001630"


def test_several_documents_of_the_same_type_each_get_their_own_key():
    result = _expediente_result()
    result["expediente"]["documents"].append(
        {
            "documentType": "PEDIMENTO_ADUANAL",
            "extractedEntities": {"NUMERO_PEDIMENTO_NORMALIZADO": "260708236001999"},
        }
    )
    keys = [r["UniqueKey"] for r in unique_keys.derive_unique_keys(result)]
    assert keys == [
        "PEDIMENTO_ADUANAL#260708236001630",
        "PEDIMENTO_ADUANAL#260708236001999",
    ]


def test_single_document_result_reads_the_top_level_field_tree():
    """No `expediente` block: the values live at the envelope's top level, in
    the {value, confidence, ...} shape (handler._entities_for_envelope)."""
    rows = unique_keys.derive_unique_keys(
        {
            "documentId": "doc-2",
            "documentClassification": "PEDIMENTO_ADUANAL",
            "processedAt": "2026-08-31T00:00:00Z",
            "validation": {"status": "APROBADO"},
            "extractedEntities": {
                "NUMERO_PEDIMENTO_NORMALIZADO": {"value": "260708236001631", "valid": True}
            },
        }
    )
    assert [r["UniqueKey"] for r in rows] == ["PEDIMENTO_ADUANAL#260708236001631"]
    assert "ContainerType" not in rows[0]


def test_a_type_without_unique_keys_produces_nothing():
    rows = unique_keys.derive_unique_keys(
        {
            "documentId": "doc-3",
            "documentClassification": "INE",
            "extractedEntities": {"CURP": "XXXX000000HDFXXX00"},
        }
    )
    assert rows == []


def test_a_missing_value_produces_nothing():
    result = _expediente_result()
    result["expediente"]["documents"][0]["extractedEntities"] = {
        "NUMERO_PEDIMENTO": None,
        "NUMERO_PEDIMENTO_NORMALIZADO": "",
    }
    assert unique_keys.derive_unique_keys(result) == []


def test_an_unidentified_document_produces_nothing():
    assert unique_keys.derive_unique_keys({"documentId": "unknown"}) == []
    assert unique_keys.derive_unique_keys({}) == []


def test_the_cap_stops_writing_instead_of_failing(monkeypatch):
    """Past the cap the remaining values just keep the slow search path."""
    monkeypatch.setattr(unique_keys, "_MAX_KEYS_PER_DOCUMENT", 2)
    result = _expediente_result()
    for n in range(5):
        result["expediente"]["documents"].append(
            {
                "documentType": "PEDIMENTO_ADUANAL",
                "extractedEntities": {"NUMERO_PEDIMENTO_NORMALIZADO": f"26070823600{n:04d}"},
            }
        )
    assert len(unique_keys.derive_unique_keys(result)) == 2


def test_write_is_a_no_op_without_a_table(monkeypatch):
    monkeypatch.setattr(unique_keys, "_UNIQUE_KEY_TABLE", None)
    assert unique_keys.write_unique_keys(_expediente_result()) == []


def test_v2_varias_facturas_del_mismo_tipo_no_reportan_contenedor():
    """Un archivo con dos facturas del mismo tipo se clasifica con ESE tipo
    (ver pipeline/envelope.py::classification_of), así que no hay contenedor
    del cual venir: cada factura escribe su propia fila."""
    rows = unique_keys.derive_unique_keys(
        {
            "documentId": "doc-9",
            "envelopeVersion": 2,
            "documentClassification": "PEDIMENTO_ADUANAL",
            "validation": {"status": "APROBADO"},
            "documents": [
                {"index": 1, "documentType": "PEDIMENTO_ADUANAL",
                 "fields": {"NUMERO_PEDIMENTO_NORMALIZADO": {"value": "260708236001630"}}},
                {"index": 2, "documentType": "PEDIMENTO_ADUANAL",
                 "fields": {"NUMERO_PEDIMENTO_NORMALIZADO": {"value": "260708236001631"}}},
            ],
        }
    )
    assert [r["UniqueKey"] for r in rows] == [
        "PEDIMENTO_ADUANAL#260708236001630",
        "PEDIMENTO_ADUANAL#260708236001631",
    ]
    assert all("ContainerType" not in r for r in rows)
