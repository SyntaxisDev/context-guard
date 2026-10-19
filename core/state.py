"""Locked per-session state with one schema, and the atomic JSON helpers it relies on."""

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from core import deadline

SCHEMA = {
    "tier": str,
    "turn": int,
    "asked_turn": int,
    "handoff_turn": int,
    "refresh_turn": int,
    "blocked_prompt": str,
    "handoff_path": str,
    "handoff_label": str,
    "handoff_at": int,
    "pending_handoff_tokens": int,
    "handoff_requested_at": float,
    "budget_sent": bool,
    "learned_window": int,
    "adapter": dict,
}
KEY_LENGTH = 24
SESSION_FILE = re.compile(rf"[0-9a-f]{{{KEY_LENGTH}}}\.(?:json|lock)")


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on a lock file, creating it and its parents as needed.

    Args:
        path: Lock file; it is kept after release.

    Yields:
        None, while the lock is held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_json(path: Path, data: dict) -> None:
    """Write a JSON object atomically through a temporary file in the same directory.

    Args:
        path: Destination file; parents are created.
        data: JSON-serialisable object.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    """Read a JSON object file, failing open.

    Args:
        path: File to read.

    Returns:
        The object, or an empty one when the file is missing, unreadable, malformed or not an object.
    """
    try:
        text = path.read_text()
    except OSError, UnicodeDecodeError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def valid_field(value: object, kind: type) -> bool:
    """Tell whether a value is valid for a schema field type.

    Args:
        value: Stored value.
        kind: The field's type from SCHEMA.

    Returns:
        True for exact bools, non-negative ints, finite non-negative numbers for floats, strings
        without NUL characters, and instances of any other type.
    """
    if kind is bool:
        return type(value) is bool
    if kind is int:
        return type(value) is int and value >= 0
    if kind is float:
        try:
            return type(value) in (int, float) and value >= 0 and math.isfinite(value)
        except OverflowError:
            return False
    if kind is str:
        return isinstance(value, str) and "\0" not in value
    return isinstance(value, kind)


def validated(state: dict) -> dict:
    """Keep only schema fields with valid values; damaged state fails open to defaults.

    Args:
        state: Session state as read or as mutated.

    Returns:
        A new dict with the valid schema fields of `state`.
    """
    return {key: value for key, value in state.items() if key in SCHEMA and valid_field(value, SCHEMA[key])}


def session_key(runtime: str, session_id: str) -> str:
    """Derive the file name stem for one runtime's session.

    Args:
        runtime: Runtime name.
        session_id: The runtime's session id.

    Returns:
        A KEY_LENGTH-character hex digest of both.
    """
    return hashlib.sha256((runtime + "\0" + session_id).encode()).hexdigest()[:KEY_LENGTH]


@contextlib.contextmanager
def session_state(directory: Path, runtime: str, session_id: str, *, commit: bool = False) -> Iterator[dict]:
    """Yield one session's state under an exclusive lock and persist it on normal exit.

    With `commit`, the state decides the hook's response: it is saved only when the hook
    deadline still leaves time to deliver that response, and the deadline then stops interrupting.

    Args:
        directory: The runtime's state directory.
        runtime: Runtime name.
        session_id: The runtime's session id.
        commit: Commit the active hook deadline before saving.

    Yields:
        The validated state, to be mutated in place; it is validated again before saving.
    """
    key = session_key(runtime, session_id)
    path = directory / f"{key}.json"
    with file_lock(directory / f"{key}.lock"):
        state = validated(read_json(path))
        yield state
        if commit:
            deadline.commit()
        write_json(path, validated(state))


def prune(directory: Path, days: float, now: float | None = None) -> None:
    """Remove session state untouched for `days`, with its lock; every other file in the directory is kept.

    Locking never updates a lock file's mtime, so only the state file's mtime shows a session is live.
    Unreadable directories and entries are skipped.

    Args:
        directory: The runtime's state directory.
        days: Age in days after which a session is removed.
        now: Current time in epoch seconds; None reads the clock.
    """
    cutoff = (time.time() if now is None else now) - days * 86400
    try:
        entries = [entry for entry in directory.iterdir() if SESSION_FILE.fullmatch(entry.name)]
    except OSError:
        return
    for entry in entries:
        orphan = entry.suffix == ".lock" and not entry.with_suffix(".json").exists()
        try:
            if (entry.suffix == ".json" or orphan) and entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                entry.with_suffix(".lock").unlink(missing_ok=True)
        except OSError:
            continue
