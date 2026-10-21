# Claude Code

Shared behaviour is in [architecture.md](architecture.md). This page lists only what differs.

| item | Claude Code |
| --- | --- |
| Telemetry | Tail of the session transcript JSONL: latest main-context assistant input usage (cache tokens included, output excluded). The window comes from `context_windows` patterns (model id to window), replaced by the learned compaction point (tokens before an auto compaction, recorded via `PreCompact`/`PostCompact`) once known. |
| Events | `SessionStart`, `UserPromptSubmit`, `PostToolUse` (matcher `*`), `Stop`, `PreCompact`, `PostCompact` in `~/.claude/settings.json` (merged; left as is when it already holds exactly these groups). The compact events only maintain the learned window. |
| Subagent detection | `agent_id` on `UserPromptSubmit`/`PostToolUse`. `agent_type` alone is not enough (also set for `--agent` sessions). `Stop` fires only for the main agent. |
| Channel | Policy in `~/.claude/rules/context-guard.md` (loaded at launch, survives `/compact`); handoff list through `SessionStart` `additionalContext`. Output is clipped to 10,000 characters (2,000 for tool notes). |
| Notes | `hookSpecificOutput.additionalContext` in the same hook, logged as `note` and `note_delivered`; block and stop refresh as `{"decision":"block","reason"}`. |
| Agents | `~/.claude/agents/<role>.md`, frontmatter `name, description, model, effort`, plus tool restrictions. |
| Capability mapping | `shell: false` gives `tools: Read, Glob, Grep`, which enforces every limit. Otherwise `disallowedTools` lists `Edit, Write, NotebookEdit` when `write` is false and `WebFetch, WebSearch` when `network` is false, but Bash can still do both, and no documented agent setting removes MCP tools, so a shell role's limits are advisory and the policy's role table lists them as not enforced. |
| Per-spawn override | Agent tool `model` alias. No per-call effort: effort is fixed in the agent definition. The tool description discourages setting `model`, so the rendered policy explicitly authorises it per tier. Without `model`, `subagent_spawn` logs the role's default tier and that tier's `model`; the logged effort is always the default tier's. |
| Transcripts | `~/.claude/projects/<project-slug>/<session-id>.jsonl`. |

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
  `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `PreCompact` and `PostCompact`
  hooks, and after a prompt `~/.context-guard/state/claude/events.jsonl` ends with a `prompt`
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
