"""Install or remove the context-guard role agents for a Genie plugin: `python agents.py install|uninstall`.

Genie plugins cannot ship agents, so a plugin user who wants the named roles runs this through the
packaged launcher. It writes only the role agent TOMLs, rendered as the script installer renders
them, into `$GENIE_HOME/agents` (else `~/.genie/agents`), and records them in its own install
manifest so uninstall removes only what it wrote. Edited files conflict and are kept unless forced.
Install also creates the profile-specific offer marker, so the SessionStart
hook never offers the step again.
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

from adapters.genie import hooks, mapping  # noqa: E402
from core import config as config_mod, install, render  # noqa: E402
from core.install_spec import InstallSpec  # noqa: E402

MANIFEST_RUNTIME = mapping.PLUGIN_AGENTS
COMMANDS = ("install", "uninstall")
EXIT_OK, EXIT_ISSUES, EXIT_ERROR = 0, 1, 2


def agents_spec(home: Path, config: dict, environ: Mapping[str, str]) -> InstallSpec:
    """Plan the role agent files and nothing else.

    Args:
        home: The user home.
        config: Loaded Genie adapter configuration.
        environ: Environment that may set GENIE_HOME.

    Returns:
        The install spec owning one agent TOML per role.
    """
    runtime = mapping.runtime_mapping(config)
    directory = mapping.agents_dir(home, environ)
    return InstallSpec(
        runtime=MANIFEST_RUNTIME,
        files={directory / Path(item.path).name: item.text for item in render.render_agents(runtime)},
        kit_signatures=mapping.kit_signatures(directory, mapping.role_descriptions(runtime)),
    )


def plan(command: str, home: Path, environ: Mapping[str, str], *, force: bool) -> install.Plan:
    """Plan an install or uninstall of the role agents without writing anything.

    Rendering and manifest failures raise ValueError or OSError from `core.install` and `core.render`.

    Args:
        command: "install" or "uninstall".
        home: The user home that holds the manifest.
        environ: Environment for configuration overrides and GENIE_HOME.
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
    result.add_argument("--force", action="store_true", help="replace or remove edited agent files, keeping backups")
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
            identity = mapping.install_identity(home, MANIFEST_RUNTIME, environment)
            install.apply(result, install.manifest_path(home, identity), install.backup_dir(home))
            if args.command == "install":
                install.record_offer(home, identity)
    except (ValueError, OSError) as exc:
        print(f"context-guard: {exc}", file=sys.stderr)
        return EXIT_ERROR
    lines = install.describe(result, dry_run=args.dry_run)
    if args.dry_run:
        lines.append("Dry run: nothing was written.")
    elif result.changes:
        lines.append("Start a new Genie session to load the change.")
    print("\n".join(lines), file=out or sys.stdout)
    return EXIT_ISSUES if result.conflicts else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
