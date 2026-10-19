"""Reusable, dependency-free terminal prompts for repository CLI tools.

Arrow menus require a TTY. Path prompts use readline Tab completion. All
prompts validate and retry in place; NO_COLOR disables ANSI styling.
"""
from __future__ import annotations

import getpass
import os
import select as io_select
import sys
import termios
import threading
import tty
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Sequence

try:
    import readline
except ImportError:  # pragma: no cover - readline is available on Linux/macOS
    readline = None


Validator = Callable[[str], str | None]


def path_error(value: str, *, kind: str = "any", must_exist: bool = False,
               must_not_exist: bool = False) -> str | None:
    """Return a user-facing validation message, or None for a usable path."""
    path = Path(value).expanduser()
    if must_exist and not path.exists():
        return "La ruta no existe. Usa Tab para completar una ruta existente."
    if kind == "file" and path.exists() and not path.is_file():
        return "Se esperaba un archivo."
    if kind == "directory" and path.exists() and not path.is_dir():
        return "Se esperaba una carpeta."
    if must_not_exist and path.exists():
        return "La ruta ya existe; el respaldo no sobrescribe archivos."
    if not must_exist and not path.parent.is_dir():
        return "La carpeta que contiene esta ruta no existe."
    return None


def path_matches(prefix: str) -> list[str]:
    """Complete path segments, including '~' and directory suffixes."""
    expanded = os.path.expanduser(prefix)
    parent, fragment = os.path.split(expanded)
    folder = Path(parent or ".")
    try:
        candidates = [item for item in folder.iterdir() if item.name.startswith(fragment)]
    except (OSError, PermissionError):
        return []
    results = []
    for item in sorted(candidates, key=lambda p: p.name.lower()):
        candidate = os.path.join(parent, item.name) if parent else item.name
        if prefix.startswith("~"):
            home = str(Path.home())
            if candidate.startswith(home):
                candidate = "~" + candidate[len(home):]
        results.append(candidate + ("/" if item.is_dir() else ""))
    return results


class TerminalUI:
    def __init__(self, *, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.color = self.stdout.isatty() and "NO_COLOR" not in os.environ

    def _style(self, value: str, code: str) -> str:
        return f"\x1b[{code}m{value}\x1b[0m" if self.color else value

    def write(self, value: str = "") -> None:
        print(value, file=self.stdout, flush=True)

    def heading(self, title: str) -> None:
        self.write("\n" + self._style("◆ " + title, "1;36"))

    def note(self, message: str) -> None:
        self.write(self._style("  ℹ " + message, "36"))

    def success(self, message: str) -> None:
        self.write(self._style("  ✓ " + message, "32"))

    def warning(self, message: str) -> None:
        self.write(self._style("  ! " + message, "33"))

    def error(self, message: str) -> None:
        self.write(self._style("  ✗ " + message, "31"))

    @contextmanager
    def _cbreak(self):
        fd = self.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _key(self) -> str:
        fd = self.stdin.fileno()
        first = os.read(fd, 1).decode("utf-8", errors="ignore")
        if first == "\x03":
            raise KeyboardInterrupt
        if first != "\x1b":
            return first
        sequence = ""
        for _ in range(2):
            ready, _, _ = io_select.select([fd], [], [], 0.06)
            if not ready:
                break
            sequence += os.read(fd, 1).decode("utf-8", errors="ignore")
        return {"[A": "up", "[B": "down"}.get(sequence, "escape")

    def select(self, label: str, options: Sequence[tuple[str, str]], *, default: str | None = None,
               hint: str | None = None) -> str:
        if not self.stdin.isatty() or not self.stdout.isatty():
            raise RuntimeError("El selector con flechas requiere una terminal interactiva")
        if not options:
            raise ValueError("El selector necesita opciones")
        values = [value for value, _ in options]
        position = values.index(default) if default in values else 0
        self.write(self._style("? " + label, "1;35"))
        self.note(hint or "↑/↓ para elegir · Enter para confirmar")

        def draw() -> None:
            for index, (_, title) in enumerate(options):
                line = ("  ❯ " if index == position else "    ") + title
                self.write(self._style(line, "1;36" if index == position else "2"))

        draw()
        with self._cbreak():
            while True:
                key = self._key()
                if key in ("\r", "\n"):
                    break
                if key in ("up", "k"):
                    position = (position - 1) % len(options)
                elif key in ("down", "j"):
                    position = (position + 1) % len(options)
                elif key == "escape":
                    raise KeyboardInterrupt
                elif key.isdigit() and 1 <= int(key) <= len(options):
                    position = int(key) - 1
                else:
                    continue
                self.stdout.write(f"\x1b[{len(options)}A\x1b[0J")
                self.stdout.flush()
                draw()
        self.success(f"{label}: {options[position][1]}")
        return values[position]

    def text(self, label: str, *, default: str | None = None, required: bool = True,
             validate: Validator | None = None, hint: str | None = None) -> str | None:
        if hint:
            self.note(hint)
        suffix = self._style(f" [{default}]", "2") if default else ""
        while True:
            # Readline treats ANSI escapes inside input()'s prompt as printable
            # characters on some terminals, leaving fragments such as [1;35m.
            # Render the styled label separately; keep the editable prompt plain.
            self.write(self._style("? " + label, "1;35") + suffix)
            value = input("  › ").strip() or default
            if not value:
                if required:
                    self.error("Este dato es obligatorio; vuelve a intentarlo.")
                    continue
                return None
            problem = validate(value) if validate else None
            if problem:
                self.error(problem)
                continue
            return value

    @contextmanager
    def _path_completion(self):
        if readline is None:
            yield
            return
        previous = readline.get_completer()
        delimiters = readline.get_completer_delims()
        readline.set_completer_delims("\t\n")
        cache: list[str] = []

        def complete(text: str, state: int) -> str | None:
            nonlocal cache
            if state == 0:
                cache = path_matches(text)
            return cache[state] if state < len(cache) else None

        readline.set_completer(complete)
        readline.parse_and_bind("tab: complete")
        try:
            yield
        finally:
            readline.set_completer(previous)
            readline.set_completer_delims(delimiters)

    def path(self, label: str, *, default: str | None = None, required: bool = True,
             kind: str = "any", must_exist: bool = False, must_not_exist: bool = False,
             validate: Validator | None = None, hint: str | None = None) -> str | None:
        self.note(hint or "Tab completa rutas · ~ representa tu carpeta personal")

        def check(value: str) -> str | None:
            return path_error(value, kind=kind, must_exist=must_exist,
                              must_not_exist=must_not_exist) or (validate(value) if validate else None)

        with self._path_completion():
            value = self.text(label, default=default, required=required, validate=check)
        return str(Path(value).expanduser().resolve()) if value else None

    def password(self, label: str, *, min_length: int = 0, repeat: bool = False) -> str:
        while True:
            self.write(self._style("? " + label + " (oculta)", "1;35"))
            value = getpass.getpass("  › ")
            if len(value) < min_length:
                self.error(f"Usa al menos {min_length} caracteres; vuelve a intentarlo.")
                continue
            if repeat:
                self.write(self._style("? Repítela", "1;35"))
                if getpass.getpass("  › ") != value:
                    self.error("No coincide; vuelve a intentarlo.")
                    continue
            return value

    def confirm_phrase(self, phrase: str) -> None:
        self.warning(f"Para continuar escribe exactamente: {phrase}")
        if self.text("Confirmación") != phrase:
            raise RuntimeError("Operación cancelada; no se escribió en AWS")

    @contextmanager
    def spinner(self, message: str):
        stop = threading.Event()
        status = {"error": None}

        def animate() -> None:
            frames = "◐◓◑◒"
            index = 0
            while not stop.wait(0.12):
                self.stdout.write("\r" + self._style(frames[index % len(frames)] + " " + message, "36"))
                self.stdout.flush()
                index += 1

        if self.stdout.isatty():
            self.stdout.write("\r" + self._style("◐ " + message, "36"))
            self.stdout.flush()
            thread = threading.Thread(target=animate, daemon=True)
            thread.start()
        else:
            thread = None
            self.write(message)
        try:
            yield
        except BaseException as error:
            status["error"] = error
            raise
        finally:
            stop.set()
            if thread:
                thread.join(timeout=1)
                self.stdout.write("\r\x1b[2K")
                self.stdout.flush()
            if status["error"] is None:
                self.success(message)
            else:
                self.error(f"{message}: {status['error']}")
