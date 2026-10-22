# Claude Code

Shared behaviour is in [architecture.md](architecture.md). This page lists only what differs.

| item | Claude Code |
| --- | --- |
| Telemetry | Tail of the session transcript JSONL: latest main-context assistant input usage (cache tokens included, output excluded). The window comes from `context_windows` patterns (model id to window), replaced by the learned compaction point (tokens before an auto compaction, recorded via `PreCompact`/`PostCompact`) once known. |
| Events | `SessionStart`, `UserPromptSubmit`, `PostToolUse` (matcher `*`), `Stop`, `PreCompact`, `PostCompact`, `SubagentStop` in `~/.claude/settings.json` (merged; left as is when it already holds exactly these groups). The compact events only maintain the learned window; `SubagentStop` only ends background subagent tracking. |
| Subagent detection | `agent_id` on `UserPromptSubmit`/`PostToolUse`. `agent_type` alone is not enough (also set for `--agent` sessions). `Stop` fires only for the main agent. |
| Channel | Policy in `~/.claude/rules/context-guard.md` (loaded at launch, survives `/compact`); handoff list through `SessionStart` `additionalContext`. Output is clipped to 10,000 characters (2,000 for tool notes). |
| Notes | `hookSpecificOutput.additionalContext` in the same hook, logged as `note` and `note_delivered`; block and stop refresh as `{"decision":"block","reason"}`. |
| Agents | `~/.claude/agents/<role>.md`, frontmatter `name, description, model, effort`, plus tool restrictions. |
| Capability mapping | `shell: false` gives `tools: Read, Glob, Grep`, which enforces every limit. Otherwise `disallowedTools` lists `Edit, Write, NotebookEdit` when `write` is false and `WebFetch, WebSearch` when `network` is false, but Bash can still do both, and no documented agent setting removes MCP tools, so a shell role's limits are advisory and the policy's role table lists them as not enforced. |
| Per-spawn override | Agent tool `model` alias. No per-call effort: effort is fixed in the agent definition. The tool description discourages setting `model`, so the rendered policy explicitly authorises it per tier. Without `model`, `subagent_spawn` logs the role's default tier and that tier's `model`; the logged effort is always the default tier's. |
| Transcripts | `~/.claude/projects/<project-slug>/<session-id>.jsonl`. |
| Silent subagents | `adapters/claude/subagents.py`. An `Agent` `PostToolUse` whose `tool_response.status` is `async_launched` (it also names `agentId`) records a background spawn in the parent's session state: role, spawn time and transcript `<session transcript without .jsonl>/subagents/agent-<id>.jsonl`. The subagent's own `PostToolUse` (parent `session_id` plus `agent_id`) refreshes its last activity, kept to the minute; its `SubagentStop` ends tracking. On each parent `PostToolUse`, `UserPromptSubmit` (unless blocked) and `SessionStart`, a spawn whose last activity (else its spawn time) is older than `subagents.silence_minutes` (10) gets one note per silence period in that same response (role, agent id, minutes, transcript, what to check) and a `subagent_silent` event (role, agent, minutes). |

## How the orchestrator is woken

Claude Code wakes the parent natively. When a background subagent stops, it queues a `<task-notification>` (status, summary, result) and a subagent hand-back message; if the parent's turn has ended, Claude Code starts a new parent turn with them, without a user message, and mid-turn it delivers them at the next tool result. In recorded transcripts a parent turn ended (`stop_hook_summary`), a hand-back or `<task-notification>` arrived seconds to minutes later, and the next record was the parent's own reply. So the shared policy holds as written: spawn in the background, end the turn, pick up each result as a new turn.

`scripts/plugin_live.sh check --run` checks this headlessly with one `claude -p --input-format stream-json --output-format stream-json` session, the streaming-input mode the Agent SDK uses: stdin stays open, so the session outlives the first turn. The parent spawns a background subagent that sleeps 20 s and answers a token, and ends its turn; the case passes when a later turn's `result` quotes the token (subagent messages print no `result`). The interactive terminal session takes the same path but is not driven by the check.

context-guard adds the silent-subagent note above, the closest equivalent of Cursor's notes: a hung or killed subagent never notifies, and the note reaches the parent on its next tool result, prompt or session start. Cursor's unreturned and orphaned notes have no Claude Code counterpart: a foreground `Agent` call holds the parent's tool call until the result is its tool result, and no recorded transcript shows a parent moving on while a foreground subagent still runs.

## Tiers

<!-- BEGIN generated: tiers -->
| tier | model | effort | call |
| --- | --- | --- | --- |
| cheap | `claude-haiku-5-5` | `low` | `haiku` |
| standard | `sonnet` | `medium` | `sonnet` |
| deep | `opus` | `high` | `opus` |
<!-- END generated: tiers -->

## Plugin

`cli.py package --runtime claude` renders a Claude plugin; its layout, CoWork limits and the
launcher are in [distribution.md](distribution.md). Use one path per machine: with both the
installed kit and the plugin, both sets of hooks fire.

- Claude Code: unzip `context-guard-claude-marketplace-<version>.zip`, then
  `claude plugin marketplace add <dir>` and `claude plugin install context-guard@context-guard`,
  and start a new session. Claude copies the plugin to
  `~/.claude/plugins/cache/<marketplace>/context-guard/<version>/` and loads its hooks, agents and
  skills from there; there is no extra install step.
- CoWork: upload `context-guard-claude-<version>.zip` (the plugin root is the zip root). CoWork
  loads plugins only, so the script installer has no effect there.
- Verify: `claude plugin list` shows `context-guard@context-guard` enabled, `/hooks` lists its
  `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `PreCompact`, `PostCompact` and
  `SubagentStop` hooks, and after a prompt `~/.context-guard/state/claude/events.jsonl` ends with a `prompt`
  record.
- Update: replace the marketplace directory with the new release, then
  `claude plugin marketplace update context-guard` and
  `claude plugin update context-guard@context-guard`, and restart Claude Code. In CoWork, upload
  the new zip.
- Remove: `claude plugin uninstall context-guard@context-guard` (optionally
  `claude plugin marketplace remove context-guard`); in CoWork, remove the uploaded plugin.

## Unverified

- `~/.claude/rules` loading and its application to subagents come from documentation only.
- Transcript usage records are not a hook contract; a changed shape disables warnings (fail open).
- `agent_id` as the subagent marker on tool and prompt events is documented, not observed in local payloads.
- Whether the model follows the policy's `model` authorisation despite the tool description is untested.
- The `async_launched` Agent result and the automatic parent turn come from transcripts; that `SubagentStop` fires for background subagents is documented, not observed in a live hook run.
