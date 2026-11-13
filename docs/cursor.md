# Cursor

Shared behaviour is in [architecture.md](architecture.md). This page lists only what differs.

## Directory overrides

Cursor documents `CURSOR_CONFIG_DIR` for its CLI configuration file, but does not establish
that it relocates user hooks, rules, agents or chat storage. context-guard therefore keeps
these install paths under `<home>/.cursor`; it does not interpret that variable as a runtime
home override. Custom telemetry locations remain configurable through the Cursor adapter's
telemetry settings. This gap needs confirmation of Cursor's hook and extension discovery
contract before an override can safely relocate the installed files.

| item | Cursor |
| --- | --- |
| Telemetry | Chat record `composerData:<conversation_id>` in Cursor's `state.vscdb` (read-only, 1 s timeout): `contextTokensUsed`, `contextTokenLimit`. Path from `telemetry.state_db` or the per-platform default. Cursor CLI (`cursor-agent`) chats are not in that database; when it has no record, the CLI's own store `<telemetry.cli_chats>/<workspace hash>/<conversation_id>/store.db` (default `~/.cursor/chats`, found by conversation id, read-only) is used: `meta` row `0` holds hex-encoded JSON whose `latestRootBlobId` names a protobuf blob in `blobs`; its field 5 is the context usage message with tokens used (field 1) and context window (field 2). `telemetry.window_fallback` applies when no window is known. Any changed shape, invalid count or undecodable protobuf means no telemetry (fail open). |
| Events | `sessionStart`, `preCompact`, `beforeSubmitPrompt`, `preToolUse` (matcher `Task`, spawns only), `postToolUse`, `stop` (`loop_limit` 1) in `~/.cursor/hooks.json`, each with the shared hook timeout (5 s), registered by `mapping.py` and merged without touching other hooks; a file that already holds exactly these entries is left as is. `subagentStart`/`subagentStop` are not registered: they can only pass. |
| Subagent detection | `parent_tool_call_id` on tool hooks. `sessionStart`, `beforeSubmitPrompt` and `stop` do not fire inside subagents, so leaf observations come from tool hooks only. `is_background_agent` is a cloud agent, not a subagent. Leaf state and telemetry are keyed by `conversation_id`, which assumes each subagent has its own conversation id (as in every recorded subagent payload). A subagent carrying its parent's id would read the parent's tokens and share its state: the budget note would follow the parent's size, and a leaf observation under the note threshold would re-arm the parent's fired tiers. |
| Compaction | `preCompact` records a pending session/generation; only its matching explicitly completed, non-loop stop confirms it. The next model-context opportunity (`postToolUse` or `sessionStart`) delivers recovery policy and handoff facts and rearms warning tiers once. Low usage alone never confirms recovery or rearms tiers. |
| Channel | Policy in `~/.cursor/rules/context-guard.mdc` (`alwaysApply`, reaches subagents); handoff list through `sessionStart` `additional_context` (fire-and-forget, best effort), which always starts with the marker line `context-guard hooks: active.` so the plugin rule can tell running hooks from missing ones. |
| Notes | `beforeSubmitPrompt` cannot add model context, so a prompt note is held (also across a `stop` that sends no follow-up) and delivered as `additional_context` after the next tool result or at the next `sessionStart`; a tool-result note goes out as `additional_context` at once. Each delivered note is logged as `note_delivered`; a held note is dropped when its tier re-arms, when a tool result writes the handoff, or when `stop` sends its refresh follow-up, which supersedes it. Block: `{"continue": false, "user_message"}`. Stop refresh: `followup_message`. |
| Unreturned subagents | A foreground Task result is lost when the parent turn is aborted or Cursor retries the parent request after a dropped connection; the subagent may keep running. `adapters/cursor/subagents.py` records each spawn under the parent's Task `tool_use_id` (role, parent conversation, spawn time, transcript directory `<parent transcript dir>/subagents`, and from the subagent's tool hooks its agent id and last activity, kept to the minute). A foreground spawn closes when the parent's Task `postToolUse` reports `status` `success` (output without a status counts as returned). A background spawn (`"isBackground": true` in that result, which also names its `agentId`) is detached instead: it is tracked until the subagent's own `stop` reports `completed` (mapped through the parent call its tool hooks recorded) or, because Cursor does not send that hook for background subagents (none fired in 3.26), until its transcript `<transcript dir>/<agent id>.jsonl` ends with a `{"type": "turn_ended", ...}` record. Cursor 3.26 sends `"transcript_path": null` on the Task `preToolUse` and `postToolUse` and on subagent tool hooks, so a spawn recorded without a transcript directory takes it from the parent's later hooks that carry one (only the last 4 KiB are read, only for detached spawns idle for a minute or spawns past the silence threshold). At any parent `stop` with `loop_count` 0 (completed, aborted or error), each foreground spawn still open is logged as `subagent_unreturned` (role, agent, status) and a note naming the role, agent id, last activity and transcript is queued. A parent `postToolUse` of a non-Task tool that started more than 30 s after a still-open foreground spawn (start = now − `duration` ms) means the parent moved on without its result: the spawn is detached, logged as `subagent_orphaned` (role, agent) and an orphan note queued; tools started within 30 s of the spawn may run beside it in the same step and are ignored, as are Task results. On each parent `postToolUse` and `sessionStart`, every open or detached spawn whose last activity (else its spawn time) is older than `subagents.silence_minutes` (10) and whose transcript does not show a finished turn gets one note per silence period (role, agent id, minutes, transcript, what to check) and a `subagent_silent` event (role, agent, minutes). Queued notes are appended once to the parent's next tool-result or `sessionStart` `additional_context`; subagents never get them. |
| Agents | `~/.cursor/agents/<role>.md`, frontmatter `name, description, model, readonly`; `model` is the tier's plain `call` slug (an option list such as `[effort=low]` is not a valid id: Cursor silently falls back to the parent's model; Cursor has no per-agent context size, so `-long` variants differ only in name). Cursor strips `model` from plugin agents, which then inherit the parent's model, but keeps it for disk agents, which win over a same-named plugin agent in the IDE. `hooks_install.py install` therefore also writes the role files to `~/.cursor/agents` (owned by its manifest; `uninstall` removes exactly those). `cursor-agent` (CLI) reads only the workspace `.cursor/agents` and plugin agents, never `~/.cursor/agents`. |
| Capability limits | Only `readonly` is enforced, and it is set when `shell` is false (it also removes the shell). `network`, `mcp`, `git_push` and `write` for shell roles are advisory; the policy's role table lists them as limits Cursor does not enforce. No per-agent tool list exists. |
| Per-spawn override | Task `model` slug; effort is encoded in the slug suffix. No per-call context window. The policy tells the orchestrator to pass the tier's slug as `model` on every Task (the user authorises it), because plugin agents inherit the parent's model and the CLI ignores `~/.cursor/agents`. A Task without `model` is logged as `subagent_spawn` with the role's default tier and its `call` slug and suffix effort. |
| Shell handoff access | `Shell` commands resolve relative handoff paths against `tool_input.cwd`, else the first workspace root. |
| Transcripts | `~/.cursor/projects/<workspace>/agent-transcripts/`. |
| Live smoke | `cursor-agent -p` fires neither `beforeSubmitPrompt` nor `stop`, so `scripts/sandbox_smoke.sh` drives one interactive `cursor-agent` session with two prompts through `scripts/cursor_interactive.exp` (needs `expect`). |

## How the orchestrator is woken

Cursor wakes the parent natively: when a background Task subagent finishes, Cursor starts a new parent turn with its result, without a user message, so the shared policy's "end your turn and pick up each result as a new turn" holds as written. context-guard adds only fallback notes for the cases the runtime cannot report: a background subagent silent past `subagents.silence_minutes` (its transcript shows no finished turn), a foreground subagent whose result never arrived, and one the parent moved on from (see Unreturned subagents above). The notes reach the parent on its next tool result or `sessionStart`; an idle parent fires no hook, so it sees them at its first tool result after the user next writes (`beforeSubmitPrompt` cannot add context).

The independent shared wake driver replays a recorded Cursor CLI capture using
`adapters/cursor/tests/native_lifecycle.py`. It requires a correlated background
Task and agent identity, a completed root generation before the actual child token
read, actual native child output, a matching **subagent** completion notification
(a shell notification is insufficient), and distinct root assistant text
in a new completed generation with no further terminal input. Native messages have
per-session order and blob identities; root text messages are correlated in order
with completed hook generations, rather than inventing native message timestamps.
The replay establishes native wake but does not establish compliance with the child's
requested delay.
Fixtures are generated only by `scripts/record_payloads.py --runtime cursor
--cursor-lifecycle-root <capture>`; this explicit route reads capture artifacts only.
It hashes projected artifacts separately from reported original DB/WAL hashes;
projected events are not a database snapshot. Replay is neither fresh host execution
nor IDE wake verification, and absent subagent lifecycle hooks remain a native gap.

`scripts/plugin_live.sh check --run` checks the wake with `cursor-agent -p --output-format stream-json --force` (`--force` lets the subagent run `sleep`): print mode waits for background subagents after the parent's turn and runs the parent's follow-up turn itself, then prints one `result`. The case passes when a Task call reports `isBackground: true` and that final reply quotes the subagent's token. The IDE's wake is not covered headlessly; `scripts/plugin_live.sh checklist` lists it as a manual step.

## Plugin

`cli.py package --runtime cursor` renders a Cursor plugin (`adapters/cursor/plugin.py`; packaging itself is in [distribution.md](distribution.md)):

| path | content |
| --- | --- |
| `.cursor-plugin/plugin.json` | `name` `context-guard`, `version`, `description`; no `hooks` |
| `hooks/context-guard-hook` | the shared launcher; no `hooks/hooks.json` is shipped |
| `rules/context-guard.mdc` | the installed policy rule (`alwaysApply: true`) followed by a `Hooks` section: the hooks install command and when to offer it |
| `agents/<role>.md`, `skills/<name>/SKILL.md` | as installed; Cursor does not namespace plugin agents, so names stay plain. `install-context-guard` is not shipped |
| `core/`, `content/`, `adapters/cursor/` | vendored code, including `adapters/cursor/hooks_install.py` |

The Cursor app (3.25) loads plugin hooks only when Settings → Agents → Third-Party Imports ("Include Third-Party Plugins, Skills, and Other Configs") is on. With it off, rules, skills and agents still load and the Hooks output channel logs `Claude Code hooks disabled (thirdPartyExtensibilityEnabled off)`, but `~/.cursor/hooks.json` is still read. The same setting imports Claude Code and Codex configuration, which duplicates other configs. So the plugin registers no hooks, and turning the setting on can never run them twice; the user registers them once in `~/.cursor/hooks.json`.

### Registering the hooks

1. Install the plugin (marketplace, or a release zip unzipped into `~/.cursor/plugins/local/context-guard`).
2. Register the hooks and the role agents (which pin each role to its tier model in the IDE, see Agents above). The simplest way is to ask the Cursor agent to enable the context-guard hooks: the shipped rule knows its own path and runs the command below. By hand:

   ```sh
   sh "<plugin root>/hooks/context-guard-hook" adapters/cursor/hooks_install.py install
   ```

   `<plugin root>` is the directory holding `.cursor-plugin/` and `rules/context-guard.mdc`: `~/.cursor/plugins/local/context-guard` for a local install, `~/.cursor/plugins/cache/<marketplace>/context-guard/<version or commit>/` for a marketplace install (`ls -d ~/.cursor/plugins/cache/*/context-guard/*/` lists it).
3. Reload the window (Developer: Reload Window).
4. Turn Settings → Agents → Third-Party Imports off unless you use it for something else: context-guard does not need it, and it imports duplicate Claude Code and Codex configuration.
5. Verify: `~/.cursor/hooks.json` has `sessionStart`, `preCompact`, `beforeSubmitPrompt`, `preToolUse`, `postToolUse` and `stop` entries running `~/.context-guard/cursor-hooks/hooks/context-guard-hook`; after the next prompt in a new chat, the Hooks output channel shows them running and `~/.context-guard/state/cursor/events.jsonl` ends with a `prompt` record.

While the hooks are not registered, the rule's `Hooks` section has the main agent offer this step once, ever. The signal is the `sessionStart` context: the hooks always send one, starting with `context-guard hooks: active.`, so a main chat without it has no running hooks (or none that reached the model). In such a chat the agent checks for the marker file `~/.context-guard/state/offers/cursor-hooks` (under `$CONTEXT_GUARD_HOME` instead of `~` when that is set): if it exists the agent says nothing; otherwise it offers the step in its reply and creates the marker with a file-writing tool. `hooks_install.py install` also creates the marker, and `uninstall` keeps it, so a user who installed and later removed the hooks is not offered them again. Delete the marker to be offered the step again. Subagents ignore the section.

Updating: rerun step 2 after a plugin update to refresh the copy the hooks run from; until then they keep running the previous version (an unchanged copy is left as is).

Removing: run the same command with `uninstall`, from the plugin or, once the plugin is gone, from the copy: `sh ~/.context-guard/cursor-hooks/hooks/context-guard-hook adapters/cursor/hooks_install.py uninstall`. Then remove the plugin and reload the window.

A marketplace install lives in a versioned cache directory that a plugin update replaces, so the registered commands cannot name the plugin root. `hooks_install.py` copies the code the hook runs (vendored `core/`, `content/`, `adapters/cursor/`, `LICENSE`, `NOTICE`) plus the launcher into `~/.context-guard/cursor-hooks/` and registers the installer's events, matchers, `loop_limit` and timeouts, each as `sh ~/.context-guard/cursor-hooks/hooks/context-guard-hook adapters/cursor/hooks.py <event>` (absolute path). The copy keeps working when the plugin moves or is removed; rerun `install` after a plugin update to refresh it. It also writes the rendered role files to `~/.cursor/agents/`. It records the copy, those agent files and the `hooks.json` merge in its own manifest (`~/.context-guard/state/cursor-hooks/install-manifest.json`), keeps every other hook, reports edited copy files as conflicts (kept unless `--force`, with backups), writes nothing with `--dry-run`, and acts on another home with `--home`. `uninstall` removes only what it wrote (agent files included), including the bytecode caches the hooks left in the copy: `hooks.json` gets its other entries back unchanged, or is deleted if the install created it.

The vendored hook reads the baked `adapters/cursor/config.json` and resolves its home as `CONTEXT_GUARD_HOME`, else the user's home. A marketplace listing (`--marketplace NAME`) goes to `.cursor-plugin/marketplace.json` beside `plugins/`. To try a release zip, unzip `context-guard-cursor-<version>.zip` into `~/.cursor/plugins/local/context-guard` and follow the steps above.

An installed context-guard Claude plugin registers its hooks in Cursor when Third-Party Imports is on; the Claude adapter ignores payloads carrying `cursor_version`, leaving those events to the Cursor hooks.

Use one path per machine: the script install and `hooks_install.py` both register hooks in `~/.cursor/hooks.json`, so both would fire on every event and double notes, state and logs. `hooks_install.py install` warns when the script install is present; uninstall one before enabling the other.

## Compaction recovery

The adapter registers [`preCompact`](https://cursor.com/docs/hooks#precompact) as observation only.
It records a pending generation under the native session identity. Only a `stop` with the same
session and generation, explicit `status: completed` and zero loop count confirms completion.
Cancelled, failed, missing-status and mismatched stops cannot confirm it. Duplicate initiations
and stops cannot rearm warnings or redeliver recovery for a consumed generation. Only one
pending/ready recovery is tracked at a time; overlapping initiations are ignored and their
identities retired so duplicate callbacks cannot later become new recoveries. A lower token
reading alone never rearms warnings.

Confirmed recovery waits for the next main-session `postToolUse` or `sessionStart` response.
Task pre-hooks and prompts cannot carry it. Recovery context contains the shipped policy,
open handoff listing and pending reports, composed with warnings and child notes in the same
state transaction. Successful rendering commits consumption and clears fired tiers once;
render failure or deadline fallback before commit preserves retry. Persisted response state
is the hook's delivery boundary; host receipt and model compliance cannot be acknowledged by hooks.

The CLI recovery fixture covers manual `/summarize`, a matching completed stop,
an independently rendered Summary and later recall of Read-hook `additional_context`.
Delivery and recall do not guarantee instruction compliance or handoff writes.
The CLI may choose a different summarization model from the explicit conversation model.
IDE and automatic compaction recovery remain unverified; the adapter uses the nearest
equivalent lifecycle and context channel there, conditional on those hooks being emitted.

The source-backed recipe fixtures are generated only with
`scripts/record_payloads.py --runtime cursor --cursor-capture <captured-hooks.jsonl>`.
Their hashes identify the original hook JSONL capture, not original native database bytes;
sanitized database snapshots are separate evidence and are not read by this recorder.

## Handoff writes outside the workspace

Handoffs live in `~/.context-guard/handoffs` (`paths.handoff_dir`), outside the workspace.
Cursor CLI users can explicitly install a project permission with:

```sh
uv run --python 3.14 python adapters/cursor/access.py install --workspace /absolute/project --home /explicit/home
```

From a packaged plugin, use `sh "<plugin root>/hooks/context-guard-hook" adapters/cursor/access.py`
with the same arguments. Both `--workspace` and `--home` are required existing directories;
workspace aliases and the configured existing handoff directory resolve canonically. Directory
names containing permission pattern syntax are rejected rather than broadening the grant.
Configuration uses defaults, adapter config, the explicit home's user config (including `runtimes.cursor`),
then `CONTEXT_GUARD_PATHS_HANDOFF_DIR`. The helper adds only
`Write(<canonical handoff directory>/**)` to `permissions.allow` in the project's
`.cursor/cli.json`. `CURSOR_CONFIG_DIR` does not relocate this project file. It grants no Shell
permission and changes neither global CLI settings nor hook trust. Ordinary installation and
`handoff_access.enabled` do not install this optional project grant.

Use `--dry-run` to inspect without writing, and `uninstall` with the same home and workspace
to retire the recorded grant, even if configuration has since changed. A changed configured
handoff directory requires uninstalling the previous grant first. Ownership manifests are scoped
to the canonical project under the explicit home's `.context-guard/state/`; backups use that
home's `.context-guard/backups/`. Other entries, denies and user edits are preserved. Preexisting
grants remain user-owned; an unchanged file returns to its original bytes on uninstall, including
empty containers. Malformed native JSON or permission containers are reported without replacement.
Readonly filesystem errors are reported. Potential overlapping project Write denies are diagnosed
and retained; runtime readonly, protected paths, ignore rules and higher-level native restrictions
remain overrides, so an installed grant alone is not proof of effective access.

The project grant allows external Writes without approval; an ungranted external Write
can prompt for approval and explicit denies still block writes.
OS temporary directory exemptions can independently permit ungranted writes: this grant is
an auto-approval mechanism, not an exclusive filesystem boundary. See the
[CLI permission contract](https://cursor.com/docs/cli/reference/permissions).

The IDE uses Settings → Agents → External-File Protection, which the helper does not change.
IDE file-tool approval remains unverified. IDE sandbox `additionalReadwritePaths` applies to
terminal access and does not establish approval for native file-tool writes.

## Tiers

Agent files and the Task `model` both use the plain `call` slug (valid ids: `cursor-agent --list-models`).

<!-- BEGIN generated: tiers -->
| tier | call |
| --- | --- |
| cheap | `claude-haiku-5-5-thinking-low` |
| standard | `claude-sonnet-5-5-medium` |
| deep | `claude-opus-5-5-high` |
<!-- END generated: tiers -->

## Unverified

- `~/.cursor/rules/*.mdc` as a global always-on channel is observed but undocumented.
- `parent_tool_call_id` is undocumented (present in observed payloads and wire protos); based on a small sample.
- That every subagent gets its own `conversation_id` rests on the same small sample.
- `sessionStart` additional-context size limit and delivery ordering are unknown.
- The CLI store layout (`meta`/`blobs` tables, root blob field 5) is undocumented and observed on `cursor-agent` 2026.10.01; the numbers matched the CLI's own context display.
