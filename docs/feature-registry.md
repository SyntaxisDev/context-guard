# Feature registry

`core/features.py` defines capabilities that a runtime can independently lack and universal
guarantees that cannot be opted out. `adapters/registry.py` discovers declarations in
`adapters/<runtime>/adapter.py`. Each declaration names hook and mapping modules by import path,
so discovery does not load them. Declarations describe current behavior; they do not dispatch
hooks. The CLI, documentation generator, recording selection and shared tests consume the
registry. Test drivers are loaded separately by tests. Source documentation and anchors are
validated by structural checks and documentation generation, separately from installed metadata.

Discovery is deterministic in alphabetical runtime order. CLI `all` selection follows this order.
A runtime directory containing `hooks.py` must also contain an adapter declaration.

## Capabilities

| ID | Contract and boundary |
| --- | --- |
| `SESSION_CONTEXT` | Provide policy and ranked handoffs at session start. |
| `PROMPT_WARNING` | Deliver context warnings for prompts. |
| `PROMPT_BLOCK` | Block a prompt at the configured threshold and allow a resend. |
| `TOOL_WARNING` | Deliver tool warnings without blocking the tool. |
| `HANDOFF_REFRESH` | Request handoff refreshes and credit handoff writes through file and shell tools. |
| `COMPACTION_RECOVERY` | Rearm warning tiers and redeliver session context after compaction. |
| `SUBAGENT_BUDGET` | Deliver leaf and role-specific context budgets. |
| `SILENT_SUBAGENT_NOTE` | Notify the parent about a subagent that has gone silent. |
| `BACKGROUND_RESULT_WAKE` | Start an idle parent turn when a background result arrives. |
| `HANDOFF_ACCESS` | Configure access to handoffs outside the workspace. |
| `ROLES` | Install named delegation roles with their tier models. |
| `RUNNER` | Support the optional model-pinned runner, classifier and calibration. |

Resend, unwritten handoffs, file and shell write credit, and leaf and role budgets are scenarios
within their respective contracts, not separate opt-outs. Conformance should still exercise each
scenario. Prompt warning and blocking remain separate because a runtime may warn without blocking.
Compaction recovery includes both warning rearming and context redelivery, so Cursor's below-threshold
rearming alone does not satisfy it.

## Guarantees

| ID | Universal contract |
| --- | --- |
| `HOOK_BOUNDARY` | Fail open on bad input or telemetry, pass on deadline, and emit one native response. |
| `INSTALL_PRESERVATION` | Preserve user files through install and uninstall; dry runs write nothing. |
| `DOCTOR` | Report installation health and drift. |
| `SKILLS` | Install shared skills. |
| `EVENT_LOGGING` | Record shared events, including subagent spawns. |

Guarantees are not `Feature` values and cannot be declared as gaps. Spawn logging is diagnostic
event logging, not the separate activity tracking needed for silence notes.

## Declared gaps

Every gap has a kind (`runtime_limit`, `not_applicable`, or `not_implemented`), reason and link
to its runtime's documentation. A missing implementation is not evidence that the runtime cannot
support it. Cursor, Codex, Claude and Copilot implement every catalogue capability through their
native adapter mechanisms or explicit setup helpers. Cursor CLI handoff access requires
the project helper; IDE external-file approval remains outside that helper's scope. Cursor
compaction recovery uses confirmed native generations, and its wake scenario independently
replays a measured CLI lifecycle. Genie implements silence notes but retains runtime limits
for starting an idle parent turn and isolated model-pinned runner calls.

Cursor's foreground unreturned and orphaned-result notes are runtime-specific: other runtimes do
not have the same observed condition or implementation. Their existing adapter tests remain useful
without making those notes a universal opt-out capability. Claude tracks silent background agents,
while Cursor tracks foreground and background spawns. A silence note is independent of an idle
parent wake: Cursor and Claude wake natively, Codex uses a waker, and Genie cannot wake an idle
parent. See each runtime's documentation for details and limits.
