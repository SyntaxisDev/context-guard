"""Live-context estimate from the tail of a Copilot session `events.jsonl`.

The latest `session.usage_record` carries `inputTokens`, which already includes cache reads and writes,
and `maxPromptTokens`, the window of the model that made the call. The file is an unstable interface:
missing, unreadable or changed records yield None so warnings fail open, and no window is ever guessed.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from core.tail import tail_lines

USAGE_RECORD = "session.usage_record"
COMPACTION_RECORD = "session.compaction_complete"
ROOT_INTERACTION = "conversation-agent"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
STATE_DIR = "session-state"
EVENTS_FILE = "events.jsonl"


@dataclass(frozen=True)
class Usage:
    """Live context usage read from a session event log.

    Attributes:
        tokens: Input tokens of the latest model call, cache included.
        window: Context window of the model that made the call, or None when absent.
        model: The model that made the call, or None when absent.
    """

    tokens: int
    window: int | None
    model: str | None


def count(value: object) -> int | None:
    """Validate an untrusted JSON token count.

    Args:
        value: Untrusted JSON value.

    Returns:
        The value when it is a non-negative int (bools excluded), else None.
    """
    return value if type(value) is int and value >= 0 else None


def events_path(copilot_home: Path, session_id: object, transcript_path: object) -> Path | None:
    """Locate a session's event log.

    Args:
        copilot_home: The Copilot home directory.
        session_id: Untrusted session id from the hook payload.
        transcript_path: Untrusted `transcriptPath` from the hook payload, or None.

    Returns:
        The explicit path when it is an absolute `events.jsonl` path; otherwise the log of a validated
        session id under `<copilot_home>/session-state`; None when neither is usable.
    """
    if isinstance(transcript_path, str) and transcript_path and "\0" not in transcript_path:
        explicit = Path(transcript_path)
        if explicit.is_absolute() and explicit.name == EVENTS_FILE:
            return explicit
    if isinstance(session_id, str) and SESSION_ID.fullmatch(session_id):
        return copilot_home / STATE_DIR / session_id / EVENTS_FILE
    return None


def parse(line: bytes) -> dict | None:
    """Decode one event line.

    Args:
        line: One JSONL line.

    Returns:
        The record object, or None when the line is not a JSON object.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def usage_from(data: object, *, child: bool = False) -> Usage | None:
    """Read live usage from a usage record's `data`.

    Args:
        data: Untrusted `data` value of a `session.usage_record`.
        child: Whether the record belongs to a child agent.

    Returns:
        The usage of the requested interaction; None when counts are missing or malformed or the call
        belongs to another interaction type.
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    interaction = "conversation-subagent" if child else ROOT_INTERACTION
    if not isinstance(usage, dict) or usage.get("interactionType") != interaction:
        return None
    tokens = count(usage.get("inputTokens"))
    if tokens is None:
        return None
    model = usage.get("model")
    return Usage(
        tokens, count(usage.get("maxPromptTokens")) or None, model if isinstance(model, str) and model else None
    )


def latest_usage(path: Path | None, tail_bytes: int, agent_id: str | None = None) -> Usage | None:
    """Estimate live context from the latest usage record for one native agent.

    Args:
        path: The event log, or None.
        tail_bytes: How many trailing bytes of the log to scan.
        agent_id: Exact native child identity; None selects root records.

    Returns:
        The latest usage, or None when the log is unusable, no usage record is in the tail, a
        compaction record follows the latest one, or the latest matching record is malformed.
        Missing or unrecognized interaction types invalidate usage rather than reuse older counts.
    """
    if path is None or tail_bytes <= 0:
        return None
    try:
        lines = tail_lines(path, tail_bytes)
    except OSError:
        return None
    for line in reversed(lines):
        record = parse(line)
        if record is None:
            continue
        if record.get("agentId") != agent_id:
            continue
        if applied_compaction(record):
            return None
        if record.get("type") == USAGE_RECORD:
            return usage_from(record.get("data"), child=agent_id is not None)
    return None


def applied_compaction(record: dict) -> bool:
    """Recognize completed compaction that actually changed the conversation.

    Args:
        record: Decoded native event.

    Returns:
        True for successful applied compaction, otherwise False.
    """
    data = record.get("data")
    return (
        record.get("type") == COMPACTION_RECORD
        and isinstance(data, dict)
        and data.get("success") is True
        and data.get("outcome") == "applied"
    )


def latest_compaction(path: Path | None, tail_bytes: int) -> str | None:
    """Find a successful root compaction's stable native event identity.

    Args:
        path: Root session event log.
        tail_bytes: Maximum bytes scanned.

    Returns:
        The latest applied root compaction id, or None without usable evidence.
    """
    if path is None or tail_bytes <= 0:
        return None
    try:
        lines = tail_lines(path, tail_bytes)
    except OSError:
        return None
    for line in reversed(lines):
        record = parse(line)
        if record and not record.get("agentId") and applied_compaction(record):
            identity = record.get("id")
            return identity if isinstance(identity, str) and identity and "\0" not in identity else None
    return None
