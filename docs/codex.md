# Codex

Shared behaviour is in [architecture.md](architecture.md). This page lists only what differs.

| item               | Codex                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Telemetry          | Tail of the rollout JSONL (`transcript_path`): latest `token_count` `last_token_usage` and `model_context_window`. A compaction record after it invalidates the count. For subagents, `agent_transcript_path`, else the single `rollout-*<agent_id>.jsonl` in the parent rollout's date directory or the day before or after.                                                                                                                  |
| Events             | `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SubagentStop` in `~/.codex/hooks.json` (merged; left as is when it already holds exactly these handlers).                                                                                                                                                                                                                                                                          |
| Subagent detection | `agent_id` present (also `SubagentStop`). Subagent hooks carry the parent's `session_id`, so state is keyed by `agent_id`. `SessionStart` and `Stop` never fire in subagents.                                                                                                                                                                                                                                                                  |
| Channel            | Policy and handoff list through `SessionStart` `additionalContext`. `PostCompact` cannot carry context, so compaction is handled by `SessionStart` with `source: compact` (re-arms tiers, re-injects). Default cap is about 2,500 tokens per message; the handler sets `additionalContextLimit` from `additional_context_limit` (6000).                                                                                                        |
| Notes              | UserPromptSubmit/PostToolUse `additionalContext` in the same hook, logged as `note` and `note_delivered`; block `{"decision":"block","reason","systemMessage"}`; Stop/SubagentStop refresh as `decision: block` (continue).                                                                                                                                                                                                                                                                            |
| Agents             | `~/.codex/agents/<role>.toml`, registered in a kit-owned `[agents.<role>]` section of `config.toml` (parsed with `tomllib`). Codex rewrites `config.toml` itself and inserts new tables (hook trust, projects, tui) before the trailing END marker, so install and uninstall move any table other than the kit's `[agents.<role>]` to just after the section. An edited kit table or an existing agent name outside the section is a conflict. |
| Capability limits  | Role TOML ignores `sandbox_mode`, approval, network and MCP; subagents inherit the parent turn. Only `features.shell_tool = false` is real (set when `shell` is false). `write`, `network`, `mcp`, `git_push` are advisory lines in `developer_instructions` and listed as not enforced in the policy's role table.                                                                                                                            |
| Per-spawn override | `spawn_agent` `model` and `reasoning_effort`; policy also asks for `agent_type` and `fork_turns: "none"`.                                                                                                                                                                                                                                                                                                                                      |
| Transcripts        | `~/.codex/sessions/<yyyy>/<mm>/<dd>/`.                                                                                                                                                                                                                                                                                                                                                                                                         |

## Tiers

<!-- BEGIN generated: tiers -->
| tier | model | effort |
| --- | --- | --- |
| cheap | `gpt-6-luna` | `low` |
| standard | `gpt-6.1-sol` | `medium` |
| deep | `gpt-6.1-sol` | `high` |
<!-- END generated: tiers -->

## Unverified

- A role file `model` is documented to win over an explicit spawn `model`; observed calls do not separate the two, so per-spawn override is unproven when the role file also sets a model.
- Injection of `SessionStart` context at compaction time is not distinguishable in rollouts.
- The exact `tool_name` of `spawn_agent` in a real `PostToolUse` payload was not observed (documented alias `Agent`).
- `additionalContextLimit` handling by the hooks schema is verified in the binary only.
- `agent_id` on `UserPromptSubmit`/`PostToolUse` comes from the schema and docs.
