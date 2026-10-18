"""Live-context estimate from the tail of a Codex rollout.

The latest token_count `last_token_usage` estimates live context, not cumulative usage. A compaction
record after it means the count no longer describes the context. Rollouts are an unstable interface:
missing or changed records yield None so warnings fail open.
"""
import json
from dataclasses import dataclass
from pathlib import Path

COMPACTION_PAYLOADS = {"context_compacted", "compaction"}

@dataclass(frozen=True)
class Usage:
    tokens: int
    window: int | None


def count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def tail_lines(path: Path, tail_bytes: int) -> list[bytes]:
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        offset = max(0, size - tail_bytes)
        handle.seek(offset)
        if offset:
            handle.readline()
        return handle.read().splitlines()


def parse(line: bytes) -> dict | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def is_compaction(record: dict) -> bool:
    body = record.get("payload")
    return record.get("type") == "compacted" or (isinstance(body, dict) and body.get("type") in COMPACTION_PAYLOADS)


def usage_from(info: object) -> Usage | None:
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
