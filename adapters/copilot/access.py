"""Explicitly grant or retire handoff directory access for one Copilot workspace.

Only permissions-config.json locations[workspace].allowed_directories is changed.
Tool approval and hook trust remain separate native controls.
"""

import argparse
import copy
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
from core.install_spec import InstallSpec, JsonMerge  # noqa: E402

PERMISSIONS_FILE = "permissions-config.json"
MANIFEST_RUNTIME = "copilot-workspace-access"


def directory(path: Path) -> Path:
    """Resolve an existing directory, rejecting files and missing inputs.

    Args:
        path: Directory supplied by configuration or the caller.

    Returns:
        Its canonical absolute path.

    Raises:
        ValueError: The path is not an existing directory.
    """
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        msg = f"Not a directory: {resolved}"
        raise ValueError(msg)
    return resolved


def access_merge(path: Path, workspace: str, grant: str, manifest: dict) -> JsonMerge:
    """Describe the reversible per-workspace directory list change.

    Args:
        path: Native permissions file.
        workspace: Canonical workspace key.
        grant: Canonical handoff directory.
        manifest: Persisted ownership of this workspace's merge.

    Returns:
        A merge preserving foreign entries and intervening edits.
    """
    recorded = manifest.get("merges", {}).get(str(path), {})
    original = install.original_object(path, recorded.get("original"))
    if original is None and recorded.get("created"):
        original = {}

    def merge(data: dict) -> dict:
        """Add the directory after object_lists validates native containers.

        Malformed shapes propagate ValueError from object_lists.

        Args:
            data: Current native permissions object.

        Returns:
            The permissions object with the grant.
        """
        nonlocal original
        if original is None:
            original = copy.deepcopy(data)
        location = install.object_lists(data, ("locations", workspace), ("allowed_directories",))
        locations = data.setdefault("locations", {})
        locations.setdefault(workspace, location)
        allowed = location.setdefault("allowed_directories", [])
        if grant not in allowed:
            allowed.append(grant)
        return data

    def unmerge(data: dict) -> dict:
        """Retire the unchanged owned grant, preserving user edits.

        Args:
            data: Current native permissions.

        Returns:
            Permissions after retiring the owned directory.
        """
        return retire_grant(data, original if original is not None else data, workspace, grant)

    return JsonMerge(path, merge, unmerge)


def retire_grant(data: dict, baseline: dict, workspace: str, grant: str) -> dict:
    """Retire an owned grant after native validation, preserving other values.

    Malformed containers propagate ValueError from object_lists to retain ownership for retry.

    Args:
        data: Current native permissions.
        baseline: Permissions before installation.
        workspace: Canonical workspace key.
        grant: Directory added by this manifest.

    Returns:
        Permissions after retiring the unchanged owned directory.
    """
    location = install.object_lists(data, ("locations", workspace), ("allowed_directories",))
    locations = data.get("locations")
    prior_locations = baseline.get("locations", {})
    prior = prior_locations.get(workspace, {}) if isinstance(prior_locations, dict) else {}
    if not isinstance(location, dict) or not isinstance(prior, dict):
        return data
    allowed = location.get("allowed_directories")
    if isinstance(allowed, list) and grant in allowed and grant not in prior.get("allowed_directories", []):
        allowed.remove(grant)
        if not allowed and "allowed_directories" not in prior:
            del location["allowed_directories"]
        if not location and workspace not in prior_locations:
            del locations[workspace]
        if not locations and baseline.get("locations") != {}:
            del data["locations"]
    return data


def plan(command: str, home: Path, workspace: Path, environ: Mapping[str, str]) -> install.Plan:
    """Plan access changes using independent profile and workspace ownership.

    Args:
        command: Install or uninstall.
        home: Explicit user home holding configuration and manifests.
        workspace: Existing workspace directory.
        environ: Configuration and native profile overrides.

    Returns:
        A reversible installer plan without filesystem mutations.

    Raises:
        ValueError: A changed configured grant requires uninstalling the previous grant first.
    """
    workspace = directory(workspace)
    root = directory(mapping.copilot_home(home, environ))
    identity = install.profile_name(mapping.install_identity(home, MANIFEST_RUNTIME, environ), workspace, home)
    path = root / PERMISSIONS_FILE
    manifest = install.read_manifest(install.manifest_path(home, identity))
    recorded = manifest.get("merges", {}).get(str(path), {}).get("directory")
    if command == "uninstall" and isinstance(recorded, str):
        grant = recorded
    else:
        grant = str(directory(config_mod.handoff_dir(hooks.load_config(home, environ), home)))
        if recorded is not None and recorded != grant:
            msg = "Configured handoff directory changed; uninstall the previous workspace grant first"
            raise ValueError(msg)
    spec = InstallSpec(identity, json_merges=[access_merge(path, str(workspace), grant, manifest)])
    if command == "uninstall":
        return install.plan_uninstall(spec, manifest)
    result = install.plan(spec, manifest)
    if result.manifest and str(path) in result.manifest["merges"]:
        result.manifest["merges"][str(path)]["directory"] = grant
    return result


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    """Run explicit workspace setup, printing plans and expected failures.

    Args:
        argv: Command-line arguments; None reads process arguments.
        environ: Configuration environment; None reads the process environment.
        out: Plan output stream; None uses stdout.

    Returns:
        Zero on success, one on conflicts, or two on invalid inputs.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("install", "uninstall"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    environment = os.environ if environ is None else environ
    try:
        home = directory(args.home)
        result = plan(args.command, home, args.workspace, environment)
        if not args.dry_run:
            install.apply(result, install.manifest_path(home, result.runtime), install.backup_dir(home))
    except (ValueError, OSError) as exc:
        print(f"context-guard: {exc}", file=sys.stderr)
        return 2
    print("\n".join(install.describe(result, dry_run=args.dry_run)), file=out or sys.stdout)
    return int(bool(result.conflicts))


if __name__ == "__main__":
    raise SystemExit(main())
