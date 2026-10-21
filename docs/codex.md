# Codex

Shared behaviour is in [architecture.md](architecture.md). This page lists only what differs.

| item               | Codex                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Telemetry          | Tail of the rollout JSONL (`transcript_path`, last `rollout.tail_bytes`, 2 MiB): latest `token_count` `last_token_usage` and `model_context_window`. A compaction record after it invalidates the count. For subagents, the single `rollout-*<agent_id>.jsonl` in the parent rollout's date directory or the day before or after.                                                                                                                  |
| Events             | `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop` in `~/.codex/hooks.json` (merged; left as is when it already holds exactly these handlers). `SubagentStop` is not registered: leaf mode passes every stop, so it could only pass; a reinstall removes an earlier registration.                                                                                                                                                         |
| Subagent detection | `agent_id` present. Subagent hooks carry the parent's `session_id`, so state is keyed by `agent_id`. `SessionStart` and `Stop` never fire in subagents.                                                                                                                                                                                                                                                                  |
| Channel            | Policy and handoff list through `SessionStart` `additionalContext`. `PostCompact` cannot carry context, so compaction is handled by `SessionStart` with `source: compact` (re-arms tiers, re-injects). Default cap is about 2,500 tokens per message; the handler sets `additionalContextLimit` from `hook_output.session_context_limit` (6000; env `CONTEXT_GUARD_HOOK_OUTPUT_SESSION_CONTEXT_LIMIT`, applied at install).                                                                                                                |
| Notes              | UserPromptSubmit/PostToolUse `additionalContext` in the same hook, logged as `note` and `note_delivered`; block `{"decision":"block","reason","systemMessage"}`; Stop refresh as `decision: block` (continue).                                                                                                                                                                                                                                                                            |
| Agents             | `~/.codex/agents/<role>.toml`, registered in a kit-owned `[agents.<role>]` section of `config.toml` (parsed with `tomllib`). Codex rewrites `config.toml` itself and inserts new tables (hook trust, projects, tui) before the trailing END marker, so install and uninstall move any table other than the kit's `[agents.<role>]` to just after the section. An edited kit table or an existing agent name outside the section is a conflict. |
| Capability limits  | Role TOML ignores `sandbox_mode`, approval, network and MCP; subagents inherit the parent turn. Only `features.shell_tool = false` is real (set when `shell` is false). `write`, `network`, `mcp`, `git_push` are advisory lines in `developer_instructions` and listed as not enforced in the policy's role table.                                                                                                                            |
| Per-spawn override | `spawn_agent` `model` and `reasoning_effort`; policy also asks for `agent_type` and `fork_turns: "none"`. A spawn without `model` runs the role file, which pins the role's default tier, and is logged as `subagent_spawn` with that tier and its `model`/`effort` (a different `reasoning_effort` alone is matched to its tier). |
| Shell handoff access | Shell tools resolve relative handoff paths against the call's `workdir`, else the payload `cwd`. |
| Transcripts        | `~/.codex/sessions/<yyyy>/<mm>/<dd>/`.                                                                                                                                                                                                                                                                                                                                                                                                         |

## Tiers

<!-- BEGIN generated: tiers -->
| tier | model | effort |
| --- | --- | --- |
| cheap | `gpt-6-luna` | `low` |
| standard | `gpt-6.1-sol` | `medium` |
| deep | `gpt-6.1-sol` | `high` |
<!-- END generated: tiers -->

## Plugin

`cli.py package --runtime codex --out DIR [--marketplace NAME]` renders a Codex plugin (shared
packaging rules in [distribution.md](distribution.md)). Use one path per runtime: with both the
installed kit and the plugin, both sets of hooks fire.

| file | role |
| --- | --- |
| `.codex-plugin/plugin.json` | `name`, `version`, `description`, `skills: "./skills/"`, `hooks: "./hooks/hooks.json"`, `interface` (`displayName`, `shortDescription`, `developerName`, `category`). No `.claude-plugin/` or root `plugin.json`: Codex reads those too and would mix formats. |
| `hooks/hooks.json` | The installer's handler groups (same events, timeout and `additionalContextLimit`), each `sh "${PLUGIN_ROOT}/hooks/context-guard-hook" adapters/codex/hooks.py <event>`. Codex sets `PLUGIN_ROOT` in the hook environment. |
| `skills/<name>/SKILL.md` | Rendered like the installer's; Codex names them `context-guard:<skill>`. `install-context-guard` is not shipped. |
| `policy/context-guard.md` | The installer's policy, for a Codex home with the named role agents. |
| `policy/context-guard-roleless.md` | The same policy for a home without them: spawn Codex's default agent (no `agent_type`, `fork_turns: "none"`) with the role's tier `model` and `reasoning_effort`, and start the message with `roles/<role>.md`. Every denied capability is listed as not enforced. Ends with the agents install command. |
| `roles/<role>.md` | Role brief: the role body plus an advisory line for every denied capability, shell included. |
| `core/`, `content/`, `adapters/codex/` | Vendored code. |

`.agents/plugins/marketplace.json` (with `--marketplace`) lists the plugin as
`{"source": "local", "path": "./plugins/context-guard"}`, `installation: AVAILABLE`,
`authentication: ON_INSTALL`.

### Installing the plugin

1. Add the marketplace and the plugin: `codex plugin marketplace add <owner/repo|path>` (for a
   release, the unzipped `context-guard-codex-marketplace-<version>.zip` directory), then
   `codex plugin add context-guard@<marketplace>` (`context-guard@context-guard` for a release).
   Codex copies it to `~/.codex/plugins/cache/<marketplace>/context-guard/<version>/`, the
   `<plugin root>` below.
2. Start Codex and trust the hooks in `/hooks`. Plugin hooks run only after this review, and a
   plugin update that changes the hooks needs a new one. context-guard never writes hook trust.
3. Optional, for the named subagents: run the agents command below, then start a new session.
   Without them the role-less policy, which names this command with the plugin root filled in,
   spawns Codex's default agent per role.
4. Verify: `/hooks` lists the four context-guard handlers as trusted, and after a prompt
   `~/.context-guard/state/codex/events.jsonl` ends with a `prompt` record; with the agents,
   `~/.codex/agents/` holds one `<role>.toml` per role.

Updating: replace the marketplace directory with the new release (or `codex plugin marketplace
upgrade` for a Git marketplace), reinstall with `codex plugin remove` and `codex plugin add
context-guard@<marketplace>`, review `/hooks` if asked, and rerun the agents command from the new plugin root so the role files match.
Removing: run the agents command with `uninstall` first (it lives in the plugin), then
`codex plugin remove context-guard@<marketplace>`.

Plugins cannot ship agents (Codex loads them only from `~/.codex/agents/*.toml` or
`.codex/agents/`) or always-on instructions, so the policy reaches the model only through
`SessionStart` context. At `SessionStart` the packaged `hooks.py` injects
`policy/context-guard.md` when every role's `<name>.toml` exists in `$CODEX_HOME/agents` (else
`~/.codex/agents`), otherwise the role-less policy with the plugin root's absolute path filled in.
A failure to read either passes without a policy. To get the named agents, the user runs:

```sh
sh "<plugin root>/hooks/context-guard-hook" adapters/codex/agents.py install   # or uninstall
```

`agents.py` writes only the role agent TOMLs, rendered exactly as the installer renders them, and
records them in its own manifest (`~/.context-guard/state/codex-agents/install-manifest.json`), so
`uninstall` removes only what it wrote. Edited or foreign files are conflicts and are kept unless
`--force` (backups are kept); `--dry-run` writes nothing; `--home` acts on another home. It does
not touch `config.toml`.

The step is offered once, ever. When `SessionStart` of a main session picks the role-less policy,
no install manifest exists for `codex-agents` or the script install (`codex`, which renders the
agents itself), and the marker file `~/.context-guard/state/offers/codex-agents` (under
`CONTEXT_GUARD_HOME` when set) is missing, the hook creates the marker and appends a note asking
the model to offer the agents command once. Later sessions, subagents, and a marker that cannot be
written get no note. `agents.py install` also creates the marker, and `uninstall` keeps it. Delete
the marker to be offered the step again.

## Unverified

- A role file `model` is documented to win over an explicit spawn `model`; observed calls do not separate the two, so per-spawn override is unproven when the role file also sets a model.
- Injection of `SessionStart` context at compaction time is not distinguishable in rollouts.
- The exact `tool_name` of `spawn_agent` in a real `PostToolUse` payload was not observed (documented alias `Agent`).
- `additionalContextLimit` handling by the hooks schema is verified in the binary only.
- `agent_id` on `UserPromptSubmit`/`PostToolUse` comes from the schema and docs.
