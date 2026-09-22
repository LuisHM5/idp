#!/usr/bin/env bash
# Claude Code SessionStart hook: make sure the tgrep search server is running
# for this repo. Builds .tgrep/ on first run and keeps it updated via a file
# watcher, so nobody has to run `tgrep index` or `tgrep serve` by hand.
# Never fails the session: any problem exits 0 silently.
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$ROOT" 2>/dev/null || exit 0
command -v tgrep >/dev/null 2>&1 || { echo "tgrep not installed; falling back to rg/grep"; exit 0; }

status="$(tgrep status . 2>/dev/null)"
if printf '%s' "$status" | grep -q '^  PID:'; then
  pid=$(printf '%s' "$status" | awk '/^  PID:/{print $2}')
  files=$(printf '%s' "$status" | awk '/^  Files:/{print $2}')
  echo "tgrep server already running (PID $pid, $files files indexed). Use: tgrep -- \"pattern\" ."
  exit 0
fi

# Stale serve.json (server died) or no index at all: start a detached server.
mkdir -p .tgrep
setsid nohup tgrep serve . >.tgrep/serve.log 2>&1 </dev/null &
disown 2>/dev/null
# Give it a moment so the first searches of the session hit the index.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 0.3
  if tgrep status . 2>/dev/null | grep -q '^  PID:'; then
    echo "tgrep server started for $ROOT (index in .tgrep/, watcher active). Use: tgrep -- \"pattern\" ."
    exit 0
  fi
done
echo "tgrep server starting in background (log: .tgrep/serve.log). Use: tgrep -- \"pattern\" ."
exit 0
