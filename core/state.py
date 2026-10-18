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
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_json(path: Path, data: dict) -> None:
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
    """Return a JSON object, or an empty one when the file is missing or malformed."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def valid_field(value: object, kind: type) -> bool:
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
    """Keep only schema fields with valid values; damaged state fails open to defaults."""
    return {key: value for key, value in state.items()
            if key in SCHEMA and valid_field(value, SCHEMA[key])}


def session_key(runtime: str, session_id: str) -> str:
    return hashlib.sha256((runtime + "\0" + session_id).encode()).hexdigest()[:KEY_LENGTH]


@contextlib.contextmanager
def session_state(directory: Path, runtime: str, session_id: str, commit: bool = False):
    """Yield one session's state under an exclusive lock and persist it on normal exit.

    With `commit`, the state decides the hook's response: it is saved only when the hook
    deadline still leaves time to deliver that response, and the deadline then stops interrupting.
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
    """Remove session state files untouched for `days`; every other file in the directory is kept."""
    cutoff = (time.time() if now is None else now) - days * 86400
    try:
        entries = [entry for entry in directory.iterdir() if SESSION_FILE.fullmatch(entry.name)]
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue
