"""Los tipos del portal para el alta de un proveedor.

IDENTIFICACION_OFICIAL_PORTAL, ESTADO_CUENTA_PORTAL, CONTRATO_SERVICIO_PORTAL,
ACTA_CONSTITUTIVA_PORTAL y REGISTRO_PUBLICO_COMERCIO_PORTAL. El contrato que
este test fija:

- los cinco se piden declarándolos, nunca por clasificación automática;
- cada uno acepta la variante mexicana y la extranjera del documento: las
  reglas propias de México (clave de elector, CLABE, número de escritura,
  folio mercantil) llevan `when` y no reprueban a un documento de otro país;
- lo que se puede decidir sin el modelo se decide: una credencial del INE y
  una CLABE fijan `PAIS = MEX`;
- los documentos que el portal compara entre sí comparten nombres de campo.

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

IDENTIFICACION = "IDENTIFICACION_OFICIAL_PORTAL"
ESTADO_CUENTA = "ESTADO_CUENTA_PORTAL"
CONTRATO = "CONTRATO_SERVICIO_PORTAL"
ACTA = "ACTA_CONSTITUTIVA_PORTAL"
REGISTRO = "REGISTRO_PUBLICO_COMERCIO_PORTAL"
CONSTANCIA = "CONSTANCIA_SITUACION_FISCAL_PORTAL"
TIPOS = [IDENTIFICACION, ESTADO_CUENTA, CONTRATO, ACTA, REGISTRO]
HOY = date(2026, 10, 5)


def crudo(type_id):
    path = active_catalog_path(REPO_ROOT, type_id)
    return json.loads(path.read_text(encoding="utf-8"))[type_id]


def procesar(type_id, entidades):
    """Lo que hace el pipeline después de extraer: derivados y luego reglas."""
    entry = _parse_entry(type_id, crudo(type_id))
    entidades = json.loads(json.dumps(entidades))
    derivados = apply_derived_fields(entidades, {}, entry)
    arbol, checks, _ = resolve_field_tree(
        entidades, {}, entry, ValidationContext(today=HOY),
        source_overrides=derivados.sources, default_confidence=0.95,
    )
    return arbol, checks


def fallas(type_id, entidades):
    _, checks = procesar(type_id, entidades)
    return sorted(c.field for c in checks if not c.valid and not c.skipped)


def nombres(campos):
    return {
        nombre: nombres(spec.get("fields") or spec.get("item_fields") or {})
        for nombre, spec in campos.items() if not nombre.startswith("$")
    }


# --- un documento de cada tipo y variante ----------------------------------


def ine(**cambios):
    entidades = {
        "PAIS": "MEX",
        "TIPO_IDENTIFICACION": "INE",
        "NOMBRE_COMPLETO": "JUAN CARLOS PEREZ LOPEZ",
        "NOMBRES": "JUAN CARLOS",
        "PRIMER_APELLIDO": "PEREZ",
        "SEGUNDO_APELLIDO": "LOPEZ",
        "NUMERO_DOCUMENTO": "PRLPJN85010109H400",
        "CURP": "PELJ850101HCHRPN09",
        "CIC": "123456789",
        "FECHA_NACIMIENTO": "1985-01-01",
        "FECHA_VENCIMIENTO": "2036-12-31",
        "AUTORIDAD_EMISORA": "INSTITUTO NACIONAL ELECTORAL",
    }
    entidades.update(cambios)
    return entidades


def licencia_extranjera(**cambios):
    """Una licencia de conducir de Estados Unidos: sin CURP, sin CIC, con un
    número que lleva guiones y un solo apellido."""
    entidades = {
        "PAIS": "USA",
        "TIPO_IDENTIFICACION": "LICENCIA_CONDUCIR",
        "NOMBRE_COMPLETO": "JANE EXAMPLE",
        "NOMBRES": "JANE",
        "PRIMER_APELLIDO": "EXAMPLE",
        "NUMERO_DOCUMENTO": "E123-456-78-901-0",
        "FECHA_NACIMIENTO": "1985-08-22",
        "FECHA_VENCIMIENTO": "2030-08-22",
    }
    entidades.update(cambios)
    return entidades


def estado_cuenta_mexicano(**cambios):
    entidades = {
        "PAIS": "MEX",
        "TIPO_DOCUMENTO": "ESTADO_CUENTA",
        "BANCO": "BBVA",
        "TITULAR": "EJEMPLO COMERCIAL SA DE CV",
        "RFC": "ECO190522AB1",
        "NUMERO_CUENTA": "0123456789",
        "CLABE": "012345678901234567",
        "MONEDA": "MXN",
        "FECHA_EMISION": "2026-03-31",
    }
    entidades.update(cambios)
    return entidades


def carta_bancaria_extranjera(**cambios):
    """La carta de un banco europeo: la cuenta es un IBAN, sin CLABE ni RFC."""
    entidades = {
        "PAIS": "NLD",
        "TIPO_DOCUMENTO": "CARTA_BANCARIA",
        "BANCO": "Ejemplo Bank",
        "TITULAR": "Ejemplo B.V.",
        "IBAN": "NL00BANK0123456789",
        "SWIFT_BIC": "BANKNL2UXXX",
        "MONEDA": "EUR,USD",
        "FECHA_EMISION": "2025-01-21",
    }
    entidades.update(cambios)
    return entidades


def contrato(**cambios):
    entidades = {
        "TIPO_CONTRATO": "CONTRATO DE PRESTACIÓN DE SERVICIOS",
        "CONTRATANTE_NOMBRE": "EJEMPLO INMOBILIARIA, S.A. DE C.V.",
        "CONTRATANTE_REPRESENTANTE": "MARIA EJEMPLO PRUEBA",
        "PROVEEDOR_NOMBRE": "JUAN CARLOS PEREZ LOPEZ",
        "PROVEEDOR_RFC": "PELJ850101AB1",
        "OBJETO_CONTRATO": "Servicios de corresponsable de instalaciones eléctricas en la sucursal X",
        "MONTO_TOTAL": 40000.0,
        "MONEDA": "MXN",
        "MONTO_INCLUYE_IVA": False,
        "FECHA_FIRMA": "2026-06-26",
    }
    entidades.update(cambios)
    return entidades


def escritura(**cambios):
    entidades = {
        "PAIS": "MEX",
        "TIPO_INSTRUMENTO": "PROTOCOLIZACION_ASAMBLEA",
        "NOMBRE_O_RAZON_SOCIAL": "EJEMPLO COMERCIAL",
        "REGIMEN_CAPITAL": "SOCIEDAD ANÓNIMA DE CAPITAL VARIABLE",
        "NUMERO_ESCRITURA": "12345",
        "FECHA_ESCRITURA": "2022-03-08",
        "NOTARIO_NOMBRE": "PEDRO EJEMPLO NOTARIO",
        "NOTARIA_NUMERO": "15",
        "NUMERO_ESCRITURA_CONSTITUTIVA": "4321",
        "FECHA_CONSTITUCION": "2011-01-03",
        "CAPITAL_SOCIAL": 50000.0,
        "SOCIOS": [
            {"NOMBRE": "EJEMPLO HOLDING, S.A. DE C.V.", "RFC": "EHO090420AB1", "ACCIONES": 48, "IMPORTE": 48000.0},
            {"NOMBRE": "JUAN CARLOS PEREZ LOPEZ", "ACCIONES": 2, "IMPORTE": 2000.0},
        ],
        "REPRESENTANTES_LEGALES": [{"NOMBRE": "JUAN CARLOS PEREZ LOPEZ", "CARGO": "ADMINISTRADOR ÚNICO"}],
    }
    entidades.update(cambios)
    return entidades


def boleta(**cambios):
    entidades = {
        "PAIS": "MEX",
        "NOMBRE_O_RAZON_SOCIAL": "EJEMPLO COMERCIAL",
        "REGIMEN_CAPITAL": "SOCIEDAD ANÓNIMA DE CAPITAL VARIABLE",
        "FOLIO_MERCANTIL": "N-2019012345",
        "NUMERO_UNICO_DOCUMENTO": "20190001234500AB",
        "NCI": "201900012345",
        "FECHA_INSCRIPCION": "2019-05-29",
        "ACTOS_INSCRITOS": [
            {"FORMA_PRECODIFICADA": "M4", "NOMBRE_ACTO": "Constitución de sociedad mercantil", "FECHA_INGRESO": "2019-05-29"},
        ],
        "NUMERO_ESCRITURA": "12345",
        "FECHA_ESCRITURA": "2019-05-21",
        "NOTARIO_NOMBRE": "PEDRO EJEMPLO NOTARIO",
    }
    entidades.update(cambios)
    return entidades


EJEMPLOS = [
    (IDENTIFICACION, ine()),
    (IDENTIFICACION, licencia_extranjera()),
    (ESTADO_CUENTA, estado_cuenta_mexicano()),
    (ESTADO_CUENTA, carta_bancaria_extranjera()),
    (CONTRATO, contrato()),
    (ACTA, escritura()),
    (ACTA, {"PAIS": "USA", "TIPO_INSTRUMENTO": "CONSTITUTIVA",
            "NOMBRE_O_RAZON_SOCIAL": "Ejemplo LLC", "FECHA_ESCRITURA": "2014-02-19"}),
    (REGISTRO, boleta()),
    (REGISTRO, {"PAIS": "NLD", "NOMBRE_O_RAZON_SOCIAL": "Ejemplo B.V.",
                "FOLIO_MERCANTIL": "12345678", "FECHA_INSCRIPCION": "2010-01-01", "ACTOS_INSCRITOS": []}),
]


# --- lo común a los cinco ---------------------------------------------------


@pytest.mark.parametrize("type_id", TIPOS)
def test_se_piden_declarandolos_nunca_por_clasificacion_automatica(type_id):
    assert _parse_entry(type_id, crudo(type_id)).segmentation_candidate is False


@pytest.mark.parametrize("type_id, entidades", EJEMPLOS)
def test_un_documento_completo_se_aprueba_y_ninguna_regla_queda_inerte(type_id, entidades):
    _, checks = procesar(type_id, entidades)
    assert [c.field for c in checks if c.skipped] == []
    assert sorted(c.field for c in checks if not c.valid) == []
    assert document_status_from_checks(checks) == DOC_APPROVED


def test_los_documentos_de_la_sociedad_comparten_nombres_de_campo():
    """El portal compara acta, boleta del registro y constancia fiscal campo
    contra campo: lo que significa lo mismo se llama igual en los tres."""
    acta, registro, constancia = (nombres(crudo(t)["fields"]) for t in (ACTA, REGISTRO, CONSTANCIA))
    for campo in ("PAIS", "NOMBRE_O_RAZON_SOCIAL", "REGIMEN_CAPITAL"):
        assert campo in acta and campo in registro and campo in constancia
    for campo in ("FOLIO_MERCANTIL", "NUMERO_ESCRITURA", "FECHA_ESCRITURA", "NOTARIO_NOMBRE",
                  "NOTARIA_NUMERO", "NOTARIA_PLAZA", "DOMICILIO_SOCIAL", "DURACION",
                  "OBJETO_SOCIAL", "CAPITAL_SOCIAL", "SOCIOS", "REPRESENTANTES_LEGALES"):
        assert acta[campo] == registro[campo]


def test_el_identificador_fiscal_se_llama_igual_en_todos_los_documentos():
    """`RFC` es solo el RFC mexicano; quién es el titular se lee de `ID_FISCAL`
    en la constancia, el estado de cuenta y el acta (y del proveedor en el
    contrato, con su prefijo)."""
    for tipo in (CONSTANCIA, ESTADO_CUENTA, ACTA):
        campos = nombres(crudo(tipo)["fields"])
        assert {"RFC", "ID_FISCAL", "TIPO_ID_FISCAL"} <= set(campos)
    assert {"PROVEEDOR_RFC", "PROVEEDOR_ID_FISCAL", "PROVEEDOR_TIPO_ID_FISCAL"} <= set(nombres(crudo(CONTRATO)["fields"]))


@pytest.mark.parametrize("type_id, entidades, rfc, id_fiscal, tipo", [
    (ESTADO_CUENTA, estado_cuenta_mexicano(), "RFC", "ID_FISCAL", "TIPO_ID_FISCAL"),
    (ACTA, escritura(RFC="ECO190522AB1"), "RFC", "ID_FISCAL", "TIPO_ID_FISCAL"),
    (CONTRATO, contrato(), "PROVEEDOR_RFC", "PROVEEDOR_ID_FISCAL", "PROVEEDOR_TIPO_ID_FISCAL"),
])
def test_con_rfc_el_id_fiscal_es_su_copia(type_id, entidades, rfc, id_fiscal, tipo):
    arbol, checks = procesar(type_id, {**entidades, id_fiscal: "otra lectura del modelo"})
    assert arbol[id_fiscal]["value"] == arbol[rfc]["value"] == entidades[rfc]
    assert arbol[id_fiscal]["source"] == SOURCE_DERIVED
    assert arbol[tipo]["value"] == "RFC"
    assert sorted(c.field for c in checks if not c.valid) == []


@pytest.mark.parametrize("type_id, entidades, rfc", [
    (ESTADO_CUENTA, carta_bancaria_extranjera(), "RFC"),
    (ACTA, {"PAIS": "USA", "TIPO_INSTRUMENTO": "CONSTITUTIVA", "NOMBRE_O_RAZON_SOCIAL": "Ejemplo LLC",
            "FECHA_ESCRITURA": "2014-02-19"}, "RFC"),
    (CONTRATO, contrato(), "PROVEEDOR_RFC"),
])
def test_un_identificador_extranjero_dentro_de_un_rfc_se_marca(type_id, entidades, rfc):
    assert fallas(type_id, {**entidades, rfc: "12-3456789"}) == [rfc]


def test_la_persona_se_nombra_igual_en_la_identificacion_y_en_la_constancia():
    identificacion, constancia = nombres(crudo(IDENTIFICACION)["fields"]), nombres(crudo(CONSTANCIA)["fields"])
    for campo in ("PAIS", "CURP", "NOMBRES", "PRIMER_APELLIDO", "SEGUNDO_APELLIDO"):
        assert campo in identificacion and campo in constancia


# --- identificación oficial -------------------------------------------------


@pytest.mark.parametrize("leido", [None, "MX", "USA"])
def test_una_credencial_del_ine_fija_mexico(leido):
    arbol, _ = procesar(IDENTIFICACION, ine(PAIS=leido))
    assert arbol["PAIS"]["value"] == "MEX"
    assert arbol["PAIS"]["source"] == SOURCE_DERIVED


@pytest.mark.parametrize("cambios, campo", [
    ({"NUMERO_DOCUMENTO": "PELJ8501"}, "NUMERO_DOCUMENTO"),
    ({"CURP": "PRLPJN85010109H400"}, "CURP"),
    ({"CIC": "1234567890"}, "CIC"),
    ({"FECHA_VENCIMIENTO": None}, "FECHA_VENCIMIENTO"),
    ({"FECHA_VENCIMIENTO": "2025-12-31"}, "FECHA_VENCIMIENTO"),
    ({"TIPO_IDENTIFICACION": "CREDENCIAL PARA VOTAR"}, "TIPO_IDENTIFICACION"),
    ({"NOMBRE_COMPLETO": None}, "NOMBRE_COMPLETO"),
])
def test_una_credencial_del_ine_se_juzga_con_sus_reglas(cambios, campo):
    assert fallas(IDENTIFICACION, ine(**cambios)) == [campo]


def test_una_identificacion_extranjera_no_se_juzga_con_las_reglas_del_ine():
    arbol, checks = procesar(IDENTIFICACION, licencia_extranjera())
    assert document_status_from_checks(checks) == DOC_APPROVED
    assert arbol["PAIS"]["value"] == "USA"
    assert arbol["NUMERO_DOCUMENTO"]["value"] == "E123-456-78-901-0"  # con sus guiones


def test_un_pasaporte_se_juzga_con_el_formato_de_su_numero():
    pasaporte = licencia_extranjera(PAIS="ESP", TIPO_IDENTIFICACION="PASAPORTE", NUMERO_DOCUMENTO="XAB123456")
    assert fallas(IDENTIFICACION, pasaporte) == []
    assert fallas(IDENTIFICACION, dict(pasaporte, NUMERO_DOCUMENTO="XAB-123")) == ["NUMERO_DOCUMENTO"]


def test_una_identificacion_vencida_se_marca_sin_importar_el_pais():
    assert fallas(IDENTIFICACION, licencia_extranjera(FECHA_VENCIMIENTO="2025-01-01")) == ["FECHA_VENCIMIENTO"]


def test_una_cedula_profesional_no_necesita_vencimiento():
    cedula = ine(TIPO_IDENTIFICACION="CEDULA_PROFESIONAL", NUMERO_DOCUMENTO="1234567", FECHA_VENCIMIENTO=None, CIC=None)
    assert fallas(IDENTIFICACION, cedula) == []


# --- estado de cuenta -------------------------------------------------------


@pytest.mark.parametrize("leido", [None, "MX", "USA"])
def test_una_clabe_fija_mexico(leido):
    arbol, checks = procesar(ESTADO_CUENTA, estado_cuenta_mexicano(PAIS=leido))
    assert arbol["PAIS"]["value"] == "MEX"
    assert arbol["PAIS"]["source"] == SOURCE_DERIVED
    assert document_status_from_checks(checks) == DOC_APPROVED


@pytest.mark.parametrize("cambios, campo", [
    ({"CLABE": None}, "CLABE"),
    ({"CLABE": "01234567890123456"}, "CLABE"),
    ({"RFC": "12-3456789"}, "RFC"),
    ({"MONEDA": "PESOS"}, "MONEDA"),
    ({"TIPO_DOCUMENTO": "CARATULA"}, "TIPO_DOCUMENTO"),
    ({"BANCO": None}, "BANCO"),
    ({"TITULAR": None}, "TITULAR"),
])
def test_una_cuenta_mexicana_se_juzga_con_sus_reglas(cambios, campo):
    assert fallas(ESTADO_CUENTA, estado_cuenta_mexicano(**cambios)) == [campo]


def test_un_pais_mal_escrito_no_apaga_las_reglas_mexicanas():
    """Sin CLABE no hay de dónde derivar el país: queda el que leyó el modelo,
    marcado, y la CLABE se sigue exigiendo."""
    assert fallas(ESTADO_CUENTA, estado_cuenta_mexicano(PAIS="México", CLABE=None)) == ["CLABE", "PAIS"]


def test_una_cuenta_extranjera_entra_con_su_iban_o_con_su_numero_de_cuenta():
    assert fallas(ESTADO_CUENTA, carta_bancaria_extranjera()) == []
    con_cuenta = carta_bancaria_extranjera(
        PAIS="USA", IBAN=None, NUMERO_CUENTA="1234567890", ABA_ROUTING="123456789",
        ABA_ROUTING_ACH="987654321", SWIFT_BIC="BANKUS6S", MONEDA=None,
        ID_FISCAL="12-3456789", TIPO_ID_FISCAL="EIN",
    )
    assert fallas(ESTADO_CUENTA, con_cuenta) == []


def test_una_cuenta_extranjera_sin_iban_ni_numero_de_cuenta_se_marca():
    assert fallas(ESTADO_CUENTA, carta_bancaria_extranjera(IBAN=None)) == ["NUMERO_CUENTA"]


@pytest.mark.parametrize("cambios, campo", [
    ({"IBAN": "NL00 BANK 0123"}, "IBAN"),
    ({"SWIFT_BIC": "BANK"}, "SWIFT_BIC"),
    ({"ABA_ROUTING": "12345"}, "ABA_ROUTING"),
    ({"BANCO_INTERMEDIARIO": {"NOMBRE": "Ejemplo Bankers Bank", "SWIFT_BIC": "BANK"}}, "BANCO_INTERMEDIARIO.SWIFT_BIC"),
])
def test_los_identificadores_bancarios_se_juzgan_por_su_formato(cambios, campo):
    assert fallas(ESTADO_CUENTA, carta_bancaria_extranjera(**cambios)) == [campo]


def test_el_banco_intermediario_es_opcional_y_entra_completo():
    intermediario = {"NOMBRE": "Ejemplo Bankers Bank", "SWIFT_BIC": "BANKUS66", "NUMERO_CUENTA": "100-010701-USD-01"}
    arbol, checks = procesar(ESTADO_CUENTA, carta_bancaria_extranjera(BANCO_INTERMEDIARIO=intermediario))
    assert document_status_from_checks(checks) == DOC_APPROVED
    assert arbol["BANCO_INTERMEDIARIO"]["fields"]["SWIFT_BIC"]["value"] == "BANKUS66"


# --- contrato de servicio ---------------------------------------------------


@pytest.mark.parametrize("faltante", ["TIPO_CONTRATO", "CONTRATANTE_NOMBRE", "PROVEEDOR_NOMBRE", "OBJETO_CONTRATO"])
def test_lo_que_todo_contrato_trae_se_exige(faltante):
    assert fallas(CONTRATO, contrato(**{faltante: None})) == [faltante]


def test_una_orden_de_servicio_sin_monto_ni_fecha_de_firma_se_aprueba():
    orden = contrato(MONTO_TOTAL=None, MONEDA=None, MONTO_INCLUYE_IVA=None, FECHA_FIRMA=None, PROVEEDOR_RFC=None)
    assert fallas(CONTRATO, orden) == []


def test_un_proveedor_extranjero_entra_con_su_propio_identificador_fiscal():
    extranjero = contrato(PROVEEDOR_NOMBRE="Ejemplo B.V.", PROVEEDOR_RFC=None,
                          PROVEEDOR_ID_FISCAL="NL123456789B01", PROVEEDOR_TIPO_ID_FISCAL="VAT")
    arbol, checks = procesar(CONTRATO, extranjero)
    assert sorted(c.field for c in checks if not c.valid) == []
    assert arbol["PROVEEDOR_ID_FISCAL"]["value"] == "NL123456789B01"
    assert arbol["PROVEEDOR_RFC"]["value"] is None


def test_la_moneda_del_contrato_debe_ser_la_clave_de_tres_letras():
    assert fallas(CONTRATO, contrato(MONEDA="PESOS")) == ["MONEDA"]


# --- acta constitutiva ------------------------------------------------------


@pytest.mark.parametrize("cambios, campo", [
    ({"NUMERO_ESCRITURA": None}, "NUMERO_ESCRITURA"),
    ({"NOTARIO_NOMBRE": None}, "NOTARIO_NOMBRE"),
    ({"FECHA_ESCRITURA": None}, "FECHA_ESCRITURA"),
    ({"NOMBRE_O_RAZON_SOCIAL": None}, "NOMBRE_O_RAZON_SOCIAL"),
    ({"TIPO_INSTRUMENTO": "ESCRITURA"}, "TIPO_INSTRUMENTO"),
    ({"RFC": "PELJ850101AB1"}, "RFC"),  # el de una persona física no es el de la sociedad
    ({"SOCIOS": [{"ACCIONES": 50}]}, "SOCIOS[0].NOMBRE"),
])
def test_una_escritura_mexicana_se_juzga_con_sus_reglas(cambios, campo):
    assert fallas(ACTA, escritura(**cambios)) == [campo]


@pytest.mark.parametrize("tipo", ["CONSTITUTIVA", "PROTOCOLIZACION_ASAMBLEA", "PODER", "OTRO"])
def test_el_tipo_de_instrumento_informa_pero_no_reprueba(tipo):
    assert fallas(ACTA, escritura(TIPO_INSTRUMENTO=tipo)) == []


def test_una_escritura_sin_listas_de_socios_ni_representantes_se_aprueba():
    assert fallas(ACTA, escritura(SOCIOS=[], REPRESENTANTES_LEGALES=[])) == []


def test_un_documento_de_constitucion_extranjero_no_necesita_notario():
    extranjero = {"PAIS": "USA", "TIPO_INSTRUMENTO": "CONSTITUTIVA", "NOMBRE_O_RAZON_SOCIAL": "Ejemplo LLC",
                  "ID_FISCAL": "12-3456789", "TIPO_ID_FISCAL": "EIN", "FECHA_ESCRITURA": "2014-02-19"}
    assert fallas(ACTA, extranjero) == []


# --- registro público de comercio -------------------------------------------


@pytest.mark.parametrize("cambios, campo", [
    ({"FOLIO_MERCANTIL": None}, "FOLIO_MERCANTIL"),
    ({"ACTOS_INSCRITOS": []}, "ACTOS_INSCRITOS"),
    ({"ACTOS_INSCRITOS": [{"FORMA_PRECODIFICADA": "M4"}]}, "ACTOS_INSCRITOS[0].NOMBRE_ACTO"),
    ({"FECHA_INSCRIPCION": None}, "FECHA_INSCRIPCION"),
    ({"NOMBRE_O_RAZON_SOCIAL": None}, "NOMBRE_O_RAZON_SOCIAL"),
])
def test_una_boleta_mexicana_se_juzga_con_sus_reglas(cambios, campo):
    assert fallas(REGISTRO, boleta(**cambios)) == [campo]


@pytest.mark.parametrize("folio", ["N-2019012345", "12345*10", "12345-1"])
def test_el_folio_mercantil_acepta_los_formatos_anteriores_a_siger(folio):
    assert fallas(REGISTRO, boleta(FOLIO_MERCANTIL=folio)) == []


def test_una_boleta_sin_forma_precodificada_se_aprueba():
    """Solo la boleta, sin el detalle del acto: no hay instrumento ni socios."""
    sola = boleta(NUMERO_ESCRITURA=None, FECHA_ESCRITURA=None, NOTARIO_NOMBRE=None)
    assert fallas(REGISTRO, sola) == []


def test_un_extracto_extranjero_no_necesita_folio_ni_actos():
    extranjero = {"PAIS": "NLD", "NOMBRE_O_RAZON_SOCIAL": "Ejemplo B.V.", "FECHA_INSCRIPCION": "2010-01-01"}
    assert fallas(REGISTRO, extranjero) == []
