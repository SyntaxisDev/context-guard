# Developing context-guard

Context-guard provides context warnings, task handoffs and delegation support for
Cursor, Codex and Claude Code. Layout and contracts are in `docs/architecture.md`.
Python is 3.14+ and standard library only; run everything through `uv`.

## Code ownership

- `content/` is the single source of shipped text: `policy/`, `roles/` (`roles.json` plus
  role bodies), `skills/`, `defaults.json`. Role bodies are runtime-neutral responsibilities;
  model assignments live in adapter `config.json`.
- `core/` is runtime-neutral: config, observation, policy, handoffs, state, events, render,
  install, install_spec, kit, package, deadline, processes, and the optional `runner/`. It never imports an adapter
  and never names or discovers a runtime home, executable or database; paths come in as arguments.
- `adapters/<runtime>/` owns `hooks.py` (payload to `Observation`, `Decision` to native
  response), `mapping.py` (tiers, capabilities, channel, `install_spec`), `config.json`,
  `runner.py` and `tests/`, plus runtime-specific extras: Codex `rollout.py` and `tools.py`,
  Claude `transcript.py` and `plugin.py` (the package spec). No dispatcher superclass; an adapter never reads another runtime's home.
- `cli.py` is the install/uninstall/doctor/status/package entry point; `scripts/gen_docs.py`
  regenerates the tier tables in `docs/<runtime>.md`.

## Function scope

Keep each function to one operation and at most 80 source lines
(`tests/test_architecture.py`). Separate validation, planning, rendering and file mutation
so preservation and dry-run behaviour can be reviewed independently. Use composition and
small modules; use classes only for state, resource lifetimes or invariants. Validate
external data first and catch expected failures beside the operation that raises them;
do not disguise programming errors with broad exception tuples. Name repeated protocol
values once; resolve home-dependent paths from explicit inputs, not global constants.
Comments only for constraints the code cannot show.

## Compatibility and behaviour

- Telemetry that is missing or changed fails open. Runner command validation fails closed.
  Every hook finishes within its deadline and passes on overrun.
- Installers preserve unrelated hooks, settings and user edits. Use manifests for
  replacement and retirement, report conflicts, keep backups. Dry runs write nothing.
- Never write or bypass runtime hook trust.
- Precedence is defaults, adapter config, user config, environment override.
- No backward-compatibility layers; only user-owned files and handoff data are preserved.
- Never touch live homes (`~/.cursor`, `~/.codex`, `~/.claude`, `~/.context-guard`) in tests or
  verification; use `--home` with a temporary directory.

## Validation

Run from the repository root.

```sh
uv run --python 3.14 python -m unittest discover -s tests -t .
uv run --python 3.14 python -m unittest discover -s adapters/cursor/tests -t .
uv run --python 3.14 python -m unittest discover -s adapters/codex/tests -t .
uv run --python 3.14 python -m unittest discover -s adapters/claude/tests -t .
uv run --python 3.14 python scripts/gen_docs.py --check
```

Also spot-check with `--python 3.15`. Tests assert observable contracts or distinct failure
modes, not helper structure. The `tests` run covers three layers beyond unit tests:

- `tests/contract/`: every recorded payload in `tests/fixtures/<runtime>/` runs through
  `adapters/<runtime>/hooks.py` as a process (no telemetry, above the block tier, corrupted)
  and must exit 0 with one native response, empty stderr and the expected decision. Codex
  responses are checked against the hook schemas embedded in the codex binary.
- `tests/installed/`: `cli.py install --runtime all --home <tmp>` with real interpreter
  discovery, then the exact registered hook commands run with recorded payloads and temporary
  telemetry; also doctor and byte-identical uninstall.
- `tests/live/`: `scripts/sandbox_smoke.sh --dry-run` with stub runtimes; the real run (the
  same script) is skipped unless `CONTEXT_GUARD_LIVE=1`. The script uses the real logins,
  installs into a temporary sandbox with `--home`, runs hooks with `CONTEXT_GUARD_HOME=<sandbox>`
  and spends a few cheap model calls per logged-in runtime. A Codex run that logs no hook events
  prints the one-time hook-trust review command and keeps the sandbox for the rerun. Run it only when asked.

Fixtures come only from `scripts/record_payloads.py` (read-only over real logs, rollouts,
transcripts and the codex binary; prompts, text, paths and ids scrubbed). Re-record when a
runtime changes its payloads, then review the diff. Adapter tests build on the recorded
payloads; add a hand-written payload only for a case no runtime has produced.

```sh
uv run --python 3.14 python scripts/record_payloads.py --runtime all
scripts/sandbox_smoke.sh [--only claude|codex|cursor] [--dry-run]
```

## Documentation

Update `docs/`, the README and shipped content when behaviour, configuration or paths
change; run `scripts/gen_docs.py` after editing adapter tiers. Docs stand alone: state the
current contract and the reason for compatibility code, not chat history or personal paths.
