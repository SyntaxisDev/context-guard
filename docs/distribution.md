# Distribution

context-guard ships two ways: the script installer writes native files into a runtime home,
and `cli.py package` renders a self-contained plugin for runtimes that load plugins. Both
render the same `content/` through the same adapter mapping. Tagged releases attach the built
plugins to the GitHub release; no built plugin is committed to the repository.

| path                                      | for                                                                     | what it writes                                                                                                                                                   |
| ----------------------------------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cli.py install --runtime <rt>`           | Cursor, Codex, Claude Code CLI                                          | Kit at `~/.context-guard/kit` with a venv, native rules/agents/skills, hook registrations merged into the runtime settings, an install manifest. See the README. |
| `cli.py package --runtime <rt> --out DIR` | Runtimes with a plugin spec: Claude Code and CoWork, Cursor, Codex      | `DIR/<rt>/plugins/context-guard/`: vendored code, `LICENSE`, `NOTICE`, launcher, plugin manifest, hooks (not Cursor), agents, skills, policy. Nothing outside `DIR`. |
| `... --marketplace NAME`                  | Sharing through a marketplace repo                                      | Also the runtime's marketplace listing under `DIR/<rt>/` (Claude: `.claude-plugin/marketplace.json`) listing the plugin at `./plugins/context-guard`.           |
| GitHub release assets                     | Users who do not clone the repository                                   | Per runtime a plugin zip (plugin root at the zip root) and a marketplace zip (listing + `plugins/`), plus `SHA256SUMS`. See Releases.                            |

Use one path per runtime: the installed hooks and the plugin hooks would both fire.

Some plugin features need one extra command, run by the user through the packaged launcher,
because the runtime cannot load them from a plugin: Codex role agents
(`adapters/codex/agents.py`, see [codex.md](codex.md#installing-the-plugin)) and Cursor hooks
(`adapters/cursor/hooks_install.py`, see [cursor.md](cursor.md#registering-the-hooks); the Cursor
app runs plugin hooks only with Third-Party Imports on, so the Cursor plugin ships none). Each
keeps its own install manifest and reverses cleanly. Install, verification, update and removal
steps per runtime are on the runtime pages ([claude.md](claude.md#plugin) for Claude).

## Package command

```sh
uv run --python 3.14 python cli.py package --runtime <rt|all> --out dist [--marketplace NAME] \
    [--set section.key=value ...] [--dry-run]
```

- Each runtime renders into its own `DIR/<rt>/`, so `--runtime all` builds every package side by
  side. `all` packages each runtime that has `adapters/<rt>/plugin.py` and prints a one-line note
  for the others; naming a runtime without a plugin spec is an error.
- The version is `project.version` from `pyproject.toml`; a marketplace pins users to it until it changes.
- Every package carries the repository's `LICENSE` and `NOTICE`; packaging fails when either is missing.
- An existing `marketplace.json` is merged: only the entry named like this plugin is replaced,
  other plugins' entries and the listing's own fields are kept. A file that is not a JSON object
  with a `plugins` list is an error and nothing is written.
- `--set` values are validated like the installer's and baked into the package's
  `adapters/<rt>/config.json`; they are never written to a user config. Precedence inside a
  package is therefore: shipped defaults, adapter config with baked values, the user's
  `~/.context-guard/config.json`, then `CONTEXT_GUARD_<SECTION>_<KEY>` environment variables.
- Rendering fails closed: unfilled `{{token}}`s, paths escaping the plugin root, rendered files
  that would replace vendored code, or a hook command naming a package file that is not rendered.
- Writing replaces a previous package of the same plugin wholesale (staged, then renamed); any
  other directory at the target is an error. `--dry-run` renders and validates without writing.

Try a package without touching your configuration:

```sh
claude --plugin-dir dist/claude/plugins/context-guard
claude plugin validate dist/claude/plugins/context-guard
claude plugin marketplace add ./dist/claude        # when built with --marketplace
```

## Releases

Pushing a tag `v<version>` runs `.github/workflows/release.yml`: it checks that the tag equals
`v` + `project.version`, runs the CI workflow (tests on Python 3.14 and 3.15, ruff, packaging),
then builds and publishes:

```sh
uv run --python 3.14 python cli.py package --runtime all --marketplace context-guard --out dist
uv run --python 3.14 python scripts/build_release.py --out dist [--tag v<version>]
```

`build_release.py` writes two zips per runtime and `dist/SHA256SUMS` covering all of them:

- `context-guard-<rt>-<version>.zip`: `dist/<rt>/plugins/context-guard` with the plugin root at
  the zip root, because CoWork uploads need the manifest directory there. For CoWork upload,
  `claude --plugin-dir` and Cursor's local plugin directory.
- `context-guard-<rt>-marketplace-<version>.zip`: the whole `dist/<rt>/` tree, the marketplace
  listing plus `plugins/context-guard`, for `claude plugin marketplace add <dir>` and
  `codex plugin marketplace add <dir>`. Codex cannot install a bare plugin directory.

A tree packaged without `--marketplace` is rejected before anything is written. Entries are
sorted with a fixed timestamp, so the same tree zips to identical bytes. The same commands build
the artifacts locally.

## Claude plugin layout

| file                                         | role                                                                                                                                                                                                                                                                                                                                         |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.claude-plugin/plugin.json`                 | `name`, `version`, `description`.                                                                                                                                                                                                                                                                                                            |
| `hooks/hooks.json`                           | `SessionStart`, `UserPromptSubmit`, `PostToolUse` (`*`), `Stop`, `PreCompact`, `PostCompact`, each `sh "${CLAUDE_PLUGIN_ROOT}/hooks/context-guard-hook" adapters/claude/hooks.py <event>`.                                                                                                                                                   |
| `hooks/context-guard-hook`                   | POSIX launcher, invoked through `sh` so a copy that lost its executable bit still runs. Not in `bin/`: claude.ai rejects uploaded plugins with a top-level `bin/`, which the CLI would add to `PATH`.                                                                                                                                        |
| `policy/context-guard.md`                    | Rendered policy. Plugins cannot ship rules files, so `hooks.py` reads this file at `SessionStart` (also after compaction, `source: compact`) and places it ahead of the handoff listing; an installed kit has no such file and uses its rules file. Role names are rendered as `context-guard:<role>`, the names Claude gives plugin agents. |
| `agents/<role>.md`, `skills/<name>/SKILL.md` | Rendered like the installer's. Claude namespaces them: `context-guard:scout`, `context-guard:handoff`. The `install-context-guard` skill is not shipped: it drives the script installer, which a plugin user does not need.                                                                                                                  |
| `core/`, `content/`, `adapters/claude/`      | Vendored code; no tests, caches or venv.                                                                                                                                                                                                                                                                                                     |

The launcher resolves the package root from its own path, then finds Python 3.14+ with
`uv python find --system --no-project --no-python-downloads '>=3.14'`, else the first
`python3`, `python3.15` or `python3.14` that is new enough. Desktop hosts start hooks with a
minimal `PATH`, so it also searches `~/.local/bin`, `~/.cargo/bin`, `/opt/homebrew/bin` and
`/usr/local/bin` (replace the list with `CONTEXT_GUARD_SEARCH_PATH`). Probes never read the hook's
stdin. Without a usable interpreter it prints nothing and exits 0, so the runtime proceeds as if
no hook were installed. uv keeps its interpreter cache under `~/.cache/uv`.

## Claude CoWork

CoWork runs Claude Code on the host with a per-session configuration directory and passes
plugins inline (`--plugin-dir`). It reads none of `~/.claude/settings.json` hooks,
`~/.claude/rules`, `~/.claude/agents` or `~/.claude/skills`, so the script installer has no effect
there. Plugins reach CoWork through the organisation's plugin sync or a marketplace added in
CoWork; plugin hooks, agents and skills load there.

Limits:

- Hooks run on the host, so state and the handoff listing use `~/.context-guard` as in the CLI,
  provided the host process keeps the user's `HOME` (not yet observed directly).
- The model's shell runs in a Linux VM with its own home; `~/.context-guard` is not visible from it.
  The file tools run on the host but need permission outside the folders the user selected. To let the
  model write handoffs without prompts, bake a handoff directory inside a selected folder:
  `--set paths.handoff_dir=/path/to/selected/folder/handoffs`.
- `${CLAUDE_PLUGIN_DATA}` lives under the per-session configuration directory and does not persist
  across CoWork sessions, so it is not used for state.
- Agent names are namespaced (`context-guard:<role>`) in plugin installs; hooks strip the prefix to
  find the role.

## Adding a runtime package

`core/package.py` is runtime-neutral. A runtime gains a package by adding
`adapters/<rt>/plugin.py` with:

- `plugin_spec(config) -> core.package.PluginSpec(manifest_file, manifest, hooks_file, hooks,
root_token, files)`: the native manifest and its path, the hooks file content and its path (both
  None for a plugin that registers no hooks), the token hook commands use for the plugin root, and
  rendered native files (reuse
  `core.render` with the adapter's `mapping.runtime_mapping(config)`). Hook commands should call
  `sh "<root_token>/hooks/context-guard-hook" <script> <args>` so interpreter discovery and fail-open
  behaviour stay shared.
- `marketplace(name, spec) -> (relative path, listing)`: the runtime's marketplace file.

`cli.py package --runtime <rt>` then works without CLI changes; the engine vendors
`adapters/<rt>/` only, and `--runtime all` and the release workflow pick the runtime up. Known targets:

- Cursor: `adapters/cursor/plugin.py`; layout and install in [cursor.md](cursor.md).
- Codex: `adapters/codex/plugin.py`; layout and install in [codex.md](codex.md).
- Organisation marketplaces that serve Claude-format plugins (`.claude-plugin/plugin.json` +
  `skills/`) to several runtimes can take the Claude package as is.
