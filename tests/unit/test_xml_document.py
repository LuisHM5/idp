"""Tests for the deterministic XML extraction pipeline (pipeline/xml_document.py).

Uses synthetic or sanitized CFDI fixtures (fake RFCs/UUID/names) rather than
real documents — real CFDIs carry PII and shouldn't land in the repo (see
AGENTS.md: "keep any PII out of commits"). The fixtures under
fixtures/xml_cfdi preserve the structure, concepts and accounting amounts of
the supplied FAA/GT1/GT2/OLI dataset so every catalog mapping is reproducible.
"""

import json
import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from catalog_paths import active_catalog_path  # noqa: E402
import document_catalog  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline import xml_document  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures/xml_cfdi"

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


def _load_entry(type_id="FACTURA_HONORARIOS_XML"):
    catalog_file = active_catalog_path(REPO_ROOT, type_id)
    raw = json.loads(catalog_file.read_text(encoding="utf-8"))
    return _parse_entry(type_id, raw[type_id])


def _process(monkeypatch, xml_bytes: bytes, declared_type="FACTURA_HONORARIOS_XML"):
    cache_type = declared_type or "FACTURA_HONORARIOS_XML"
    entry = _load_entry(cache_type)
    monkeypatch.setattr(document_catalog, "_CATALOG_CACHE", {cache_type: entry})
    return xml_document.process_xml(xml_bytes, declared_type=declared_type, source_name="test.xml")


def _process_fixture(monkeypatch, fixture_name, type_id):
    fixture = FIXTURE_DIR / fixture_name
    return _process(monkeypatch, fixture.read_bytes(), declared_type=type_id)


def _assert_approved_mapping(result, type_id, business_destination):
    doc = result["documents"][0]
    hard_failures = [c for c in doc["checks"] if not c["valid"] and not c["skipped"]]
    assert result["status"] == "APPROVED"
    assert doc["documentType"] == type_id
    assert doc["businessDestination"] == business_destination
    assert hard_failures == []
    return doc["extractedEntities"]


def _values(entities, *field_names):
    return {name: entities[name]["value"] for name in field_names}


def _concept_values(entities):
    return [
        {name: field["value"] for name, field in item["fields"].items()}
        for item in entities["CONCEPTOS"]["items"]
    ]


def test_faa_fixture_maps_every_declared_business_field(monkeypatch):
    entities = _assert_approved_mapping(
        _process_fixture(monkeypatch, "faa.xml", "FACTURA_HONORARIOS_XML"),
        "FACTURA_HONORARIOS_XML",
        "FACTURA_HONORARIOS",
    )

    assert _values(
        entities,
        "VERSION_CFDI", "SERIE", "FOLIO", "UUID", "FECHA_EMISION",
        "FECHA_TIMBRADO", "CERTIFICADO_EMISOR", "TIPO_COMPROBANTE", "FORMA_PAGO", "METODO_PAGO",
        "MONEDA", "TIPO_CAMBIO", "LUGAR_EXPEDICION", "EMISOR_RFC",
        "EMISOR_NOMBRE", "EMISOR_REGIMEN_FISCAL", "RECEPTOR_RFC",
        "RECEPTOR_NOMBRE", "RECEPTOR_REGIMEN_FISCAL",
        "RECEPTOR_CODIGO_POSTAL", "USO_CFDI", "SUBTOTAL_CFDI",
        "IVA_TRASLADADO", "ISR_RETENIDO", "IVA_RETENIDO", "TOTAL_CFDI",
    ) == {
        "VERSION_CFDI": "4.0",
        "SERIE": "A",
        "FOLIO": "23268",
        "UUID": "00000000-0000-4000-8000-000000000101",
        "FECHA_EMISION": "2026-07-10",
        "FECHA_TIMBRADO": "2026-07-10",
        "CERTIFICADO_EMISOR": "00000000000000000001",
        "TIPO_COMPROBANTE": "I",
        "FORMA_PAGO": "99",
        "METODO_PAGO": "PPD",
        "MONEDA": "MXN",
        "TIPO_CAMBIO": 1.0,
        "LUGAR_EXPEDICION": "32310",
        "EMISOR_RFC": "EMIT800101AB1",
        "EMISOR_NOMBRE": "PERSONA EMISORA DE PRUEBA",
        "EMISOR_REGIMEN_FISCAL": "612",
        "RECEPTOR_RFC": "RECE850202CD2",
        "RECEPTOR_NOMBRE": "RECEPTOR DE PRUEBA",
        "RECEPTOR_REGIMEN_FISCAL": "623",
        "RECEPTOR_CODIGO_POSTAL": "31125",
        "USO_CFDI": "G03",
        "SUBTOTAL_CFDI": 335.0,
        "IVA_TRASLADADO": 53.6,
        "ISR_RETENIDO": 33.5,
        "IVA_RETENIDO": 35.73,
        "TOTAL_CFDI": 319.37,
    }
    assert _concept_values(entities) == [{
        "CANTIDAD": 1.0,
        "CLAVE_UNIDAD": "E48",
        "CLAVE_PRODUCTO_SERVICIO": "80151605",
        "DESCRIPCION": "HONORARIOS",
        "PRECIO_UNITARIO": 335.0,
        "IMPORTE": 335.0,
    }]


def test_gt1_fixture_maps_every_declared_business_field(monkeypatch):
    entities = _assert_approved_mapping(
        _process_fixture(monkeypatch, "gt1.xml", "CFDI_PAGO_DERECHOS_SANITARIOS_XML"),
        "CFDI_PAGO_DERECHOS_SANITARIOS_XML",
        "PERMISO_SALUD",
    )

    assert _values(
        entities,
        "VERSION_CFDI", "SERIE", "FOLIO", "UUID", "FECHA_EMISION",
        "FECHA_TIMBRADO", "TIPO_COMPROBANTE", "FORMA_PAGO", "METODO_PAGO",
        "MONEDA", "TIPO_CAMBIO", "LUGAR_EXPEDICION", "EMISOR_RFC",
        "EMISOR_NOMBRE", "EMISOR_REGIMEN_FISCAL", "RECEPTOR_RFC",
        "RECEPTOR_NOMBRE", "RECEPTOR_REGIMEN_FISCAL",
        "RECEPTOR_CODIGO_POSTAL", "USO_CFDI", "SUBTOTAL_CFDI",
        "DESCUENTO_CFDI", "IVA_TRASLADADO", "IMPUESTOS_RETENIDOS", "TOTAL_CFDI",
    ) == {
        "VERSION_CFDI": "4.0",
        "SERIE": "B",
        "FOLIO": "2458119",
        "UUID": "00000000-0000-4000-8000-000000000102",
        "FECHA_EMISION": "2026-07-14",
        "FECHA_TIMBRADO": "2026-07-14",
        "TIPO_COMPROBANTE": "I",
        "FORMA_PAGO": "03",
        "METODO_PAGO": "PUE",
        "MONEDA": "MXN",
        "TIPO_CAMBIO": 1.0,
        "LUGAR_EXPEDICION": "06100",
        "EMISOR_RFC": "EMIT800101AB1",
        "EMISOR_NOMBRE": "AUTORIDAD DE SANIDAD E INOCUIDAD DE PRUEBA",
        "EMISOR_REGIMEN_FISCAL": "603",
        "RECEPTOR_RFC": "RECE850202CD2",
        "RECEPTOR_NOMBRE": "RECEPTOR DE PRUEBA",
        "RECEPTOR_REGIMEN_FISCAL": "623",
        "RECEPTOR_CODIGO_POSTAL": "31125",
        "USO_CFDI": "G03",
        "SUBTOTAL_CFDI": 3356.0,
        "DESCUENTO_CFDI": None,
        "IVA_TRASLADADO": None,
        "IMPUESTOS_RETENIDOS": None,
        "TOTAL_CFDI": 3356.0,
    }
    assert _concept_values(entities) == [{
        "CLAVE_PRODUCTO_SERVICIO": "93161700",
        "NUMERO_IDENTIFICACION": None,
        "CANTIDAD": 1.0,
        "CLAVE_UNIDAD": "E48",
        "UNIDAD": "Servicio",
        "DESCRIPCION": "ARTÍCULO 86-A FRACC. V. POR CADA CERTIFICADO FITOSANITARIO INTERNACIONAL PARA LA IMPORTACION DE VEGETALES, SUS PRODUCTOS Y SUBPRODUCTOS",
        "VALOR_UNITARIO": 3355.55,
        "IMPORTE": 3356.0,
        "OBJETO_IMPUESTO": "01",
    }]


def test_gt2_fixture_maps_every_declared_business_field(monkeypatch):
    entities = _assert_approved_mapping(
        _process_fixture(monkeypatch, "gt2.xml", "CFDI_GASTO_TERCERO_XML"),
        "CFDI_GASTO_TERCERO_XML",
        "GASTOS_TERCEROS",
    )

    assert _values(
        entities,
        "VERSION_CFDI", "SERIE", "FOLIO", "UUID", "FECHA_EMISION", "FECHA_TIMBRADO",
        "TIPO_COMPROBANTE", "FORMA_PAGO", "METODO_PAGO", "MONEDA", "TIPO_CAMBIO",
        "LUGAR_EXPEDICION", "EMISOR_RFC", "EMISOR_NOMBRE",
        "EMISOR_REGIMEN_FISCAL", "RECEPTOR_RFC", "RECEPTOR_NOMBRE",
        "RECEPTOR_REGIMEN_FISCAL", "RECEPTOR_CODIGO_POSTAL", "USO_CFDI",
        "SUBTOTAL_CFDI", "DESCUENTO_CFDI", "IVA_TRASLADADO",
        "IMPUESTOS_RETENIDOS", "TOTAL_CFDI",
    ) == {
        "VERSION_CFDI": "4.0",
        "SERIE": "AXCR",
        "FOLIO": "24528",
        "UUID": "00000000-0000-4000-8000-000000000103",
        "FECHA_EMISION": "2026-07-09",
        "FECHA_TIMBRADO": "2026-07-09",
        "TIPO_COMPROBANTE": "I",
        "FORMA_PAGO": "03",
        "METODO_PAGO": "PUE",
        "MONEDA": "MXN",
        "TIPO_CAMBIO": None,
        "LUGAR_EXPEDICION": "32695",
        "EMISOR_RFC": "EMIT800101AB1",
        "EMISOR_NOMBRE": "PROVEEDOR OPERATIVO DE PRUEBA",
        "EMISOR_REGIMEN_FISCAL": "601",
        "RECEPTOR_RFC": "RECE850202CD2",
        "RECEPTOR_NOMBRE": "RECEPTOR DE PRUEBA",
        "RECEPTOR_REGIMEN_FISCAL": "623",
        "RECEPTOR_CODIGO_POSTAL": "31125",
        "USO_CFDI": "G03",
        "SUBTOTAL_CFDI": 6550.0,
        "DESCUENTO_CFDI": None,
        "IVA_TRASLADADO": 1048.0,
        "IMPUESTOS_RETENIDOS": None,
        "TOTAL_CFDI": 7598.0,
    }
    assert _concept_values(entities) == [
        {
            "CLAVE_PRODUCTO_SERVICIO": "83112600",
            "NUMERO_IDENTIFICACION": "1",
            "CANTIDAD": 1.0,
            "CLAVE_UNIDAD": "E48",
            "UNIDAD": "SERV",
            "DESCRIPCION": "DERECHO DE ACCESO POR VEHICULO",
            "VALOR_UNITARIO": 1400.0,
            "IMPORTE": 1400.0,
            "OBJETO_IMPUESTO": "02",
        },
        {
            "CLAVE_PRODUCTO_SERVICIO": "78121601",
            "NUMERO_IDENTIFICACION": "2",
            "CANTIDAD": 1.0,
            "CLAVE_UNIDAD": "E48",
            "UNIDAD": "SERV",
            "DESCRIPCION": "MANIOBRAS POR VEHICULO",
            "VALOR_UNITARIO": 3050.0,
            "IMPORTE": 3050.0,
            "OBJETO_IMPUESTO": "02",
        },
        {
            "CLAVE_PRODUCTO_SERVICIO": "81141601",
            "NUMERO_IDENTIFICACION": "23",
            "CANTIDAD": 1.0,
            "CLAVE_UNIDAD": "E48",
            "UNIDAD": "SERV",
            "DESCRIPCION": "MANIOBRAS DE MUESTREO X CARGA",
            "VALOR_UNITARIO": 2100.0,
            "IMPORTE": 2100.0,
            "OBJETO_IMPUESTO": "02",
        },
    ]


def test_oli_fixture_maps_every_declared_business_field(monkeypatch):
    entities = _assert_approved_mapping(
        _process_fixture(monkeypatch, "oli.xml", "FACTURA_LOGISTICA_XML"),
        "FACTURA_LOGISTICA_XML",
        "OPERACIONES_LOGISTICAS_INTERNACIONALES",
    )

    assert _values(
        entities,
        "VERSION_CFDI", "SERIE", "FOLIO", "UUID", "FECHA_EMISION", "FECHA_TIMBRADO",
        "TIPO_COMPROBANTE", "FORMA_PAGO", "METODO_PAGO", "MONEDA", "TIPO_CAMBIO",
        "LUGAR_EXPEDICION", "EMISOR_RFC", "EMISOR_NOMBRE",
        "EMISOR_REGIMEN_FISCAL", "RECEPTOR_RFC", "RECEPTOR_NOMBRE",
        "RECEPTOR_REGIMEN_FISCAL", "RECEPTOR_CODIGO_POSTAL", "USO_CFDI",
        "SUBTOTAL_CFDI", "DESCUENTO_CFDI", "IVA_TRASLADADO",
        "IMPUESTOS_RETENIDOS", "TOTAL_CFDI",
    ) == {
        "VERSION_CFDI": "4.0",
        "SERIE": "A",
        "FOLIO": "11184",
        "UUID": "00000000-0000-4000-8000-000000000104",
        "FECHA_EMISION": "2026-07-10",
        "FECHA_TIMBRADO": "2026-07-10",
        "TIPO_COMPROBANTE": "I",
        "FORMA_PAGO": "99",
        "METODO_PAGO": "PPD",
        "MONEDA": "MXN",
        "TIPO_CAMBIO": None,
        "LUGAR_EXPEDICION": "32300",
        "EMISOR_RFC": "EMIT800101AB1",
        "EMISOR_NOMBRE": "SERVICIOS DE COMERCIO EXTERIOR DE PRUEBA",
        "EMISOR_REGIMEN_FISCAL": "601",
        "RECEPTOR_RFC": "RECE850202CD2",
        "RECEPTOR_NOMBRE": "RECEPTOR DE PRUEBA",
        "RECEPTOR_REGIMEN_FISCAL": "623",
        "RECEPTOR_CODIGO_POSTAL": "31125",
        "USO_CFDI": "G03",
        "SUBTOTAL_CFDI": 6368.83,
        "DESCUENTO_CFDI": None,
        "IVA_TRASLADADO": 1019.01,
        "IMPUESTOS_RETENIDOS": None,
        "TOTAL_CFDI": 7387.84,
    }
    assert _concept_values(entities) == [{
        "CLAVE_PRODUCTO_SERVICIO": "81141601",
        "NUMERO_IDENTIFICACION": "SERV007",
        "CANTIDAD": 1.0,
        "CLAVE_UNIDAD": "E48",
        "UNIDAD": "SERVICIO",
        "DESCRIPCION": "SERVICIOS POR LOGISTICA",
        "VALOR_UNITARIO": 6368.83,
        "IMPORTE": 6368.83,
        "OBJETO_IMPUESTO": "02",
    }]


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
