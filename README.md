# context-guard

Context warnings, task handoffs and delegation support for AI coding runtimes: Cursor,
Codex and Claude Code. One core and one content source; thin per-runtime adapters.

- Warns once per tier as chat context grows and blocks just before compaction (identical resend continues).
- Keeps handoff files so a new chat can resume a task.
- Installs role-based subagents (scout, investigator, reviewer, implementer, runner) with model tiers.

See [docs/architecture.md](docs/architecture.md) and the runtime pages
[Cursor](docs/cursor.md), [Codex](docs/codex.md), [Claude Code](docs/claude.md).

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14+. From the repository root:

```sh
uv run --python 3.14 python cli.py install --runtime all --dry-run   # preview, writes nothing
uv run --python 3.14 python cli.py install --runtime cursor          # or codex, claude, all
uv run --python 3.14 python cli.py status --runtime all
uv run --python 3.14 python cli.py doctor --runtime all
uv run --python 3.14 python cli.py uninstall --runtime all
```

Options: `--runtime` (repeatable; `cursor`, `codex`, `claude` or `all`), `--home DIR` (install into
another home, for testing), `--dry-run`, `--force` (replace conflicting kit files, keeping
backups), `--set section.key=value` (write the user config; the key must exist in
`content/defaults.json` or an adapter `config.json`).
Exit codes: 0 ok, 1 conflicts, doctor problems or (doctor) nothing installed, 2 the command could not run.

Installation copies one kit to `~/.context-guard/kit` with a pinned Python 3.14 venv; hooks
call that interpreter. Start a new session afterwards and review the new hooks in the
runtime: context-guard never writes hook trust. Unrelated hooks and settings are preserved;
see [docs/architecture.md](docs/architecture.md#install-engine).

### Updating

The kit is a copy: rerun `install` from the repository to update. At session start the hook
tells you when the checkout has a newer commit, and `status`/`doctor` report it. Set
`--set kit_check.auto_update=true` to update in the background. Details are in the Kit
section of [docs/architecture.md](docs/architecture.md#install-engine).

## Claude Code plugin and CoWork

Claude CoWork loads plugins only. `package` supports `claude` only and renders a
self-contained plugin instead of installing:

```sh
uv run --python 3.14 python cli.py package --runtime claude --out dist [--marketplace NAME]
```

Use either the plugin or `install --runtime claude`, not both. Formats, settings baked with
`--set` and CoWork limits are in [docs/distribution.md](docs/distribution.md).

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
installation.
