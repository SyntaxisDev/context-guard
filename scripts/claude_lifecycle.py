"""Record explicit Claude stream captures without executing the captured probe."""

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

MARKER = re.compile(r"NATIVE_CLAUDE_WAKE_[A-Z0-9]+")
IDENTITIES = frozenset({"session_id", "task_id", "tool_use_id", "parent_tool_use_id", "agent_id", "run_id"})
FIELDS = frozenset({"type", "subtype", "status", "is_backgrounded", "is_error", "stop_reason", "result_index"})


def framed_text(text: str) -> str:
    """Retain experiment tokens and the presence of surrounding text.

    Args:
        text: Original captured text.

    Returns:
        Tokens in their original order with all other nonempty spans redacted.
    """
    return "".join(
        part if MARKER.fullmatch(part) else "<scrubbed>" for part in re.split(f"({MARKER.pattern})", text) if part
    )


def native_event(row: dict, fake_id: Callable[[str], str]) -> dict:
    """Select native boundaries, identities and redacted actual model output.

    Args:
        row: Original native stream event.
        fake_id: Stable identity redactor shared across the recording.

    Returns:
        A scrubbed event retaining its native message structure.
    """
    kept = {key: value for key, value in row.items() if key in FIELDS}
    kept.update({key: fake_id(value) if value else value for key, value in row.items() if key in IDENTITIES})
    for key in ("result", "summary"):
        if key in row:
            kept[key] = framed_text(row[key])
    if "message" in row:
        message = row["message"]
        content = message.get("content", [])
        kept["message"] = {
            "role": message["role"],
            "content": [
                {"type": part["type"], **({"text": framed_text(part["text"])} if part["type"] == "text" else {})}
                for part in content
            ]
            if isinstance(content, list)
            else framed_text(content),
        }
        for key in ("id", "model"):
            if key in message:
                kept["message"][key] = fake_id(message[key]) if key == "id" else message[key]
    return kept


def source_artifact(path: Path, name: str) -> dict:
    """Hash original bytes without including personal filesystem paths.

    Args:
        path: Explicit source file, opened read only.
        name: Logical source identity.

    Returns:
        Byte count and SHA-256 of the unmodified source.
    """
    data = path.read_bytes()
    return {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def record_claude_lifecycle(root: Path, fake_id: Callable[[str], str]) -> dict[str, dict]:
    """Record native lifecycle evidence only from an explicit sandbox capture.

    Args:
        root: Capture directory containing wire and request, beside the probe source.
        fake_id: Stable identity redactor.

    Returns:
        One fixture with measured events and original source provenance, no verdict flags.

    Raises:
        ValueError: When the capture lacks a unique native identity, version or experiment token.
    """
    rows = [json.loads(line) for line in (root / "wire.jsonl").read_text().splitlines() if line.startswith("{")]
    starts = [row for row in rows if row.get("subtype") == "init"]
    sessions = {row["session_id"] for row in starts}
    versions = {row["claude_code_version"] for row in starts}
    tokens = set(MARKER.findall(json.dumps(rows)))
    if len(sessions) != 1 or len(versions) != 1 or len(tokens) != 1:
        msg = "Claude capture requires one native session, version and experiment token"
        raise ValueError(msg)
    request = json.loads((root / "request.json").read_text())
    paths = {
        "wire": root / "wire.jsonl",
        "request": root / "request.json",
        "probe": root.parent / "claude-existing-auth.py",
        "verifier": root.parent / "verify-claude.py",
        "original_smoke_verdict": root / "verdict.txt",
    }
    return {
        "lifecycle/idle-parent-wake": {
            "source": "Measured Claude native stream; offline replay, not fresh host execution",
            "provenance": {
                "version": versions.pop(),
                "artifacts": [source_artifact(path, name) for name, path in paths.items()],
            },
            "root_session": fake_id(sessions.pop()),
            "experiment": {
                "token": tokens.pop(),
                "prompt_template": "<scrubbed>" if request["prompt_template"] else "",
            },
            "wire": [native_event(row, fake_id) for row in rows],
        }
    }
