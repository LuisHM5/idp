"""CLI de `pnpm test:e2e:suite` / `pnpm test:e2e:init`: tipos posicionales
(uno para correr un solo dataset), `-d` para usar otra carpeta, sugerencias
ante un tipo mal escrito y la ayuda estilo man. Nada toca AWS: los errores
de uso salen antes de leer credenciales o la tabla de costos."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("e2e_suite_script_under_test", REPO / "scripts/test-e2e-suite.py")
suite = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["e2e_suite_script_under_test"] = suite
spec.loader.exec_module(suite)

TYPES = ["CURP", "INE", "PEDIMENTO_ADUANAL", "PEDIMENTO_ERP"]


def fake_plan(groups, env, stack, region):
    """Plan de costo sin AWS: el estimador tiene sus propias pruebas (test_cost_estimate.py)."""
    plans = []
    for doc_type, files in groups:
        rows = [{"file": f.name, "docType": doc_type, "model": "minimax.minimax-m2.5", "pages": 1, "ocrPages": 0,
                 "extractPages": 1, "calls": 1, "inputTokens": 10, "outputTokens": 10, "totalUsd": 0.0, "totalHighUsd": 0.0,
                 "exact": True} for f in files]
        plans.append({"files": rows, "total": {
            "docType": doc_type, "files": len(rows), "models": ["minimax.minimax-m2.5"], "pages": len(rows), "ocrPages": 0,
            "extractPages": len(rows), "calls": len(rows), "textractUsd": 0.0, "bedrockUsd": 0.0, "bedrockHighUsd": 0.0,
            "lambdaUsd": 0.0, "estimateUsd": 0.0, "ceilingUsd": 0.0, "inputTokens": 0, "outputTokens": 0, "errors": [],
            "uncertain": [], "uncalibratedModels": [], "deployment": {"source": "lambda", "warnings": []}, "calibration": {}}})
    return plans


def no_plan(*_args, **_kwargs):
    pytest.fail("un error de uso no debe calcular el plan de costo (lee AWS)")


def run_main(monkeypatch, capsys, *argv: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["test-e2e-suite.py", *argv])
    code = suite.main()
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_types_are_case_insensitive_and_typos_get_a_suggestion() -> None:
    selected, errors = suite.resolve_types(["pedimento_erp", "CURP", " "], TYPES)
    assert selected == {"PEDIMENTO_ERP", "CURP"} and errors == []

    selected, errors = suite.resolve_types(["pedimento_er", "XYZ"], TYPES)
    assert selected == set()
    assert errors == ["'pedimento_er' no es un tipo activo — ¿quisiste decir PEDIMENTO_ERP?",
                      "'XYZ' no es un tipo activo"]


def test_parser_accepts_positional_types_short_flags_and_legacy_long_names() -> None:
    parser = suite.build_parser()
    args = parser.parse_args(["PEDIMENTO_ERP", "-e", "prod", "-p", "-n", "2", "-j", "4", "-m", "5", "-d", "x"])
    assert args.types == ["PEDIMENTO_ERP"] and args.env == "prod" and args.plan
    assert (args.limit_per_type, args.concurrency, args.max_cost_usd, args.docs) == (2, 4, 5.0, "x")
    legacy = parser.parse_args(["--limit-per-type", "3", "--only", "CURP,INE", "-t", "PEDIMENTO_ERP"])
    assert legacy.limit_per_type == 3 and legacy.only == "CURP,INE" and legacy.type_flags == ["PEDIMENTO_ERP"]


def test_types_can_be_interleaved_with_options(monkeypatch, capsys, tmp_path: Path) -> None:
    for type_id in ("CURP", "INE"):
        (tmp_path / type_id).mkdir()
        (tmp_path / type_id / "doc.png").write_bytes(b"png")
    monkeypatch.setattr(suite.e2e, "estimate_plan", fake_plan)
    code, output = run_main(monkeypatch, capsys, "-r", str(tmp_path), "curp", "-n", "1", "ine", "-p")
    assert code == 0 and "2 tipo(s)" in output
    assert "CURP " in output and "INE " in output


def test_help_is_man_style(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        suite.build_parser().parse_args(["--help"])
    assert exit_info.value.code == 0
    text = capsys.readouterr().out
    for section in ("USO:", "DESCRIPCIÓN:", "ARGUMENTOS:", "OPCIONES:", "EJEMPLOS:", "ENTORNO:", "ESTADO DE SALIDA:"):
        assert section in text
    assert "pnpm test:e2e:suite TIPO -d DIR" in text and "usage:" not in text


def test_discover_selected_type_reads_only_its_folder(tmp_path: Path) -> None:
    (tmp_path / "PEDIMENTO_ERP").mkdir()
    (tmp_path / "PEDIMENTO_ERP" / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "PEDIMENTO_ERP" / "notas.txt").write_text("no soportado")
    (tmp_path / "CURP").mkdir()
    (tmp_path / "NO_ES_TIPO").mkdir()

    ready, pending, ignored = suite.discover(tmp_path, set(TYPES), None, None)
    assert [(t, [f.name for f in files]) for t, _, files in ready] == [("PEDIMENTO_ERP", ["a.pdf"])]
    assert pending == ["CURP"] and ignored == ["NO_ES_TIPO"]

    ready, pending, ignored = suite.discover(tmp_path, set(TYPES), {"PEDIMENTO_ERP", "INE"}, None)
    assert [t for t, _, _ in ready] == ["PEDIMENTO_ERP"]
    assert pending == ["INE"]  # pedido pero sin carpeta
    assert ignored == []       # con selección explícita no se reportan carpetas ajenas


@pytest.mark.parametrize("argv, message", [
    (["pedimento_er"], "¿quisiste decir PEDIMENTO_ERP?"),
    (["CURP", "INE", "-d", "."], "-d necesita exactamente un TIPO (recibí 2)"),
    (["-d", "."], "-d necesita exactamente un TIPO (recibí 0)"),
    (["--init", "CURP", "-d", "."], "-d no aplica con --init"),
])
def test_usage_errors_exit_2_before_touching_aws(monkeypatch, capsys, argv, message) -> None:
    monkeypatch.setattr(suite.e2e, "estimate_plan", no_plan)
    code, output = run_main(monkeypatch, capsys, *argv)
    assert code == 2
    assert message in output and "USO:" in output


def test_requested_type_without_files_exits_2(monkeypatch, capsys, tmp_path: Path) -> None:
    (tmp_path / "CURP").mkdir()
    monkeypatch.setattr(suite.e2e, "estimate_plan", no_plan)
    code, output = run_main(monkeypatch, capsys, "-r", str(tmp_path), "curp", "-p")
    assert code == 2 and "Sin archivos soportados" in output

    code, output = run_main(monkeypatch, capsys, "PEDIMENTO_ERP", "-d", str(tmp_path / "no-existe"), "-p")
    assert code == 2 and "No existe la carpeta" in output


def test_single_type_with_docs_plans_that_folder(monkeypatch, capsys, tmp_path: Path) -> None:
    dataset = tmp_path / "full-dataset"
    dataset.mkdir()
    (dataset / "gasto.xml").write_text("<cfdi/>")
    monkeypatch.setattr(suite.e2e, "estimate_plan", fake_plan)
    code, output = run_main(monkeypatch, capsys, "cfdi_gasto_tercero_xml", "-d", str(dataset), "-p",
                            "-r", str(tmp_path / "raiz-que-no-existe"))
    assert code == 0
    assert "1 tipo(s)" in output and str(dataset) in output
    assert "CFDI_GASTO_TERCERO_XML" in output and "$0.00" in output


def test_init_accepts_positional_types(monkeypatch, capsys, tmp_path: Path) -> None:
    code, output = run_main(monkeypatch, capsys, "--init", "-r", str(tmp_path), "curp", "pedimento_erp_xml")
    assert code == 0
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == ["CURP", "PEDIMENTO_ERP_XML"]
    assert "xml" in output  # PEDIMENTO_ERP_XML declara xpath
