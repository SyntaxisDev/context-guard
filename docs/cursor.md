# Cursor

Shared behaviour is in [architecture.md](architecture.md). This page lists only what differs.

| item | Cursor |
| --- | --- |
| Telemetry | Chat record `composerData:<conversation_id>` in Cursor's `state.vscdb` (read-only, 1 s timeout): `contextTokensUsed`, `contextTokenLimit`. Path from `telemetry.state_db` or the per-platform default. Cursor CLI (`cursor-agent`) chats are not in that database; when it has no record, the CLI's own store `<telemetry.cli_chats>/<workspace hash>/<conversation_id>/store.db` (default `~/.cursor/chats`, found by conversation id, read-only) is used: `meta` row `0` holds hex-encoded JSON whose `latestRootBlobId` names a protobuf blob in `blobs`; its field 5 is the context usage message with tokens used (field 1) and context window (field 2). `telemetry.window_fallback` applies when no window is known. Any changed shape, invalid count or undecodable protobuf means no telemetry (fail open). |
| Events | `sessionStart`, `beforeSubmitPrompt`, `preToolUse` (matcher `Task`, spawns only), `postToolUse`, `stop` (`loop_limit` 1) in `~/.cursor/hooks.json`, each with the shared hook timeout (5 s), registered by `mapping.py` and merged without touching other hooks; a file that already holds exactly these entries is left as is. `subagentStart`/`subagentStop` are not registered: they can only pass. |
| Subagent detection | `parent_tool_call_id` on tool hooks. `sessionStart`, `beforeSubmitPrompt` and `stop` do not fire inside subagents, so leaf observations come from tool hooks only. `is_background_agent` is a cloud agent, not a subagent. Leaf state and telemetry are keyed by `conversation_id`, which assumes each subagent has its own conversation id (as in every recorded subagent payload). A subagent carrying its parent's id would read the parent's tokens and share its state: the budget note would follow the parent's size, and a leaf observation under the note threshold would re-arm the parent's fired tiers. |
| Compaction | No compaction hook is registered and no hook payload marks a summarised chat. Re-arming relies on the shared token rule: the next observation whose `contextTokensUsed` is back under the note threshold resets fired tiers. A summary that stays above it re-arms nothing, so notes resume only at a higher tier. The policy needs no re-injection because the rules file persists; the handoff list is not re-sent until the next `sessionStart`. |
| Channel | Policy in `~/.cursor/rules/context-guard.mdc` (`alwaysApply`, reaches subagents); handoff list through `sessionStart` `additional_context` (fire-and-forget, best effort). |
| Notes | `beforeSubmitPrompt` cannot add model context, so a prompt note is held (also across a `stop` that sends no follow-up) and delivered as `additional_context` after the next tool result or at the next `sessionStart`; a tool-result note goes out as `additional_context` at once. Each delivered note is logged as `note_delivered`; a held note is dropped when its tier re-arms, when a tool result writes the handoff, or when `stop` sends its refresh follow-up, which supersedes it. Block: `{"continue": false, "user_message"}`. Stop refresh: `followup_message`. |
| Agents | `~/.cursor/agents/<role>.md`, frontmatter `name, description, model, readonly`. |
| Capability limits | Only `readonly` is enforced, and it is set when `shell` is false (it also removes the shell). `network`, `mcp`, `git_push` and `write` for shell roles are advisory; the policy's role table lists them as limits Cursor does not enforce. No per-agent tool list exists. |
| Per-spawn override | Task `model` slug; effort is encoded in the slug suffix. No per-call context window. A Task without `model` runs the role's agent file and is logged as `subagent_spawn` with the role's default tier and that tier's `agent_model` (model before the brackets, `effort=` inside them). |
| Shell handoff access | `Shell` commands resolve relative handoff paths against `tool_input.cwd`, else the first workspace root. |
| Transcripts | `~/.cursor/projects/<workspace>/agent-transcripts/`. |
| Live smoke | `cursor-agent -p` fires neither `beforeSubmitPrompt` nor `stop`, so `scripts/sandbox_smoke.sh` drives one interactive `cursor-agent` session with two prompts through `scripts/cursor_interactive.exp` (needs `expect`). |

## Tiers

Agent files use `agent_model` (bracket syntax); the `call` slug is passed as the Task `model`.

<!-- BEGIN generated: tiers -->
| tier | agent_model | call |
| --- | --- | --- |
| cheap | `claude-haiku-5-5[effort=low]` | `claude-haiku-5-5-thinking-high` |
| standard | `claude-sonnet-5-5[effort=medium]` | `claude-sonnet-5-5-medium` |
| deep | `claude-opus-5-5[effort=high,context=300k]` | `claude-opus-5-5-high` |
<!-- END generated: tiers -->

## Unverified

- `~/.cursor/rules/*.mdc` as a global always-on channel is observed but undocumented.
- `parent_tool_call_id` is undocumented (present in observed payloads and wire protos); based on a small sample.
- That every subagent gets its own `conversation_id` rests on the same small sample.
- `sessionStart` additional-context size limit and delivery ordering are unknown.
- The CLI store layout (`meta`/`blobs` tables, root blob field 5) is undocumented and observed on `cursor-agent` 2026.10.01; the numbers matched the CLI's own context display.
- Whether the `[effort=..,context=..]` frontmatter model syntax takes effect on a subagent was not observed; `model` may fall back (team block, plan limits).
