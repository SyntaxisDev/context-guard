"""The single kit copy every runtime's hooks run from, and the interpreter it is pinned to.

The kit lives at <home>/.context-guard/kit with core/, content/, adapters/ and cli.py,
plus a .venv whose python every hook command uses. Kit files are owned through the
same install manifest as runtime files (runtime name "kit"), so stale files are removed
and user edits are reported. Interpreter discovery and venv creation are injectable
through Toolchain so tests can pin the current interpreter without uv.
"""

import os
import re
import shlex
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
from core.state import file_lock

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
    """Locate the kit copy for a home.

    Args:
        home: The user home.

    Returns:
        The kit directory.
    """
    return kit_root(home) / KIT_NAME


def venv_dir(kit: Path) -> Path:
    """Locate the kit's virtual environment.

    Args:
        kit: The kit directory.

    Returns:
        The venv directory.
    """
    return kit / ".venv"


def venv_python(kit: Path) -> Path:
    """Locate the interpreter every hook command runs.

    Args:
        kit: The kit directory.

    Returns:
        The venv's python.
    """
    return venv_dir(kit) / "bin" / "python"


def shipped(path: Path, source: Path) -> bool:
    """Tell whether a source file belongs in a kit or package copy.

    Args:
        path: A file inside `source`.
        source: The source checkout.

    Returns:
        False for tests, caches, venvs and bytecode.
    """
    parts = path.relative_to(source).parts
    return not SKIPPED_NAMES.intersection(parts) and path.suffix not in SKIPPED_SUFFIXES


def source_files(source: Path) -> list[Path]:
    """List the shipped files of KIT_ENTRIES in a source checkout.

    Args:
        source: The source checkout.

    Returns:
        The files, sorted within each entry.
    """
    found = []
    for entry in KIT_ENTRIES:
        path = source / entry
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        found.extend(p for p in candidates if shipped(p, source))
    return found


def bytecode_dirs(kit: Path) -> list[Path]:
    """List the caches the kit python writes beside the hook modules; uninstall removes them with the kit.

    Args:
        kit: The kit directory.

    Returns:
        The `__pycache__` directories outside the venv, sorted; empty when the kit is missing.
    """
    if not kit.is_dir():
        return []
    return sorted(
        path
        for path in kit.rglob("__pycache__")
        if path.is_dir() and not path.is_symlink() and venv_dir(kit) not in path.parents
    )


def kit_spec(kit: Path, source: Path = SOURCE_ROOT) -> InstallSpec:
    """Describe the kit install: a copy of every shipped source file.

    Args:
        kit: The kit directory.
        source: The source checkout to copy from.

    Returns:
        The kit's install spec, owned as runtime "kit".
    """
    return InstallSpec(
        KIT_RUNTIME,
        files={kit / p.relative_to(source): p.read_text() for p in source_files(source)},
    )


def source_version(source: Path = SOURCE_ROOT) -> str:
    """Read the project version from a source checkout.

    Args:
        source: The source checkout.

    Returns:
        The pyproject version, or "unknown" when it cannot be read.
    """
    try:
        data = tomllib.loads((source / "pyproject.toml").read_text())
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        return "unknown"
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) else "unknown"


def source_commit(source: Path) -> str | None:
    """Read the HEAD commit of a source checkout within GIT_SECONDS.

    Args:
        source: The source checkout.

    Returns:
        The full commit hash, or None without git, outside a repository or on timeout.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [git, "-C", str(source), "rev-parse", "HEAD"],
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
    """Describe where a kit install came from, for its manifest.

    Args:
        source: The source checkout.

    Returns:
        `source` and, when known, `commit`.
    """
    commit = source_commit(source)
    return {"source": str(source), **({"commit": commit} if commit else {})}


def behind(manifest: dict) -> tuple[Path, str, str] | None:
    """Compare the kit manifest's recorded commit with its source checkout's HEAD.

    Args:
        manifest: The kit manifest.

    Returns:
        (source, recorded commit, current commit) when the source checkout has a different HEAD;
        None when nothing is recorded, the source is gone or HEAD is unknown or unchanged.
    """
    source, recorded = manifest.get("source"), manifest.get("commit")
    if not source or not recorded or not Path(source).is_dir():
        return None
    current = source_commit(Path(source))
    if current is None or current == recorded:
        return None
    return Path(source), recorded, current


def staleness(home: Path, manifest: dict) -> str | None:
    """Build the notice for a kit whose source checkout has moved on.

    Args:
        home: The user home, used in the update command.
        manifest: The kit manifest.

    Returns:
        A one-line notice with the update command, or None when the kit is current.
    """
    found = behind(manifest)
    if found is None:
        return None
    source, recorded, current = found
    return (
        f"context-guard kit is behind its source: installed {recorded[:8]} from {source}, repo now at "
        f"{current[:8]}. Tell the user once: run `{update_command(home, source)}` to update."
    )


def update_log(home: Path) -> Path:
    """Locate the log of the background kit update.

    Args:
        home: The user home.

    Returns:
        The log file path.
    """
    return kit_root(home) / UPDATE_LOG


def installed_runtimes(home: Path) -> list[str]:
    """List the runtimes that have an install manifest.

    Args:
        home: The user home.

    Returns:
        Runtime names, sorted, without the kit itself.
    """
    manifests = manifest_path(home, KIT_RUNTIME).parent.parent.glob(f"*/{MANIFEST_NAME}")
    return sorted(path.parent.name for path in manifests if path.parent.name != KIT_RUNTIME)


def update_argv(home: Path, source: Path, runtimes: list[str]) -> list[str]:
    """Build the install command that updates the kit from its source.

    Args:
        home: The user home to install into.
        source: The source checkout whose cli.py runs.
        runtimes: The runtimes to reinstall.

    Returns:
        The argv, run by the kit python.
    """
    selected = [arg for runtime in runtimes for arg in ("--runtime", runtime)]
    return [
        str(venv_python(kit_dir(home))),
        str(source / "cli.py"),
        "install",
        *selected,
        "--home",
        str(home),
    ]


def update_command(home: Path, source: Path) -> str:
    """Build the shell command a user runs to update the kit.

    Args:
        home: The user home.
        source: The source checkout.

    Returns:
        The quoted command for the installed runtimes, or for all when none is installed.
    """
    return shlex.join(update_argv(home, source, installed_runtimes(home) or ["all"]))


def spawn_update(argv: list[str], log: Path) -> bool:
    """Start install detached from the hook; its output goes to the log, the hook never waits.

    Args:
        argv: The update command; `argv[0]` is an absolute interpreter path.
        log: The file receiving stdout and stderr, truncated first.

    Returns:
        Whether the process started.
    """
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
    """Tell whether an update started within UPDATE_RETRY_SECONDS.

    Args:
        log: The update log, whose mtime marks the last attempt.

    Returns:
        False when the log is missing or unreadable.
    """
    try:
        return time.time() - log.stat().st_mtime < UPDATE_RETRY_SECONDS
    except OSError:
        return False


def claim_update(log: Path) -> bool:
    """Record an update attempt unless one started within UPDATE_RETRY_SECONDS, as one locked step.

    Args:
        log: The update log; touching it records the attempt.

    Returns:
        Whether this caller claimed the attempt; False on any I/O failure.
    """
    try:
        with file_lock(log.with_name(log.name + ".lock")):
            if recently_attempted(log):
                return False
            log.touch()
    except OSError:
        return False
    return True


def auto_update(home: Path, manifest: dict) -> str | None:
    """Reinstall from the source checkout in the background when it moved on.

    Args:
        home: The user home.
        manifest: The kit manifest.

    Returns:
        A one-line notice that the update started; None when the kit is current, no runtime is
        installed, an attempt is recent, or the update could not start.
    """
    found = behind(manifest)
    runtimes = installed_runtimes(home)
    log = update_log(home)
    if found is None or not runtimes or not claim_update(log):
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
    """Run one of this module's fixed toolchain commands within TOOL_SECONDS.

    Args:
        argv: An interpreter or uv command built in this module.

    Returns:
        The completed process with text output, or None when it could not start or timed out.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TOOL_SECONDS, check=False)  # noqa: S603
    except OSError, subprocess.TimeoutExpired:
        return None


def python_version(python: Path) -> tuple[int, int] | None:
    """Probe an interpreter's version by running it.

    Args:
        python: The interpreter.

    Returns:
        (major, minor), or None when it does not run or prints something else.
    """
    result = run_tool([str(python), "-c", VERSION_PROBE])
    match = re.fullmatch(r"(\d+)\.(\d+)", result.stdout.strip()) if result else None
    if result is None or result.returncode != 0 or match is None:
        return None
    return int(match[1]), int(match[2])


def find_python() -> Path:
    """Find a system interpreter of at least MIN_PYTHON for the kit venv.

    Prefers `uv python find`; otherwise a python3 on PATH that is new enough.

    Returns:
        The interpreter path.

    Raises:
        ToolchainError: No candidate runs and is new enough.
    """
    candidates = []
    uv = shutil.which("uv")
    found = run_tool([uv, "python", "find", "--system", "--no-project", REQUIREMENT]) if uv else None
    if found and found.returncode == 0 and found.stdout.strip():
        candidates.append(Path(found.stdout.strip()))
    system = shutil.which("python3")
    if system:
        candidates.append(Path(system))
    for candidate in candidates:
        version = python_version(candidate)
        if version is not None and version >= MIN_PYTHON:
            return candidate
    msg = (
        f"context-guard needs Python {REQUIREMENT}; install one with "
        "`uv python install 3.14` (or put python3 >= 3.14 on PATH) and rerun install"
    )
    raise ToolchainError(msg)


def create_venv(python: Path, venv: Path) -> None:
    """Create or recreate a venv, with uv when available and the venv module otherwise.

    Args:
        python: The base interpreter.
        venv: The venv directory; existing content is cleared.

    Raises:
        ToolchainError: Every attempt failed.
    """
    attempts = [[str(python), "-m", "venv", "--clear", str(venv)]]
    uv = shutil.which("uv")
    if uv:
        attempts.insert(0, [uv, "venv", "--clear", "--quiet", "--python", str(python), str(venv)])
    for argv in attempts:
        result = run_tool(argv)
        if result is not None and result.returncode == 0:
            return
    msg = f"could not create {venv} with {python}"
    raise ToolchainError(msg)


@dataclass(frozen=True)
class Toolchain:
    """Interpreter discovery and venv creation, injectable so tests need no uv.

    Attributes:
        find_python: Return a base interpreter for the venv or raise ToolchainError.
        create_venv: Create a venv from a base interpreter at a directory.
        version: Return an interpreter's (major, minor), or None when it does not run.
    """

    find_python: Callable[[], Path] = find_python
    create_venv: Callable[[Path, Path], None] = create_venv
    version: Callable[[Path], tuple[int, int] | None] = python_version


def interpreter_problem(python: Path, toolchain: Toolchain) -> str | None:
    """Check that an interpreter runs and is new enough.

    Args:
        python: The interpreter to check.
        toolchain: Supplies the version probe.

    Returns:
        A problem with a remedy, or None when the interpreter is usable.
    """
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
    """Decide whether the kit venv needs building, and from which interpreter.

    Propagates `ToolchainError` from `toolchain.find_python` when no interpreter is usable.

    Args:
        kit: The kit directory.
        toolchain: Supplies the version probe and interpreter discovery.

    Returns:
        The interpreter to build the venv from, or None when the venv already works.
    """
    if interpreter_problem(venv_python(kit), toolchain) is None:
        return None
    return toolchain.find_python()


def create_kit_venv(kit: Path, base: Path, toolchain: Toolchain) -> Path:
    """Build the kit venv and check its interpreter.

    Args:
        kit: The kit directory.
        base: The interpreter to build from.
        toolchain: Supplies venv creation and the version probe.

    Returns:
        The venv python.

    Raises:
        ToolchainError: The new venv's interpreter does not run or is too old.
    """
    toolchain.create_venv(base, venv_dir(kit))
    problem = interpreter_problem(venv_python(kit), toolchain)
    if problem:
        raise ToolchainError(problem)
    return venv_python(kit)
