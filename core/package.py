"""Render a self-contained runtime plugin directory from the shipped content, core and one adapter.

A package vendors core/, content/ and adapters/<runtime>/ (no tests, caches or venv), a POSIX
launcher that finds Python >= 3.14 when a hook runs and otherwise exits 0 printing nothing, and the
files the adapter describes through `plugin_spec(config) -> PluginSpec`: the native manifest, the
hook registrations and rendered native files (agents, skills, policy). Planning, validation and
writing are separate so a dry run renders and checks everything without touching the output.
"""

import json
import re
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from core import kit, render
from core.config import deep_merge
from core.state import read_json, write_json

PLUGINS_DIR = "plugins"
LAUNCHER = "hooks/context-guard-hook"
VENDORED = ("core", "content")
ADAPTER_CONFIG = "config.json"
STAGING_SUFFIX = ".staging"
LAUNCHER_TEXT = r"""#!/bin/sh
# Runs a context-guard hook with Python >= 3.14; without one it prints nothing and exits 0.
# Usage: context-guard-hook <script relative to the package root> <hook arguments...>
# Desktop hosts start hooks with a minimal PATH, so common install directories are searched too;
# CONTEXT_GUARD_SEARCH_PATH replaces that list.
root=$(CDPATH= cd -- "${0%/*}/.." 2>/dev/null && pwd) || exit 0
script=$root/${1:-}
[ -n "${1:-}" ] && [ -f "$script" ] || exit 0
shift
search=${CONTEXT_GUARD_SEARCH_PATH-$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin}
PATH=${PATH:-}${PATH:+${search:+:}}$search
if uv=$(command -v uv 2>/dev/null); then
    python=$("$uv" python find --system --no-project --no-python-downloads '>=3.14' </dev/null 2>/dev/null) \
        && [ -x "$python" ] && exec "$python" "$script" "$@"
fi
for name in python3 python3.15 python3.14; do
    python=$(command -v "$name" 2>/dev/null) || continue
    "$python" -c 'import sys; sys.exit(sys.version_info < (3, 14))' </dev/null >/dev/null 2>&1 \
        && exec "$python" "$script" "$@"
done
exit 0
"""


class PackageError(ValueError):
    """A package that cannot be rendered completely or written safely."""


@dataclass(frozen=True)
class PluginSpec:
    """One runtime's plugin; every path is relative to the plugin root.

    Attributes:
        manifest_file: Path of the native plugin manifest.
        manifest: The manifest object; its `name` names the plugin directory.
        hooks_file: Path of the hook registration file.
        hooks: The hook registrations.
        root_token: How hook commands name the plugin root (e.g. an environment variable the
            runtime expands).
        files: Further rendered files (agents, skills, policy) mapped to their text.
    """

    manifest_file: str
    manifest: Mapping
    hooks_file: str
    hooks: Mapping
    root_token: str
    files: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """The plugin name from the manifest, empty when absent."""
        return str(self.manifest.get("name", ""))


def plugin_dir(out: Path, spec: PluginSpec) -> Path:
    """Locate the plugin directory inside an output directory.

    Args:
        out: The package output directory.
        spec: The plugin.

    Returns:
        `<out>/plugins/<name>`.
    """
    return out / PLUGINS_DIR / spec.name


def dump_json(data: Mapping) -> str:
    """Serialize a JSON object the way package files are written.

    Args:
        data: The object.

    Returns:
        Indented JSON with a trailing newline.
    """
    return json.dumps(data, indent=2) + "\n"


def vendored_files(source: Path, runtime: str) -> dict[str, str]:
    """Collect core/, content/ and the runtime's adapter from a source checkout.

    Args:
        source: The source checkout.
        runtime: The adapter to vendor.

    Returns:
        POSIX paths relative to `source` mapped to file text; tests, caches and venvs are skipped.
    """
    roots = [source / entry for entry in VENDORED] + [source / "adapters" / runtime]
    files = {}
    for root in roots:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            if kit.shipped(path, source):
                files[path.relative_to(source).as_posix()] = path.read_text()
    return files


def rendered_files(spec: PluginSpec) -> dict[str, str]:
    """Collect the files the package renders rather than vendors.

    Args:
        spec: The plugin.

    Returns:
        The manifest, hook registrations, launcher and spec files, by relative path.
    """
    return {
        spec.manifest_file: dump_json(spec.manifest),
        spec.hooks_file: dump_json(spec.hooks),
        LAUNCHER: LAUNCHER_TEXT,
        **spec.files,
    }


def plan(source: Path, runtime: str, spec: PluginSpec, overrides: Mapping | None = None) -> dict[str, str]:
    """Plan every package file without writing anything.

    Args:
        source: The source checkout to vendor from.
        runtime: The adapter to vendor.
        spec: The plugin.
        overrides: Settings baked into the vendored adapter config; None or empty keeps it as shipped.

    Returns:
        Every package file by relative path.

    Raises:
        PackageError: A rendered file would replace vendored code, or `validate` rejects the spec.
    """
    files = vendored_files(source, runtime)
    adapter_config = f"adapters/{runtime}/{ADAPTER_CONFIG}"
    if overrides:
        files[adapter_config] = dump_json(deep_merge(read_json(source / adapter_config), overrides))
    rendered = rendered_files(spec)
    clashes = sorted(rendered.keys() & files.keys())
    if clashes:
        msg = f"rendered files would replace vendored code: {', '.join(clashes)}"
        raise PackageError(msg)
    validate(rendered, spec)
    return files | rendered


def strings(value: object) -> Iterator[str]:
    """Walk every string in a JSON value.

    Args:
        value: A JSON value.

    Yields:
        Each string, depth first through objects and lists.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def unsafe_path(relative: str) -> bool:
    """Tell whether a package-relative path could leave the plugin root.

    Args:
        relative: A POSIX path relative to the plugin root.

    Returns:
        True for empty, absolute, `..`-holding or backslash-holding paths.
    """
    path = PurePosixPath(relative)
    return not relative or path.is_absolute() or ".." in path.parts or "\\" in relative


def validate(rendered: Mapping[str, str], spec: PluginSpec) -> None:
    """Check the rendered files and hook commands before anything is written.

    Args:
        rendered: The rendered files by relative path.
        spec: The plugin.

    Raises:
        PackageError: The plugin name is not a plain directory name, a rendered path escapes the
            root, a file holds unfilled tokens, or a hook command refers to a path the package
            does not render.
    """
    if not spec.name or unsafe_path(spec.name):
        msg = f"plugin name {spec.name!r} is not a plain directory name"
        raise PackageError(msg)
    for relative, text in rendered.items():
        if unsafe_path(relative):
            msg = f"{relative!r} escapes the plugin root"
            raise PackageError(msg)
        unfilled = sorted({match[1] for match in render.TOKEN.finditer(text)})
        if unfilled:
            msg = f"{relative}: unfilled tokens {', '.join(unfilled)}"
            raise PackageError(msg)
    referenced = re.compile(re.escape(spec.root_token) + r"/([^\s\"'\\]+)")
    for command in strings(spec.hooks):
        for target in referenced.findall(command):
            if unsafe_path(target) or (
                target not in rendered and not any(known.startswith(target.rstrip("/") + "/") for known in rendered)
            ):
                msg = f"hook command refers to {target!r}, which the package does not render"
                raise PackageError(msg)


def stage(directory: Path, files: Mapping[str, str]) -> None:
    """Write a fresh copy of the package into a staging directory.

    Args:
        directory: The staging directory; any previous content is removed first.
        files: Every package file by relative path; must include the launcher.
    """
    shutil.rmtree(directory, ignore_errors=True)
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (directory / LAUNCHER).chmod(0o755)


def read_listing(path: Path) -> dict | None:
    """Read an existing marketplace listing.

    Args:
        path: The listing file.

    Returns:
        The listing, or None when the file does not exist.

    Raises:
        PackageError: The file is unreadable or is not a listing with a `plugins` list.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read {path}: {exc}"
        raise PackageError(msg) from exc
    try:
        current = json.loads(text)
    except ValueError:
        current = None
    if not isinstance(current, dict) or not isinstance(current.get("plugins", []), list):
        msg = f"{path} is not a marketplace listing; choose another --out"
        raise PackageError(msg)
    return current


def merged_listing(path: Path, listing: Mapping, name: str) -> dict:
    """Merge this plugin's listing into the existing one; other plugins' entries are kept.

    Args:
        path: The listing file, which may not exist yet.
        listing: The listing for this plugin alone, with a `plugins` list.
        name: The plugin name whose existing entries are replaced.

    Returns:
        The listing to write; existing top-level fields win over `listing`'s.
    """
    current = read_listing(path)
    if current is None:
        return dict(listing)
    others = [
        entry for entry in current.get("plugins", []) if not (isinstance(entry, Mapping) and entry.get("name") == name)
    ]
    return {**listing, **current, "plugins": others + list(listing["plugins"])}


def write(
    out: Path,
    spec: PluginSpec,
    files: Mapping[str, str],
    listing: tuple[str, Mapping] | None = None,
) -> dict | None:
    """Write the package, replacing a previous package of this plugin wholesale.

    The package is staged beside the target and renamed into place.

    Args:
        out: The package output directory.
        spec: The plugin.
        files: Every package file by relative path, as planned.
        listing: Marketplace listing file name relative to `out` and this plugin's listing, or
            None to write no listing.

    Returns:
        The marketplace listing as written, or None when none was requested.

    Raises:
        PackageError: The target exists and is not a package of this plugin, or the existing
            listing cannot be merged.
    """
    target = plugin_dir(out, spec)
    if target.is_symlink() or (target.exists() and not (target / spec.manifest_file).is_file()):
        msg = f"{target} exists and is not a {spec.name} package; choose another --out"
        raise PackageError(msg)
    merged = merged_listing(out / listing[0], listing[1], spec.name) if listing is not None else None
    staging = target.with_name(target.name + STAGING_SUFFIX)
    stage(staging, files)
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    if merged is not None:
        write_json(out / listing[0], merged)
    return merged
