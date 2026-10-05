"""CONSTANCIA_SITUACION_FISCAL_PORTAL: una constancia fiscal de cualquier país.

El contrato que este test fija:

- entrega TODOS los campos de CONSTANCIA_SITUACION_FISCAL con los mismos
  nombres, más `PAIS` y la identificación fiscal neutral (`ID_FISCAL`,
  `TIPO_ID_FISCAL`, `OTROS_IDENTIFICADORES`): el portal consume un solo contrato;
- quién es el contribuyente se lee SIEMPRE de `ID_FISCAL`: en una constancia
  mexicana es una copia del RFC, en una extranjera es su propio identificador,
  y `RFC` nunca carga un identificador que no sea un RFC;
- una constancia mexicana se juzga con las reglas del formato del SAT (patrón
  del RFC, QR válido, estatus en el padrón, CP de 5 dígitos, actividad y
  régimen), igual que el tipo base;
- una extranjera (W-9, certificado censal español, ...) NO se juzga con esas
  reglas: se aprueba con su `ID_FISCAL`, nombre, domicilio, fecha y
  país, y lo que su formato no trae queda vacío sin contar como falla;
- un QR del SAT válido decide `PAIS = MEX` sin depender del modelo.

Las entidades de abajo son lo que la extracción entrega para cada documento
(los datos son inventados, con la forma de los ejemplos reales).
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from catalog_paths import active_catalog_path  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline.derived import SOURCE_DERIVED, apply_derived_fields  # noqa: E402
from validators.engine import (  # noqa: E402
    DOC_APPROVED,
    ValidationContext,
    document_status_from_checks,
    resolve_field_tree,
)

PORTAL = "CONSTANCIA_SITUACION_FISCAL_PORTAL"
BASE = "CONSTANCIA_SITUACION_FISCAL"
HOY = date(2026, 10, 5)


def crudo(type_id):
    path = active_catalog_path(REPO_ROOT, type_id)
    return json.loads(path.read_text(encoding="utf-8"))[type_id]


def cargar(type_id=PORTAL):
    return _parse_entry(type_id, crudo(type_id))


def mexicana(**cambios):
    entidades = {
        "PAIS": "MEX",
        "RFC": "XAXX010101AB1",
        "CURP": "XAXX010101MCLSRT00",
        "NOMBRE_O_RAZON_SOCIAL": "MARIA EJEMPLO PRUEBA",
        "ID_CIF": "19060000000",
        "QR_CONSTANCIA": "https://siat.sat.gob.mx/app/qr/faces/pages/mobile/validadorqr.jsf?D1=10&D2=1&D3=19060000000_XAXX010101AB1",
        "CONSTANCIA_QR_VALIDO": True,
        "LUGAR_EMISION": "SALTILLO, COAHUILA DE ZARAGOZA",
        "FECHA_EMISION": "2026-01-12",
        "ESTATUS_EN_PADRON": "ACTIVO",
        "DOMICILIO_FISCAL": {"CODIGO_POSTAL": "25280", "NOMBRE_VIALIDAD": "VERACRUZ", "NUMERO_EXTERIOR": "355"},
        "ACTIVIDADES_ECONOMICAS": [{"ACTIVIDAD_ECONOMICA": "Comercio al por menor", "PORCENTAJE": 100}],
        "REGIMENES_FISCALES": [{"REGIMEN_FISCAL": "Régimen Simplificado de Confianza"}],
    }
    entidades.update(cambios)
    return entidades


def w9(**cambios):
    """Un W-9 del IRS: sin QR del SAT, sin estatus, sin actividades, y sin
    régimen — su clasificación fiscal es una casilla, que el texto no revela."""
    entidades = {
        "PAIS": "USA",
        "ID_FISCAL": "12-3456789",
        "TIPO_ID_FISCAL": "EIN",
        "NOMBRE_O_RAZON_SOCIAL": "Ejemplo International Inc.",
        "CONSTANCIA_QR_VALIDO": False,
        "FECHA_EMISION": "2025-08-22",
        "DOMICILIO_FISCAL": {
            "CODIGO_POSTAL": "93312", "NOMBRE_VIALIDAD": "Hageman Rd.", "NUMERO_EXTERIOR": "9530",
            "NUMERO_INTERIOR": "Suite B-152", "MUNICIPIO_O_DEMARCACION_TERRITORIAL": "Bakersfield",
            "ENTIDAD_FEDERATIVA": "CA",
        },
        "ACTIVIDADES_ECONOMICAS": [],
        "REGIMENES_FISCALES": [],
    }
    entidades.update(cambios)
    return entidades


def procesar(entidades):
    """Lo que hace el pipeline después de extraer: derivados y luego reglas."""
    entry = cargar()
    entidades = json.loads(json.dumps(entidades))
    confianzas = {"CONSTANCIA_QR_VALIDO": 0.99} if "CONSTANCIA_QR_VALIDO" in entidades else {}
    if entidades.get("CONSTANCIA_QR_VALIDO"):
        confianzas["RFC"] = 0.99  # el RFC de una constancia con QR válido sale del QR
    derivados = apply_derived_fields(entidades, confianzas, entry)
    arbol, checks, _ = resolve_field_tree(
        entidades, confianzas, entry, ValidationContext(today=HOY),
        source_overrides=derivados.sources, default_confidence=0.95,
    )
    return arbol, checks


def fallas(checks):
    return sorted(c.field for c in checks if not c.valid and not c.skipped)


# --- el contrato -----------------------------------------------------------


def nombres(campos):
    return {
        nombre: nombres(spec.get("fields") or spec.get("item_fields") or {})
        for nombre, spec in campos.items() if not nombre.startswith("$")
    }


def test_entrega_los_campos_del_tipo_base_mas_el_pais_y_la_identificacion_neutral():
    base, portal = nombres(crudo(BASE)["fields"]), nombres(crudo(PORTAL)["fields"])
    assert portal.pop("PAIS") == {}
    assert portal.pop("ID_FISCAL") == {}
    assert portal.pop("TIPO_ID_FISCAL") == {}
    assert portal.pop("OTROS_IDENTIFICADORES") == {"TIPO": {}, "NUMERO": {}}
    assert portal == base


def test_se_pide_declarandolo_nunca_por_clasificacion_automatica():
    """Una constancia mexicana es el mismo papel que ya reconoce el tipo base."""
    assert cargar().segmentation_candidate is False


def test_ninguna_regla_queda_inerte():
    for entidades in (mexicana(), w9()):
        _, checks = procesar(entidades)
        assert [c.field for c in checks if c.skipped] == []


# --- constancia mexicana: las reglas del SAT siguen aplicando ---------------


def test_una_constancia_mexicana_completa_se_aprueba():
    arbol, checks = procesar(mexicana())
    assert fallas(checks) == []
    assert document_status_from_checks(checks) == DOC_APPROVED
    assert arbol["PAIS"]["value"] == "MEX"


@pytest.mark.parametrize("cambios, campo", [
    ({"RFC": "XAXX010101AB", "ID_FISCAL": "XAXX010101AB"}, "RFC"),
    ({"RFC": None, "ID_FISCAL": "XAXX010101AB1"}, "RFC"),
    ({"ESTATUS_EN_PADRON": None}, "ESTATUS_EN_PADRON"),
    ({"DOMICILIO_FISCAL": {"CODIGO_POSTAL": "2528"}}, "DOMICILIO_FISCAL.CODIGO_POSTAL"),
    ({"DOMICILIO_FISCAL": {"NOMBRE_VIALIDAD": "VERACRUZ"}}, "DOMICILIO_FISCAL.CODIGO_POSTAL"),
    ({"ACTIVIDADES_ECONOMICAS": []}, "ACTIVIDADES_ECONOMICAS"),
    ({"REGIMENES_FISCALES": []}, "REGIMENES_FISCALES"),
    ({"CONSTANCIA_QR_VALIDO": False}, "CONSTANCIA_QR_VALIDO"),
])
def test_una_constancia_mexicana_se_juzga_con_las_reglas_del_sat(cambios, campo):
    _, checks = procesar(mexicana(**cambios))
    assert fallas(checks) == [campo]
    assert document_status_from_checks(checks) != DOC_APPROVED


@pytest.mark.parametrize("pais", ["MX", "México", "MEXICO"])
def test_un_pais_mal_escrito_no_apaga_las_reglas_mexicanas(pais):
    """El modelo se salió de la clave de 3 letras: PAIS queda marcado, y las
    reglas del SAT siguen corriendo en vez de callarse."""
    _, checks = procesar(mexicana(PAIS=pais, CONSTANCIA_QR_VALIDO=False, ESTATUS_EN_PADRON=None))
    assert fallas(checks) == ["CONSTANCIA_QR_VALIDO", "ESTATUS_EN_PADRON", "PAIS"]


# --- constancia extranjera --------------------------------------------------


def test_un_w9_se_aprueba_sin_los_datos_propios_del_sat():
    arbol, checks = procesar(w9())
    assert fallas(checks) == []
    assert document_status_from_checks(checks) == DOC_APPROVED
    assert arbol["PAIS"]["value"] == "USA"
    assert arbol["ID_FISCAL"]["value"] == "12-3456789"  # el EIN, con su guion
    assert arbol["ID_FISCAL"]["source"] == "MODEL_EXTRACTION"
    assert arbol["TIPO_ID_FISCAL"]["value"] == "EIN"
    assert arbol["RFC"]["value"] is None and arbol["RFC"]["valid"] is True
    assert arbol["CONSTANCIA_QR_VALIDO"]["value"] is False
    assert arbol["CONSTANCIA_QR_VALIDO"]["valid"] is True


@pytest.mark.parametrize("pais, id_fiscal, tipo, cp, otros", [
    ("ESP", "B12345678", "NIF", "28001", []),                                         # tarjeta del NIF
    ("CAN", "12345 6789 RT0001", "BN", "K1A 0B1", []),                                # cuenta GST/HST de la CRA
    ("AUS", "12 345 678 901", "ABN", "3000", [{"TIPO": "TFN", "NUMERO": "123 456 789"}]),   # certificado de la ATO
    ("NLD", "123456789", "RSIN", "1234AB", [{"TIPO": "KVK", "NUMERO": "12345678"}]),  # extracto del registro
    ("GBR", "GB123456789", "VAT", "SW1A 1AA", []),
])
def test_cualquier_otro_pais_entra_con_el_mismo_contrato(pais, id_fiscal, tipo, cp, otros):
    entidades = w9(PAIS=pais, ID_FISCAL=id_fiscal, TIPO_ID_FISCAL=tipo, OTROS_IDENTIFICADORES=otros)
    entidades["DOMICILIO_FISCAL"]["CODIGO_POSTAL"] = cp
    arbol, checks = procesar(entidades)
    assert fallas(checks) == []
    assert arbol["ID_FISCAL"]["value"] == id_fiscal
    assert arbol["RFC"]["value"] is None
    assert len(arbol["OTROS_IDENTIFICADORES"]["items"]) == len(otros)


def test_un_identificador_extranjero_dentro_del_rfc_se_marca():
    """RFC es solo el RFC mexicano: lo que no lo es no pasa en silencio."""
    _, checks = procesar(w9(RFC="12-3456789"))
    assert fallas(checks) == ["RFC"]


def test_un_identificador_secundario_necesita_su_tipo_y_su_numero():
    _, checks = procesar(w9(OTROS_IDENTIFICADORES=[{"TIPO": "TFN"}]))
    assert fallas(checks) == ["OTROS_IDENTIFICADORES[0].NUMERO"]


@pytest.mark.parametrize("faltante", ["PAIS", "ID_FISCAL", "NOMBRE_O_RAZON_SOCIAL", "FECHA_EMISION", "DOMICILIO_FISCAL"])
def test_lo_que_toda_constancia_trae_si_se_exige_a_la_extranjera(faltante):
    _, checks = procesar(w9(**{faltante: None}))
    assert fallas(checks) == [faltante]


def test_el_pais_debe_ser_la_clave_de_tres_letras():
    _, checks = procesar(w9(PAIS="United States"))
    assert fallas(checks) == ["PAIS"]


def test_una_constancia_vieja_se_marca_sin_importar_el_pais():
    _, checks = procesar(w9(FECHA_EMISION="2019-03-01"))
    assert fallas(checks) == ["FECHA_EMISION"]


# --- en una mexicana el identificador ES el RFC -----------------------------


@pytest.mark.parametrize("leido", [None, "XAXX010101AB1", "XAXX0I0101ABI", "19060000000"])
def test_en_una_mexicana_el_id_fiscal_es_una_copia_del_rfc(leido):
    """No importa qué haya leído el modelo en ID_FISCAL: manda el RFC, que en
    una constancia con QR válido es el del SAT."""
    arbol, checks = procesar(mexicana(ID_FISCAL=leido, TIPO_ID_FISCAL=None))
    assert arbol["ID_FISCAL"]["value"] == arbol["RFC"]["value"] == "XAXX010101AB1"
    assert arbol["ID_FISCAL"]["source"] == SOURCE_DERIVED
    assert arbol["ID_FISCAL"]["confidence"] == 0.99
    assert arbol["TIPO_ID_FISCAL"]["value"] == "RFC"
    assert arbol["OTROS_IDENTIFICADORES"]["items"] == []
    assert fallas(checks) == []


def test_sin_rfc_legible_no_se_inventa_el_id_fiscal():
    arbol, checks = procesar(mexicana(RFC=None))
    assert arbol["ID_FISCAL"]["value"] is None
    assert fallas(checks) == ["ID_FISCAL", "RFC"]


# --- el QR del SAT decide el país -------------------------------------------


@pytest.mark.parametrize("leido", [None, "USA", "MX"])
def test_un_qr_del_sat_valido_fija_mexico(leido):
    arbol, checks = procesar(mexicana(PAIS=leido))
    assert arbol["PAIS"]["value"] == "MEX"
    assert arbol["PAIS"]["source"] == SOURCE_DERIVED
    assert arbol["PAIS"]["confidence"] == 0.99
    assert fallas(checks) == []


def test_sin_qr_valido_el_pais_es_el_que_leyo_el_modelo():
    arbol, _ = procesar(w9())
    assert arbol["PAIS"]["value"] == "USA"
    assert arbol["PAIS"]["source"] == "MODEL_EXTRACTION"


# --- QR_CONSTANCIA es solo el QR del SAT -------------------------------------


def test_un_codigo_que_no_es_del_sat_no_se_publica_como_qr_de_la_constancia():
    """La recuperación de QR entrega cualquier código que decodifique (el de
    barras de una tarjeta NIF, por ejemplo): eso no es la URL de la constancia."""
    arbol, checks = procesar(w9(PAIS="ESP", ID_FISCAL="B12345678", TIPO_ID_FISCAL="NIF", QR_CONSTANCIA="1234567890123"))
    assert arbol["QR_CONSTANCIA"]["value"] is None
    assert fallas(checks) == []


def test_el_qr_del_sat_se_conserva_tal_cual():
    arbol, _ = procesar(mexicana())
    assert arbol["QR_CONSTANCIA"]["value"].startswith("https://siat.sat.gob.mx/")
    assert arbol["QR_CONSTANCIA"]["source"] != SOURCE_DERIVED
