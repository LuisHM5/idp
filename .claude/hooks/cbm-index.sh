#!/usr/bin/env bash
# Claude Code hook: keep this repo indexed in codebase-memory-mcp so
# search_graph / trace_path / get_code_snippet always see current code.
#
#   cbm-index.sh session   SessionStart: index (full on first run, incremental
#                          after) and print one status line for the agent.
#   cbm-index.sh post      PostToolUse (Edit/Write/Bash): incremental reindex.
#
# The global `auto_index` setting only fires when a new MCP server starts, and
# the server's watcher does not pick up a project indexed after it started, so
# this hook does it explicitly. Reindexing runs detached and debounced: a dirty
# flag plus flock means bursts of edits collapse into one incremental pass
# (~30 ms when nothing changed, ~5 s / ~0.9 GB RSS for a full index).
# Never fails the session or the tool call: any problem exits 0 silently.
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$ROOT" 2>/dev/null || exit 0
BIN="$(command -v codebase-memory-mcp || echo "$HOME/.local/bin/codebase-memory-mcp")"
if [ ! -x "$BIN" ]; then
  [ "$1" = session ] && echo "codebase-memory-mcp not installed; use tgrep (fallback rg) for code search"
  exit 0
fi

PROJECT="$(printf '%s' "${ROOT#/}" | tr '/' '-')"
STATE="${XDG_RUNTIME_DIR:-/tmp}/cbm-index-$PROJECT"
touch "$STATE.dirty" 2>/dev/null || exit 0

# Detached worker: loop while edits keep arriving, only one worker per repo.
CBM_BIN="$BIN" CBM_ROOT="$ROOT" CBM_STATE="$STATE" setsid nohup flock -n "$STATE.lock" bash -c '
  while [ -e "$CBM_STATE.dirty" ]; do
    rm -f "$CBM_STATE.dirty"
    "$CBM_BIN" cli index_repository "{\"repo_path\":\"$CBM_ROOT\"}" >"$CBM_STATE.log" 2>&1
  done
' >/dev/null 2>&1 </dev/null &
disown 2>/dev/null

[ "$1" = session ] || exit 0

status="$("$BIN" cli index_status "{\"project\":\"$PROJECT\"}" 2>/dev/null)"
if [[ $status == *'"status":"ready"'* ]]; then
  [[ $status =~ \"nodes\":([0-9]+) ]] && nodes=${BASH_REMATCH[1]}
  echo "codebase-memory-mcp: project $PROJECT indexed ($nodes nodes, incremental sync running). Query it FIRST for code search: search_graph / trace_path / get_code_snippet / search_code; then tgrep for plain text."
else
  echo "codebase-memory-mcp: indexing $PROJECT in background (~5 s first time). Check index_status before querying; meanwhile use tgrep."
fi
exit 0
