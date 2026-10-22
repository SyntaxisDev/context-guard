"""One bounded event log schema shared by every runtime."""

import collections
import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from core.observation import Observation
from core.state import file_lock

EVENTS_FILE = "events.jsonl"
TRIM_SLACK = 100
EVENT_NAMES = frozenset(
    {
        "prompt",
        "note",
        "note_delivered",
        "block",
        "resend",
        "handoff_requested",
        "handoff_written",
        "handoff_read",
        "subagent_spawn",
        "subagent_budget",
        "subagent_unreturned",
        "subagent_orphaned",
        "subagent_silent",
        "subagent_wake",
    }
)


def record(
    event: str,
    obs: Observation,
    tier: str | None = None,
    detail: Mapping[str, object] | None = None,
    ts: float | None = None,
) -> dict:
    """Build one event log record.

    Args:
        event: One of EVENT_NAMES.
        obs: The observation the event belongs to; supplies runtime, session and token counts.
        tier: The context tier involved, if any; omitted from the record when None.
        detail: Event-specific fields.
        ts: Epoch seconds; None reads the clock.

    Returns:
        The record, ready for `append`.

    Raises:
        ValueError: When `event` is not one of EVENT_NAMES.
    """
    if event not in EVENT_NAMES:
        msg = f"unknown event {event!r}"
        raise ValueError(msg)
    entry = {
        "ts": time.time() if ts is None else ts,
        "runtime": obs.runtime,
        "session": obs.session_id,
        "event": event,
        "tokens": obs.tokens,
        "window": obs.window,
    }
    if tier is not None:
        entry["tier"] = tier
    entry["detail"] = dict(detail or {})
    return entry


def append(path: Path, records: Iterable[dict], max_lines: int, slack: int = TRIM_SLACK) -> None:
    """Append records and trim to the newest max_lines; I/O failures are dropped (fail open).

    Args:
        path: The JSONL log file.
        records: Records to append; values JSON cannot encode are written as strings.
        max_lines: Lines kept after a trim.
        slack: Extra lines allowed past `max_lines` before the file is trimmed.
    """
    lines = [json.dumps(entry, default=str) + "\n" for entry in records]
    if not lines:
        return
    try:
        with file_lock(path.with_name(path.name + ".lock")):
            with path.open("a") as handle:
                handle.writelines(lines)
            with path.open() as handle:
                kept = collections.deque(handle, maxlen=max_lines + slack + 1)
            if len(kept) > max_lines + slack:
                with path.open("w") as handle:
                    handle.writelines(list(kept)[-max_lines:])
    except OSError:
        return


def read(path: Path) -> list[dict]:
    """Read every well-formed record of an event log.

    Args:
        path: The JSONL log file.

    Returns:
        The object lines in file order; a missing or unreadable file gives an empty list and malformed
        lines are skipped.
    """
    try:
        text = path.read_text()
    except OSError, UnicodeDecodeError:
        return []
    found = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found
