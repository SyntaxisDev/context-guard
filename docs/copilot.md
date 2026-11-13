# GitHub Copilot CLI

The Copilot adapter participates in registry discovery, shared conformance, recorded-payload
contracts and exact installed-command checks. Declared support is separate from executable
conformance. Handoff directory access requires explicit setup for each workspace; normal
installation does not infer a global grant. The [conformance guide](conformance.md)
defines the shared runtime contracts. Offline validation needs no live installation or trust change.

## Native mechanisms and evidence

Native sandbox observations use Copilot CLI 1.0.107. Recorded payloads and telemetry live in
`tests/fixtures/copilot/`, with provenance from `events.jsonl` hook-start inputs and hook-end
outputs. `output-contract.json` records the independently maintained SDK output contract.
Fixtures are collected explicitly through `scripts/record_payloads.py --runtime copilot
--copilot-home <sandbox-root>`; ordinary tests never collect them or read live runtime homes.

The kit registers flat command handlers in `$COPILOT_HOME/hooks/context-guard.json`, with
`bash` commands and `timeoutSec`. With no override, the root is `<home>/.copilot`. The installer
owns that one file and preserves other hook files; hook trust remains entirely with Copilot.
`sessionStart` returns `additionalContext`, containing policy and ranked handoffs. Native
resume payloads are recorded. Payload event names come from the registered command argument.

Command-hook `userPromptSubmitted` accepts
`additionalContext` and `decision: "block"` with a user-facing `reason`. Native headless
sentinel and refusal probes established this on `gpt-6-luna`: a refusal ran no model turn.
The adapter uses this single prompt hook; it does not also inject through
`userPromptTransformed`. A refusal reason is user-visible text, not model-facing delivery.
`agentStop` uses the same decision spelling to continue the agent with `reason` as the next
message. The observed loop guard is `stop_hook_active`; the SDK also documents `stopHookActive`.

Telemetry uses a bounded tail of the native session event log. A root
`session.usage_record.data.usage.inputTokens` already includes cache reads and writes;
`maxPromptTokens` is the model window from telemetry, not a fixed runtime constant.
Missing or malformed telemetry fails open.
Usage-output files use different cache accounting and are not this telemetry source.

## Tiers

<!-- BEGIN generated: tiers -->
| tier | model | effort |
| --- | --- | --- |
| cheap | `gpt-6-luna` | `low` |
| standard | `gpt-6-sol` | `medium` |
| deep | `gpt-6.1-sol` | `high` |
<!-- END generated: tiers -->

## Tool events

Tool hook inputs are recorded for `view`, `task`, `skill`, root and child `bash`, and successful
`apply_patch` Add/Update operations. Patch arguments are strings; other recorded tool arguments
are objects. Successful patch and shell writes credit handoffs, reads credit access, and task
calls log spawns. Failed tools never credit writes. Unobserved patch delete/move operations are
not credited. Tool warnings return `additionalContext` without blocking. Both returned text
and `note_delivered` are checked; native probes separately established model-facing delivery.

## Compaction

Manual `preCompact` and `session.compaction_start` / `session.compaction_complete` records
are captured. A successful, applied completion is consumed once using its native event ID.
The next prompt/tool hook rearms warnings and redelivers policy and handoffs. Recovery survives
an interrupted hook or refused prompt, and a later compaction replaces stale pending context.
Local post-compaction token estimates are not provider usage; new request records supply
provider counts. Automatic compaction has not been forced in native testing.

## Subagents

Recorded native hooks carry `agentId`, `agentType` and `parentAgentId`, including the built-in
`general-purpose` agent. Child usage is selected by exact agent identity and native interaction
type from the root journal; parent usage never becomes child usage. `subagentStart` records
the genuine root/child relationship. Leaf and role budgets deliver one advisory note without
root policy or handoffs. Child prompt/tool activity rearms silence tracking; `subagentStop`
removes completed children. Parent prompt/tool responses deliver one silence notice per
inactive period and log it only with returned text. Refused prompts retain pending notices.
An idle parent receives the notice at its next prompt/tool opportunity; this mechanism does
not start a turn independently. Native completion wake remains separate from silence tracking.

## Background results

Native ACP evidence establishes an idle root, child completion, a new root turn without another
user submission, and model-visible child content. No adapter waker or polling is needed.
The recorder's explicit `--copilot-lifecycle-root` route captures scrubbed journal and ACP
chunks with source hashes. The independent driver computes wake from chronology, identities
and actual model output; removing idle/start/completion/model evidence or inserting a human
prompt fails validation. This replays a recorded native lifecycle, not a fresh host run
during ordinary offline conformance. Notification context also reached the model, asynchronously.

## Handoff access

Run the public helper explicitly for each workspace, using an existing user home, Copilot
configuration root and configured handoff directory. Paths are resolved to canonical existing
directories. The root is `$COPILOT_HOME`, defaulting to `<home>/.copilot`; the handoff store
comes from context-guard configuration, including environment overrides.

```sh
uv run --python 3.14 python adapters/copilot/access.py install --home /path/to/home --workspace /path/to/project --dry-run
uv run --python 3.14 python adapters/copilot/access.py install --home /path/to/home --workspace /path/to/project
uv run --python 3.14 python adapters/copilot/access.py uninstall --home /path/to/home --workspace /path/to/project
```

For a plugin package, replace the Python invocation with
`sh <plugin-root>/hooks/context-guard-hook adapters/copilot/access.py` and keep the same arguments.
Both `--home` and `--workspace` are required. Create the handoff directory before setup.
The helper changes only `permissions-config.json`
`locations[canonical workspace].allowed_directories`. Its shared-installer manifests are scoped
to both native profile and workspace. Uninstall retires only its directory entry, preserving
pre-existing grants, foreign locations and intervening user edits. Unchanged original settings
round-trip byte for byte; dry runs write nothing. If the configured store changes, uninstall the
previous grant before installing the new one; uninstall uses the recorded original directory.

Native Copilot CLI 1.0.107 observations established successful file creation in the granted
directory, denial in a sibling and persistence across a new process, using canonical existing
paths, with temporary-directory exemptions disabled and the same separate tool approval.
Offline conformance runs the real helper and independently reads the workspace grant.

Directory access and tool approval remain separate: this helper installs no tool approval,
unscoped `kind:write` rule, hook trust, `--add-dir` substitute or automatic instruction admission.
It does not grant access for other workspaces. Routine handoff saves have standing user
authorization, while native tool approval remains a separate control. Verification uses only
temporary homes.

Shared policy and the handoff skill instruct agents to request native filesystem approval when
a required handoff read or write is blocked, then retry after approval. In an interactive
Copilot session, the file operation can trigger the native directory-access prompt. A denied
request or a session without an approval flow must be reported as blocked; the handoff remains
pending. This guidance does not itself install a grant or guarantee model compliance.

## Roles

Agents install as `agents/<role>.agent.md`; tier model, effort and context preferences are
written under `settings.json` `subagents.agents.<effective-name>`. Native plugin-agent probes
established namespaced model/effort resolution. Every permission limit is described as advisory;
parsed tool allowlists alone do not prove enforcement. Skills install under `skills/`, and an
actual native skill invocation delivered its content to the model.

## Plugin package

```sh
uv run --python 3.14 python cli.py package --runtime copilot --out /tmp/copilot-package --marketplace context-guard
```

The plugin root is `/tmp/copilot-package/copilot/plugins/context-guard`, with
`.github/plugin/plugin.json`, flat `hooks/hooks.json`, agents and skills. The local catalogue
uses `.github/plugin/marketplace.json`, a path recognized by the pinned native loader.
Hook environment mapping expands `${PLUGIN_ROOT}` into the packaged launcher path. Package
rendering and the public CLI route are tested. A pinned 1.0.107 native package probe also
executed the expanded hook and returned a blind context marker in the model response.
No plugin was published or installed live.

Use one installation route per profile. Packaged policy names roles `context-guard:<role>`;
only that known prefix is stripped for budget lookup. Model frontmatter travels with agents,
but per-agent effort/context preferences live in the host profile's `settings.json`, which a
package cannot install automatically. Apply or remove just those namespaced preferences:

```sh
sh <plugin-root>/hooks/context-guard-hook adapters/copilot/agents.py install --dry-run
sh <plugin-root>/hooks/context-guard-hook adapters/copilot/agents.py install
sh <plugin-root>/hooks/context-guard-hook adapters/copilot/agents.py uninstall
```

The helper honors `COPILOT_HOME` and `--home`, uses its own profile manifest, and changes no
hooks, agent files, trust or permissions. Uninstall preserves user-owned and edited settings.
Both main installation and the helper preserve matching pre-existing entries and empty
containers through reinstall/uninstall using their manifest originals. Their manifests also
record each owned preference's last installed value: a changed tier replaces an unchanged
kit entry, and uninstall removes it even when the current tier differs. User-edited entries
stay the user's: they are kept and reported as notes without failing the run, and entries
with no recorded value are treated as user-owned.

## Runner

The optional runner pins model and reasoning effort, uses scratch `COPILOT_HOME`, disables
hooks/custom instructions/MCP/history synchronization/updates and denies tool categories.
Only account-selection metadata is copied; credentials remain in the existing auth store.
Tools can remain visible to the model, so a run with tool activity is discarded. Native
deny behavior was observed; shared classifier/calibration and command-validation checks pass.
Missing, empty or corrupt native tool-activity evidence also discards the answer.
The invocation currently uses POSIX `/usr/bin/env`; Windows runner execution is unverified.

## Native smoke

```sh
uv run --python 3.14 python scripts/copilot_smoke.py --dry-run --output /tmp/copilot-smoke-offline
uv run --python 3.14 python scripts/copilot_smoke.py --run --output /tmp/copilot-smoke-native
```

The native command is opt-in, authenticated and spends credits. It installs only into its
temporary sandbox, uses existing authentication and never writes trust. The smoke checks
handoff context, warning delivery, refusal and resend behavior, and whether a denied handoff
write leaves permissions unchanged. Interactive delivery remains unverified.
Without `--output`, each run creates a private temporary evidence directory. An explicit
output must be owned by the caller, not writable by others, and have no existing sandbox or
result file. Smoke subprocesses discard inherited `CONTEXT_GUARD_*` overrides.

The smoke counts unique native request event IDs. Usage exports across resumed turns are
cumulative and must not be summed.

## Validation

Run from the repository root, using temporary homes for installed and native checks:

```sh
uv run --python 3.14 python scripts/conformance.py --structure
uv run --python 3.14 python scripts/conformance.py --runtime copilot
uv run --python 3.14 python -m unittest discover -s adapters/copilot/tests -t .
uv run --python 3.14 python -m unittest tests.contract.test_recorded_payloads
uv run --python 3.14 python -m unittest tests.installed.test_installed_kit
uv run --python 3.14 python scripts/conformance.py --json /tmp/conformance.json
uv run --python 3.15 python scripts/conformance.py --structure
```

Conformance must remain nonzero when FAIL or NOT IMPLEMENTED results remain.
