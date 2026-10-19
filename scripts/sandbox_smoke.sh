#!/usr/bin/env bash
# Live smoke of context-guard in Claude Code, Codex and Cursor with your real logins.
#
# Only the hook and config wiring is sandboxed: each runtime is installed with
# `cli.py install --home "$SANDBOX"` and its hooks run with CONTEXT_GUARD_HOME="$SANDBOX", so
# state, events and handoffs land in $SANDBOX/.context-guard. A current kit installed in your real
# home stays inert while CONTEXT_GUARD_HOME names the sandbox (an older one still fires; reinstall
# it). Tiny thresholds make the note tier fire in a two-prompt chat; the second prompt makes one
# tool call so a held note can reach the model. A run fails unless a note fired and was delivered.
# Spends cheap model calls: Claude 3 (2 + plugin), Codex 2, Cursor 2. The runtimes keep their
# session records and logins under your real home, so Cursor hooks read CLI chats from
# ~/.cursor/chats. Cursor runs one interactive `cursor-agent` session through
# scripts/cursor_interactive.exp (needs expect); `cursor-agent -p` fires no beforeSubmitPrompt or stop.
#
# Usage: scripts/sandbox_smoke.sh [--only claude|codex|cursor] [--dry-run]
#   --dry-run       print every command without installing or calling a model
#   KEEP=1          keep the temporary sandbox after the run
#   SANDBOX=<dir>   reuse a persistent sandbox (never deleted), e.g. after the Codex hook review
#   SMOKE_PYTHON=<path>  interpreter for cli.py (default: uv run --python 3.14 python)
#   CLAUDE_USE_API_KEY=1  keep ANTHROPIC_API_KEY for Claude (default: unset, use the login)
#   CLAUDE_MODEL, CODEX_MODEL, CODEX_EFFORT, CURSOR_MODEL override the cheap defaults
# shellcheck disable=SC2329  # cleanup and claude_agents run through trap and run()
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
CLAUDE_MODEL=${CLAUDE_MODEL:-haiku}
CODEX_MODEL=${CODEX_MODEL:-gpt-6-luna}
CODEX_EFFORT=${CODEX_EFFORT:-low}
CURSOR_MODEL=${CURSOR_MODEL:-composer-2.5}
NOTE_CAP=1000
REFRESH_TOKENS=1000
FIRST_PROMPT="Use your file-reading tool once to read README.md, then reply with only the word ok. Follow any context-guard instructions you receive."
NEXT_PROMPT="Use your file-reading tool once to read README.md again, then reply with only the word ok."

ONLY="" DRY_RUN=0 KEEP=${KEEP:-0} FAILED=0
while (($#)); do
    case $1 in
        --only) [[ $# -ge 2 ]] || { echo "--only needs a runtime" >&2; exit 2; }; ONLY=$2; shift 2 ;;
        --only=*) ONLY=${1#*=}; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,21s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
case $ONLY in ""|claude|codex|cursor) ;; *) echo "--only takes claude, codex or cursor" >&2; exit 2 ;; esac

if [[ -n ${SMOKE_PYTHON:-} ]]; then PY=("$SMOKE_PYTHON"); else PY=(uv run --python 3.14 python); fi
if [[ -n ${SANDBOX:-} ]]; then
    OWNED=0
    mkdir -p "$SANDBOX"
else
    OWNED=1
    SANDBOX=$(mktemp -d "${TMPDIR:-/tmp}/context-guard-smoke.XXXXXX")
fi
SANDBOX=$(cd "$SANDBOX" && pwd -P)
WORK=$SANDBOX/work
LOGS=$SANDBOX/logs
STORE=$SANDBOX/.context-guard/handoffs
ROWS=()

cleanup() {
    if [[ $OWNED == 1 && $KEEP != 1 ]]; then
        rm -rf "$SANDBOX"
    else
        echo "Sandbox kept: $SANDBOX"
    fi
}
trap cleanup EXIT

export CONTEXT_GUARD_HOME=$SANDBOX
export CONTEXT_GUARD_THRESHOLDS_NOTE_CAP=$NOTE_CAP
export CONTEXT_GUARD_HANDOFF_REFRESH_REFRESH_TOKENS=$REFRESH_TOKENS
export CONTEXT_GUARD_HANDOFF_REFRESH_MIN_SECONDS=0
export CONTEXT_GUARD_TELEMETRY_CLI_CHATS=$HOME/.cursor/chats
SETTINGS=(--set "thresholds.note_cap=$NOTE_CAP" --set "handoff_refresh.refresh_tokens=$REFRESH_TOKENS"
          --set "handoff_refresh.min_seconds=0")

show() { printf '+'; printf ' %q' "$@"; printf '\n'; }
run() { show "$@"; ((DRY_RUN)) || "$@"; }
model() {
    local dir=$1 log=$2
    shift 2
    show cd "$dir"
    show "$@"
    ((DRY_RUN)) && return 0
    (cd "$dir" && "$@" >"$log" 2>&1 </dev/null)
}
record() { ROWS+=("$1|$2|$3"); }
wanted() { [[ -z $ONLY || $ONLY == "$1" ]]; }
events_of() { echo "$1/.context-guard/state/$2/events.jsonl"; }

prepare() {
    run mkdir -p "$WORK" "$LOGS" "$STORE"
    ((DRY_RUN)) && return 0
    [[ -d $WORK/.git ]] || git init -q "$WORK"
    printf '# Sandbox\nA tiny project for the context-guard live smoke.\n' | tee "$WORK/README.md" >"$SANDBOX/README.md"
}

install_runtime() {
    run "${PY[@]}" "$ROOT/cli.py" install --runtime "$1" --home "$SANDBOX" "${SETTINGS[@]}"
}

event_count() {
    local count
    count=$(grep -c "\"event\": *\"$2\"" "$1" 2>/dev/null) || true
    echo "${count:-0}"
}

events_status() {
    local events=$1
    if [[ $(event_count "$events" prompt) == 0 ]]; then
        echo "failed: no hook events"
    elif [[ $(event_count "$events" note) == 0 ]]; then
        echo "failed: no note"
    elif [[ $(event_count "$events" note_delivered) == 0 ]]; then
        echo "failed: note never delivered"
    else
        echo ran
    fi
}

finish() {
    local runtime=$1 home=$2 status=$3 events
    events=$(events_of "$home" "${runtime%-*}")
    if ((DRY_RUN)); then
        status="dry run"
    elif [[ $status == ran ]]; then
        status=$(events_status "$events")
    fi
    [[ $status == failed* ]] && FAILED=1
    record "$runtime" "$status" "$events"
}

claude_agents() {
    "${PY[@]}" - "$SANDBOX/.claude/agents" "$SANDBOX/claude-agents.json" <<'PY'
import json, pathlib, sys
agents = {}
for path in sorted(pathlib.Path(sys.argv[1]).glob("*.md")):
    _, front, body = path.read_text().split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        meta[key.strip()] = json.loads(value) if value.startswith('"') else value
    agents[meta["name"]] = {"description": meta.get("description", ""), "prompt": body.strip(),
                            **({"model": meta["model"]} if meta.get("model") else {})}
pathlib.Path(sys.argv[2]).write_text(json.dumps(agents))
PY
}

claude_status() {
    if grep -q -e '"authentication_failed"' -e '"api_error_status": *401' "$1" 2>/dev/null; then
        echo "failed: Claude auth (run claude, then /login)"
    else
        echo ran
    fi
}

smoke_claude() {
    if ! command -v claude >/dev/null; then record claude "skipped: claude not on PATH" ""; return; fi
    install_runtime claude
    run claude_agents
    local auth=(env -u ANTHROPIC_API_KEY)
    [[ ${CLAUDE_USE_API_KEY:-0} == 1 ]] && auth=(env)
    local base=("${auth[@]}" claude -p --model "$CLAUDE_MODEL" --setting-sources "project,local" --permission-mode acceptEdits
                --output-format stream-json --verbose --include-hook-events)
    local kit=("${base[@]}" --add-dir "$STORE" --settings "$SANDBOX/.claude/settings.json" --agents "$SANDBOX/claude-agents.json"
               --append-system-prompt-file "$SANDBOX/.claude/rules/context-guard.md")
    local status=ran
    model "$WORK" "$LOGS/claude-1.jsonl" "${kit[@]}" "$FIRST_PROMPT" || status="failed: claude exited non-zero"
    [[ $status == ran ]] && status=$(claude_status "$LOGS/claude-1.jsonl")
    if [[ $status == ran ]]; then
        model "$WORK" "$LOGS/claude-2.jsonl" "${kit[@]}" --continue "$NEXT_PROMPT" \
            || status="failed: claude --continue exited non-zero"
    fi
    finish claude "$SANDBOX" "$status"
    smoke_claude_plugin "${base[@]}"
}

smoke_claude_plugin() {
    local home=$SANDBOX/plugin-home status=ran
    run mkdir -p "$home/.context-guard/handoffs"
    run "${PY[@]}" "$ROOT/cli.py" package --runtime claude --out "$SANDBOX/pkg"
    model "$WORK" "$LOGS/claude-plugin.jsonl" env CONTEXT_GUARD_HOME="$home" "$@" \
        --add-dir "$home/.context-guard/handoffs" --plugin-dir "$SANDBOX/pkg/plugins/context-guard" \
        "$FIRST_PROMPT" || status="failed: claude exited non-zero"
    [[ $status == ran ]] && status=$(claude_status "$LOGS/claude-plugin.jsonl")
    finish claude-plugin "$home" "$status"
}

codex_trust_help() {
    KEEP=1
    cat <<EOF
Codex needs a one-time review of the sandbox hooks; the trust is stored in $SANDBOX/.codex only.
  CODEX_HOME="$SANDBOX/.codex" codex -C "$WORK"
Choose "Trust all and continue" when Codex lists the context-guard hooks, quit, then rerun:
  SANDBOX="$SANDBOX" $0 --only codex
EOF
}

smoke_codex() {
    if ! command -v codex >/dev/null; then record codex "skipped: codex not on PATH" ""; return; fi
    local real=${CODEX_HOME:-$HOME/.codex}
    if ((!DRY_RUN)) && [[ ! -f $real/auth.json ]]; then
        record codex "skipped: no $real/auth.json (run codex login)" ""
        return
    fi
    run mkdir -p "$SANDBOX/.codex"
    run ln -sfn "$real/auth.json" "$SANDBOX/.codex/auth.json"
    install_runtime codex
    local flags=(--json --skip-git-repo-check -m "$CODEX_MODEL" -c "model_reasoning_effort=\"$CODEX_EFFORT\""
                 -c 'sandbox_mode="workspace-write"' -c "sandbox_workspace_write.writable_roots=[\"$STORE\"]")
    local status=ran
    model "$WORK" "$LOGS/codex-1.jsonl" env CODEX_HOME="$SANDBOX/.codex" codex exec "${flags[@]}" \
        -C "$WORK" "$FIRST_PROMPT" || status="failed: codex exec exited non-zero"
    if [[ $status == ran ]]; then
        model "$WORK" "$LOGS/codex-2.jsonl" env CODEX_HOME="$SANDBOX/.codex" codex exec resume --last \
            "${flags[@]}" "$NEXT_PROMPT" || status="failed: codex exec resume exited non-zero"
    fi
    if [[ $status == ran ]] && ! ((DRY_RUN)) && ! grep -q '"event": *"prompt"' "$(events_of "$SANDBOX" codex)" 2>/dev/null; then
        codex_trust_help
        record codex "needs hook trust (no hook events)" "$(events_of "$SANDBOX" codex)"
        return
    fi
    finish codex "$SANDBOX" "$status"
}

cursor_status() {
    case $1 in
        0) echo ran ;;
        3) echo "failed: cursor-agent timed out (see the session log)" ;;
        4) echo "failed: cursor-agent exited early (see the session log)" ;;
        *) echo "failed: cursor driver exited $1" ;;
    esac
}

smoke_cursor() {
    if ! command -v cursor-agent >/dev/null; then record cursor "skipped: cursor-agent not on PATH" ""; return; fi
    if ((!DRY_RUN)) && ! command -v expect >/dev/null; then record cursor "skipped: expect not on PATH" ""; return; fi
    if ((!DRY_RUN)) && [[ $(cursor-agent status 2>&1 || true) == *[Nn]"ot logged in"* ]]; then
        record cursor "skipped: cursor-agent not logged in (run cursor-agent login)" ""
        return
    fi
    install_runtime cursor
    local code=0
    model "$SANDBOX" "$LOGS/cursor.log" expect -f "$ROOT/scripts/cursor_interactive.exp" "$FIRST_PROMPT" "$NEXT_PROMPT" \
        -- cursor-agent --trust --workspace "$SANDBOX" --model "$CURSOR_MODEL" || code=$?
    finish cursor "$SANDBOX" "$(cursor_status "$code")"
}

summary() {
    "${PY[@]}" - "$@" <<'PY'
import collections, json, sys

columns = ("prompt", "note", "note_delivered", "handoff_written", "refresh", "unwritten", "block")
rows = [("runtime", "result", *columns)]
for row in sys.argv[1:]:
    runtime, result, path = row.split("|", 2)
    counts = collections.Counter()
    try:
        lines = open(path).read().splitlines() if path else []
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        name = entry.get("event")
        if name == "handoff_requested":
            name = (entry.get("detail") or {}).get("source")
        counts[name] += 1
    rows.append((runtime, result, *(str(counts[column]) for column in columns)))
widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
for row in rows:
    print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
PY
}

prepare
wanted claude && smoke_claude
wanted codex && smoke_codex
wanted cursor && smoke_cursor

echo
echo "context-guard sandbox smoke ($SANDBOX)"
summary ${ROWS[@]+"${ROWS[@]}"}
((DRY_RUN)) || echo "Model output logs: $LOGS; events: $SANDBOX/.context-guard/state/<runtime>/events.jsonl"
exit "$FAILED"
