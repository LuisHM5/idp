"""Reusable terminal UI behavior independent from AWS migration."""
from __future__ import annotations

import io
import sys
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from terminal_ui import TerminalUI, path_error, path_matches  # noqa: E402


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


def test_arrow_menu_moves_and_returns_selected_value(monkeypatch):
    output = TtyBuffer()
    ui = TerminalUI(stdin=TtyBuffer(), stdout=output)
    keys = iter(["down", "\n"])
    monkeypatch.setattr(ui, "_cbreak", nullcontext)
    monkeypatch.setattr(ui, "_key", lambda: next(keys))
    assert ui.select("Ambiente", [("both", "Ambos"), ("prod", "Producción")]) == "prod"
    assert "Producción" in output.getvalue()


def test_path_tab_candidates_and_retry(tmp_path, monkeypatch):
    folder = tmp_path / "respaldos"
    folder.mkdir()
    target = folder / "archivo.enc"
    target.write_text("cifrado")
    assert str(folder) + "/" in path_matches(str(tmp_path / "res"))
    assert str(target) in path_matches(str(folder / "arc"))
    assert path_error(str(tmp_path / "no-existe.enc"), must_exist=True)

    answers = iter([str(tmp_path / "no-existe.enc"), str(target)])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    ui = TerminalUI(stdin=TtyBuffer(), stdout=TtyBuffer())
    assert ui.path("Archivo", kind="file", must_exist=True) == str(target)


def test_input_prompts_keep_ansi_outside_readline(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TtyBuffer()
    ui = TerminalUI(stdin=TtyBuffer(), stdout=output)
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", answer)
    assert ui.text("Región del stack", default="us-east-1") == "us-east-1"
    assert prompts == ["  › "]
    assert "\x1b[1;35m? Región del stack\x1b[0m" in output.getvalue()
