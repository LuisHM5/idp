"""`$presets` en `fields`: un tipo inyecta con una línea los campos `computed`
de verificación definidos una sola vez en `_shared.json` → `_field_presets`.

- los campos del preset se parsean como cualquier otro (con su `authenticity_source`)
- un campo redeclarado por el tipo GANA sobre el del preset (así se endurece una señal)
- un preset desconocido se ignora con warning, no truena
- `$presets` no aplica dentro de `item_fields`
- el bundle real (`_shared.json`) trae los tres presets documentados, todos con
  señales que la etapa conoce
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src/document-processing/resources/enrichment-lambda"))

import pytest  # noqa: E402

import document_catalog as dc  # noqa: E402
from pipeline import authenticity, pdf_integrity  # noqa: E402

COMPARTIDO = json.loads((REPO_ROOT / "catalogs/_shared.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def presets_del_repo():
    dc._FIELD_PRESETS.clear()
    dc._FIELD_PRESETS.update({k: v for k, v in COMPARTIDO["_field_presets"].items() if not k.startswith("__")})
    yield
    dc._FIELD_PRESETS.clear()


def _tipo(fields, bloque=None):
    data = {"description": "x", "fields": fields}
    if bloque is not None:
        data["authenticity"] = bloque
    return dc._parse_entry("TIPO_PRUEBA", data)


def test_el_preset_inyecta_sus_campos_con_su_senal():
    entry = _tipo({"$presets": ["INTEGRIDAD_PDF"], "FOLIO": {"type": "string", "required": True, "hint": "h"}})

    assert "ARCHIVO_SIN_REGUARDADO" in entry.field_tree
    assert entry.field_tree["ARCHIVO_SIN_REGUARDADO"].params["authenticity_source"] == "integrity.single_generation"
    assert entry.field_tree["ARCHIVO_SIN_REGUARDADO"].computed is True
    assert "FOLIO" in entry.field_tree
    assert "$presets" not in entry.field_tree
    # Los del preset van primero; los propios después.
    assert list(entry.field_tree).index("FOLIO") > list(entry.field_tree).index("MOTIVOS_AUTENTICIDAD")


def test_el_tipo_gana_al_redeclarar_un_campo_del_preset():
    entry = _tipo({
        "$presets": ["INTEGRIDAD_PDF"],
        "SIN_ANOTACIONES": {
            "type": "boolean", "required": False, "computed": True,
            "authenticity_source": "integrity.no_annotations", "hint": "propio",
            "validation": [{"rule_type": "boolean_true", "allow_empty": True, "on_fail": "reject", "message": "rechazo"}],
        },
    })

    regla = entry.field_tree["SIN_ANOTACIONES"].validation[0]
    assert regla.params.get("on_fail") == "reject"
    assert entry.field_tree["SIN_ANOTACIONES"].hint == "propio"


def test_un_preset_desconocido_se_ignora_sin_tronar(caplog):
    entry = _tipo({"$presets": ["NO_EXISTE", "INTEGRIDAD_IMAGEN"]})

    assert "NIVEL_DE_ERROR_UNIFORME" in entry.field_tree
    assert "NO_EXISTE" in caplog.text


def test_presets_no_aplica_dentro_de_item_fields():
    entry = _tipo({"FILAS": {"type": "array", "required": False, "hint": "h",
                             "item_fields": {"$presets": ["INTEGRIDAD_PDF"], "MONTO": {"type": "number", "hint": "h"}}}})

    filas = entry.field_tree["FILAS"]
    assert "ARCHIVO_SIN_REGUARDADO" not in (filas.item_fields or {})


def test_un_tipo_con_preset_y_sin_bloque_activa_la_etapa_solo_con_capas_gratis():
    entry = _tipo({"$presets": ["INTEGRIDAD_PDF"]})

    spec = authenticity.spec_for(entry)
    assert spec is not None
    assert spec["layers"] == {"integrity"}
    assert authenticity.applies_to(entry)


def test_los_presets_del_repo_usan_senales_que_la_etapa_conoce():
    conocidas = set(pdf_integrity.SIGNALS) | {
        "evaluated", "verdict", "reasons", "capture_kind",
        "structure.physical_capture", "structure.origin", "structure.capture_as_expected",
        "pixels.natural_background", "pixels.error_level_uniform", "vision.no_edit_signs",
    }
    presets = {k: v for k, v in COMPARTIDO["_field_presets"].items() if not k.startswith("__")}
    assert set(presets) == {"INTEGRIDAD_PDF", "INTEGRIDAD_IMAGEN", "CAPTURA_FISICA"}
    for nombre, campos in presets.items():
        for campo, spec in campos.items():
            if campo.startswith("__"):
                continue
            assert spec["computed"] is True and spec["required"] is False, (nombre, campo)
            assert spec["authenticity_source"] in conocidas, (nombre, campo, spec["authenticity_source"])
            for regla in spec["validation"]:
                assert regla["on_fail"] in ("review", "info", "reject"), (nombre, campo)
                if spec["authenticity_source"] != "evaluated":
                    assert regla.get("allow_empty") is True, (nombre, campo)
        assert "AUTENTICIDAD_EVALUADA" in campos and "VEREDICTO_AUTENTICIDAD" in campos, nombre
