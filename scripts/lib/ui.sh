# shellcheck shell=bash
# Terminal output shared by scripts/plugin_live.sh and scripts/sandbox_smoke.sh (sourced; bash 3.2).
#
# A step is one line: a status symbol, a tool and a short result. On a terminal without NO_COLOR the
# line uses symbols and colors and a pending step is replaced in place; otherwise it is plain ASCII.
# Every command and its output go to the run log $UI_LOG. The caller sets DRY_RUN=1 to print each
# command instead of running it, VERBOSE=1 to also print commands and output, and PY to the
# interpreter used for event counts. Terminal paths are shortened ($HOME to ~, temporary dirs to
# $TMPDIR/…); the log keeps them whole.

# ui_init <log> [<tool column width>]: the log is /dev/null when nothing may be written.
ui_init() {
    UI_LOG=$1 UI_TOOL_WIDTH=${2:-8} UI_PENDING=0 UI_STARTED=0 UI_TOOL="" UI_T0=$SECONDS UI_FANCY=0
    UI_PASSED=0 UI_FAILED=0 UI_WARNED=0 UI_SKIPPED=0
    if [[ -t 1 && -z ${NO_COLOR:-} ]]; then UI_FANCY=1; fi
    if ((UI_FANCY)); then
        UI_OK='✓' UI_FAIL='✗' UI_WARN='!' UI_BUSY='…' UI_SKIP='-' UI_DRY='·' UI_SEP=' · ' UI_MORE='…' UI_WIDTH=1
        UI_GREEN=$'\033[32m' UI_RED=$'\033[31m' UI_YELLOW=$'\033[33m' UI_DIM=$'\033[2m' UI_BOLD=$'\033[1m' UI_OFF=$'\033[0m'
    else
        UI_OK='[ok]' UI_FAIL='[FAIL]' UI_WARN='[warn]' UI_BUSY='[..]' UI_SKIP='[skip]' UI_DRY='[dry]' UI_SEP=' - ' UI_MORE='...'
        UI_WIDTH=6 UI_GREEN='' UI_RED='' UI_YELLOW='' UI_DIM='' UI_BOLD='' UI_OFF=''
    fi
    UI_INDENT=$(printf '%*s' $((UI_WIDTH + UI_TOOL_WIDTH + 4)) '')
    local tmp=${TMPDIR:-/tmp} real prefix label="\$TMPDIR" bound='(^|[[:space:]"'\''=:(])'
    tmp=${tmp%/}
    [[ -n ${TMPDIR:-} ]] || label=/tmp
    real=$(cd "$tmp" 2>/dev/null && pwd -P) || real=$tmp
    UI_PATHS=()
    for prefix in "$real" "$tmp"; do
        UI_PATHS+=(-e "s#${bound}$(ui_regex "$prefix")/[^/[:space:]\"']+#\\1${label}/${UI_MORE}#g")
    done
    UI_PATHS+=(-e "s#${bound}$(ui_regex "${HOME%/}")([/[:space:]\"']|\$)#\\1~\\2#g")
}

ui_regex() { printf '%s' "$1" | sed 's/[][\.*^$+?(){}|#]/\\&/g'; }

# ui_short <text>: the text with $HOME and temporary paths shortened.
ui_short() { printf '%s\n' "$*" | LC_ALL=C sed -E "${UI_PATHS[@]}"; }

# ui_words <arg>...: a command as one readable line; long prompts are cut to 40 characters.
ui_words() {
    local arg out="" safe='^[A-Za-z0-9_./:=@%+,~-]+$'
    for arg in "$@"; do
        if [[ ${#arg} -gt 44 && $arg == *' '* ]]; then arg=${arg:0:40}$UI_MORE; fi
        if ! [[ $arg =~ $safe ]]; then
            if [[ $arg == *"'"* ]]; then arg=$(printf '%q' "$arg"); else arg="'$arg'"; fi
        fi
        out+=${out:+ }$arg
    done
    ui_short "$out"
}

ui_break() { if ((UI_PENDING)); then printf '\n'; UI_PENDING=0; fi; }

# ui_show [--in <dir>] <command>...: log the command; print it too in dry runs and verbose mode.
ui_show() {
    local dir="" prefix=""
    if [[ ${1:-} == --in ]]; then dir=$2; shift 2; fi
    { [[ -z $dir ]] || printf 'cd %q && ' "$dir"; printf '$'; printf ' %q' "$@"; printf '\n'; } >>"$UI_LOG"
    if ((DRY_RUN || VERBOSE)); then
        [[ -z $dir ]] || prefix="cd $(ui_words "$dir") && "
        ui_break
        printf '    %s$ %s%s%s\n' "$UI_DIM" "$prefix" "$(ui_words "$@")" "$UI_OFF"
    fi
}

# ui_run <command>...: run the command (not in dry runs) with its output, also kept in UI_OUTPUT,
# in the log. On failure the current step fails with the last output lines; returns the exit code.
ui_run() {
    local code=0 what
    ui_show "$@"
    UI_OUTPUT=""
    if ((DRY_RUN)); then return 0; fi
    UI_OUTPUT=$("$@" 2>&1 </dev/null) || code=$?
    printf '%s\n' "$UI_OUTPUT" >>"$UI_LOG"
    if ((VERBOSE)) && [[ -n $UI_OUTPUT ]]; then printf '%s\n' "$UI_OUTPUT" | sed 's/^/      /'; fi
    if ((code == 0)); then return 0; fi
    what=$(ui_words "$@")
    [[ ${#what} -le 70 ]] || what=${what:0:70}$UI_MORE
    ui_done fail "failed$UI_SEP$what exited $code"
    printf '%s\n' "$UI_OUTPUT" | ui_tail
    return "$code"
}

# ui_query <command>...: run a read-only command, even in dry runs; log it and print its stdout.
ui_query() {
    local out
    out=$("$@" 2>>"$UI_LOG" </dev/null) || true
    { printf '$'; printf ' %q' "$@"; printf '\n%s\n' "$out"; } >>"$UI_LOG"
    printf '%s\n' "$out"
}

# ui_model <dir> <log> <command>...: run a model command in dir with its output in log, then copy
# that output into the run log. Returns the command's exit code; skipped in dry runs.
ui_model() {
    local dir=$1 log=$2 code=0
    shift 2
    ui_show --in "$dir" "$@"
    if ((DRY_RUN)); then return 0; fi
    (cd "$dir" && "$@" >"$log" 2>&1 </dev/null) || code=$?
    { printf -- '--- %s (exit %s)\n' "$log" "$code"; cat "$log"; printf -- '--- end\n'; } >>"$UI_LOG" 2>&1
    return "$code"
}

ui_section() {
    ui_break
    if ((UI_STARTED)); then printf '\n'; fi
    UI_STARTED=1
    printf '%s%s%s\n' "$UI_BOLD" "$1" "$UI_OFF"
}

ui_title() { ui_section "context-guard$UI_SEP$1"; }
ui_say() { ui_break; printf '  %s\n' "$(ui_short "$1")"; }
ui_detail() { printf '%s%s\n' "$UI_INDENT" "$(ui_short "$1")"; }
ui_hint() { printf '%s%s-> %s%s\n' "$UI_INDENT" "$UI_YELLOW" "$(ui_short "$1")" "$UI_OFF"; }

ui_line() {
    printf '  %s%-*s%s %-*s %s' "$2" "$UI_WIDTH" "$1" "$UI_OFF" "$UI_TOOL_WIDTH" "$3" "$(ui_short "$4")"
}

# ui_start <tool> <action> [long]: begin a step. A terminal shows it pending until it finishes;
# plain output announces only long steps; dry runs list the step with its commands below.
ui_start() {
    UI_TOOL=$1 UI_T0=$SECONDS
    ui_break
    if ((DRY_RUN)); then
        ui_line "$UI_DRY" "$UI_DIM" "$1" "$2"; printf '\n'
    elif ((UI_FANCY && !VERBOSE)); then
        ui_line "$UI_BUSY" "$UI_DIM" "$1" "$2"; UI_PENDING=1
    elif ((VERBOSE)) || [[ ${3:-} == long ]]; then
        ui_line "$UI_BUSY" "$UI_DIM" "$1" "$2"; printf '\n'
    fi
}

# ui_step <ok|fail|warn|skip> <tool> <text>: print a finished step, replacing a pending one.
ui_step() {
    local symbol=$UI_SKIP color=$UI_DIM
    case $1 in
        ok) symbol=$UI_OK color=$UI_GREEN ;;
        fail) symbol=$UI_FAIL color=$UI_RED ;;
        warn) symbol=$UI_WARN color=$UI_YELLOW ;;
    esac
    if ((UI_PENDING)); then printf '\r\033[K'; UI_PENDING=0; fi
    ui_line "$symbol" "$color" "$2" "$3"; printf '\n'
}

# ui_done <ok|fail|warn|skip> <text>: finish the current step; dry runs print no ok lines.
ui_done() {
    if ((DRY_RUN)) && [[ $1 == ok ]]; then return 0; fi
    ui_step "$1" "$UI_TOOL" "$2"
}

ui_elapsed() {
    local seconds=$((SECONDS - UI_T0))
    if ((seconds < 60)); then echo "${seconds}s"; else printf '%dm %02ds\n' $((seconds / 60)) $((seconds % 60)); fi
}

# ui_tail [<file>]: the last 5 meaningful lines of a log (or stdin), without escape codes, dimmed.
ui_tail() {
    local esc bel line
    esc=$(printf '\033') bel=$(printf '\007')
    cat "${1:--}" 2>/dev/null | tr '\r' '\n' | LC_ALL=C sed -E \
        -e "s/${esc}\\[[0-9;?<=>]*[ -\\/]*[@-~]//g" -e "s/${esc}\\][^${bel}]*(${bel}|${esc}\\\\)//g" \
        -e "s/${esc}.//g" -e 's/[[:cntrl:]]/ /g' -e 's/^[[:space:]]+//' -e 's/[[:space:]]+$//' -e '/^$/d' \
        | tail -n 5 | while IFS= read -r line; do
            [[ ${#line} -le 110 ]] || line=${line:0:110}$UI_MORE
            printf '%s%s%s%s\n' "$UI_INDENT" "$UI_DIM" "$(ui_short "$line")" "$UI_OFF"
        done
}

# ui_width <text>: set UI_W to the text's display width, counting each symbol as one column.
ui_width() {
    local text=$1
    if ((UI_FANCY)); then text=${text//✓/x}; text=${text//✗/x}; text=${text//·/x}; text=${text//…/x}; fi
    UI_W=${#text}
}

# ui_table <row>...: rows of |-separated cells, the first a header, printed aligned.
ui_table() {
    local row line cell pad i first=1 widths=() cells=()
    for row in "$@"; do
        IFS='|' read -r -a cells <<<"$row"
        for ((i = 0; i < ${#cells[@]}; i++)); do
            ui_width "${cells[i]}"
            if ((UI_W > ${widths[i]:-0})); then widths[i]=$UI_W; fi
        done
    done
    for row in "$@"; do
        IFS='|' read -r -a cells <<<"$row"
        line=""
        for ((i = 0; i < ${#widths[@]}; i++)); do
            cell=${cells[i]:-}
            ui_width "$cell"
            printf -v pad '%*s' $((widths[i] - UI_W + 2)) ''
            line+=$cell$pad
        done
        line=$(ui_short "$line" | sed -E 's/[[:space:]]+$//')
        if ((first)); then
            line=$UI_BOLD$line$UI_OFF first=0
        elif ((UI_FANCY)); then
            line=${line//"$UI_OK"/$UI_GREEN$UI_OK$UI_OFF}
            line=${line//"$UI_FAIL"/$UI_RED$UI_FAIL$UI_OFF}
        fi
        printf '  %s\n' "$line"
    done
}

ui_footer() { ui_break; if [[ $UI_LOG != /dev/null ]]; then printf '\nFull log: %s\n' "$UI_LOG"; fi; }

smoke_count() {
    local count
    count=$(tail -n "+$(($2 + 1))" "$1" 2>/dev/null | grep -c "\"event\": *\"$3\"") || true
    echo "${count:-0}"
}

# smoke_events_detail <events> <offset>: the non-zero event counts after offset, in words.
smoke_events_detail() {
    "${PY[@]}" - "$1" "$2" "$UI_SEP" <<'PY'
import collections, json, sys

counts = collections.Counter()
try:
    lines = open(sys.argv[1]).read().splitlines()[int(sys.argv[2]):]
except OSError:
    lines = []
for line in lines:
    try:
        entry = json.loads(line)
    except ValueError:
        continue
    if isinstance(entry, dict):
        name = entry.get("event")
        counts[(entry.get("detail") or {}).get("source") if name == "handoff_requested" else name] += 1
names = [("note_delivered", "note delivered", "notes delivered")] if counts["note_delivered"] else [("note", "note fired", "notes fired")]
names += [("handoff_written", "handoff written", "handoffs written"), ("refresh", "refresh", "refreshes"),
          ("unwritten", "unwritten handoff", "unwritten handoffs"), ("block", "block", "blocks")]
print(sys.argv[3].join(f"{counts[n]} {many}" if counts[n] > 1 else one for n, one, many in names if counts[n]))
PY
}

# smoke_hint <runtime> <log>: an actionable hint for a failed run, from its model log.
smoke_hint() {
    [[ -f $2 ]] || return 0
    case $1 in
        claude*)
            if grep -qE 'Invalid API key|ANTHROPIC_API_KEY' "$2"; then
                echo "check ANTHROPIC_API_KEY, or leave CLAUDE_USE_API_KEY unset to use your claude login"
            elif grep -qE '"api_error_status": *401|authentication_failed|OAuth|[Tt]oken (has )?expired|/login' "$2"; then
                echo "run claude, then /login"
            fi ;;
        codex*) if grep -qiE '401 Unauthorized|"status": *401|not logged in|codex login' "$2"; then echo "run codex login"; fi ;;
        cursor*) if grep -qiE 'not logged in|log ?in required|cursor-agent login' "$2"; then echo "run cursor-agent login"; fi ;;
    esac
}

# smoke_cursor_reason <driver exit code> <log>: "ran", or why cursor_interactive.exp failed.
smoke_cursor_reason() {
    case $1 in
        0) echo ran ;;
        3) if grep -q 'waiting for startup' "$2" 2>/dev/null; then echo "fail:cursor-agent never became ready"
           else echo "fail:a cursor-agent turn timed out"; fi ;;
        4) echo "fail:cursor-agent exited early" ;;
        *) echo "fail:the cursor driver exited $1" ;;
    esac
}

# smoke_report <runtime> <status> <events> <offset> <log> [<hint>]: finish a smoke step. status is
# "ran" (passes when a new note fired and was delivered), or fail:, warn: or skip: plus a reason.
# A failure sets FAILED=1 and shows the hint, the log path and its last lines.
smoke_report() {
    local status=$2 log=$5 hint=${6:-} detail
    if ((DRY_RUN)); then return 0; fi
    if [[ $status == ran ]]; then
        if [[ $(smoke_count "$3" "$4" prompt) == 0 ]]; then status="fail:no hook events logged"
        elif [[ $(smoke_count "$3" "$4" note) == 0 ]]; then status="fail:hooks ran but no note fired"
        elif [[ $(smoke_count "$3" "$4" note_delivered) == 0 ]]; then status="fail:a note fired but was never delivered"; fi
    fi
    detail=$(smoke_events_detail "$3" "$4")
    case $status in
        ran) UI_PASSED=$((UI_PASSED + 1)); ui_done ok "passed in $(ui_elapsed)${detail:+$UI_SEP$detail}" ;;
        warn:*)
            UI_WARNED=$((UI_WARNED + 1))
            ui_done warn "${status#warn:}${detail:+$UI_SEP$detail}"
            [[ -z $hint ]] || ui_hint "$hint" ;;
        *)
            # shellcheck disable=SC2034  # read by the sourcing script
            FAILED=1 UI_FAILED=$((UI_FAILED + 1))
            ui_done fail "failed after $(ui_elapsed)$UI_SEP${status#fail:}${detail:+$UI_SEP$detail}"
            [[ -n $hint ]] || hint=$(smoke_hint "$1" "$log")
            [[ -z $hint ]] || ui_hint "$hint"
            if [[ -s $log ]]; then ui_detail "see $log"; ui_tail "$log"; fi ;;
    esac
}

# smoke_skip <tool> <reason> [<hint>]: a smoke that could not start.
smoke_skip() {
    UI_SKIPPED=$((UI_SKIPPED + 1))
    ui_step skip "$1" "skipped$UI_SEP$2"
    [[ -z ${3:-} ]] || ui_hint "$3"
}

smoke_summary() {
    local parts=""
    ui_section Summary
    if ((DRY_RUN)); then ui_say "dry run: nothing was changed and no model was called"; return 0; fi
    ((UI_PASSED == 0)) || parts+="$UI_PASSED passed"
    ((UI_FAILED == 0)) || parts+="${parts:+$UI_SEP}$UI_FAILED failed"
    ((UI_WARNED == 0)) || parts+="${parts:+$UI_SEP}$UI_WARNED unverified"
    ((UI_SKIPPED == 0)) || parts+="${parts:+$UI_SEP}$UI_SKIPPED skipped"
    ui_say "${parts:-nothing ran}"
}
