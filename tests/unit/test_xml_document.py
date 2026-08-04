"""Tests for the deterministic XML extraction pipeline (pipeline/xml_document.py).

Uses a synthetic CFDI fixture (fake RFCs/UUID/names) rather than a real
document — real CFDIs carry PII and shouldn't land in the repo (see AGENTS.md:
"keep any PII out of commits"). The synthetic fixture mirrors the real SAT
CFDI 4.0 shape closely enough to exercise the same xpath/namespace/predicate
paths declared in catalogs/FACTURA_HONORARIOS_XML.json.
"""

import json
import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

import document_catalog  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline import xml_document  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_FILE = REPO_ROOT / "catalogs" / "FACTURA_HONORARIOS_XML.json"

_SYNTHETIC_CFDI = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    Version="4.0" Serie="T" Folio="1" Fecha="2026-01-15T11:59:00"
    FormaPago="99" NoCertificado="00000000000000000000"
    SubTotal="100.00" Moneda="MXN" TipoCambio="1" Total="95.33"
    TipoDeComprobante="I" Exportacion="01" MetodoPago="PPD" LugarExpedicion="01000">
  <cfdi:Emisor Rfc="EMIT800101AB1" Nombre="EMISOR DE PRUEBA SA DE CV" RegimenFiscal="601"/>
  <cfdi:Receptor Rfc="RECE850202CD2" Nombre="RECEPTOR DE PRUEBA SA DE CV"
      DomicilioFiscalReceptor="01000" RegimenFiscalReceptor="601" UsoCFDI="G03"/>
  <cfdi:Conceptos>
    <cfdi:Concepto ClaveProdServ="80151605" Cantidad="1.00" ClaveUnidad="E48"
        Unidad="SERVICIO" Descripcion="HONORARIOS DE PRUEBA" ValorUnitario="100.0000"
        Importe="100.00" ObjetoImp="02"/>
  </cfdi:Conceptos>
  <cfdi:Impuestos TotalImpuestosRetenidos="20.67" TotalImpuestosTrasladados="16.00">
    <cfdi:Retenciones>
      <cfdi:Retencion Impuesto="002" Importe="10.67"/>
      <cfdi:Retencion Impuesto="001" Importe="10.00"/>
    </cfdi:Retenciones>
    <cfdi:Traslados>
      <cfdi:Traslado Base="100.00" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="16.00"/>
    </cfdi:Traslados>
  </cfdi:Impuestos>
  <cfdi:Complemento>
    <tfd:TimbreFiscalDigital xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
        Version="1.1" UUID="00000000-0000-4000-8000-000000000001"
        FechaTimbrado="2026-01-15T12:00:00" RfcProvCertif="PAC800101XX1"/>
  </cfdi:Complemento>
</cfdi:Comprobante>"""


def _load_entry():
    raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    return _parse_entry("FACTURA_HONORARIOS_XML", raw["FACTURA_HONORARIOS_XML"])


def _process(monkeypatch, xml_bytes: bytes, declared_type="FACTURA_HONORARIOS_XML"):
    entry = _load_entry()
    monkeypatch.setattr(document_catalog, "_CATALOG_CACHE", {declared_type: entry})
    return xml_document.process_xml(xml_bytes, declared_type=declared_type, source_name="test.xml")


def test_extracts_and_validates_a_well_formed_cfdi(monkeypatch):
    result = _process(monkeypatch, _SYNTHETIC_CFDI.encode("utf-8"))
    doc = result["documents"][0]

    assert result["status"] == "APPROVED"
    assert doc["status"] == "APPROVED"
    hard_failures = [c for c in doc["checks"] if not c["valid"] and not c["skipped"]]
    assert hard_failures == []

    entities = doc["extractedEntities"]
    assert entities["EMISOR_RFC"]["value"] == "EMIT800101AB1"
    assert entities["RECEPTOR_RFC"]["value"] == "RECE850202CD2"
    assert entities["TOTAL_CFDI"]["value"] == 95.33
    assert entities["SUBTOTAL_CFDI"]["value"] == 100.0
    assert len(entities["CONCEPTOS"]["items"]) == 1
    concepto = entities["CONCEPTOS"]["items"][0]["fields"]
    assert concepto["DESCRIPCION"]["value"] == "HONORARIOS DE PRUEBA"
    assert concepto["IMPORTE"]["value"] == 100.0


def test_tags_values_as_xml_extraction_not_model(monkeypatch):
    """The engine's default_source override must apply: a value pulled from
    XML should never read as if a model guessed it (see validators/engine.py
    resolve_field_tree's `default_source` param)."""
    result = _process(monkeypatch, _SYNTHETIC_CFDI.encode("utf-8"))
    entities = result["documents"][0]["extractedEntities"]
    assert entities["EMISOR_RFC"]["source"] == "XML_EXTRACTION"
    assert entities["CONCEPTOS"]["items"][0]["fields"]["DESCRIPCION"]["source"] == "XML_EXTRACTION"
    # Confidence is the deterministic-parse ceiling, not an LLM guess.
    assert entities["EMISOR_RFC"]["confidence"] == 1.0


def test_predicate_xpath_disambiguates_isr_and_iva_retenciones(monkeypatch):
    """cfdi:Retencion[@Impuesto='001'] (ISR) vs. [@Impuesto='002'] (IVA) must
    each resolve to their own row, not the first Retencion in document order
    (the fixture deliberately lists IVA before ISR to catch that bug)."""
    result = _process(monkeypatch, _SYNTHETIC_CFDI.encode("utf-8"))
    entities = result["documents"][0]["extractedEntities"]
    assert entities["ISR_RETENIDO"]["value"] == 10.0
    assert entities["IVA_RETENIDO"]["value"] == 10.67


def test_requires_a_declared_type(monkeypatch):
    result = _process(monkeypatch, _SYNTHETIC_CFDI.encode("utf-8"), declared_type=None)
    assert result["status"] == "PROCESSING_ERROR" or result["documents"] == []
    assert result["warnings"]
    assert "declared type" in result["warnings"][0]


def test_invalid_xml_fails_gracefully(monkeypatch):
    result = _process(monkeypatch, b"<not><valid</xml>")
    assert result["documents"] == []
    assert result["warnings"]
