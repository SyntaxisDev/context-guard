"""Install or remove namespaced Copilot plugin role preferences.

Run explicitly through the packaged launcher. Only model, effort and context preferences in
settings.json are merged; hooks, agent files, permissions and trust are never written.
"""

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

KIT = Path(__file__).resolve().parents[2]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from adapters.copilot import hooks, mapping  # noqa: E402
from core import config as config_mod, install  # noqa: E402
from core.install_spec import InstallSpec  # noqa: E402

MANIFEST_RUNTIME = "copilot-plugin-profiles"
COMMANDS = ("install", "uninstall")
EXIT_OK, EXIT_ISSUES, EXIT_ERROR = 0, 1, 2


def agents_spec(home: Path, config: dict, environ: Mapping[str, str]) -> InstallSpec:
    """Plan only namespaced model, effort and context preferences.

    Args:
        home: The user home.
        config: Loaded Copilot adapter configuration.
        environ: Environment that may set COPILOT_HOME.

    Returns:
        The reversible preference merge.
    """
    identity = mapping.install_identity(home, MANIFEST_RUNTIME, environ)
    manifest = install.read_manifest(install.manifest_path(home, identity))
    path = mapping.copilot_home(home, environ) / mapping.SETTINGS_FILE
    entries = mapping.subagent_settings(config, f"{mapping.PLUGIN_NAME}:")
    return InstallSpec(
        runtime=MANIFEST_RUNTIME,
        json_merges=[mapping.subagent_merge(path, entries, manifest)],
    )


def plan(command: str, home: Path, environ: Mapping[str, str], *, force: bool) -> install.Plan:
    """Plan a preference install or uninstall without writing anything.

    Rendering and manifest failures raise ValueError or OSError from `core.install` and `core.render`.

    Args:
        command: "install" or "uninstall".
        home: The user home that holds the manifest.
        environ: Environment for configuration overrides and COPILOT_HOME.
        force: Whether to replace or remove user-edited files, keeping backups.

    Returns:
        The plan.
    """
    spec = agents_spec(home, hooks.load_config(home, environ), environ)
    manifest = install.read_manifest(
        install.manifest_path(home, mapping.install_identity(home, MANIFEST_RUNTIME, environ))
    )
    if command == "install":
        return install.plan(spec, manifest, force=force)
    return install.plan_uninstall(spec, manifest, force=force)


def parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The parser.
    """
    result = argparse.ArgumentParser(prog="agents.py", description=__doc__.splitlines()[0])
    result.add_argument("command", choices=COMMANDS)
    result.add_argument("--home", type=Path, help="home directory to act on (default: the user home)")
    result.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
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
        result = plan(args.command, home, environment, force=False)
        if not args.dry_run:
            identity = mapping.install_identity(home, MANIFEST_RUNTIME, environment)
            install.apply(result, install.manifest_path(home, identity), install.backup_dir(home))
    except (ValueError, OSError) as exc:
        print(f"context-guard: {exc}", file=sys.stderr)
        return EXIT_ERROR
    lines = install.describe(result, dry_run=args.dry_run)
    if args.dry_run:
        lines.append("Dry run: nothing was written.")
    elif result.changes:
        lines.append("Start a new Copilot session to load the change.")
    print("\n".join(lines), file=out or sys.stdout)
    return EXIT_ISSUES if result.conflicts else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
