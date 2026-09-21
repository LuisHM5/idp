"""El ground truth de autenticidad se valida GRATIS antes de cualquier corrida.

`tests/integration/expected_autenticidad.json` lo consume
`pnpm test:autenticidad`. Un veredicto mal escrito o un motivo que ya no existe
harían que esa comparación fallara (o peor, pasara) por la razón equivocada.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src/document-processing/resources/enrichment-lambda"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from catalog_paths import active_catalog_path  # noqa: E402
from pipeline import pdf_forensics as pf  # noqa: E402

ESPERADO = json.loads((REPO_ROOT / "tests/integration/expected_autenticidad.json").read_text(encoding="utf-8"))
VEREDICTOS = {pf.PAGE_PHYSICAL, pf.PAGE_PHYSICAL_OCR, pf.PAGE_DIGITAL, pf.PAGE_UNDETERMINED}
MOTIVOS = {pf.REASON_PASTED_IMAGE, pf.REASON_NO_CAPTURE, pf.REASON_TRANSPARENCY, pf.REASON_OFFICE_ORIGIN}


def test_el_tipo_existe_y_declara_la_etapa():
    crudo = json.loads(active_catalog_path(REPO_ROOT, ESPERADO["type"]).read_text(encoding="utf-8"))

    assert isinstance(crudo[ESPERADO["type"]].get("authenticity"), dict)


def test_cada_archivo_usa_veredictos_y_motivos_que_el_codigo_conoce():
    assert ESPERADO["files"]
    for ruta, caso in ESPERADO["files"].items():
        assert caso["structure"] in VEREDICTOS, ruta
        assert caso["physical_capture"] in (True, False, None), ruta
        assert set(caso.get("reasons_include") or []) <= MOTIVOS, ruta
        assert len(caso["sha256"]) == 64, ruta


def test_veredicto_y_captura_fisica_son_coherentes():
    for ruta, caso in ESPERADO["files"].items():
        esperado = {pf.PAGE_DIGITAL: False, pf.PAGE_UNDETERMINED: None}.get(caso["structure"], True)
        assert caso["physical_capture"] is esperado, ruta


def test_las_carpetas_ancla_cubren_genuinas_y_fabricadas():
    anclas = {ruta.split("/")[0]: caso for ruta, caso in ESPERADO["files"].items() if caso["verified_by_eye"]}

    assert set(anclas) == set(ESPERADO["anchors"])
    assert {caso["physical_capture"] for caso in anclas.values()} == {True, False}
