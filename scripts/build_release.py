#!/usr/bin/env python3
"""Turn a `cli.py package --runtime all --marketplace NAME --out DIR` tree into GitHub release artifacts.

Per runtime, `DIR/<runtime>/plugins/<plugin>` becomes `DIR/<plugin>-<runtime>-<version>.zip` with the
plugin root at the zip root (Claude CoWork uploads need the manifest directory there), and the whole
`DIR/<runtime>/` tree, marketplace listing included, becomes
`DIR/<plugin>-<runtime>-marketplace-<version>.zip` for `<runtime> plugin marketplace add`.
`DIR/SHA256SUMS` lists every zip. Entries are sorted and carry a fixed timestamp, so the same tree
zips to the same bytes. `--tag` first checks that a release tag is `v` followed by `project.version`.
"""

import argparse
import hashlib
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = "plugins"
CHECKSUMS = "SHA256SUMS"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
SKIPPED = ("__pycache__", ".DS_Store")


class ReleaseError(Exception):
    """A release that cannot be built; main prints it and exits 1."""


def project_version(root: Path) -> str:
    """Read `project.version` from a checkout's pyproject.toml.

    Args:
        root: The source checkout.

    Returns:
        The version string.

    Raises:
        ReleaseError: The file is unreadable or has no string version.
    """
    try:
        version = tomllib.loads((root / "pyproject.toml").read_text()).get("project", {}).get("version")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"cannot read {root / 'pyproject.toml'}: {exc}"
        raise ReleaseError(msg) from exc
    if not isinstance(version, str):
        msg = "pyproject.toml has no project.version"
        raise ReleaseError(msg)
    return version


def check_tag(tag: str, version: str) -> None:
    """Check that a release tag names the project version.

    Args:
        tag: The git tag, e.g. `v0.1.0`.
        version: The project version.

    Raises:
        ReleaseError: The tag is not `v<version>`.
    """
    if tag != f"v{version}":
        msg = f"tag {tag!r} does not match project.version {version!r}; expected 'v{version}'"
        raise ReleaseError(msg)


def plugin_roots(out: Path) -> list[tuple[str, Path]]:
    """Find every packaged plugin in a package output tree.

    Args:
        out: The `--out` directory of `cli.py package`.

    Returns:
        (runtime, plugin root) pairs sorted by runtime, then plugin name.

    Raises:
        ReleaseError: The tree holds no packaged plugin.
    """
    found = sorted((path.parent.parent.name, path) for path in out.glob(f"*/{PLUGINS_DIR}/*") if path.is_dir())
    if not found:
        msg = f"{out} holds no <runtime>/{PLUGINS_DIR}/<plugin>; run cli.py package --runtime all --out {out}"
        raise ReleaseError(msg)
    return found


def write_zip(root: Path, target: Path) -> None:
    """Zip a plugin directory deterministically with its root as the zip root.

    Args:
        root: The plugin directory.
        target: The zip file to write; replaced when it exists.
    """
    files = sorted(
        p for p in root.rglob("*") if p.is_file() and not any(part in SKIPPED for part in p.relative_to(root).parts)
    )
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, path.read_bytes())


def checksums(paths: list[Path]) -> str:
    """Render a `sha256sum`-compatible checksum list.

    Args:
        paths: The files to list, by name only.

    Returns:
        One `<sha256>  <name>` line per file.
    """
    return "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in paths)


def check_listings(out: Path, runtimes: list[str]) -> None:
    """Check that every runtime tree holds a marketplace listing beside its plugins.

    Args:
        out: The package output tree.
        runtimes: The runtimes with a packaged plugin.

    Raises:
        ReleaseError: A runtime tree has no file outside `plugins/`, so it was packaged without
            `--marketplace`.
    """
    for runtime in runtimes:
        tree = out / runtime
        if not any(p.is_file() and p.relative_to(tree).parts[0] != PLUGINS_DIR for p in tree.rglob("*")):
            msg = f"{tree} has no marketplace listing; package with --marketplace NAME"
            raise ReleaseError(msg)


def build(out: Path, version: str) -> list[Path]:
    """Zip every packaged plugin and its marketplace tree in `out` and write the checksum list.

    Args:
        out: The package output tree; the zips and SHA256SUMS are written into it.
        version: The project version used in the zip names.

    Returns:
        Per runtime the plugin zip and the marketplace zip, then the checksum file. A tree without
        plugins or listings raises ReleaseError through `plugin_roots` or `check_listings` before
        anything is written.
    """
    roots = plugin_roots(out)
    check_listings(out, [runtime for runtime, _ in roots])
    zips = []
    for runtime, root in roots:
        for target, source in (
            (out / f"{root.name}-{runtime}-{version}.zip", root),
            (out / f"{root.name}-{runtime}-marketplace-{version}.zip", out / runtime),
        ):
            write_zip(source, target)
            zips.append(target)
    sums = out / CHECKSUMS
    sums.write_text(checksums(zips))
    return [*zips, sums]


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    """Check the release tag and build the artifacts, printing each artifact path.

    Args:
        argv: Command-line arguments, or None for `sys.argv`.
        root: The source checkout whose pyproject.toml holds the version.

    Returns:
        0 on success, 1 when the tag does not match or the release cannot be built.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="package output tree to zip (cli.py package --marketplace --out)")
    parser.add_argument("--tag", help="release tag that must equal v<project.version>")
    args = parser.parse_args(argv)
    if args.out is None and args.tag is None:
        parser.error("give --out, --tag or both")
    try:
        version = project_version(root)
        if args.tag is not None:
            check_tag(args.tag, version)
        artifacts = build(args.out, version) if args.out is not None else []
    except (ReleaseError, OSError) as exc:
        print(f"build_release: {exc}", file=sys.stderr)
        return 1
    for path in artifacts:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
