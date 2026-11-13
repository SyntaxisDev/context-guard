# context-guard

Context warnings, task handoffs and delegation support for AI coding runtimes: Cursor,
Codex, Genie, Claude Code and GitHub Copilot CLI. One core and one content source; thin per-runtime adapters.

- Warns once per tier as chat context grows and blocks just before compaction (identical resend continues).
- Keeps handoff files so a new chat can resume a task.
- Installs role-based subagents (scout, investigator, reviewer, implementer, runner) with model tiers.
- Keeps the orchestrator responsible for explaining the combined result, verification and remaining steps.

See [docs/architecture.md](docs/architecture.md) and the runtime pages
[Cursor](docs/cursor.md), [Codex](docs/codex.md), [Genie](docs/genie.md),
[Claude Code](docs/claude.md), [Copilot CLI](docs/copilot.md).
The [conformance guide](docs/conformance.md) explains registry discovery, adapter-owned drivers,
structural validation and behavioral checks. The [measured baseline](docs/conformance-baseline.md)
records existing failures separately from the [declared feature support](docs/feature-registry.md).

## Install

There are two ways to install, and each runtime should use only one: an installed kit and a
plugin in the same runtime would both run their hooks. Both need Python 3.14+ on the machine;
without it the hooks do nothing.

### Script installer

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14+. From the repository root:

```sh
uv run --python 3.14 python cli.py install --runtime all --dry-run   # preview, writes nothing
uv run --python 3.14 python cli.py install --runtime cursor          # or codex, genie, claude, copilot, all
uv run --python 3.14 python cli.py status --runtime all
uv run --python 3.14 python cli.py doctor --runtime all
uv run --python 3.14 python cli.py uninstall --runtime all
```

Options: `--runtime` (repeatable; `cursor`, `codex`, `genie`, `claude`, `copilot` or `all`), `--home DIR` (install into
another home, for testing), `--dry-run`, `--force` (replace conflicting kit files, keeping
backups), `--set section.key=value` (write the user config; the key must exist in
`content/defaults.json` or an adapter `config.json`).
Exit codes: 0 ok, 1 conflicts, doctor problems or (doctor) nothing installed, 2 the command could not run.

Claude Code uses `CLAUDE_CONFIG_DIR` (default `~/.claude`), Codex uses `CODEX_HOME`
(default `~/.codex`), Genie uses `GENIE_HOME` (default `~/.genie`), and Copilot uses
`COPILOT_HOME` (default `~/.copilot`). Set the same variable
when installing, updating, checking or uninstalling a profile. Each profile has its own
install record; handoffs and the kit remain shared under `~/.context-guard`.

```sh
CLAUDE_CONFIG_DIR="$HOME/.claude_personal" uv run --python 3.14 python cli.py install --runtime claude
```

`--home` changes context-guard's user home; it does not override these native directory
variables. For sandbox checks, unset them or point them inside the temporary home.
Cursor's CLI config-directory override is not applied to hooks or agents; see
[Cursor directory overrides](docs/cursor.md#directory-overrides).

Installation copies one kit to `~/.context-guard/kit` with a pinned Python 3.14 venv; hooks
call that interpreter. Start a new session afterwards (reload the window in Cursor) and, in
Codex and Genie, trust the new hooks in `/hooks`: context-guard never writes hook trust. `doctor` checks
the install; after a prompt, `~/.context-guard/state/<runtime>/events.jsonl` ends with a `prompt`
record. Unrelated hooks and settings are preserved; see
[docs/architecture.md](docs/architecture.md#install-engine). For Codex, Genie and Claude Code the install
also lets the agent write handoffs without an approval prompt: the handoff directory (only that
directory) goes into Codex and Genie's sandbox `writable_roots` and a Claude Code `Edit` allow rule. Opt out
with `--set handoff_access.enabled=false`, then reinstall; details in
[docs/codex.md](docs/codex.md#handoff-write-access), [docs/genie.md](docs/genie.md#handoff-write-access)
and [docs/claude.md](docs/claude.md#handoff-write-access).

Copilot local packaging and namespaced role-preference setup are described in
[Copilot packaging](docs/copilot.md#plugin-package). Durable handoff-directory access is configured
per workspace through an explicit helper; [native evidence and current measurements](docs/copilot.md) state the boundary.

### Updating

The kit is a copy: rerun `install` from the repository to update. At session start the hook
tells you when the checkout has a newer commit, and `status`/`doctor` report it. Set
`--set kit_check.auto_update=true` to update in the background. Details are in the Kit
section of [docs/architecture.md](docs/architecture.md#install-engine).

### Plugins from GitHub Releases

Each [release](https://github.com/SyntaxDev/context-guard/releases) attaches, per runtime, a plugin
zip `context-guard-<runtime>-<version>.zip` (plugin at the zip root), a marketplace zip
`context-guard-<runtime>-marketplace-<version>.zip` (listing plus `plugins/`), and `SHA256SUMS`.

- Claude CoWork: upload the Claude plugin zip (CoWork loads plugins only). Claude Code: unzip the
  Claude marketplace zip, then
  `claude plugin marketplace add <dir>` and `claude plugin install context-guard@context-guard`.
  Verification, updates and removal: [docs/claude.md](docs/claude.md#plugin).
- Cursor (the plugin ships no hooks; they are registered once in `~/.cursor/hooks.json`):
  1. Unzip the Cursor plugin zip into `~/.cursor/plugins/local/context-guard`, or install it from a marketplace.
  2. Ask the Cursor agent to enable the context-guard hooks, or run
     `sh "<plugin root>/hooks/context-guard-hook" adapters/cursor/hooks_install.py install`.
  3. Reload the window, and turn Settings → Agents → Third-Party Imports off: it is not needed and imports duplicate configs.
  4. Rerun step 2 after every plugin update; until then the hooks run the previous version.

  The agent offers step 2 once, ever. Plugin root, verification and removal: [docs/cursor.md](docs/cursor.md#registering-the-hooks).
- Codex: unzip the Codex marketplace zip, then `codex plugin marketplace add <dir>` and
  `codex plugin add context-guard@context-guard`; trust the hooks with `/hooks`. For the named
  subagents, run the agents command, and for handoff writes without sandbox approval the access
  command (the first session offers each once); plugin root, verification, updates and removal:
  [docs/codex.md](docs/codex.md#installing-the-plugin).
- Genie: unzip the Genie marketplace zip, then `genie plugin marketplace add <dir>` and
  `genie plugin add context-guard@context-guard`; trust the hooks. The first session offers the
  optional named-agent and handoff-access commands. From a checkout,
  `scripts/plugin_live.sh install --only genie` packages and installs the plugin and named agents
  automatically. Full behavior and the idle-parent wake gap:
  [docs/genie.md](docs/genie.md#plugin).

To build the same packages from a checkout, with settings baked in through `--set`:

```sh
uv run --python 3.14 python cli.py package --runtime all --out dist [--marketplace context-guard]
```

Layout, marketplace listings, release builds and CoWork limits are in
[docs/distribution.md](docs/distribution.md).

## Configuration

`~/.context-guard/config.json` (top level, optional `runtimes.<name>` overrides) refines
shipped defaults in `content/defaults.json` and `adapters/<runtime>/config.json`.
Environment variables `CONTEXT_GUARD_<SECTION>_<KEY>` override everything, for example:

```sh
CONTEXT_GUARD_THRESHOLDS_NOTE_FRACTION=0.5
CONTEXT_GUARD_HANDOFF_LIST_LIMIT=10
```

State, events and handoffs live under `~/.context-guard/` (layout in
[docs/architecture.md](docs/architecture.md)). Hooks keep state in the home of the kit they run from (`<home>/.context-guard/kit`, so a kit
installed with `--home <dir>` stays in `<dir>`); `CONTEXT_GUARD_HOME=<dir>` overrides it for a
checkout or plugin, while an installed kit from another home then passes without acting.

The optional runner, classifier and calibration keys are listed in the
[runner key table](docs/architecture.md#optional-runner).

## Tests

Commands, test layers and the live smoke (`scripts/sandbox_smoke.sh`, which spends model calls)
are described in [AGENTS.md](AGENTS.md). Tests use temporary homes; never test against a live
installation. `scripts/plugin_live.sh` is the exception: it swaps the script install in your real
home for the plugins and checks them in each runtime (`--dry-run` prints what it would do).

## License

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

<!-- BEGIN generated: declared-support -->
Declared support (capability declarations, not test results).

Gap links explain the reason and kind; universal guarantees cannot be opted out.

| Capability | claude | codex | copilot | cursor | genie |
| --- | --- | --- | --- | --- | --- |
| `session_context` | supported | supported | supported | supported | supported |
| `prompt_warning` | supported | supported | supported | supported | supported |
| `prompt_block` | supported | supported | supported | supported | supported |
| `tool_warning` | supported | supported | supported | supported | supported |
| `handoff_refresh` | supported | supported | supported | supported | supported |
| `compaction_recovery` | supported | supported | supported | supported | supported |
| `subagent_budget` | supported | supported | supported | supported | supported |
| `silent_subagent_note` | supported | supported | supported | supported | supported |
| `background_result_wake` | supported | supported | supported | supported | [runtime_limit](docs/genie.md#unsupported) |
| `handoff_access` | supported | supported | supported | supported | supported |
| `roles` | supported | supported | supported | supported | supported |
| `runner` | supported | supported | supported | supported | [runtime_limit](docs/genie.md#unsupported) |
<!-- END generated: declared-support -->
