"""Explicitly grant or retire handoff directory access for one Cursor CLI workspace.

Only workspace/.cursor/cli.json permissions.allow is changed.
Native denies, readonly and protected paths remain overrides.
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

from adapters.cursor import hooks  # noqa: E402
from core import config as config_mod, install  # noqa: E402
from core.install_spec import InstallSpec, JsonMerge  # noqa: E402

PERMISSIONS_FILE = ".cursor/cli.json"
MANIFEST_RUNTIME = "cursor-workspace-access"


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


def permission_lists(data: dict) -> dict:
    """Validate present native permissions; malformed shapes propagate ValueError from object_lists.

    Args:
        data: Current native project settings.

    Returns:
        Existing permissions, or an empty object when the user removed that container.
    """
    return install.object_lists(data, ("permissions",), ("allow", "deny"))


def access_merge(path: Path, grant: str, manifest: dict) -> JsonMerge:
    """Describe the reversible per-workspace directory list change.

    Args:
        path: Native permissions file.
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
        """Add the directory after permission_lists validates native containers.

        Malformed permission containers propagate ValueError from permission_lists.

        Args:
            data: Current native permissions object.

        Returns:
            The native object with the grant.

        """
        nonlocal original
        if original is None:
            original = copy.deepcopy(data)
        permissions = permission_lists(data)
        data.setdefault("permissions", permissions)
        allowed = permissions.setdefault("allow", [])
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
        return retire_grant(data, original if original is not None else data, grant)

    return JsonMerge(path, merge, unmerge)


def retire_grant(data: dict, baseline: dict, grant: str) -> dict:
    """Retire an owned grant after validating current native permission containers.

    Malformed containers raise ValueError through permission_lists, retaining ownership for retry.

    Args:
        data: Current native permissions.
        baseline: Permissions before installation.
        grant: Directory added by this manifest.

    Returns:
        Permissions after retiring the unchanged owned directory.
    """
    permissions = permission_lists(data)
    prior = baseline.get("permissions", {})
    if not isinstance(permissions, dict) or not isinstance(prior, dict):
        return data
    allowed = permissions.get("allow")
    if isinstance(allowed, list) and grant in allowed and grant not in prior.get("allow", []):
        allowed.remove(grant)
        if not allowed and "allow" not in prior:
            del permissions["allow"]
        if not permissions and "permissions" not in baseline:
            del data["permissions"]
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
    identity = f"{MANIFEST_RUNTIME}-{install.digest(str(workspace))[:32]}"
    path = workspace / PERMISSIONS_FILE
    manifest = install.read_manifest(install.manifest_path(home, identity))
    recorded = manifest.get("merges", {}).get(str(path), {}).get("directory")
    if command == "uninstall" and isinstance(recorded, str):
        grant = recorded
    else:
        grant = write_grant(config_mod.handoff_dir(hooks.load(home, environ), home))
        if recorded is not None and recorded != grant:
            msg = "Configured handoff directory changed; uninstall the previous workspace grant first"
            raise ValueError(msg)
    spec = InstallSpec(identity, json_merges=[access_merge(path, grant, manifest)])
    if command == "uninstall":
        result = install.plan_uninstall(spec, manifest)
        validate_writes(result)
        return result
    result = install.plan(spec, manifest)
    validate_writes(result)
    if result.manifest and str(path) in result.manifest["merges"]:
        result.manifest["merges"][str(path)]["directory"] = grant
    result.notes.extend(access_notes(path, grant))
    return result


def validate_writes(result: install.Plan) -> None:
    """Reject readonly native files even when atomic replacement would be possible.

    Args:
        result: Planned native permission changes.

    Raises:
        PermissionError: A changed file or its nearest existing parent is readonly.
    """
    for change in result.changes:
        parent = change.path.parent
        while not parent.exists():
            parent = parent.parent
        if (change.path.exists() and not os.access(change.path, os.W_OK)) or not os.access(parent, os.W_OK):
            msg = f"Permission denied: readonly native configuration {change.path}"
            raise PermissionError(msg)


def write_grant(path: Path) -> str:
    """Encode one literal existing directory as a native Write subtree rule.

    Args:
        path: Configured handoff directory.

    Returns:
        A canonical native Write permission.

    Raises:
        ValueError: The directory name contains native permission pattern syntax.
    """
    resolved = directory(path)
    if any(character in str(resolved) for character in "*?[]{}()"):
        msg = "Handoff directory contains permission pattern syntax; choose a literal directory path"
        raise ValueError(msg)
    return f"Write({resolved / '**'})"


def access_notes(path: Path, grant: str) -> list[install.Conflict]:
    """Report native overrides without changing them or claiming effective approval.

    Args:
        path: Project CLI configuration file.
        grant: Proposed Write directory permission.

    Returns:
        Native override diagnostics, including potentially overlapping denies.
    """
    notes = [
        install.Conflict(
            path,
            "Native readonly, protected paths and ignore rules still override Write grants; "
            "IDE External-File Protection is unchanged.",
        )
    ]
    try:
        data = install.parse_object(path, install.read_text(path))
    except ValueError:
        return notes
    permissions = data.get("permissions", {})
    denied = permissions.get("deny", []) if isinstance(permissions, dict) else []
    if not isinstance(denied, list):
        return notes
    directory_path = grant.removeprefix("Write(").removesuffix("/**)")
    for rule in denied:
        if not isinstance(rule, str) or not rule.startswith("Write(") or not rule.endswith(")"):
            continue
        pattern = rule[6:-1]
        prefix = pattern
        for wildcard in "*?[{":
            prefix = prefix.split(wildcard, 1)[0]
        if not prefix or directory_path.startswith(prefix) or prefix.startswith(directory_path + "/"):
            notes.append(install.Conflict(path, f"Potential conflicting deny retained (native deny wins): {rule}"))
    return notes


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
