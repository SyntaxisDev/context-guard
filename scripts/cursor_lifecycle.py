"""Project explicit Cursor captures into scrubbed independent lifecycle evidence."""

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

MARKER = re.compile(r"CLEAN_BLIND_[a-f0-9]+")
IDENTITIES = ("session_id", "generation_id", "tool_use_id", "parent_tool_call_id", "agent_id")


def framed_text(text: str) -> str:
    """Retain controlled sentinel framing while removing all unrelated text.

    Args:
        text: Captured text.

    Returns:
        Sentinel occurrences separated by redacted nonempty spans.
    """
    return "".join(p if MARKER.fullmatch(p) else "<scrubbed>" for p in re.split(f"({MARKER.pattern})", text) if p)


def native_event(row: dict, fake_id: Callable[[str], str]) -> dict:
    """Allowlist native message identities, tool boundaries and actual text.

    Args:
        row: Projected native database event, never a raw database row.
        fake_id: Shared stable identity redactor.

    Returns:
        Scrubbed message retaining native tool and notification kinds.
    """
    kept = {"session_id": fake_id(row["session_id"]), "message_id": fake_id(row["blob_id"]), "role": row["role"]}
    kept.update({k: fake_id(row[k]) if row[k] else row[k] for k in IDENTITIES[3:] if k in row})
    content = row["content"]
    if row["role"] == "user":
        kept["text"] = framed_text(content)
        task = re.search(r"<system_notification>.*?<task>\n(.*?)</task>", content, re.DOTALL)
        if task:
            fields = dict(re.findall(r"^(kind|status|task_id): ([^\n]+)", task[1], re.MULTILINE))
            kept["notification"] = {k: fake_id(v) if k == "task_id" else v for k, v in fields.items()}
            detail = re.search(r"^detail: (.*)", task[1], re.DOTALL | re.MULTILINE)
            kept["notification"]["detail"] = framed_text(detail[1].rstrip()) if detail else ""
        return kept
    kept["content"] = []
    for part in json.loads(content):
        item = {"type": part["type"]}
        if part["type"] == "text":
            item["text"] = framed_text(part["text"])
        elif part["type"] in {"tool-call", "tool-result"}:
            item.update(toolCallId=fake_id(part["toolCallId"]), toolName=part["toolName"])
            if part["type"] == "tool-call":
                args = part.get("args", {})
                item["operation"] = args.get("toolName", part["toolName"])
                arguments = args.get("arguments", args)
                if "run_in_background" in arguments:
                    item["run_in_background"] = arguments["run_in_background"]
                item["command"] = (
                    args.get("command", "").split(" ")[0]
                    if args.get("command", "").split(" ")[0] in {"cat", "sleep"}
                    else ""
                )
            else:
                item["result"] = framed_text(part.get("result", ""))
                agent = re.search(r"Agent ID: ([\w-]+)", part.get("result", ""))
                if agent:
                    item["agent_id"] = fake_id(agent[1])
        else:
            continue
        kept["content"].append(item)
    return kept


def hook_event(row: dict, fake_id: Callable[[str], str]) -> dict:
    """Allowlist hook timing, identities, command kinds and structured outcomes.

    Args:
        row: Captured hook envelope.
        fake_id: Shared stable identity redactor.

    Returns:
        Scrubbed native hook envelope.
    """
    payload = row["payload"]
    kept = {"ns": row["ns"], "event": row["event"]}
    kept.update({k: fake_id(payload[k]) if payload[k] else payload[k] for k in IDENTITIES if k in payload})
    for key in ("status", "loop_count", "tool_name", "cursor_version"):
        if key in payload:
            kept[key] = payload[key]
    if "prompt" in payload:
        kept["prompt"] = framed_text(payload["prompt"])
    args = payload.get("tool_input", {})
    kept["command"] = (
        args.get("command", "").split(" ")[0] if args.get("command", "").split(" ")[0] in {"cat", "sleep"} else ""
    )
    if "run_in_background" in args:
        kept["run_in_background"] = args["run_in_background"]
    if "tool_output" in payload:
        output = json.loads(payload["tool_output"])
        kept["output"] = {k: output[k] for k in ("status", "isBackground", "exitCode") if k in output}
        if "agentId" in output:
            kept["output"]["agentId"] = fake_id(output["agentId"])
        if "output" in output:
            kept["output"]["text"] = framed_text(output["output"])
    return kept


def artifact(path: Path, name: str) -> dict:
    """Hash unmodified capture artifacts separately from original database digests.

    Args:
        path: Explicit capture artifact.
        name: Logical artifact name.

    Returns:
        Hash and byte count without personal paths.
    """
    data = path.read_bytes()
    return {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def record_cursor_lifecycle(root: Path, fake_id: Callable[[str], str]) -> dict[str, dict]:
    """Record only explicit capture files, without consulting runtime homes or databases.

    Args:
        root: Existing controlled experiment capture directory.
        fake_id: Shared stable identity redactor.

    Returns:
        One lifecycle fixture without computed wake verdicts.

    Raises:
        ValueError: When the capture lacks a unique root or controlled sentinel.
    """
    hooks = [json.loads(line) for line in (root / "hooks.jsonl").read_text().splitlines()]
    starts = [r["payload"]["session_id"] for r in hooks if r["event"] == "sessionStart"]
    native = json.loads((root / "native-allowlisted-events.json").read_text())
    tokens = set(MARKER.findall(json.dumps(native)))
    if len(starts) != 1 or len(tokens) != 1:
        msg = "Cursor capture requires one root and one controlled token"
        raise ValueError(msg)
    transcripts = sorted(root.glob("native-*.jsonl"))
    names = (
        "hooks.jsonl",
        "native-allowlisted-events.json",
        "terminal-chronology.jsonl",
        "ordered-events.json",
        "native-source-hashes.json",
        "transcript-source-hashes.json",
        "source-hashes.json",
        "report.md",
    )
    terminal = [json.loads(line) for line in (root / "terminal-chronology.jsonl").read_text().splitlines()]
    originals = json.loads((root / "native-source-hashes.json").read_text())
    fixture = {
        "source": "Measured Cursor CLI capture; offline replay, not fresh host execution or IDE proof",
        "root_session": fake_id(starts[0]),
        "token": tokens.pop(),
        "provenance": {
            "artifacts": [artifact(root / n, n) for n in names]
            + [artifact(p, f"transcript-{i}") for i, p in enumerate(transcripts)],
            "original_database_hashes": [
                {
                    "source_id": fake_id(p),
                    "session_id": fake_id(Path(p).parent.name),
                    "kind": "wal" if p.endswith("-wal") else "database",
                    "sha256": h,
                }
                for p, h in originals.items()
            ],
            "database_hash_scope": (
                "Reported original DB/WAL bytes; projected events are separate artifacts, not a snapshot"
            ),
        },
        "hooks": [hook_event(row, fake_id) for row in hooks],
        "native": [native_event(row, fake_id) for row in native],
        "terminal": [{"ns": r["ns"], "kind": "input" if "input" in r else "output"} for r in terminal],
        "ordered": [
            {
                k: fake_id(v) if k in IDENTITIES and v else v
                for k, v in r.items()
                if k in {*IDENTITIES, "ns", "kind", "event", "tool_name"}
            }
            for r in json.loads((root / "ordered-events.json").read_text())
        ],
        "transcript_sessions": [fake_id(p.stem.removeprefix("native-")) for p in transcripts],
        "transcripts": [
            [
                {
                    "role": r.get("role"),
                    "type": r.get("type"),
                    "status": r.get("status"),
                    "text": framed_text("\n".join(p.get("text", "") for p in r.get("message", {}).get("content", []))),
                }
                for r in map(json.loads, p.read_text().splitlines())
            ]
            for p in transcripts
        ],
    }
    return {"lifecycle/idle-parent-wake": fixture}
