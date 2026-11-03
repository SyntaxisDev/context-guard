"""Install or remove the context-guard hooks for a Cursor plugin: `python hooks_install.py install|uninstall`.

The Cursor app runs a plugin's own hooks only while Settings > Agents > Third-Party Imports is on,
and that setting also imports Claude Code and Codex configuration. The plugin therefore registers
no hooks; a plugin user who wants them runs this once through the packaged launcher. It merges the
hook entries into `~/.cursor/hooks.json`, which Cursor reads whatever that setting says, keeping
every other hook, and records everything it writes in its own install manifest so uninstall removes
only that. A hooks.json the install created is deleted again; one it changed gets its other entries
back unchanged.

A marketplace install lives in a versioned cache directory that a plugin update replaces, so the
registered commands cannot name the plugin root. Install copies the code the hook runs (the
vendored core/, content/, adapters/cursor/, LICENSE and NOTICE of the tree this script runs from)
plus the launcher into `~/.context-guard/cursor-hooks`, owned through the same manifest, and
registers `sh "<that dir>/hooks/context-guard-hook" adapters/cursor/hooks.py <event>`. The copy
keeps working when the plugin moves or is removed, needs no lookup of Cursor's undocumented cache
layout, and fails open through the launcher like the plugin itself. Rerunning install after a
plugin update refreshes the copy; an unchanged copy is left as is. Install also creates the
`~/.context-guard/state/offers/cursor-hooks` marker, so the plugin rule never offers the step again.

Cursor runs a plugin's agents on the parent's model (it strips their `model`), but keeps the `model`
of agents under `~/.cursor/agents` and lets them win over a same-named plugin agent. Install
therefore also writes the rendered role files there (each on its tier's model), owned through the
same manifest: uninstall removes exactly those, and an unchanged file is left as is. Role files that the
`cursor` script install already owns stay with it (and it leaves ours), so uninstalling one never
deletes the other's. The Cursor CLI
reads no `~/.cursor/agents`, which the policy covers by passing the tier model on every Task.
"""

import argparse
import os
import shlex
import sys
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import TextIO

KIT = Path(__file__).resolve().parents[2]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from adapters.cursor import hooks, mapping  # noqa: E402
from core import config as config_mod, install, kit, package, render  # noqa: E402
from core.install_spec import InstallSpec, JsonMerge  # noqa: E402

MANIFEST_RUNTIME = mapping.PLUGIN_HOOKS
HOOKS_DIRNAME = "cursor-hooks"
COMMANDS = ("install", "uninstall")
EXIT_OK, EXIT_ISSUES, EXIT_ERROR = 0, 1, 2


def hooks_dir(home: Path) -> Path:
    """Locate the stable copy the registered hooks run from.

    Args:
        home: The user home.

    Returns:
        `<home>/.context-guard/cursor-hooks`.
    """
    return config_mod.kit_root(home) / HOOKS_DIRNAME


def hook_command(launcher: Path, event: str) -> str:
    """Build the registered command for one native event.

    Args:
        launcher: The launcher inside the stable copy.
        event: Native Cursor hook event.

    Returns:
        The shell-quoted command running the hook script through the launcher.
    """
    return shlex.join(["sh", str(launcher), mapping.HOOK_SCRIPT, event])


def hooks_spec(home: Path, source: Path, config: dict) -> InstallSpec:
    """Plan the stable hook copy, the role agent files and the hooks.json merge.

    A missing LICENSE or NOTICE in `source` raises `core.package.PackageError` (a ValueError).

    Args:
        home: The user home.
        source: The plugin root or source checkout whose code is copied.
        config: Loaded Cursor adapter configuration, whose tiers set the agents' models.

    Returns:
        The install spec owning the copied files, the agent files and our hooks.json entries.
    """
    directory = hooks_dir(home)
    launcher = directory / package.LAUNCHER
    files = {directory / relative: text for relative, text in package.vendored_files(source, mapping.RUNTIME).items()}
    agent_prefix = f"{mapping.AGENTS_DIR}/"
    agents = {
        home / item.path: item.text
        for item in render.rendered_files(mapping.runtime_mapping(config))
        if item.path.startswith(agent_prefix)
    }
    files |= install.unclaimed(home, MANIFEST_RUNTIME, agents)
    files[launcher] = package.LAUNCHER_TEXT
    script = str(launcher)
    merge = JsonMerge(
        home / mapping.HOOKS_FILE,
        partial(
            mapping.merge_hooks, registrations=mapping.registrations(partial(hook_command, launcher)), script=script
        ),
        partial(mapping.unmerge_hooks, script=script),
    )
    return InstallSpec(MANIFEST_RUNTIME, files=files, json_merges=[merge])


def plan(command: str, home: Path, environ: Mapping[str, str], *, force: bool) -> install.Plan:
    """Plan an install or uninstall of the hooks without writing anything.

    Rendering and manifest failures raise ValueError or OSError from `core.install` and `core.package`.

    Args:
        command: "install" or "uninstall".
        home: The user home that holds hooks.json, the agents, the stable copy and the manifest.
        environ: Environment for the configuration overrides.
        force: Whether to replace or remove user-edited files, keeping backups.

    Returns:
        The plan; an uninstall also removes the bytecode caches the hooks wrote into the copy.
    """
    spec = hooks_spec(home, KIT, hooks.load(home, environ))
    manifest = install.read_manifest(install.manifest_path(home, MANIFEST_RUNTIME))
    if command == "install":
        return install.plan(spec, manifest, force=force)
    result = install.plan_uninstall(spec, manifest, force=force)
    result.changes += [
        install.Change(install.REMOVE_TREE, cache, reason="bytecode", backup=False)
        for cache in kit.bytecode_dirs(hooks_dir(home))
    ]
    return result


def parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The parser.
    """
    result = argparse.ArgumentParser(prog="hooks_install.py", description=__doc__.splitlines()[0])
    result.add_argument("command", choices=COMMANDS)
    result.add_argument("--home", type=Path, help="home directory to act on (default: the user home)")
    result.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    result.add_argument("--force", action="store_true", help="replace or remove edited files, keeping backups")
    return result


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    """Run the command; expected failures print one stderr line.

    Args:
        argv: The arguments without the program name, or None for `sys.argv`.
        environ: Environment to read; None reads the process environment.
        out: Where the plan is printed, or None for stdout.

    Returns:
        EXIT_OK, EXIT_ISSUES when conflicts were kept, or EXIT_ERROR.
    """
    args = parser().parse_args(argv)
    environment = os.environ if environ is None else environ
    home = args.home.expanduser().absolute() if args.home else config_mod.resolve_home(KIT, environment)
    try:
        result = plan(args.command, home, environment, force=args.force)
        if not args.dry_run:
            install.apply(result, install.manifest_path(home, MANIFEST_RUNTIME), install.backup_dir(home))
            if args.command == "install":
                install.record_offer(home, MANIFEST_RUNTIME)
    except (ValueError, OSError) as exc:
        print(f"context-guard: {exc}", file=sys.stderr)
        return EXIT_ERROR
    lines = install.describe(result, dry_run=args.dry_run)
    if args.command == "install" and install.manifest_path(home, mapping.RUNTIME).is_file():
        lines.append("Warning: the script install also registers Cursor hooks; uninstall one so hooks run once.")
    if args.dry_run:
        lines.append("Dry run: nothing was written.")
    elif result.changes:
        lines.append("Reload the Cursor window to load the change.")
    print("\n".join(lines), file=out or sys.stdout)
    return EXIT_ISSUES if result.conflicts else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
