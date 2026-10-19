"""Best-effort context telemetry from the tail of a Claude Code JSONL transcript.

Transcripts are written asynchronously and their schema is not a hook contract. Only the
latest main-context assistant input usage counts (cache tokens included, output excluded);
a changed or unreadable latest record yields no count so policy fails open.
"""

import datetime as dt
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from core.tail import tail_lines

SYNTHETIC_MODEL = "<synthetic>"
AUTO_TRIGGER = "auto"
AGENT_ID = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Reading:
    """What the transcript tail says about the live context; unknown fields are None.

    Attributes:
        tokens: Input tokens of the latest main-context assistant usage.
        model: Model of the latest main-context assistant message.
        compaction: `preTokens` of the latest automatic compaction.
    """

    tokens: int | None = None
    model: str | None = None
    compaction: int | None = None


def records(lines: Iterable[bytes]) -> Iterator[dict]:
    """Decode transcript lines, skipping those that are not JSON objects.

    Args:
        lines: JSONL lines.

    Yields:
        Each decoded record object.
    """
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError, UnicodeError:
            continue
        if isinstance(record, dict):
            yield record


def usage_tokens(message: dict) -> int | None:
    """Count the context input of an assistant message's usage.

    Args:
        message: Decoded assistant `message` object.

    Returns:
        Input plus cache creation and cache read tokens, or None when a count is malformed.
    """
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    parts = [
        usage.get("input_tokens"),
        usage.get("cache_creation_input_tokens", 0),
        usage.get("cache_read_input_tokens", 0),
    ]
    if any(type(part) is not int or part < 0 for part in parts):
        return None
    return sum(parts)


def auto_compaction(record: dict) -> int | None:
    """Read the pre-compaction size of an automatic compact boundary.

    Args:
        record: Decoded compact_boundary record.

    Returns:
        Its positive `preTokens`, or None for manual or malformed compactions.
    """
    meta = record.get("compactMetadata")
    if not isinstance(meta, dict) or meta.get("trigger") != AUTO_TRIGGER:
        return None
    tokens = meta.get("preTokens")
    return tokens if type(tokens) is int and tokens > 0 else None


def written_after(record: dict, since_time: float) -> bool:
    """Tell whether a record postdates a compaction.

    Records without a parseable timestamp cannot prove they postdate a compaction.

    Args:
        record: Decoded transcript record.
        since_time: Compaction time as a POSIX timestamp.

    Returns:
        True when the record's ISO timestamp is later than `since_time`.
    """
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return False
    try:
        return dt.datetime.fromisoformat(stamp).timestamp() > since_time
    except ValueError, OverflowError:
        return False


def scan(found: Iterable[dict], since_time: float | None) -> Reading:
    """Fold transcript records, oldest first, into the latest reading.

    A compact boundary clears the token count; subagent, synthetic and pre-compaction
    assistant records are skipped.

    Args:
        found: Decoded records in transcript order.
        since_time: Time of the latest compaction, or None to keep every assistant record.

    Returns:
        The latest token count, model and automatic compaction size.
    """
    tokens = model = compaction = None
    for record in found:
        if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            tokens, compaction = None, auto_compaction(record) or compaction
            continue
        if record.get("type") != "assistant" or record.get("parent_tool_use_id") is not None:
            continue
        message = record.get("message")
        if isinstance(message, dict) and message.get("model") == SYNTHETIC_MODEL:
            continue
        if since_time is not None and not written_after(record, since_time):
            continue
        tokens = usage_tokens(message) if isinstance(message, dict) else None
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            model = message["model"]
    return Reading(tokens, model, compaction)


def read(path: str | None, tail_bytes: int, since_bytes: int = 0, since_time: float | None = None) -> Reading:
    """Read the transcript tail after an optional compaction offset.

    Args:
        path: Transcript path from the hook payload, or None.
        tail_bytes: How many trailing bytes of the transcript to scan.
        since_bytes: Offset the transcript had at the latest compaction; earlier bytes are skipped.
        since_time: Time of the latest compaction, or None.

    Returns:
        The reading; empty when the path is invalid or the file is missing or unreadable.
    """
    if not path or "\0" in path or tail_bytes <= 0:
        return Reading()
    try:
        lines = tail_lines(Path(path), tail_bytes, since_bytes)
    except OSError:
        return Reading()
    return scan(records(lines), since_time)


def size(path: str | None) -> int:
    """Measure a transcript.

    Args:
        path: Transcript path from the hook payload, or None.

    Returns:
        Its size in bytes, or 0 when the path is invalid or the file is unreadable.
    """
    if not path or "\0" in path:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def subagent_path(payload: dict[str, object]) -> str | None:
    """Locate a subagent's own transcript.

    Args:
        payload: Validated subagent hook payload.

    Returns:
        `agent_transcript_path`, else `<session transcript>/subagents/agent-<id>.jsonl`, or
        None when neither can be derived.
    """
    explicit = payload.get("agent_transcript_path")
    if isinstance(explicit, str) and explicit:
        return explicit
    parent, agent = payload.get("transcript_path"), payload.get("agent_id")
    if (
        not isinstance(parent, str)
        or not parent.endswith(".jsonl")
        or not isinstance(agent, str)
        or not AGENT_ID.fullmatch(agent)
    ):
        return None
    return str(Path(parent).with_suffix("") / "subagents" / f"agent-{agent}.jsonl")
