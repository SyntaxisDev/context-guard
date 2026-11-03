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
# The terminal shows one line per step; every command and its output go to a run log in $TMPDIR.
#
# Usage: scripts/sandbox_smoke.sh [--only claude|codex|cursor] [--dry-run] [-v]
#   --dry-run       print every command without installing or calling a model
#   -v, --verbose   also print every command and its output (or VERBOSE=1)
#   KEEP=1          keep the temporary sandbox after the run
#   SANDBOX=<dir>   reuse a persistent sandbox (never deleted), e.g. after the Codex hook review
#   SMOKE_PYTHON=<path>  interpreter for cli.py (default: uv run --python 3.14 python)
#   CLAUDE_USE_API_KEY=1  keep ANTHROPIC_API_KEY for Claude (default: unset, use the login)
#   CLAUDE_MODEL, CODEX_MODEL, CODEX_EFFORT, CURSOR_MODEL override the cheap defaults
#   NO_COLOR=1      plain ASCII output even on a terminal
# shellcheck disable=SC2329  # cleanup and claude_agents run through trap and ui_run
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=scripts/lib/ui.sh
source "$ROOT/scripts/lib/ui.sh"
CLAUDE_MODEL=${CLAUDE_MODEL:-haiku}
CODEX_MODEL=${CODEX_MODEL:-gpt-6-luna}
CODEX_EFFORT=${CODEX_EFFORT:-low}
CURSOR_MODEL=${CURSOR_MODEL:-composer-2.5}
NOTE_CAP=1000
REFRESH_TOKENS=1000
FIRST_PROMPT="Use your file-reading tool once to read README.md, then reply with only the word ok. Follow any context-guard instructions you receive."
NEXT_PROMPT="Use your file-reading tool once to read README.md again, then reply with only the word ok."

ONLY="" DRY_RUN=0 KEEP=${KEEP:-0} FAILED=0 TRUST=0
case ${VERBOSE:-0} in ""|0) VERBOSE=0 ;; *) VERBOSE=1 ;; esac
while (($#)); do
    case $1 in
        --only) [[ $# -ge 2 ]] || { echo "--only needs a runtime" >&2; exit 2; }; ONLY=$2; shift 2 ;;
        --only=*) ONLY=${1#*=}; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -v|--verbose) VERBOSE=1; shift ;;
        -h|--help) sed -n '2,24s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
case $ONLY in ""|claude|codex|cursor) ;; *) echo "--only takes claude, codex or cursor" >&2; exit 2 ;; esac

if [[ -n ${SMOKE_PYTHON:-} ]]; then PY=("$SMOKE_PYTHON"); else PY=(uv run --python 3.14 python); fi
if ((DRY_RUN)); then RUN_LOG=/dev/null; else RUN_LOG=$(mktemp "${TMPDIR:-/tmp}/context-guard-smoke.log.XXXXXX"); fi
ui_init "$RUN_LOG" 13
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

cleanup() {
    ui_footer
    if [[ $OWNED == 1 && $KEEP != 1 ]]; then
        rm -rf "$SANDBOX"
    else
        printf 'Sandbox kept: %s\n' "$SANDBOX"
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
CLAUDE_AUTH=(env -u ANTHROPIC_API_KEY)
if [[ ${CLAUDE_USE_API_KEY:-0} == 1 ]]; then CLAUDE_AUTH=(env); fi
CLAUDE_OPTIONS=(--model "$CLAUDE_MODEL" --setting-sources "project,local" --permission-mode acceptEdits
                --output-format stream-json --verbose --include-hook-events)

wanted() { [[ -z $ONLY || $ONLY == "$1" ]]; }
present() { command -v "$1" >/dev/null; }
events_of() { echo "$1/.context-guard/state/$2/events.jsonl"; }

prepare() {
    ui_start Setup "preparing the sandbox"
    ui_run mkdir -p "$WORK" "$LOGS" "$STORE"
    if ! ((DRY_RUN)); then
        [[ -d $WORK/.git ]] || ui_run git init -q "$WORK"
        printf '# Sandbox\nA tiny project for the context-guard live smoke.\n' | tee "$WORK/README.md" >"$SANDBOX/README.md"
    fi
    ui_done ok "sandbox ready at $SANDBOX${UI_SEP}note after $NOTE_CAP tokens"
}

install_runtime() {
    ui_run env -u CODEX_HOME -u CLAUDE_CONFIG_DIR \
        "${PY[@]}" "$ROOT/cli.py" install --runtime "$1" --home "$SANDBOX" "${SETTINGS[@]}"
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

claude_auth() {
    if grep -q -e '"authentication_failed"' -e '"api_error_status": *401' "$1" 2>/dev/null; then
        echo "fail:Claude is not logged in"
    else
        echo ran
    fi
}

smoke_claude() {
    if ! present claude; then smoke_skip Claude "claude not on PATH"; return; fi
    local kit=(--add-dir "$STORE" --settings "$SANDBOX/.claude/settings.json" --agents "$SANDBOX/claude-agents.json"
               --append-system-prompt-file "$SANDBOX/.claude/rules/context-guard.md")
    local status=ran log=$LOGS/claude-1.jsonl
    ui_start Claude "running smoke (2 prompts)" long
    install_runtime claude
    ui_run claude_agents
    ui_model "$WORK" "$log" "${CLAUDE_AUTH[@]}" claude -p "$FIRST_PROMPT" "${CLAUDE_OPTIONS[@]}" "${kit[@]}" \
        || status="fail:claude exited $?"
    if [[ $status == ran ]]; then status=$(claude_auth "$log"); fi
    if [[ $status == ran ]]; then
        log=$LOGS/claude-2.jsonl
        ui_model "$WORK" "$log" "${CLAUDE_AUTH[@]}" claude -p "$NEXT_PROMPT" --continue "${CLAUDE_OPTIONS[@]}" "${kit[@]}" \
            || status="fail:claude --continue exited $?"
    fi
    smoke_report claude "$status" "$(events_of "$SANDBOX" claude)" 0 "$log"
    smoke_claude_plugin
}

smoke_claude_plugin() {
    local home=$SANDBOX/plugin-home status=ran log=$LOGS/claude-plugin.jsonl
    ui_start "Claude plugin" "running plugin smoke (1 prompt)" long
    ui_run mkdir -p "$home/.context-guard/handoffs"
    ui_run "${PY[@]}" "$ROOT/cli.py" package --runtime claude --out "$SANDBOX/pkg"
    ui_model "$WORK" "$log" env CONTEXT_GUARD_HOME="$home" "${CLAUDE_AUTH[@]}" claude -p "$FIRST_PROMPT" "${CLAUDE_OPTIONS[@]}" \
        --add-dir "$home/.context-guard/handoffs" --plugin-dir "$SANDBOX/pkg/claude/plugins/context-guard" \
        || status="fail:claude exited $?"
    if [[ $status == ran ]]; then status=$(claude_auth "$log"); fi
    smoke_report claude-plugin "$status" "$(events_of "$home" claude)" 0 "$log"
}

smoke_codex() {
    if ! present codex; then smoke_skip Codex "codex not on PATH"; return; fi
    local real=${CODEX_HOME:-$HOME/.codex}
    if ((!DRY_RUN)) && [[ ! -f $real/auth.json ]]; then smoke_skip Codex "no $real/auth.json (run codex login)"; return; fi
    local flags=(--json --skip-git-repo-check -m "$CODEX_MODEL" -c "model_reasoning_effort=\"$CODEX_EFFORT\""
                 -c 'sandbox_mode="workspace-write"' -c "sandbox_workspace_write.writable_roots=[\"$STORE\"]")
    local status=ran log=$LOGS/codex-1.jsonl events hint=""
    events=$(events_of "$SANDBOX" codex)
    ui_start Codex "running smoke (2 prompts)" long
    ui_run mkdir -p "$SANDBOX/.codex"
    ui_run ln -sfn "$real/auth.json" "$SANDBOX/.codex/auth.json"
    install_runtime codex
    ui_model "$WORK" "$log" env CODEX_HOME="$SANDBOX/.codex" codex exec "${flags[@]}" -C "$WORK" "$FIRST_PROMPT" \
        || status="fail:codex exec exited $?"
    if [[ $status == ran ]]; then
        log=$LOGS/codex-2.jsonl
        ui_model "$WORK" "$log" env CODEX_HOME="$SANDBOX/.codex" codex exec resume --last "${flags[@]}" "$NEXT_PROMPT" \
            || status="fail:codex exec resume exited $?"
    fi
    if [[ $status == ran ]] && ! ((DRY_RUN)) && [[ $(smoke_count "$events" 0 prompt) == 0 ]]; then
        KEEP=1 TRUST=1
        status="warn:needs hook trust${UI_SEP}no hook events logged"
        hint="trust the hooks: open codex on the sandbox, run /hooks, then rerun (see Next steps)"
    fi
    smoke_report codex "$status" "$events" 0 "$log" "$hint"
}

smoke_cursor() {
    if ! present cursor-agent; then smoke_skip Cursor "cursor-agent not on PATH"; return; fi
    if ((!DRY_RUN)) && ! present expect; then smoke_skip Cursor "expect not on PATH"; return; fi
    if ((!DRY_RUN)); then
        local login
        login=$(cursor-agent status 2>&1 </dev/null || true)
        printf '$ cursor-agent status\n%s\n' "$login" >>"$UI_LOG"
        if [[ $login == *[Nn]"ot logged in"* ]]; then smoke_skip Cursor "cursor-agent not logged in (run cursor-agent login)"; return; fi
    fi
    local code=0 log=$LOGS/cursor.log
    ui_start Cursor "running smoke (2 prompts)" long
    install_runtime cursor
    ui_model "$SANDBOX" "$log" expect -f "$ROOT/scripts/cursor_interactive.exp" "$FIRST_PROMPT" "$NEXT_PROMPT" \
        -- cursor-agent --trust --workspace "$SANDBOX" --model "$CURSOR_MODEL" || code=$?
    smoke_report cursor "$(smoke_cursor_reason "$code" "$log")" "$(events_of "$SANDBOX" cursor)" 0 "$log"
}

trust_steps() {
    ui_section "Next steps"
    printf '  1. Review the sandbox hooks once (the trust is stored in %s only):\n' "$SANDBOX/.codex"
    printf '       CODEX_HOME="%s" codex -C "%s"\n' "$SANDBOX/.codex" "$WORK"
    printf '     choose "Trust all and continue" when Codex lists the context-guard hooks, then quit\n'
    printf '  2. Rerun: SANDBOX="%s" %s --only codex\n' "$SANDBOX" "$0"
}

ui_title "sandbox smoke"
ui_section "Live smoke (2 cheap prompts per tool)"
prepare
if wanted claude; then smoke_claude; fi
if wanted codex; then smoke_codex; fi
if wanted cursor; then smoke_cursor; fi
smoke_summary
if ((TRUST)); then trust_steps; fi
exit "$FAILED"
