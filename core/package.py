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
from core.state import read_json

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
    """One runtime's plugin: every path is relative to the plugin root, root_token is how hook
    commands name that root (e.g. an environment variable the runtime expands)."""

    manifest_file: str
    manifest: Mapping
    hooks_file: str
    hooks: Mapping
    root_token: str
    files: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.manifest.get("name", ""))


def plugin_dir(out: Path, spec: PluginSpec) -> Path:
    return out / PLUGINS_DIR / spec.name


def dump_json(data: Mapping) -> str:
    return json.dumps(data, indent=2) + "\n"


def vendored_files(source: Path, runtime: str) -> dict[str, str]:
    roots = [source / entry for entry in VENDORED] + [source / "adapters" / runtime]
    files = {}
    for root in roots:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            if kit.shipped(path, source):
                files[path.relative_to(source).as_posix()] = path.read_text()
    return files


def rendered_files(spec: PluginSpec) -> dict[str, str]:
    return {
        spec.manifest_file: dump_json(spec.manifest),
        spec.hooks_file: dump_json(spec.hooks),
        LAUNCHER: LAUNCHER_TEXT,
        **spec.files,
    }


def plan(
    source: Path, runtime: str, spec: PluginSpec, overrides: Mapping | None = None
) -> dict[str, str]:
    """Every package file by relative path; `overrides` are baked into the vendored adapter config."""
    files = vendored_files(source, runtime)
    adapter_config = f"adapters/{runtime}/{ADAPTER_CONFIG}"
    if overrides:
        files[adapter_config] = dump_json(
            deep_merge(read_json(source / adapter_config), overrides)
        )
    rendered = rendered_files(spec)
    clashes = sorted(rendered.keys() & files.keys())
    if clashes:
        raise PackageError(
            f"rendered files would replace vendored code: {', '.join(clashes)}"
        )
    validate(rendered, spec)
    return files | rendered


def strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def unsafe_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    return not relative or path.is_absolute() or ".." in path.parts or "\\" in relative


def validate(rendered: Mapping[str, str], spec: PluginSpec) -> None:
    if not spec.name or unsafe_path(spec.name):
        raise PackageError(f"plugin name {spec.name!r} is not a plain directory name")
    for relative, text in rendered.items():
        if unsafe_path(relative):
            raise PackageError(f"{relative!r} escapes the plugin root")
        unfilled = sorted({match[1] for match in render.TOKEN.finditer(text)})
        if unfilled:
            raise PackageError(f"{relative}: unfilled tokens {', '.join(unfilled)}")
    referenced = re.compile(re.escape(spec.root_token) + r"/([^\s\"'\\]+)")
    for command in strings(spec.hooks):
        for target in referenced.findall(command):
            if (
                unsafe_path(target)
                or target not in rendered
                and not any(
                    known.startswith(target.rstrip("/") + "/") for known in rendered
                )
            ):
                raise PackageError(
                    f"hook command refers to {target!r}, which the package does not render"
                )


def stage(directory: Path, files: Mapping[str, str]) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (directory / LAUNCHER).chmod(0o755)


def write(
    out: Path,
    spec: PluginSpec,
    files: Mapping[str, str],
    listing: tuple[str, Mapping] | None = None,
) -> Path:
    """Replace a previous package of this plugin wholesale; anything else at the target is an error."""
    target = plugin_dir(out, spec)
    if (
        target.is_symlink()
        or target.exists()
        and not (target / spec.manifest_file).is_file()
    ):
        raise PackageError(
            f"{target} exists and is not a {spec.name} package; choose another --out"
        )
    staging = target.with_name(target.name + STAGING_SUFFIX)
    stage(staging, files)
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    if listing is not None:
        path = out / listing[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_json(listing[1]))
    return target
