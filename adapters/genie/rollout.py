"""Live-context estimate from the tail of a Genie rollout.

The latest token_count `last_token_usage` estimates live context, not cumulative usage. A compaction
record after it means the count no longer describes the context. Rollouts are an unstable interface:
missing or changed records yield None so warnings fail open.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from core.tail import tail_lines

COMPACTION_PAYLOADS = {"context_compacted", "compaction"}


@dataclass(frozen=True)
class Usage:
    """Live context usage read from a rollout.

    Attributes:
        tokens: Tokens in the live context.
        window: Model context window in tokens, or None when unknown.
    """

    tokens: int
    window: int | None


def count(value: object) -> int | None:
    """Validate an untrusted JSON token count.

    Args:
        value: Untrusted JSON value.

    Returns:
        The value when it is a non-negative int (bools excluded), else None.
    """
    return value if type(value) is int and value >= 0 else None


def parse(line: bytes) -> dict | None:
    """Decode one rollout line.

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


def is_compaction(record: dict) -> bool:
    """Tell whether a rollout record marks a context compaction.

    Args:
        record: Decoded rollout record.

    Returns:
        True for a `compacted` record or a payload of a compaction type.
    """
    body = record.get("payload")
    return record.get("type") == "compacted" or (isinstance(body, dict) and body.get("type") in COMPACTION_PAYLOADS)


def usage_from(info: object) -> Usage | None:
    """Read live usage from a token_count event's `info`.

    Args:
        info: Untrusted `info` value of a token_count event.

    Returns:
        `total_tokens`, else input plus output tokens, with the model window; None when the
        counts are missing or malformed.
    """
    last = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(last, dict):
        return None
    tokens = count(last.get("total_tokens"))
    if "total_tokens" not in last:
        incoming, outgoing = count(last.get("input_tokens")), count(last.get("output_tokens", 0))
        tokens = None if incoming is None or outgoing is None else incoming + outgoing
    if tokens is None:
        return None
    window = count(info.get("model_context_window"))
    return Usage(tokens, window or None)


def latest_usage(path: object, tail_bytes: int) -> Usage | None:
    """Estimate live context from the latest token count in a rollout's tail.

    Args:
        path: Untrusted rollout path from the hook payload.
        tail_bytes: How many trailing bytes of the rollout to scan.

    Returns:
        The latest usage, or None when the path is invalid or unreadable, no token count is in
        the tail, or a compaction follows the latest one.
    """
    if not isinstance(path, str) or not path or "\0" in path or tail_bytes <= 0:
        return None
    try:
        lines = tail_lines(Path(path), tail_bytes)
    except OSError:
        return None
    for line in reversed(lines):
        record = parse(line)
        if record is None:
            continue
        if is_compaction(record):
            return None
        body = record.get("payload")
        if record.get("type") == "event_msg" and isinstance(body, dict) and body.get("type") == "token_count":
            return usage_from(body.get("info"))
    return None
