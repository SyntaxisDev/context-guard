---
name: install-context-guard
description: Install, update, check or uninstall context-guard for Cursor, Codex or Claude Code.
---

# Install context-guard

Context-guard is installed from a checkout of its repository with one command line
interface. Requires Python 3.14+ (resolved through `uv` when available).

```sh
python3 <repo>/cli.py install   --runtime cursor|codex|claude|all [--home <dir>] [--dry-run]
python3 <repo>/cli.py uninstall --runtime cursor|codex|claude|all [--home <dir>] [--dry-run]
python3 <repo>/cli.py doctor    --runtime cursor|codex|claude|all [--home <dir>]
python3 <repo>/cli.py status    --runtime cursor|codex|claude|all [--home <dir>]
```

1. Pick the runtime from the request; use `all` only when asked. This session runs in {{runtime_name}}.
2. Preview with `install --dry-run` and show the summary: files to write, keep or
   remove, and conflicts.
3. Apply within the user's authorized scope. Conflicting or locally edited
   generated files are reported and kept; do not force them.
4. Run `doctor` and report its result: installed files compared with freshly
   rendered ones, and the pinned interpreter.
5. Tell the user what remains: restart or start a new session, and review the new
   or changed hooks through the runtime's own hook review. Never write or bypass
   runtime hook trust.

The installer copies one kit to `~/.context-guard/kit/` with its own virtual
environment, and writes only kit-owned files into the runtime home: hook
registrations, the always-on policy, agents and skills, tracked in an install
manifest. User instruction files and unrelated hooks and settings are preserved.
User configuration lives in `~/.context-guard/config.json` (optional
`runtimes.<runtime>` section); handoffs stay in `{{handoff_dir}}`.

Use `--home <temporary dir>` for checks; never test against a live home.
