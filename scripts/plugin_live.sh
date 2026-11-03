#!/usr/bin/env bash
# Install the context-guard plugins into your REAL home and test them in Claude Code, Codex, Genie and Cursor.
#
# Unlike scripts/sandbox_smoke.sh this changes your real home. `install` packages every plugin
# into $PLUGIN_DIST, removes the script install of the selected runtimes with `cli.py uninstall`
# (handoffs, state and ~/.context-guard/config.json stay; the kit goes once no runtime uses it),
# then installs each plugin: Claude, Codex and Genie from a local marketplace named context-guard, Cursor
# as a copy in ~/.cursor/plugins/local/context-guard whose hooks adapters/cursor/hooks_install.py
# registers in ~/.cursor/hooks.json (the plugin ships none). Plugins are reinstalled, not updated: the
# version stays the same and the runtimes cache per version. `check` reports what is installed,
# leftover script-install files and the events each runtime logged; `check --run` spends cheap
# model calls (Claude 4, Codex 4, Genie 3, Cursor 4: 2 prompts, a wake case unless Genie, a roster case)
# in a temporary work dir with tiny thresholds and
# passes a runtime when a note fired and was delivered. Hooks keep the real CONTEXT_GUARD_HOME.
# Then a wake case per runtime except Genie (whose missing queue API is documented): the parent spawns a
# background subagent that sleeps 20 s and answers a token, ends its turn, and passes when it
# starts a new turn by itself that quotes the token (Codex: through an app server it starts, so the
# context-guard waker's `codex queue` reaches it). Handoffs the run wrote for its work dir are deleted.
# Then a roster case per runtime: a parent on another model spawns one role without a per-spawn model
# (Claude, Codex: investigator, spawned as context-guard:investigator for Claude; Cursor: scout; Genie: runner) and scripts/roster_check.py compares the subagent's observed model with
# the role's tier model in adapters/<runtime>/config.json (Cursor: the tier's call slug, with the rendered
# role files copied to the work dir's .cursor/agents because cursor-agent ignores ~/.cursor/agents).
#
# Usage: scripts/plugin_live.sh install|check|checklist|uninstall [--only claude|codex|genie|cursor] [--run] [--dry-run] [-v]
#   --dry-run       print every command without changing anything or calling a model
#   --run           check: one live smoke per runtime, wake cases where supported and a roster case
#   -v, --verbose   also print each command and its output (VERBOSE=1); they always go to the run log
#   PLUGIN_DIST=<dir>    package output (default: ~/.context-guard/plugins)
#   KEEP=1          keep the temporary work dir of `check --run` (kept anyway on failure)
#   SMOKE_PYTHON=<path>  interpreter for cli.py (default: uv run --python 3.14 python)
#   CLAUDE_USE_API_KEY=1  keep ANTHROPIC_API_KEY for Claude (default: unset, use the login)
#   CLAUDE_MODEL, CODEX_MODEL, CODEX_EFFORT, CURSOR_MODEL override the cheap defaults
#   WAKE_TIMEOUT=<s>     seconds each wake case may take (default: 180)
#   ROSTER_TIMEOUT=<s>   seconds each roster case may take (default: 240)
#   CURSOR_ROSTER_PARENT  Cursor parent model of the roster case (default: claude-sonnet-5-5-low)
# shellcheck disable=SC2329  # cleanup runs through trap
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
NAME=context-guard
PLUGIN=$NAME@$NAME
WAKE_TIMEOUT=${WAKE_TIMEOUT:-180}
ROSTER_TIMEOUT=${ROSTER_TIMEOUT:-240}
CURSOR_ROSTER_PARENT=${CURSOR_ROSTER_PARENT:-claude-sonnet-5-5-low}

COMMAND="" ONLY="" DRY_RUN=0 LIVE=0 VERBOSE=${VERBOSE:-0} KEEP=${KEEP:-0} FAILED=0 READY=0 CLEANED=0
while (($#)); do
    case $1 in
        install|check|checklist|uninstall) [[ -z $COMMAND ]] || { echo "one command only" >&2; exit 2; }; COMMAND=$1; shift ;;
        --only) [[ $# -ge 2 ]] || { echo "--only needs a runtime" >&2; exit 2; }; ONLY=$2; shift 2 ;;
        --only=*) ONLY=${1#*=}; shift ;;
        --run) LIVE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -v|--verbose) VERBOSE=1; shift ;;
        -h|--help) sed -n '2,34s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -n $COMMAND ]] || { echo "usage: $0 install|check|checklist|uninstall [--only RUNTIME] [--run] [--dry-run] [-v]" >&2; exit 2; }
case $ONLY in ""|claude|codex|genie|cursor) ;; *) echo "--only takes claude, codex, genie or cursor" >&2; exit 2 ;; esac

unset VIRTUAL_ENV
if [[ -n ${SMOKE_PYTHON:-} ]]; then PY=("$SMOKE_PYTHON"); else PY=(uv run --python 3.14 python); fi
DIST=${PLUGIN_DIST:-$HOME/.context-guard/plugins}
STORE=$HOME/.context-guard/handoffs
CURSOR_PLUGIN=$HOME/.cursor/plugins/local/$NAME
CODEX_AGENTS=${CODEX_HOME:-$HOME/.codex}/agents
GENIE_AGENTS=${GENIE_HOME:-$HOME/.genie}/agents
LAUNCHER=$DIST/codex/plugins/$NAME/hooks/context-guard-hook
GENIE_LAUNCHER=$DIST/genie/plugins/$NAME/hooks/context-guard-hook
SELF=scripts/plugin_live.sh
TMP=${TMPDIR:-/tmp}
TMP=${TMP%/}

if ((DRY_RUN)) || [[ $COMMAND == checklist ]]; then
    ui_init /dev/null 12
else
    ui_init "$(mktemp "$TMP/context-guard-plugin-$COMMAND.XXXXXX")" 12
fi

wanted() { [[ -z $ONLY || $ONLY == "$1" ]]; }
present() { command -v "$1" >/dev/null; }
events_of() { echo "$HOME/.context-guard/state/$1/events.jsonl"; }
line_count() { if [[ -f $1 ]]; then wc -l <"$1" | tr -d ' '; else echo 0; fi; }
manifest_version() { sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -n 1; }
dist_version() {
    case $1 in
        codex|genie) manifest_version "$DIST/$1/plugins/$NAME/.codex-plugin/plugin.json" ;;
        *) manifest_version "$DIST/$1/plugins/$NAME/.$1-plugin/plugin.json" ;;
    esac
}

claude_listed() { [[ $(ui_query claude plugin marketplace list --json) == *"\"name\": \"$NAME\""* ]]; }
claude_installed() { [[ $(ui_query claude plugin list --json) == *"\"$PLUGIN\""* ]]; }
codex_marketplace() { ui_query codex plugin marketplace list | grep -E "^${NAME}[[:space:]]" || true; }
codex_plugin_line() {
    [[ -n $(codex_marketplace) ]] || return 0
    ui_query codex plugin list --marketplace "$NAME" | grep -E "^${PLUGIN}[[:space:]]" || true
}
codex_installed() { [[ $(codex_plugin_line) =~ ^${PLUGIN}[[:space:]]+installed ]]; }
genie_marketplace() { ui_query genie plugin marketplace list | grep -E "^${NAME}[[:space:]]" || true; }
genie_plugin_line() { ui_query genie plugin list | grep -E "^${PLUGIN}[[:space:]]" || true; }
genie_installed() { [[ $(genie_plugin_line) =~ ^${PLUGIN}[[:space:]]+installed ]]; }

script_installed() {
    ui_query "${PY[@]}" "$ROOT/cli.py" status --runtime all --home "$HOME" \
        | sed -nE 's/^\[(claude|codex|genie|cursor)\] installed.*/\1/p' || true
}

package_plugins() {
    ui_start Package "building the plugins"
    ui_run "${PY[@]}" "$ROOT/cli.py" package --runtime all --out "$DIST" --marketplace "$NAME" || return 1
    ui_done ok "$(grep -c '] packaged ' <<<"$UI_OUTPUT" || true) plugins built in $DIST"
}

remove_script_install() {
    local runtime names="" args=()
    for runtime in $(script_installed); do
        if wanted "$runtime"; then args+=(--runtime "$runtime"); names+="${names:+, }$runtime"; fi
    done
    if ((${#args[@]} == 0)); then ui_step skip Kit "no script install of the selected tools"; return 0; fi
    ui_start Kit "removing the script install"
    ui_run "${PY[@]}" "$ROOT/cli.py" uninstall "${args[@]}" --home "$HOME" || return 1
    ui_done ok "removed the script install of $names: $(grep -c '] remove' <<<"$UI_OUTPUT" || true) files removed$UI_SEP$(grep -c '] write ' <<<"$UI_OUTPUT" || true) settings files updated"
}

install_claude() {
    if ! present claude; then ui_step skip Claude "claude not on PATH"; return 0; fi
    ui_start Claude "installing the plugin"
    local listed
    listed=$(ui_query claude plugin marketplace list --json)
    if [[ $listed != *"\"name\": \"$NAME\""* || $listed != *"$DIST/claude"* ]]; then
        if [[ $listed == *"\"name\": \"$NAME\""* ]]; then ui_run claude plugin marketplace remove "$NAME" || return 1; fi
        ui_run claude plugin marketplace add "$DIST/claude" || return 1
    fi
    if claude_installed; then ui_run claude plugin uninstall "$PLUGIN" || return 1; fi
    ui_run claude plugin install "$PLUGIN" --scope user || return 1
    ui_done ok "plugin installed ($(dist_version claude), user scope)"
}

install_codex() {
    if ! present codex; then ui_step skip Codex "codex not on PATH"; return 0; fi
    ui_start Codex "installing the plugin and role agents"
    local listed
    listed=$(codex_marketplace)
    if [[ -z $listed || $listed != *"$DIST/codex"* ]]; then
        if [[ -n $listed ]]; then ui_run codex plugin marketplace remove "$NAME" || return 1; fi
        ui_run codex plugin marketplace add "$DIST/codex" || return 1
    fi
    if codex_installed; then ui_run codex plugin remove "$PLUGIN" || return 1; fi
    ui_run codex plugin add "$PLUGIN" || return 1
    ui_run sh "$LAUNCHER" adapters/codex/agents.py install || return 1
    ui_done ok "plugin installed ($(dist_version codex))${UI_SEP}role agents installed in $CODEX_AGENTS"
}

install_genie() {
    if ! present genie; then ui_step skip Genie "genie not on PATH"; return 0; fi
    ui_start Genie "installing the plugin and role agents"
    local listed
    listed=$(genie_marketplace)
    if [[ -z $listed || $listed != *"$DIST/genie"* ]]; then
        if [[ -n $listed ]]; then ui_run genie plugin marketplace remove "$NAME" || return 1; fi
        ui_run genie plugin marketplace add "$DIST/genie" || return 1
    fi
    if genie_installed; then ui_run genie plugin remove "$PLUGIN" || return 1; fi
    ui_run genie plugin add "$PLUGIN" || return 1
    ui_run sh "$GENIE_LAUNCHER" adapters/genie/agents.py install || return 1
    ui_done ok "plugin installed ($(dist_version genie))${UI_SEP}role agents installed in $GENIE_AGENTS"
}

install_cursor() {
    ui_start Cursor "copying the plugin, registering its hooks and role agents"
    ui_run rm -rf "$CURSOR_PLUGIN" || return 1
    ui_run mkdir -p "${CURSOR_PLUGIN%/*}" || return 1
    ui_run cp -R "$DIST/cursor/plugins/$NAME" "$CURSOR_PLUGIN" || return 1
    ui_run sh "$CURSOR_PLUGIN/hooks/context-guard-hook" adapters/cursor/hooks_install.py install || return 1
    ui_done ok "plugin copied ($(dist_version cursor)) to $CURSOR_PLUGIN${UI_SEP}hooks registered in ~/.cursor/hooks.json, role agents in ~/.cursor/agents"
}

next_steps() {
    local step=0
    ui_section "Next steps"
    if wanted cursor; then step=$((step + 1)); ui_say "$step. Cursor: reload the window (Developer: Reload Window), then check Output > Hooks (Third-Party Imports can stay off)"; fi
    if wanted codex; then step=$((step + 1)); ui_say "$step. Codex: open codex, run /hooks and trust the context-guard hooks (again after hook changes)"; fi
    if wanted genie; then step=$((step + 1)); ui_say "$step. Genie: open a new thread, run /hooks and trust the context-guard hooks"; fi
    if wanted claude; then step=$((step + 1)); ui_say "$step. Claude Code: start a new session"; fi
    ui_say "$((step + 1)). Verify: $SELF check --run, then work through $SELF checklist"
    ui_say "Undo: $SELF uninstall, then uv run --python 3.14 python cli.py install --runtime all"
}

cmd_install() {
    ui_title "plugin install"
    package_plugins || { ui_footer; exit 1; }
    remove_script_install || FAILED=1
    if wanted claude; then install_claude || FAILED=1; fi
    if wanted codex; then install_codex || FAILED=1; fi
    if wanted genie; then install_genie || FAILED=1; fi
    if wanted cursor; then install_cursor || FAILED=1; fi
    next_steps
    ui_footer
}

events_info() {
    "${PY[@]}" - "$1" "$UI_SEP" <<'PY'
import json, sys, time

entries = []
try:
    lines = open(sys.argv[1]).read().splitlines()
except OSError:
    lines = []
for line in lines:
    try:
        entry = json.loads(line)
    except ValueError:
        continue
    if isinstance(entry, dict):
        entries.append(entry)
last, ago = (entries[-1] if entries else {}), "-"
when = last.get("ts")
if isinstance(when, (int, float)):
    seconds = max(0, int(time.time() - when))
    ago = next(f"{seconds // size}{unit} ago" for size, unit in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")) if seconds >= size or size == 1)
print(f"{len(entries)}|{last.get('event', '-')}{sys.argv[2]}{ago}" if entries else "0|-")
PY
}

claude_plugin_state() {
    "${PY[@]}" - "$PLUGIN" "$(ui_query claude plugin list --json)" <<'PY'
import json, sys

try:
    found = [item for item in json.loads(sys.argv[2]) if item.get("id") == sys.argv[1]]
except (ValueError, AttributeError):
    found = []
print(f"{found[0].get('version', '?')}, {found[0].get('scope', '?')} scope" if found else "")
PY
}

status_row() {
    local tool=$1 runtime=$2 leftovers=$3 plugin extras="" line role agents=0 roles=0
    case $runtime in
        claude)
            if ! present claude; then plugin="$UI_SKIP claude not on PATH"
            else line=$(claude_plugin_state); plugin=${line:+$UI_OK $line}; fi ;;
        codex)
            if ! present codex; then plugin="$UI_SKIP codex not on PATH"
            else
                line=$(codex_plugin_line | sed -E 's/[[:space:]]{2,}/|/g')
                if [[ $line == *"|installed"* ]]; then IFS='|' read -r _ _ line _ <<<"$line"; plugin="$UI_OK ${line:-?}"; fi
            fi
            for role in $("${PY[@]}" -c 'import json, sys; print(*json.load(open(sys.argv[1]))["roles"])' "$ROOT/content/roles/roles.json"); do
                roles=$((roles + 1))
                if [[ -f $CODEX_AGENTS/$role.toml ]]; then agents=$((agents + 1)); fi
            done
            extras="agents $agents/$roles" ;;
        genie)
            if ! present genie; then plugin="$UI_SKIP genie not on PATH"
            else
                line=$(genie_plugin_line | sed -E 's/[[:space:]]{2,}/|/g')
                if [[ $line == *"|installed"* ]]; then IFS='|' read -r _ _ line _ <<<"$line"; plugin="$UI_OK ${line:-?}"; fi
            fi
            for role in $("${PY[@]}" -c 'import json, sys; print(*json.load(open(sys.argv[1]))["roles"])' "$ROOT/content/roles/roles.json"); do
                roles=$((roles + 1))
                if [[ -f $GENIE_AGENTS/$role.toml ]]; then agents=$((agents + 1)); fi
            done
            extras="agents $agents/$roles" ;;
        cursor)
            line=$CURSOR_PLUGIN/.cursor-plugin/plugin.json
            if [[ -f $line ]]; then plugin="$UI_OK $(manifest_version "$line"), ~/.cursor/plugins/local"; fi
            if [[ -f $HOME/.context-guard/state/cursor-hooks/install-manifest.json ]]; then extras="hooks registered"
            else extras="$UI_WARN hooks not registered"; fi ;;
    esac
    if [[ $leftovers == *" $runtime "* ]]; then extras+="${extras:+$UI_SEP}$UI_WARN script install left"; fi
    echo "$tool|${plugin:-$UI_FAIL not installed}|${extras:--}|$(events_info "$(events_of "$runtime")")"
}

show_status() {
    local leftovers rows=("Tool|Plugin|Extras|Events|Last event") missing=0 left="" row runtime
    leftovers=" $(script_installed | tr '\n' ' ') "
    ui_section Status
    if wanted claude; then rows+=("$(status_row Claude claude "$leftovers")"); fi
    if wanted codex; then rows+=("$(status_row Codex codex "$leftovers")"); fi
    if wanted genie; then rows+=("$(status_row Genie genie "$leftovers")"); fi
    if wanted cursor; then rows+=("$(status_row Cursor cursor "$leftovers")"); fi
    ui_table "${rows[@]}"
    for row in "${rows[@]}"; do [[ $row != *"$UI_FAIL not installed"* ]] || missing=1; done
    for runtime in claude codex genie cursor; do
        if wanted "$runtime" && [[ $leftovers == *" $runtime "* ]]; then left+="${left:+, }$runtime"; fi
    done
    if ((missing)); then ui_hint "install the plugins: $SELF install"; fi
    if [[ -n $left ]]; then ui_hint "script install still present for $left: $SELF install removes it"; fi
}

codex_trust_hint="trust the hooks: open codex, run /hooks, trust context-guard, then rerun $SELF check --run"
genie_trust_hint="open a new Genie thread, run /hooks, trust context-guard, then rerun $SELF check --run"

claude_auth() {
    CLAUDE_AUTH=(env -u ANTHROPIC_API_KEY)
    [[ ${CLAUDE_USE_API_KEY:-0} != 1 ]] || CLAUDE_AUTH=(env)
}

smoke_claude() {
    READY=0
    if ! present claude; then smoke_skip Claude "claude not on PATH"; return; fi
    if ((!DRY_RUN)) && ! claude_installed; then smoke_skip Claude "plugin not installed" "$SELF install --only claude"; return; fi
    READY=1
    local offset status=ran code=0 log=$LOGS/claude-1.jsonl
    offset=$(line_count "$(events_of claude)")
    claude_auth
    local base=("${CLAUDE_AUTH[@]}" claude -p --model "$CLAUDE_MODEL" --add-dir "$STORE" --permission-mode acceptEdits
                --output-format stream-json --verbose --include-hook-events)
    ui_start Claude "running the smoke (2 prompts)" long
    ui_model "$WORK" "$log" "${base[@]}" "$FIRST_PROMPT" || code=$?
    if ((code)); then status="fail:claude -p exited $code"; else
        log=$LOGS/claude-2.jsonl
        ui_model "$WORK" "$log" "${base[@]}" --continue "$NEXT_PROMPT" || code=$?
        ((code == 0)) || status="fail:claude -p --continue exited $code"
    fi
    smoke_report claude "$status" "$(events_of claude)" "$offset" "$log"
}

smoke_codex() {
    READY=0
    if ! present codex; then smoke_skip Codex "codex not on PATH"; return; fi
    local real=${CODEX_HOME:-$HOME/.codex}
    if ((!DRY_RUN)) && [[ ! -f $real/auth.json ]]; then smoke_skip Codex "not logged in" "run codex login"; return; fi
    if ((!DRY_RUN)) && ! codex_installed; then smoke_skip Codex "plugin not installed" "$SELF install --only codex"; return; fi
    READY=1
    local offset status=ran code=0 hint="" log=$LOGS/codex-1.jsonl
    offset=$(line_count "$(events_of codex)")
    local flags=(--json --skip-git-repo-check -m "$CODEX_MODEL" -c "model_reasoning_effort=\"$CODEX_EFFORT\""
                 -c 'sandbox_mode="workspace-write"' -c "sandbox_workspace_write.writable_roots=[\"$STORE\"]")
    ui_start Codex "running the smoke (2 prompts)" long
    ui_model "$WORK" "$log" codex exec "${flags[@]}" -C "$WORK" "$FIRST_PROMPT" || code=$?
    if ((code)); then status="fail:codex exec exited $code"; else
        log=$LOGS/codex-2.jsonl
        ui_model "$WORK" "$log" codex exec resume --last "${flags[@]}" "$NEXT_PROMPT" || code=$?
        ((code == 0)) || status="fail:codex exec resume exited $code"
    fi
    if [[ $status == ran && $(smoke_count "$(events_of codex)" "$offset" prompt) == 0 ]]; then hint=$codex_trust_hint; fi
    smoke_report codex "$status" "$(events_of codex)" "$offset" "$log" "$hint"
}

smoke_genie() {
    READY=0
    if ! present genie; then smoke_skip Genie "genie not on PATH"; return; fi
    if ((!DRY_RUN)) && ! genie_installed; then smoke_skip Genie "plugin not installed" "$SELF install --only genie"; return; fi
    READY=1
    local offset status=ran code=0 hint="" log=$LOGS/genie-1.jsonl
    offset=$(line_count "$(events_of genie)")
    local flags=(--json --skip-git-repo-check --sandbox workspace-write --add-dir "$STORE")
    local resume_flags=(--json --skip-git-repo-check)
    ui_start Genie "running the smoke (2 prompts)" long
    ui_model "$WORK" "$log" genie exec "${flags[@]}" -C "$WORK" "$FIRST_PROMPT" || code=$?
    if ((code)); then status="fail:genie exec exited $code"; else
        log=$LOGS/genie-2.jsonl
        ui_model "$WORK" "$log" genie exec resume --last "${resume_flags[@]}" "$NEXT_PROMPT" || code=$?
        ((code == 0)) || status="fail:genie exec resume exited $code"
    fi
    if [[ $status == ran && $(smoke_count "$(events_of genie)" "$offset" prompt) == 0 ]]; then hint=$genie_trust_hint; fi
    smoke_report genie "$status" "$(events_of genie)" "$offset" "$log" "$hint"
}

smoke_cursor() {
    READY=0
    if ! present cursor-agent; then smoke_skip Cursor "cursor-agent not on PATH"; return; fi
    if ((!DRY_RUN)) && [[ $(cursor-agent status 2>&1 || true) == *[Nn]"ot logged in"* ]]; then
        smoke_skip Cursor "cursor-agent not logged in" "run cursor-agent login"
        return
    fi
    READY=1
    if ((!DRY_RUN)) && ! present expect; then smoke_skip Cursor "expect not on PATH"; return; fi
    local offset code=0 status log=$LOGS/cursor.log
    offset=$(line_count "$(events_of cursor)")
    ui_start Cursor "running the smoke (2 prompts in cursor-agent)" long
    ui_model "$WORK" "$log" expect -f "$ROOT/scripts/cursor_interactive.exp" "$FIRST_PROMPT" "$NEXT_PROMPT" \
        -- cursor-agent --trust --workspace "$WORK" --model "$CURSOR_MODEL" || code=$?
    status=$(smoke_cursor_reason "$code" "$log")
    if [[ $status == ran ]] && ((!DRY_RUN)) && [[ $(smoke_count "$(events_of cursor)" "$offset" prompt) == 0 ]]; then
        smoke_report cursor "warn:unverified${UI_SEP}cursor-agent logged no plugin hook events" "$(events_of cursor)" "$offset" "$log" \
            "plugin hooks in cursor-agent are unverified; check the IDE instead (Output > Hooks)"
        return
    fi
    smoke_report cursor "$status" "$(events_of cursor)" "$offset" "$log"
}

# wake_run <tool> <runtime> [<hint>] -- <wake_check.py args>: run one wake case and report its verdict.
wake_run() {
    local tool=$1 runtime=$2 hint=$3 log=$LOGS/$2-wake.jsonl verdict code=0
    shift 4
    ui_start "$tool wake" "background subagent wakes the parent (up to ${WAKE_TIMEOUT}s)" long
    ui_show "${PY[@]}" "$ROOT/scripts/wake_check.py" "$runtime" --log "$log" "$@"
    if ((DRY_RUN)); then return 0; fi
    # The smoke's low thresholds make the Stop hook continue the parent's turn, so no wake is needed.
    verdict=$(env -u CONTEXT_GUARD_THRESHOLDS_NOTE_CAP -u CONTEXT_GUARD_HANDOFF_REFRESH_REFRESH_TOKENS \
        "${PY[@]}" "$ROOT/scripts/wake_check.py" "$runtime" --log "$log" "$@" </dev/null 2>>"$UI_LOG") || code=$?
    { printf -- '--- %s (exit %s): %s\n' "$log" "$code" "$verdict"; cat "$log" 2>/dev/null; printf -- '--- end\n'; } >>"$UI_LOG"
    if ((code == 0)) && [[ $verdict == ok* ]]; then
        UI_PASSED=$((UI_PASSED + 1))
        ui_done ok "woke${UI_SEP}${verdict#ok }"
        return 0
    fi
    FAILED=1 UI_FAILED=$((UI_FAILED + 1))
    ui_done fail "failed$UI_SEP${verdict#fail }"
    [[ -n $hint ]] || hint=$(smoke_hint "$runtime" "$log")
    [[ -z $hint ]] || ui_hint "$hint"
    if [[ -s $log ]]; then ui_detail "see $log"; ui_tail "$log"; fi
}

wake_token() { echo "WAKE-OK-$RANDOM$RANDOM"; }

wake_claude() {
    claude_auth
    wake_run Claude claude "" -- --token "$(wake_token)" --cwd "$WORK" --timeout "$WAKE_TIMEOUT" -- \
        "${CLAUDE_AUTH[@]}" claude -p --input-format stream-json --output-format stream-json --verbose \
        --model "$CLAUDE_MODEL" --add-dir "$STORE" --permission-mode acceptEdits --allowedTools 'Bash(sleep:*)'
}

wake_codex() {
    wake_run Codex codex "" -- --token "$(wake_token)" --cwd "$WORK" --timeout "$WAKE_TIMEOUT" \
        --waker-log "$HOME/.context-guard/state/codex/waker.log" -- \
        codex app-server -c "model=\"$CODEX_MODEL\"" -c "model_reasoning_effort=\"$CODEX_EFFORT\"" \
        -c "sandbox_workspace_write.writable_roots=[\"$STORE\"]" --enable multi_agent_v2
}

wake_cursor() {
    wake_run Cursor cursor "" -- --token "$(wake_token)" --cwd "$WORK" --timeout "$WAKE_TIMEOUT" -- \
        cursor-agent -p --output-format stream-json --force --trust --workspace "$WORK" --model "$CURSOR_MODEL"
}

# roster_run <tool> <runtime> -- <roster_check.py args>: run one roster case and report its verdict.
roster_run() {
    local tool=$1 runtime=$2 log=$LOGS/$2-roster.jsonl verdict code=0 hint
    shift 3
    ui_start "$tool roster" "role subagent runs on its tier model (up to ${ROSTER_TIMEOUT}s)" long
    ui_show "${PY[@]}" "$ROOT/scripts/roster_check.py" "$runtime" --log "$log" "$@"
    if ((DRY_RUN)); then return 0; fi
    verdict=$(env -u CONTEXT_GUARD_THRESHOLDS_NOTE_CAP -u CONTEXT_GUARD_HANDOFF_REFRESH_REFRESH_TOKENS \
        "${PY[@]}" "$ROOT/scripts/roster_check.py" "$runtime" --log "$log" "$@" </dev/null 2>>"$UI_LOG") || code=$?
    { printf -- '--- %s (exit %s): %s\n' "$log" "$code" "$verdict"; cat "$log" 2>/dev/null; printf -- '--- end\n'; } >>"$UI_LOG"
    if ((code == 0)) && [[ $verdict == ok* ]]; then
        UI_PASSED=$((UI_PASSED + 1))
        ui_done ok "tier model${UI_SEP}${verdict#ok }"
        return 0
    fi
    FAILED=1 UI_FAILED=$((UI_FAILED + 1))
    ui_done fail "failed$UI_SEP${verdict#fail }"
    hint=$(smoke_hint "$runtime" "$log")
    [[ -z $hint ]] || ui_hint "$hint"
    if [[ -s $log ]]; then ui_detail "see $log"; ui_tail "$log"; fi
}

roster_claude() {
    claude_auth
    roster_run Claude claude -- --cwd "$WORK" --timeout "$ROSTER_TIMEOUT" -- \
        "${CLAUDE_AUTH[@]}" claude -p --output-format stream-json --verbose --model "$CLAUDE_MODEL" \
        --add-dir "$STORE" --permission-mode acceptEdits
}

roster_codex() {
    roster_run Codex codex -- --cwd "$WORK" --timeout "$ROSTER_TIMEOUT" --sessions "${CODEX_HOME:-$HOME/.codex}/sessions" -- \
        codex exec --json --skip-git-repo-check -C "$WORK" -m "$CODEX_MODEL" -c "model_reasoning_effort=\"$CODEX_EFFORT\"" \
        --enable multi_agent_v2
}

# No model flag: Genie's session default (gpt-5.6-sol) differs from the cheap tier of the runner role.
roster_genie() {
    roster_run Genie genie -- --cwd "$WORK" --timeout "$ROSTER_TIMEOUT" --sessions "${GENIE_HOME:-$HOME/.genie}/sessions" -- \
        genie exec --json --skip-git-repo-check --sandbox workspace-write -C "$WORK"
}

roster_cursor() {
    roster_run Cursor cursor -- --cwd "$WORK" --timeout "$ROSTER_TIMEOUT" -- \
        cursor-agent -p --output-format stream-json --trust --workspace "$WORK" --model "$CURSOR_ROSTER_PARENT"
}

# clean_handoffs: delete the handoffs this run's prompts wrote into the real store for its work dir.
clean_handoffs() {
    local out
    CLEANED=1
    ui_show "${PY[@]}" "$ROOT/scripts/wake_check.py" clean-handoffs --store "$STORE" --workspace "$WORK"
    if ((DRY_RUN)) || [[ ! -d ${WORK:-} ]]; then return 0; fi
    out=$("${PY[@]}" "$ROOT/scripts/wake_check.py" clean-handoffs --store "$STORE" --workspace "$WORK" 2>>"$UI_LOG") || true
    printf '%s\n' "$out" >>"$UI_LOG"
    ui_say "Handoffs: ${out#ok }"
}

cleanup() {
    ((CLEANED)) || clean_handoffs
    if [[ $KEEP != 1 && $FAILED == 0 ]]; then rm -rf "$RUN_DIR"; else printf 'Work dir kept at %s\n' "$RUN_DIR"; fi
}

live_smoke() {
    if ((DRY_RUN)); then
        RUN_DIR=$TMP/context-guard-plugin-live.XXXXXX
    else
        RUN_DIR=$(mktemp -d "$TMP/context-guard-plugin-live.XXXXXX")
        RUN_DIR=$(cd "$RUN_DIR" && pwd -P)
        trap cleanup EXIT
    fi
    WORK=$RUN_DIR/work
    LOGS=$RUN_DIR/logs
    ui_section "Live smoke (2 cheap prompts, a wake case and a roster case per tool)"
    ui_show mkdir -p "$WORK" "$LOGS" "$STORE"
    if ((!DRY_RUN)); then
        mkdir -p "$WORK" "$LOGS" "$STORE"
        git init -q "$WORK"
        printf '# Work\nA tiny project for the context-guard plugin smoke.\n' >"$WORK/README.md"
    fi
    export CONTEXT_GUARD_THRESHOLDS_NOTE_CAP=$NOTE_CAP
    export CONTEXT_GUARD_HANDOFF_REFRESH_REFRESH_TOKENS=$REFRESH_TOKENS
    export CONTEXT_GUARD_HANDOFF_REFRESH_MIN_SECONDS=0
    export CONTEXT_GUARD_TELEMETRY_CLI_CHATS=$HOME/.cursor/chats
    ui_show export "CONTEXT_GUARD_THRESHOLDS_NOTE_CAP=$NOTE_CAP" "CONTEXT_GUARD_HANDOFF_REFRESH_REFRESH_TOKENS=$REFRESH_TOKENS" \
        CONTEXT_GUARD_HANDOFF_REFRESH_MIN_SECONDS=0 "CONTEXT_GUARD_TELEMETRY_CLI_CHATS=$HOME/.cursor/chats"
    local tool runtime
    for runtime in claude codex genie cursor; do
        wanted "$runtime" || continue
        tool=$(tr '[:lower:]' '[:upper:]' <<<"${runtime:0:1}")${runtime:1}
        "smoke_$runtime"
        if [[ $runtime == genie ]]; then
            smoke_skip "Genie wake" "unsupported: Genie has no public idle-thread queue command"
        elif ((READY)); then
            "wake_$runtime"
        else
            smoke_skip "$tool wake" "the smoke could not start"
        fi
        if ((READY)); then "roster_$runtime"; else smoke_skip "$tool roster" "the smoke could not start"; fi
    done
    clean_handoffs
    smoke_summary
}

cmd_check() {
    ui_title "plugin check"
    show_status
    if ((LIVE)); then live_smoke; fi
    ui_footer
}

checklist_tool() {
    local step=0 item
    ui_section "$1"
    shift
    for item in "$@"; do step=$((step + 1)); ui_say "$step. $item"; done
}

cmd_checklist() {
    ui_title "manual checklist after $SELF install"
    ui_say "Lower the tiers with CONTEXT_GUARD_THRESHOLDS_* or cli.py --set to reach them sooner."
    checklist_tool "Cursor IDE (reload the window; hook runs show in Output > Hooks)" \
        "hooks: with Third-Party Imports off, Output > Hooks lists the context-guard hooks from ~/.cursor/hooks.json" \
        "note: a long chat gets the context note once" \
        "firm: the firm warning asks for a handoff" \
        "block: the block tier stops the prompt; write the handoff, resend, and it goes through" \
        "handoff: the handoff skill writes ~/.context-guard/handoffs/<task>.md; a new chat reads it" \
        "subagent: a delegated task spawns a role (scout, investigator, implementer, reviewer, runner)" \
        "wake: a background subagent's result starts a parent turn without a user message (check --run covers cursor-agent -p only)"
    checklist_tool "Claude Code" \
        "/plugin lists $PLUGIN enabled; note, handoff and a role subagent work as in Cursor" \
        "CoWork: upload a zip with the plugin root at the zip root: (cd $DIST/claude/plugins/$NAME && zip -r /tmp/$NAME-claude.zip .)" \
        "or upload dist/context-guard-claude-<version>.zip from: uv run --python 3.14 python scripts/build_release.py --out dist" \
        "CoWork: hooks fire and handoffs land in the session"
    checklist_tool "Codex" \
        "/hooks lists the context-guard hooks; trust them (again after every hook change)" \
        "after sh $LAUNCHER adapters/codex/agents.py install, a new session spawns a named role (e.g. scout)" \
        "note and handoff work as in Cursor"
    checklist_tool "Genie" \
        "the plugin is enabled and the context-guard hooks are trusted" \
        "after sh $GENIE_LAUNCHER adapters/genie/agents.py install, a new thread spawns a named role" \
        "note and handoff work; idle-parent wake is unsupported and check --run reports it as skipped"
    checklist_tool "Cross-runtime" \
        "write a handoff in one runtime and resume it in another (\"continue from the handoff\")" \
        "$SELF check shows new events for every runtime you used"
}

uninstall_claude() {
    if ! present claude; then ui_step skip Claude "claude not on PATH"; return 0; fi
    ui_start Claude "removing the plugin"
    if claude_installed; then ui_run claude plugin uninstall "$PLUGIN" || return 1; fi
    if claude_listed; then ui_run claude plugin marketplace remove "$NAME" || return 1; fi
    ui_done ok "plugin and marketplace removed"
}

uninstall_codex() {
    if ! present codex; then ui_step skip Codex "codex not on PATH"; return 0; fi
    ui_start Codex "removing the plugin and role agents"
    ui_run "${PY[@]}" "$ROOT/adapters/codex/agents.py" uninstall --home "$HOME" || return 1
    if codex_installed; then ui_run codex plugin remove "$PLUGIN" || return 1; fi
    if [[ -n $(codex_marketplace) ]]; then ui_run codex plugin marketplace remove "$NAME" || return 1; fi
    ui_done ok "plugin, marketplace and role agents removed"
}

uninstall_genie() {
    if ! present genie; then ui_step skip Genie "genie not on PATH"; return 0; fi
    ui_start Genie "removing the plugin and role agents"
    ui_run "${PY[@]}" "$ROOT/adapters/genie/agents.py" uninstall --home "$HOME" || return 1
    if genie_installed; then ui_run genie plugin remove "$PLUGIN" || return 1; fi
    if [[ -n $(genie_marketplace) ]]; then ui_run genie plugin marketplace remove "$NAME" || return 1; fi
    ui_done ok "plugin, marketplace and role agents removed"
}

uninstall_cursor() {
    ui_start Cursor "removing the plugin and its hooks"
    ui_run "${PY[@]}" "$ROOT/adapters/cursor/hooks_install.py" uninstall --home "$HOME" || return 1
    ui_run rm -rf "$CURSOR_PLUGIN" || return 1
    ui_done ok "plugin removed from $CURSOR_PLUGIN${UI_SEP}hooks removed from ~/.cursor/hooks.json, role agents from ~/.cursor/agents; reload the Cursor window"
}

cmd_uninstall() {
    ui_title "plugin uninstall"
    if wanted claude; then uninstall_claude || FAILED=1; fi
    if wanted codex; then uninstall_codex || FAILED=1; fi
    if wanted genie; then uninstall_genie || FAILED=1; fi
    if wanted cursor; then uninstall_cursor || FAILED=1; fi
    ui_section "Next steps"
    ui_say "Packages stay in $DIST. Restore the script install with:"
    ui_say "uv run --python 3.14 python cli.py install --runtime all"
    ui_footer
}

case $COMMAND in
    install) cmd_install ;;
    check) cmd_check ;;
    checklist) cmd_checklist ;;
    uninstall) cmd_uninstall ;;
esac
exit "$FAILED"
