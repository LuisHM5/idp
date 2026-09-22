#!/usr/bin/env python3
"""Claude Code PreToolUse hook: route searches and substitutions to the
repo's preferred tools.

  grep -r / grep <files> / xargs grep  ->  tgrep   (fallback: rg)
  rg (as a file search)                ->  tgrep   (allowed when tgrep is missing)
  sed                                  ->  sd      (allowed when sd is missing)
  built-in Grep tool                   ->  tgrep   (allowed when tgrep is missing)

Pipe filters (`cmd | grep x`, `cmd | rg x`) are allowed: tgrep ignores stdin.
Before any search it makes sure the tgrep server is up (auto-index).
`SEARCH_FALLBACK=1 rg ...` is the escape hatch for when tgrep itself misbehaves.

Never fails the tool call on its own errors: anything unexpected exits 0.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
# codebase-memory-mcp project name: repo path without the leading slash, "/" -> "-".
CBM_PROJECT = ROOT.lstrip("/").replace("/", "-")
EXTRA_PATH = [os.path.expanduser("~/.cargo/bin"), os.path.expanduser("~/.local/bin")]

GREP_CMDS = {"grep", "egrep", "fgrep"}
SED_CMDS = {"sed", "gsed"}
SHELLS = {"bash", "sh", "zsh", "fish", "dash"}
# Wrappers whose first non-option word is the real command.
WRAPPERS = {"sudo", "time", "command", "builtin", "exec", "nice", "nohup", "env", "stdbuf", "ionice", "doas"}
# xargs options that take a separate argument.
XARGS_ARG_OPTS = {"-I", "-i", "-n", "-P", "-d", "-L", "-l", "-s", "-E", "-e", "-a", "--delimiter",
                  "--max-args", "--max-procs", "--max-lines", "--arg-file", "--replace"}
SEPARATORS = {"|", "|&", "||", "&&", ";", "&", ";;", "(", ")", "\n"}
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def which(name):
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_PATH:
        cand = os.path.join(d, name)
        if os.access(cand, os.X_OK):
            return cand
    return None


def tgrep_server_alive():
    try:
        with open(os.path.join(ROOT, ".tgrep", "serve.json")) as f:
            pid = int(json.load(f)["pid"])
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def ensure_tgrep_server():
    """Auto-index: restart the tgrep server if it died mid-session."""
    if tgrep_server_alive():
        return
    script = os.path.join(ROOT, ".claude", "hooks", "tgrep-ensure.sh")
    if os.access(script, os.X_OK):
        try:
            subprocess.run([script], cwd=ROOT, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=8)
        except Exception:
            pass


def strip_heredocs(command):
    """Drop heredoc bodies so file contents written via `cat <<EOF` are not parsed as commands."""
    out, lines, i = [], command.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        terminators = [m.group(2) for m in HEREDOC_RE.finditer(line)]
        i += 1
        for term in terminators:
            while i < len(lines) and lines[i].strip() != term:
                i += 1
            i += 1  # skip the terminator line itself
    return "\n".join(out)


def tokenize(command):
    text = strip_heredocs(command).replace("\\\n", " ").replace("\n", " ; ")
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = "#"
    return list(lex)


def segments(tokens):
    """Yield (words, piped_in) for each simple command."""
    words, piped = [], False
    for tok in tokens:
        if tok in SEPARATORS:
            if words:
                yield words, piped
            words, piped = [], tok in ("|", "|&")
        else:
            words.append(tok)
    if words:
        yield words, piped


def base(word):
    return os.path.basename(word.lstrip("\\`$(")).rstrip("`")


def unwrap(words):
    """Return (command, args, env, via_xargs) after skipping env vars and wrappers."""
    env, i, via_xargs = {}, 0, False
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            k, _, v = w.partition("=")
            env[k] = v
            i += 1
            continue
        name = base(w)
        if name in WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            continue
        if name == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            i += 1  # duration
            continue
        if name == "xargs":
            via_xargs = True
            i += 1
            while i < len(words) and words[i].startswith("-"):
                opt = words[i]
                i += 2 if opt in XARGS_ARG_OPTS else 1
            continue
        return name, words[i + 1:], env, via_xargs
    return None, [], env, via_xargs


def exec_targets(args):
    """Commands run by `find ... -exec CMD ...`."""
    return [base(args[j + 1]) for j, a in enumerate(args[:-1]) if a in ("-exec", "-execdir", "-ok")]


def has_stdin_redirect(words):
    return "<" in words or "<<<" in words


GREP_MSG = (
    "Use tgrep (indexed, ripgrep flags) instead of {what} for code search in this repo:\n"
    '  tgrep -F -n -- "literal" .                      # literal, whole repo\n'
    '  tgrep -n -g "src/**" -- "regex" .               # scope with -g, keep PATH = .\n'
    '  tgrep -F -l -t ts -- "Symbol" .                 # files only, by type\n'
    "For symbols, callers, routes or call chains, query codebase-memory-mcp first "
    "(search_graph / trace_path / get_code_snippet, project=" + CBM_PROJECT + "). "
    "Pipe filters like `cmd | grep x` are fine. If tgrep itself fails, rerun as `SEARCH_FALLBACK=1 rg ...`."
)
GREP_NO_TGREP_MSG = "tgrep is not installed; use rg instead of grep for code search (same flags: rg -n -- \"pattern\" .)."
SED_MSG = (
    "Use sd instead of sed in this repo (faster in-place, Rust regex syntax, no s/// delimiters, "
    "capture groups are $1 not \\1, edits in place by default):\n"
    "  sd -F 'old literal' 'new literal' path/file.py              # literal replace, in place\n"
    "  sd 'find(One|By)\\(' 'lookup$1(' file.ts                     # regex, $1 = capture group\n"
    "  sd -p 'old' 'new' file.ts                                   # preview only, no write\n"
    "  tgrep -F -l -- 'old' . | xargs sd -F 'old' 'new'            # repo-wide\n"
    "  cmd | sd 'old' 'new'                                        # as a stream filter\n"
    "To view line ranges use the Read tool (offset/limit) or head/tail instead of `sed -n 'X,Yp'`. "
    "For single edits in one file prefer the Edit tool."
)


def check_command(command, depth=0):
    """Return a deny reason, or None to allow."""
    if depth > 2:
        return None
    try:
        tokens = tokenize(command)
    except ValueError:
        return None  # unbalanced quotes etc.: let the shell complain
    have_tgrep = which("tgrep") is not None
    have_sd = which("sd") is not None
    for words, piped in segments(tokens):
        name, args, env, via_xargs = unwrap(words)
        if name is None or "--version" in args or "--help" in args:
            continue
        if name in SHELLS and "-c" in args:
            idx = args.index("-c")
            if idx + 1 < len(args):
                reason = check_command(args[idx + 1], depth + 1)
                if reason:
                    return reason
            continue
        targets = [(name, piped and not via_xargs)]
        targets += [(t, False) for t in exec_targets(args)]
        for cmd, is_filter in targets:
            if cmd == "tgrep":
                ensure_tgrep_server()
            elif cmd in GREP_CMDS and not is_filter and not has_stdin_redirect(words):
                if have_tgrep:
                    ensure_tgrep_server()
                    return GREP_MSG.format(what="`%s`" % cmd)
                return GREP_NO_TGREP_MSG
            elif cmd == "rg" and not is_filter and have_tgrep and env.get("SEARCH_FALLBACK") != "1":
                ensure_tgrep_server()
                return GREP_MSG.format(what="`rg` (rg is only the fallback)")
            elif cmd in SED_CMDS and have_sd:
                return SED_MSG
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    reason = None
    if tool == "Bash":
        reason = check_command(tool_input.get("command") or "")
    elif tool == "Grep" and which("tgrep"):
        ensure_tgrep_server()
        reason = GREP_MSG.format(what="the built-in Grep tool")
    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
