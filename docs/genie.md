# Genie

Shared behaviour is in [architecture.md](architecture.md). This page lists only what differs.

## Configuration directory

The installer and plugin helpers use an absolute `GENIE_HOME`, defaulting to `<home>/.genie`
when it is empty, unset or relative. Hooks, config, agent registrations, skills and installed
policy files all target that directory. Use the same environment for install, update,
status, doctor and uninstall. Each alternate directory has an independent install manifest
and setup-offer markers, identified by a 32-character hash of its resolved path. Handoffs,
hook state and the kit remain shared under `~/.context-guard`; the kit stays until the last
profile is removed. `--home` does not override `GENIE_HOME`; unset it or point it into the
temporary home for sandbox checks. Transcript reads follow hook payload paths.

| item | Genie |
| --- | --- |
| Telemetry | Tail of the rollout JSONL from `transcript_path` (last `rollout.tail_bytes`, 2 MiB): latest `token_count.last_token_usage` and `model_context_window`. A later compaction record invalidates the count. Subagent rollouts are found by `agent_id` beside the parent date directory, including the adjacent days. Missing or changed telemetry fails open. |
| Events | `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SubagentStop` in `$GENIE_HOME/hooks.json` (else `~/.genie/hooks.json`). The installer merges only context-guard handlers. `SubagentStop` validates and passes; see [Unsupported](#unsupported). |
| Channel | Policy and handoff list through `SessionStart` `additionalContext`; prompt and tool notes through same-hook `additionalContext`; prompt blocks include `reason` and `systemMessage`; Stop refresh uses `decision: block` to continue. A compacted session is `SessionStart` with `source: compact`. |
| Agents | `$GENIE_HOME/agents/<role>.toml` (else `~/.genie/agents`), registered in a kit-owned `[agents.<role>]` section of `config.toml`. User tables and settings are preserved and uninstall reverses only the owned section. |
| Capabilities | `features.shell_tool = false` is enforced for a role without shell. Write, network, MCP and git-push restrictions are advisory because subagents inherit the parent turn's sandbox and approvals. |
| Per-spawn override | `spawn_agent` `model` and `reasoning_effort`; named roles use `agent_type` and `fork_turns: "none"`. Plugin users without installed role agents spawn the default agent with the packaged role brief. |
| Tools | Direct `apply_patch`, shell tools, and Genie's namespaced `functions.exec` code mode are classified for handoff reads/writes. Namespaced `spawn_agent` calls are logged as delegation events. |
| Transcripts | `$GENIE_HOME/sessions/<yyyy>/<mm>/<dd>/` (else `~/.genie/sessions/...`). |

The recorded Genie 0.2.1 hook schemas match the Codex-compatible response contract used by the
adapter. The only observed SessionStart input difference is that Genie's `source` enum has no
`fork` value. Fixtures in `tests/fixtures/genie/` come from `scripts/record_payloads.py` and the
installed Genie binary.

## Handoff Write Access

Handoffs live outside the workspace by default. With `handoff_access.enabled` (default `true`),
the script installer adds only the resolved handoff directory to
`[sandbox_workspace_write].writable_roots` in `$GENIE_HOME/config.toml` or
`~/.genie/config.toml`. Existing settings and foreign shapes are preserved; unsafe shapes produce
an install note instead of a conflict. Uninstall removes only an unedited owned block or line.

A plugin user runs this once, then starts a new session:

```sh
sh "<plugin root>/hooks/context-guard-hook" adapters/genie/access.py install   # or uninstall
```

The helper records its own `genie-handoff-access` manifest, supports `--dry-run`, `--force` and
`--home`, and is offered once by SessionStart when access is missing.

## Tiers

<!-- BEGIN generated: tiers -->
| tier | model | effort |
| --- | --- | --- |
| cheap | `gpt-5.6-luna` | `low` |
| standard | `gpt-5.6-sol` | `medium` |
| deep | `gpt-5.6-sol` | `high` |
<!-- END generated: tiers -->

## Plugin

`cli.py package --runtime genie --out DIR [--marketplace NAME]` renders a Genie plugin. Use either
the script install or plugin install, never both, because both register the same hooks.

From a checkout, the repository installer packages and installs the Genie plugin plus named role
agents in one command:

```sh
scripts/plugin_live.sh install --only genie
```

This changes the real Genie home, removes an existing Genie script install, and uses
`~/.context-guard/plugins` as the local marketplace. Preview it first with `--dry-run`; reverse it
with `scripts/plugin_live.sh uninstall --only genie`.

| file | role |
| --- | --- |
| `.codex-plugin/plugin.json` | Genie plugin metadata, skills path, author and interface fields. Hooks are discovered from root `hooks.json`; the current Genie validator rejects a top-level `hooks` field. |
| `hooks.json` | The five handler groups, each running `adapters/genie/hooks.py` through the packaged launcher. |
| `skills/<name>/SKILL.md` | Rendered shipped skills; `install-context-guard` is omitted. |
| `policy/context-guard.md` | Policy for a home with all named role agents. |
| `policy/context-guard-roleless.md` | Policy that spawns Genie's default agent with `roles/<role>.md` when named agents are absent. |
| `roles/<role>.md` | Runtime-neutral role body plus advisory capability limits. |
| `core/`, `content/`, `adapters/genie/` | Vendored implementation; tests and caches are excluded. |

Install a packaged marketplace and plugin with:

```sh
genie plugin marketplace add <marketplace directory>
genie plugin add context-guard@<marketplace>
```

Start Genie, review and trust the hooks, and start a new thread. The plugin cannot install named
agents itself, so run this once if desired:

```sh
sh "<plugin root>/hooks/context-guard-hook" adapters/genie/agents.py install   # or uninstall
```

The command writes only role TOMLs under `$GENIE_HOME/agents`, records a `genie-agents` manifest,
preserves edited or foreign files, and supports `--dry-run`, `--force` and `--home`. Without it,
the packaged role-less policy provides the same responsibilities and tiers through the default
agent. Verify a prompt creates `~/.context-guard/state/genie/events.jsonl` and `/hooks` shows the
five trusted handlers.

## Unsupported

- **Idle-parent wake:** Genie 0.2.1 has no public equivalent of `codex queue --thread ...`. The
  `SubagentStop` hook therefore passes without starting a detached waker. Genie still delivers
  subagent results through its native collaboration channel while the parent is running, but
  context-guard cannot start a new parent turn after the parent becomes idle. The rendered policy
  therefore keeps the parent turn active with one long event wait while required results are
  outstanding. Fire-and-forget work may outlive the turn only with explicit notice that delivery
  waits for the user's next turn.
- **Optional model runner:** `genie exec` exposes neither Codex's `-c` configuration overrides nor
  a `--model` flag. context-guard therefore cannot make an isolated, model-pinned classifier,
  planner or calibration call without mutating the user's Genie profile. The Genie provider fails
  open, so `classifier.enabled`, `runner.enabled` and calibration model calls have no effect. The
  core warning, handoff and delegation policies do not depend on this optional subsystem.

## Unverified

- A live plugin install has not yet been exercised by the repository smoke script.
- The exact `tool_name` of a real `spawn_agent` PostToolUse payload was not present in the recorded
  local rollouts; namespaced and bare forms are accepted and unit-tested.
- Per-spawn model override precedence when a named role file also pins a model is not observed.
