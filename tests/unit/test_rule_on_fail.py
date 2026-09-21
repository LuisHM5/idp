"""`on_fail` deja que el CATÁLOGO diga qué significa que una regla falle.

Hasta que existió, la severidad de un check la decidía solo el código del
handler, y un único "error" entre N checks buenos daba PARTIALLY_APPROVED — que
el API reporta como REQUIERE_REVISION. Consecuencia: ninguna regla podía
RECHAZAR un documento por sí sola, así que una señal determinista de fraude
(una carta hecha en Word con la firma pegada) jamás habría rechazado.

Es del motor, no de un tipo: `on_fail` vale para cualquier `rule_type` de
cualquier catálogo. `allow_empty` en `boolean_true` es la otra mitad: un
booleano `computed` que el pipeline no pudo determinar queda vacío ("no
evaluado") y no cuenta como "no".
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from document_catalog import _parse_entry  # noqa: E402
from pipeline.result_common import checks_to_json  # noqa: E402
from validators.engine import (  # noqa: E402
    DOC_APPROVED,
    DOC_PARTIAL,
    DOC_REJECTED,
    DOC_REVIEW,
    document_status_from_checks,
    resolve_field_tree,
)

CONFIANZA = 0.95


def catalogo(regla_marca):
    return {
        "schema_version": "1.0.0",
        "display_name": "Tipo de prueba",
        "description": "t",
        "min_confidence": 0.8,
        "fields": {
            "NOMBRE": {"type": "string", "required": True, "hint": "h", "validation": []},
            "FOLIO": {"type": "string", "required": True, "hint": "h", "validation": []},
            "MARCA": {
                "type": "boolean", "required": False, "computed": True, "hint": "h",
                "validation": [regla_marca],
            },
        },
    }


def resolver(regla_marca, marca):
    entry = _parse_entry("TIPO_PRUEBA", catalogo(regla_marca))
    entidades = {"NOMBRE": "Ana", "FOLIO": "A-1"}
    if marca is not None:
        entidades["MARCA"] = marca
    _, checks, _ = resolve_field_tree(
        entidades, {"MARCA": 0.99}, entry,
        computed_field_names={"MARCA"}, default_confidence=CONFIANZA,
    )
    return checks


def check_de_marca(checks):
    return next(c for c in checks if c.field == "MARCA" and c.rule == "boolean_true")


def test_sin_on_fail_un_fallo_entre_varios_sigue_siendo_parcial():
    """El comportamiento de siempre no cambia para ningún catálogo existente."""
    checks = resolver({"rule_type": "boolean_true", "message": "m"}, False)

    assert check_de_marca(checks).severity == "error"
    assert check_de_marca(checks).blocking is False
    assert document_status_from_checks(checks) == DOC_PARTIAL


def test_reject_rechaza_el_documento_el_solo():
    checks = resolver({"rule_type": "boolean_true", "on_fail": "reject", "message": "m"}, False)

    assert check_de_marca(checks).blocking is True
    assert document_status_from_checks(checks) == DOC_REJECTED


def test_review_manda_a_revision_sin_tumbar():
    checks = resolver({"rule_type": "boolean_true", "on_fail": "review", "message": "m"}, False)

    assert check_de_marca(checks).severity == "warning"
    assert document_status_from_checks(checks) == DOC_REVIEW


def test_info_se_reporta_pero_no_cambia_el_status():
    checks = resolver({"rule_type": "boolean_true", "on_fail": "info", "message": "m"}, False)

    assert check_de_marca(checks).valid is False
    assert check_de_marca(checks).severity == "info"
    assert document_status_from_checks(checks) == DOC_APPROVED


def test_reject_que_pasa_no_bloquea_nada():
    checks = resolver({"rule_type": "boolean_true", "on_fail": "reject", "message": "m"}, True)

    assert check_de_marca(checks).valid is True
    assert document_status_from_checks(checks) == DOC_APPROVED


def test_un_on_fail_desconocido_se_ignora_en_vez_de_adivinarse():
    checks = resolver({"rule_type": "boolean_true", "on_fail": "explotar", "message": "m"}, False)

    assert check_de_marca(checks).severity == "error"
    assert check_de_marca(checks).blocking is False


def test_on_fail_vale_para_cualquier_rule_type():
    entry = _parse_entry("TIPO_PRUEBA", {
        "schema_version": "1.0.0", "display_name": "t", "description": "t",
        "fields": {
            "NOMBRE": {"type": "string", "required": True, "hint": "h", "validation": []},
            "FOLIO": {
                "type": "string", "required": True, "hint": "h",
                "validation": [{"rule_type": "regex", "pattern": "^A-\\d+$",
                                "on_fail": "reject", "message": "m"}],
            },
        },
    })
    _, checks, _ = resolve_field_tree(
        {"NOMBRE": "Ana", "FOLIO": "zzz"}, {}, entry, default_confidence=CONFIANZA,
    )

    assert document_status_from_checks(checks) == DOC_REJECTED


def test_una_regla_que_no_pudo_correr_no_bloquea():
    """`skipped` = no hubo veredicto; `on_fail` no tiene fallo que graduar."""
    checks = resolver({"rule_type": "regla_que_no_existe", "on_fail": "reject"}, False)

    saltado = next(c for c in checks if c.field == "MARCA" and c.skipped)
    assert saltado.blocking is False
    assert document_status_from_checks(checks) == DOC_APPROVED


def test_allow_empty_da_el_estado_no_evaluado():
    regla = {"rule_type": "boolean_true", "allow_empty": True, "on_fail": "reject", "message": "m"}

    checks = resolver(regla, None)

    assert not [c for c in checks if c.field == "MARCA" and c.rule == "boolean_true"]
    assert document_status_from_checks(checks) == DOC_APPROVED


def test_sin_allow_empty_el_vacio_sigue_fallando():
    checks = resolver({"rule_type": "boolean_true", "message": "m"}, None)

    assert check_de_marca(checks).valid is False


def test_blocking_viaja_al_json_solo_cuando_aplica():
    bloqueante = checks_to_json(
        resolver({"rule_type": "boolean_true", "on_fail": "reject", "message": "m"}, False)
    )
    normal = checks_to_json(resolver({"rule_type": "boolean_true", "message": "m"}, False))

    assert next(c for c in bloqueante if c["field"] == "MARCA")["blocking"] is True
    assert all("blocking" not in c for c in normal)
