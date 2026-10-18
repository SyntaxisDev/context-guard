"""Best-effort context telemetry from the tail of a Claude Code JSONL transcript.

Transcripts are written asynchronously and their schema is not a hook contract. Only the
latest main-context assistant input usage counts (cache tokens included, output excluded);
a changed or unreadable latest record yields no count so policy fails open.
"""
import datetime as dt
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SYNTHETIC_MODEL = "<synthetic>"
AUTO_TRIGGER = "auto"
AGENT_ID = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Reading:
    """tokens/model of the latest assistant usage; compaction = preTokens of the latest auto compaction."""
    tokens: int | None = None
    model: str | None = None
    compaction: int | None = None


def tail_lines(path: Path, tail_bytes: int, since_bytes: int = 0) -> list[bytes]:
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        offset = max(0, size - tail_bytes, since_bytes)
        handle.seek(offset)
        if offset and offset != since_bytes:
            handle.readline()
        return handle.read().splitlines()


def records(lines: list[bytes]) -> Iterator[dict]:
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(record, dict):
            yield record


def usage_tokens(message: dict) -> int | None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    parts = [usage.get("input_tokens"), usage.get("cache_creation_input_tokens", 0),
             usage.get("cache_read_input_tokens", 0)]
    if any(type(part) is not int or part < 0 for part in parts):
        return None
    return sum(parts)


def auto_compaction(record: dict) -> int | None:
    meta = record.get("compactMetadata")
    if not isinstance(meta, dict) or meta.get("trigger") != AUTO_TRIGGER:
        return None
    tokens = meta.get("preTokens")
    return tokens if type(tokens) is int and tokens > 0 else None


def written_after(record: dict, since_time: float) -> bool:
    """Records without a parseable timestamp cannot prove they postdate a compaction."""
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return False
    try:
        return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() > since_time
    except (ValueError, OverflowError):
        return False


def scan(found: Iterator[dict], since_time: float | None) -> Reading:
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
    """Read the transcript tail after an optional compaction offset; missing files read as unknown."""
    if not path or "\0" in path or tail_bytes <= 0:
        return Reading()
    try:
        lines = tail_lines(Path(path), tail_bytes, since_bytes)
    except OSError:
        return Reading()
    return scan(records(lines), since_time)


def size(path: str | None) -> int:
    if not path or "\0" in path:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def subagent_path(payload: dict) -> str | None:
    """A subagent's own transcript: the payload field, else <session transcript>/subagents/agent-<id>.jsonl."""
    explicit = payload.get("agent_transcript_path")
    if isinstance(explicit, str) and explicit:
        return explicit
    parent, agent = payload.get("transcript_path"), payload.get("agent_id")
    if not isinstance(parent, str) or not parent.endswith(".jsonl") or not isinstance(agent, str) \
            or not AGENT_ID.fullmatch(agent):
        return None
    return str(Path(parent).with_suffix("") / "subagents" / f"agent-{agent}.jsonl")
