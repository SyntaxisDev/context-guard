# Architecture

context-guard warns about growing chat context, keeps task handoffs, and routes work to
role-based subagents. One runtime-neutral core and one content source serve Cursor, Codex
and Claude Code through thin adapters.

```text
content/   policy, roles.json + role bodies, skills, defaults.json   (what users receive)
core/      config, observation, policy, handoffs, state, events,     (runtime-neutral logic)
           render, install, install_spec, kit, package, deadline, processes, runner/
adapters/<runtime>/  hooks.py, mapping.py, config.json, runner.py, tests/
           (extras: codex rollout.py, tools.py; claude transcript.py, plugin.py)
cli.py     install | uninstall | doctor | status | package
```

Data flows `content -> core.render -> adapter mapping -> native files`, and
`native hook payload -> adapter observe -> core.policy -> Decision -> adapter respond`.
Python is 3.14+, standard library only. `core/` never imports an adapter and never names a
runtime home; every home and path is passed in. An adapter never reads another runtime's home.

Every feature has an equivalent in all three adapters. Where a runtime has no native mechanism,
its adapter supplies the closest equivalent, or `docs/<runtime>.md` states the gap and why.
Shared policy text holds for every runtime; runtime-specific wording comes from mapping placeholders.

## Adapter contract

No dispatcher superclass. Each adapter supplies functions:

| piece                                                            | responsibility                                                                                                                             |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `observe(payload) -> Observation`                                | native payload to `Observation` (tokens, window, model, `is_subagent`, `compacted`, cwd, prompt, `ToolUse`). `ValueError` means fail open. |
| `respond(decision) -> payload`                                   | `Decision` to the runtime's response shape; any error means pass.                                                                          |
| `mapping.runtime_mapping`/render                                 | tier profiles, override syntax, capability to native permissions, channel, skills dir.                                                     |
| `mapping.install_spec(home, kit, python, config) -> InstallSpec` | kit-owned files, JSON merges (hook events and matchers, `deadline.HOOK_TIMEOUT_SECONDS`), text edits, kit signatures.                      |
| `config.json`                                                    | tiers plus runtime-specific telemetry and hook settings.                                                                                   |
| `runner.py`                                                      | provider for the optional runner.                                                                                                          |

Hook flow, all inside `deadline.run_within(..., pass)`: read stdin, load config, `observe`,
`policy.evaluate`, `respond`. Saving the session state commits the deadline: the save happens
only while at least `deadline.COMMIT_RESERVE_SECONDS` remain, and afterwards the deadline no
longer interrupts, so the response matching the saved state is the one printed. Each adapter
`main()` is the process boundary: any other exception prints the native pass response and one
stderr line. Adapters persist their own data under `state.adapter` / `state.learned_window`.

## Policy

Tiers are `note`, `firm`, `block`: each fires at `min(fraction * window, cap)`
(defaults 0.55/150k, 0.75/200k, 0.92/260k). When the window is unknown only the caps apply;
unknown tokens fail open.

- **Once per tier.** State records the highest tier fired; a jump fires only the highest.
- **Block** happens only on prompt events. The message states tokens and limit. An identical
  (stripped) resend passes, is logged as `resend`, and is followed by silence until a higher tier.
  Session state keeps a sha256 of the blocked prompt and only the slug of the first prompt
  (the handoff name), never prompt text.
- **Re-arm.** Compaction (`compacted` or the `compact` event), or live context back under the
  note threshold, resets fired tiers. A zero token reading is unknown telemetry and never re-arms.
- **Pass-through prompts.** `[context-guard]` prompts pass silently; handoff requests
  (a prompt starting with `handoff`, "write/save/update a handoff", "wrap up", "start a new
  chat") pass and log `handoff_requested`. A prompt that only mentions handoffs is an ordinary
  prompt and still gets tier warnings.
- **Handoff writes.** Any write into the handoff store, requested or not, credits the session:
  it records the turn and the current tokens as the last handoff, adopts that file as the
  session's handoff path and acknowledges a pending request. Writes are Cursor's write tools
  and `Shell`, Codex `apply_patch` and shell or code-mode commands, and Claude `Write`/`Edit`/`MultiEdit`/
  `NotebookEdit` and `Bash`; a shell command writes when the handoff is a `>` or `>>` target,
  any `tee` operand (options such as `-a` allowed), or the destination (last operand) of `cp`,
  `mv` or `install`; a write wins over a read in the same command. Notes ask for the write in the same turn and forbid claiming an update not written.
- **Stop refresh.** At or above the note threshold, at most once per turn and never in a turn
  that wrote the handoff. A turn whose note or handoff request got no write is refreshed at
  once, saying the handoff file was not modified this turn and naming it (`source:
unwritten`). Otherwise the stop hook asks for an update when tokens grew by `refresh_tokens`
  since the last write or refresh request and `min_seconds` passed since that request
  (`source: refresh`), so an ignored refresh is not repeated without growth.
- **Leaf mode** (`is_subagent`): no policy text, handoff list, notes or refresh. Once at the
  subagent's budget one advisory note (never a block): report what is done and what is left,
  say explicitly if the remainder cannot be split, then stop unless finishing is a few steps
  away. The budget is the role's optional `budget_tokens` in `roles.json` when the adapter
  knows the role (`Observation.role`: Codex and Claude `agent_type`, Cursor the `subagent_type`
  its Task spawn recorded under the `tool_use_id` that subagent tool hooks carry as
  `parent_tool_call_id`), else `subagent_budget.tokens` (80k). The policy text tells the parent
  how to continue: re-spawn from the summary, step up a tier or variant, split, or finish it.

| event                | decisions         | meaning                                                     |
| -------------------- | ----------------- | ----------------------------------------------------------- |
| `session`, `compact` | pass, context     | inject policy and handoff list                              |
| `prompt`             | pass, note, block | note for the model; block stops the prompt                  |
| `tool`               | pass, note        | context after a tool call (notes up to `firm`, leaf budget) |
| `stop`               | pass, context     | bookkeeping follow-up                                       |

## Handoffs

`core.handoffs.listing` ranks and never filters by relevance: same-workspace entries first
with the likely match marked `(likely match)`, duplicates removed (path and identity),
`Status: done` hidden, `handoff_list.limit` (5) shown, then "N more in <dir>; search by
keyword". Outside a git project, home or scratch directories a single pointer line replaces
the list. Files live in `~/.context-guard/handoffs/` with semantic slug names plus an
identity suffix. Writes and reads inside the store are detected from tool events.

## Roles, capabilities, tiers

`content/roles/roles.json` declares per role `shell, write, network, mcp, git_push`,
`default_tier`, `allowed_tiers` and an optional `budget_tokens` (reviewer and investigator
120k, scout and runner 60k, implementer the global 80k), plus optional `variants`
(`name, role, tier, context`).

| role         | shell | write | network | mcp | git_push | default  | allowed               |
| ------------ | ----- | ----- | ------- | --- | -------- | -------- | --------------------- |
| scout        | no    | no    | no      | no  | no       | cheap    | cheap                 |
| investigator | yes   | no    | yes     | yes | no       | standard | cheap, standard, deep |
| reviewer     | yes   | no    | yes     | no  | no       | deep     | standard, deep        |
| implementer  | yes   | yes   | no      | yes | no       | standard | standard, deep        |
| runner       | yes   | yes   | yes     | yes | yes      | cheap    | cheap, standard       |

A role says what a subagent may do; a tier (`cheap|standard|deep`) says what it runs on.
Adapter `config.json` maps tiers to native model profiles. The rendered policy contains a
who-can-do-what table and a tier table with the runtime's per-spawn override syntax; the
orchestrator picks both per spawn, preferring the role default, stepping up for scope or
difficulty, and splitting work instead of raising context. `write: false` roles may write
evidence only under `/tmp`. Capabilities map to native permissions only where the runtime
can enforce them (see each runtime doc); the rest is stated in the agent text, and the role
table has a column of the limits this runtime does not enforce (the adapter's
`enforced_limits`), so it never claims enforcement that does not exist.

## Delivery channels

| runtime | policy                                              | handoff list and notes                                    |
| ------- | --------------------------------------------------- | --------------------------------------------------------- |
| Cursor  | `~/.cursor/rules/context-guard.mdc` (`alwaysApply`) | `sessionStart` additional context; notes via tool results |
| Codex   | SessionStart additionalContext                      | SessionStart (also `source: compact`)                     |
| Claude  | `~/.claude/rules/context-guard.md`                  | SessionStart additionalContext                            |

User `CLAUDE.md` and `AGENTS.md` are never edited. Skills (`handoff`,
`install-context-guard`) and agents are native files tracked in the manifest.

The Claude plugin package (`cli.py package`) is the other delivery path: its policy is a packaged
file that `hooks.py` adds to SessionStart context, and it does not ship `install-context-guard`.
Formats, layout and CoWork limits are in [distribution.md](distribution.md).

## Events

`~/.context-guard/state/<runtime>/events.jsonl`, one schema, bounded to `events.max_lines`
(appends fail open):
`{ts, runtime, session, event, tokens, window, tier?, detail{}}` with `event` in `prompt`,
`note`, `note_delivered` (a note's text was handed to the model in a native response: the same
hook in Claude Code and Codex, a later tool result or session start for a held Cursor prompt
note), `block`, `resend`,
`handoff_requested` (source `user`, `refresh` or `unwritten`), `handoff_written`, `handoff_read`,
`subagent_spawn` (role, tier, model,
effort), `subagent_budget` (role, budget), `subagent_unreturned` (Cursor: role, agent, parent stop
status, for a Task subagent whose result never reached the parent), `subagent_orphaned` (Cursor:
role, agent, for a foreground subagent the parent moved on from), `subagent_silent` (Cursor and Claude
Code: role, agent, minutes without activity; wording shared in `core/subagent_notes.py`),
`subagent_wake` (Codex: outcome `sent`, `skipped` or `failed`, reason, agent, name, matched, for
the idle-root wake after a background subagent finished; session is the root thread).

## Configuration

Precedence, low to high: `content/defaults.json`, `adapters/<rt>/config.json`,
`~/.context-guard/config.json` top level, its `runtimes.<rt>` section, then environment
`CONTEXT_GUARD_<SECTION>_<KEY>`. User and environment values must match the type of the
shipped value; malformed values are ignored, unknown keys kept, except that a new entry in a
nested section of objects (tier profiles) must be an object. The user file is
written by `cli.py ... --set section.key=value`, which rejects a `section.key` absent from the
defaults and every adapter config (except a new object entry in a section of objects) and a
value of the wrong type, so a typo fails instead of being silently ignored.

Hooks delete session state files in `paths.state_dir/<runtime>/` untouched for
`paths.prune_days` (30) days; each state file's lock goes with its json, and an orphaned lock
of the same age goes too.

Hooks and the runner worker resolve their home once in `main()` (`config.resolve_home`):
`CONTEXT_GUARD_HOME`, else the home of the installed kit they run from
(`<home>/.context-guard/kit`), else the user's home. A kit installed with `--home` into a
sandbox therefore keeps its state, events and handoffs there even when the runtime runs with
the real `HOME`; a source checkout or plugin package uses `CONTEXT_GUARD_HOME` or the user's home.
An installed kit whose `CONTEXT_GUARD_HOME` names a different home passes without touching
state or events (`config.foreign_home`): the runtime fires every registered kit, so a sandbox
run would otherwise also get duplicate events from the user's own installation.

## Install engine

`core/install.py` plans, then applies; adapters only describe their `InstallSpec`.

1. **Plan** compares the rendered spec with disk and the manifest: create, update, remove,
   JSON merges (unrelated hooks and settings preserved) and text edits. `core/hookfile.py` holds
   the hook command (`shlex`-joined python, script, event), hook ownership (the kit's hook script
   is one token of the command, so a wrapper or `hooks.py.bak` is not ours) and the grouped
   `{"hooks": {event: [{matcher?, hooks: [...]}]}}` merge used by Codex and Claude Code. A hook
   event that is not a list is a conflict; user groups and events are kept even when empty, and
   only what removing our handlers emptied goes.
2. **Conflicts.** A kit-owned file whose hash differs from the manifest (user edited) is
   reported and skipped unless `--force`; replaced files are backed
   up under `~/.context-guard/backups/<ts>/`. JSON merges leave a file that already holds
   exactly our entries untouched, so user hooks added after ours keep their place.
   **Stale kit output.** Unrecorded content that is demonstrably ours (for example after the
   manifest was lost) is replaced without `--force`, with a backup and its diff in the plan.
   The rule is deterministic: the adapter's `InstallSpec.kit_signatures` maps a path to lines
   that must all appear (whitespace-trimmed). For agent files, Cursor and Claude Code use
   `name: <role>` plus the first line of the role body; Codex, whose role body is one TOML string,
   uses the `name` and `description` lines. Anything else stays a conflict.
3. **Apply** writes each file to a temporary file beside it and renames it into place (through a
   symlink to its target, keeping the existing mode), so a hook importing a kit module during an
   install reads the old or the new file, never a partial one. It then records `state/<rt>/install-manifest.json` (sha256 of
   files, merges, edits, and the original text of a user JSON file a merge rewrote; files
   whose content would not change are not rewritten). **Dry run** prints the plan and writes nothing.
4. **Uninstall** reverses via the manifest; the shared kit goes only with the last runtime.
   A merged JSON file whose unmerged content equals its recorded original gets the original
   bytes back (formatting included); otherwise the unmerged object is written. Text edits revert
   exactly, including a missing final newline in `config.toml`.
5. **Doctor** re-renders, diffs against disk, checks hook commands and the interpreter.
6. **Kit.** One shared copy at `~/.context-guard/kit` (`core/`, `content/`, `adapters/`,
   `cli.py`) with `kit/.venv` created from `uv python find ">=3.14"` (or a `python3` >= 3.14).
   Hook command: `<kit>/.venv/bin/python <kit>/adapters/<rt>/hooks.py <event>`.
   The kit manifest also records `source` (the checkout installed from) and its git `commit`
   (omitted outside a git repo). On the `session` event (not `compact`), unless
   `kit_check.enabled` is false, `policy.session_context` adds one line when `git rev-parse HEAD`
   (1 s timeout) in `source` differs from the recorded commit; a missing source, commit or git
   failure adds nothing. The line names the update command for this home: `<kit python>
   <source>/cli.py install --runtime <each installed runtime> --home <home>`.
   With `kit_check.auto_update` (default false) the hook instead spawns
   `<kit python> <source>/cli.py install --runtime <each installed runtime> --home <home>` in
   its own session without waiting, writes its output to `~/.context-guard/kit-update.log`, and
   says the update starts in the next session. It runs without `--force`, so edited files are
   skipped and listed in that log. A log younger than 10 minutes means an update is running or
   just failed; the hook then shows the manual notice instead. The age check and touching the log
   happen under one lock (`kit-update.log.lock`), so sessions starting together launch one install. `status` prints source and commit,
   `doctor` reports the stale kit. The CoWork plugin package is not checked; repackage and re-upload it.

Hook trust is never written; each runtime reviews new hooks itself. Exit codes: 0 ok,
1 conflicts, doctor problems or nothing installed (doctor), 2 the command could not run.

## Internal runs

Runner child runs (classifier, planner, calibration labelling) start the runtime CLI with
`CONTEXT_GUARD_INTERNAL=1` (`core.runner.provider.environment`). Every hook that sees it
returns the native pass response before reading state or telemetry, so these child runs
neither get notes nor write events or handoffs.

## Optional runner

`core/runner/`, off by default. Each switch works on its own:

- `runner.enabled`: hand mechanical prompts to the runner. It turns on the classifier routing it
  needs; `classifier.enabled` is not required.
- `classifier.enabled`: classify prompts and add a delegation note (mechanical) or a fresh-chat
  note (new task); no job runs.
- `calibration.enabled`: log prompt decisions and periodically tune the prefilter.

Subagent prompts, prompts starting with `!`, bookkeeping prompts and handoff requests are never
routed. One contract on
every runtime: the adapter's provider _plans_ commands, `mechanical.execute_plan`
_validates_ the whole plan (fail closed: configured exact commands or a narrow Git grammar,
no shell metacharacters), then a detached worker _executes_. Scheduling is two-phase so a
job never runs beside a prompt that went through anyway: the hook stages the job (only when
`SCHEDULE_RESERVE_SECONDS` of its deadline remain), prints and flushes its blocking response,
and only then marks the job `scheduled`, outside the deadline. A worker whose job is still
`staged` after `CONFIRM_SECONDS` (15 s) marks it `cancelled`. The classifier is consulted only
when tokens reach the note threshold and inside the hook deadline. Reports reach the chat
through the handoff store's `pending/` directory.

- **Prefilter** (`prefilter.py`): a cheap recall-oriented check on the prompt (single line, length,
  question wording, `classifier.keywords`) that decides whether the classifier is asked at all.
- **Prompt services** (`prompt_services.py`): run the prefilter and, when the runner or classifier
  is on, ask the classifier and cache its label.
- **Calibration** (`calibration.py`): see below.

Validation (`mechanical.py`) accepts only exact `runner.commands` entries or this Git grammar:
`status`, `diff`, `log` with fixed flags, `branch --show-current`, `add <paths>`, `commit -m <msg>`
and a bare `push`; `add`, `commit` and `push` also need an affirmative request in the prompt.
`add` paths must resolve inside the workspace and must not start with `-` or `:`; Git runs with
`GIT_LITERAL_PATHSPECS=1`, so globs and pathspec magic are literal. `git commit` and `git push`
run the repository's own hooks. Any argument with shell characters, a shell or `env` as the
program, or `-c`/`-e`/`--eval`/`--command` rejects the whole plan.

**Calibration.** With `calibration.enabled`, each prompt's decision (prefilter result, label,
tokens, model, the prompt cut to `classifier.max_chars` + 1 characters and its full length, which
calibration's length check uses) is appended to `decisions.jsonl`, bounded to `calibration.window` lines.
Nothing is logged with calibration off. Once `first_run_prompts` decisions exist and
`interval_days` have passed since the last run, a prompt spawns a detached `hooks.py --calibrate`.
It labels unlabelled prompts in batches, tunes `keywords`, `short_keywords`, `short_max_words` and
`max_chars` on half of them and drops changes that score worse on the other half. With `mode`
`propose` it writes `calibration/proposal.json` and the next prompt carries a one-time note; the
user replies `calibration apply`, `calibration auto` (apply and switch to `auto`) or
`calibration keep` (discard and stay on `propose`). With `mode` `auto` the update goes straight to
the runtime's user config, the note says so once, and `calibration keep` switches back to
`propose`. Such a reply is intercepted only while calibration is on and a proposal is pending or
was just applied; otherwise it is an ordinary prompt.

Entry points of each adapter's `hooks.py`: `--worker <job.json>` plans and executes one staged
job; `--calibrate` runs calibration now; `--calibrate --dry-run` prints the result from existing
labels without model calls or writes.

| key | default | meaning |
| --- | --- | --- |
| `runner.model`, `classifier.model` | `""` | planner and classifier model; empty means the adapter's cheap tier |
| `runner.commands` | `[]` | exact argv lists the planner may use, e.g. `[["make", "test"]]`; `argv[0]` is a program name on `PATH`, not a path |
| `runner.max_commands` | 8 | commands per plan (at most 8) |
| `runner.timeout_seconds` | 300 | execution time per job (at most 1800) |
| `runner.plan_timeout_seconds` | 60 | planner call |
| `runner.cooldown_seconds` | 60 | minimum gap between scheduled jobs |
| `classifier.timeout_seconds` | 20 | classifier call, further bounded by the hook deadline |
| `classifier.cache_entries` | 5000 | cached labels |
| `classifier.max_chars`, `max_question_words`, `polite_words`, `keywords`, `short_keywords`, `short_max_words` | see `content/defaults.json` | prefilter rules (`prefilter.py`) |
| `calibration.mode` | `propose` | `propose` or `auto` |
| `calibration.first_run_prompts` | 1000 | logged decisions before the first run |
| `calibration.interval_days` | 30 | days between runs |
| `calibration.window` | 10000 | decision lines kept and read |
| `calibration.sample_rejected` | 2000 | prefilter-rejected prompts sampled for labelling |
| `calibration.batch_size` | 50 | prompts per labelling call |
| `calibration.max_fp_per_tp` | 3 | extra classifier calls accepted per extra chore caught |
| `calibration.min_mechanical` | 20 | labelled mechanical prompts needed to tune |
| `calibration.model` | `""` | labelling model; empty means `classifier.model`, then the cheap tier |

State lives in `paths.state_dir/<runtime>/`: `decisions.jsonl`, `classifier_cache.json`,
`calibration/` (`proposal.json`, `report.md`, `last_run.json`, `run.log`, locks) and `runner/`
(`<job>.json` record and `<job>.log` output per job, `last.json` for the cooldown).

## Failure contracts

- Missing, unreadable or changed telemetry, bad payloads, config errors and any unexpected
  exception at the hook boundary: **fail open** (pass, one stderr line for the unexpected case).
- Runner command validation: **fail closed**.
- Every hook finishes within `deadline.HOOK_WORK_SECONDS` (4 s) and passes on overrun.
- Installers preserve user files, dry runs write nothing, and trust is the runtime's call.

## Verification

Run `uv run --python 3.14 python -m unittest discover -s tests -t .` plus each adapter
suite (see `AGENTS.md`). Installer checks use `--home` with a temporary directory, never a
live home. Fixture tests prove contracts; hook execution inside a real runtime needs a live
smoke run, `scripts/sandbox_smoke.sh`, which uses the real logins and sandboxes only the hook
and config wiring. Each runtime doc ends with an Unverified list of facts not yet observed.
