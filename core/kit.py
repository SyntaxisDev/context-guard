"""The single kit copy every runtime's hooks run from, and the interpreter it is pinned to.

The kit lives at <home>/.context-guard/kit with core/, content/, adapters/ and cli.py,
plus a .venv whose python every hook command uses. Kit files are owned through the
same install manifest as runtime files (runtime name "kit"), so stale files are removed
and user edits are reported. Interpreter discovery and venv creation are injectable
through Toolchain so tests can pin the current interpreter without uv.
"""

import os
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.config import KIT_NAME, kit_root
from core.install import MANIFEST_NAME, manifest_path
from core.install_spec import InstallSpec

KIT_RUNTIME = "kit"
SOURCE_ROOT = Path(__file__).resolve().parent.parent
KIT_ENTRIES = ("core", "content", "adapters", "cli.py")
SKIPPED_NAMES = frozenset({"__pycache__", ".DS_Store", ".venv", "tests"})
SKIPPED_SUFFIXES = (".pyc", ".pyo")
MIN_PYTHON = (3, 14)
REQUIREMENT = f">={MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
TOOL_SECONDS = 120
GIT_SECONDS = 1
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
VERSION_PROBE = "import sys; print('%d.%d' % sys.version_info[:2])"
UPDATE_LOG = "kit-update.log"
UPDATE_RETRY_SECONDS = 600


class ToolchainError(RuntimeError):
    """No usable interpreter or venv; the message tells the user how to fix it."""


def kit_dir(home: Path) -> Path:
    return kit_root(home) / KIT_NAME


def venv_dir(kit: Path) -> Path:
    return kit / ".venv"


def venv_python(kit: Path) -> Path:
    return venv_dir(kit) / "bin" / "python"


def shipped(path: Path, source: Path) -> bool:
    parts = path.relative_to(source).parts
    return not SKIPPED_NAMES.intersection(parts) and path.suffix not in SKIPPED_SUFFIXES


def source_files(source: Path) -> list[Path]:
    found = []
    for entry in KIT_ENTRIES:
        path = source / entry
        candidates = (
            [path]
            if path.is_file()
            else sorted(p for p in path.rglob("*") if p.is_file())
        )
        found.extend(p for p in candidates if shipped(p, source))
    return found


def bytecode_dirs(kit: Path) -> list[Path]:
    """Caches the kit python writes beside the hook modules; uninstall removes them with the kit."""
    if not kit.is_dir():
        return []
    return sorted(
        path
        for path in kit.rglob("__pycache__")
        if path.is_dir() and not path.is_symlink() and venv_dir(kit) not in path.parents
    )


def kit_spec(kit: Path, source: Path = SOURCE_ROOT) -> InstallSpec:
    return InstallSpec(
        KIT_RUNTIME,
        files={
            kit / p.relative_to(source): p.read_text() for p in source_files(source)
        },
    )


def source_version(source: Path = SOURCE_ROOT) -> str:
    try:
        data = tomllib.loads((source / "pyproject.toml").read_text())
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        return "unknown"
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) else "unknown"


def source_commit(source: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and COMMIT.fullmatch(commit) else None


def provenance(source: Path) -> dict:
    commit = source_commit(source)
    return {"source": str(source), **({"commit": commit} if commit else {})}


def behind(manifest: dict) -> tuple[Path, str, str] | None:
    """(source, recorded commit, current commit) when the source checkout has a newer HEAD."""
    source, recorded = manifest.get("source"), manifest.get("commit")
    if not source or not recorded or not Path(source).is_dir():
        return None
    current = source_commit(Path(source))
    if current is None or current == recorded:
        return None
    return Path(source), recorded, current


def staleness(manifest: dict) -> str | None:
    """A one-line notice when the source checkout the kit was installed from has a newer HEAD."""
    found = behind(manifest)
    if found is None:
        return None
    source, recorded, current = found
    return (
        f"context-guard kit is behind its source: installed {recorded[:8]} from {source}, repo now at "
        f"{current[:8]}. Tell the user once: rerun `uv run --python 3.14 python cli.py install "
        f"--runtime all` in {source} to update."
    )


def update_log(home: Path) -> Path:
    return kit_root(home) / UPDATE_LOG


def installed_runtimes(home: Path) -> list[str]:
    manifests = manifest_path(home, KIT_RUNTIME).parent.parent.glob(
        f"*/{MANIFEST_NAME}"
    )
    return sorted(
        path.parent.name for path in manifests if path.parent.name != KIT_RUNTIME
    )


def update_argv(home: Path, source: Path, runtimes: list[str]) -> list[str]:
    selected = [arg for runtime in runtimes for arg in ("--runtime", runtime)]
    return [
        str(venv_python(kit_dir(home))),
        str(source / "cli.py"),
        "install",
        *selected,
        "--home",
        str(home),
    ]


def spawn_update(argv: list[str], log: Path) -> bool:
    """Start install detached from the hook; its output goes to the log, the hook never waits."""
    actions = [
        (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
        (
            os.POSIX_SPAWN_OPEN,
            1,
            str(log),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o644,
        ),
        (os.POSIX_SPAWN_DUP2, 1, 2),
    ]
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        os.posix_spawn(argv[0], argv, os.environ, file_actions=actions, setsid=True)
    except OSError:
        return False
    return True


def recently_attempted(log: Path) -> bool:
    try:
        return time.time() - log.stat().st_mtime < UPDATE_RETRY_SECONDS
    except OSError:
        return False


def auto_update(home: Path, manifest: dict) -> str | None:
    """Reinstall from the source checkout when it moved on; None when current or the update could not start."""
    found = behind(manifest)
    runtimes = installed_runtimes(home)
    log = update_log(home)
    if found is None or not runtimes or recently_attempted(log):
        return None
    source, recorded, current = found
    if not spawn_update(update_argv(home, source, runtimes), log):
        return None
    return (
        f"context-guard kit is updating in the background from {source} ({recorded[:8]} to "
        f"{current[:8]}); the new hooks load in the next session. Mention it once in one short "
        f"line; the install output is in {log}."
    )


def run_tool(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=TOOL_SECONDS, check=False
        )
    except OSError, subprocess.TimeoutExpired:
        return None


def python_version(python: Path) -> tuple[int, int] | None:
    result = run_tool([str(python), "-c", VERSION_PROBE])
    match = re.fullmatch(r"(\d+)\.(\d+)", result.stdout.strip()) if result else None
    if result is None or result.returncode != 0 or match is None:
        return None
    return int(match[1]), int(match[2])


def find_python() -> Path:
    """Prefer `uv python find`; otherwise a python3 on PATH that is new enough."""
    candidates = []
    found = run_tool(["uv", "python", "find", "--system", "--no-project", REQUIREMENT])
    if found and found.returncode == 0 and found.stdout.strip():
        candidates.append(Path(found.stdout.strip()))
    system = shutil.which("python3")
    if system:
        candidates.append(Path(system))
    for candidate in candidates:
        version = python_version(candidate)
        if version is not None and version >= MIN_PYTHON:
            return candidate
    raise ToolchainError(
        f"context-guard needs Python {REQUIREMENT}; install one with "
        "`uv python install 3.14` (or put python3 >= 3.14 on PATH) and rerun install"
    )


def create_venv(python: Path, venv: Path) -> None:
    attempts = [[str(python), "-m", "venv", "--clear", str(venv)]]
    if shutil.which("uv"):
        attempts.insert(
            0, ["uv", "venv", "--clear", "--quiet", "--python", str(python), str(venv)]
        )
    for argv in attempts:
        result = run_tool(argv)
        if result is not None and result.returncode == 0:
            return
    raise ToolchainError(f"could not create {venv} with {python}")


@dataclass(frozen=True)
class Toolchain:
    find_python: Callable[[], Path] = find_python
    create_venv: Callable[[Path, Path], None] = create_venv
    version: Callable[[Path], tuple[int, int] | None] = python_version


def interpreter_problem(python: Path, toolchain: Toolchain) -> str | None:
    version = toolchain.version(python)
    if version is None:
        return f"kit interpreter {python} does not run; rerun install to recreate the kit venv"
    if version < MIN_PYTHON:
        return (
            f"kit interpreter {python} is Python {version[0]}.{version[1]}; context-guard needs "
            f"{REQUIREMENT}; rerun install to recreate the kit venv"
        )
    return None


def venv_base(kit: Path, toolchain: Toolchain) -> Path | None:
    """Return the interpreter to build the venv from, or None when the venv already works."""
    if interpreter_problem(venv_python(kit), toolchain) is None:
        return None
    return toolchain.find_python()


def create_kit_venv(kit: Path, base: Path, toolchain: Toolchain) -> Path:
    toolchain.create_venv(base, venv_dir(kit))
    problem = interpreter_problem(venv_python(kit), toolchain)
    if problem:
        raise ToolchainError(problem)
    return venv_python(kit)
