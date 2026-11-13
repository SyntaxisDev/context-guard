"""Independently validate native Claude idle-parent wake from scrubbed stream evidence."""

import re

ARTIFACT_NAMES = frozenset({"wire", "request"})


def model_text(row: dict) -> str:
    """Read actual model text, excluding thinking and tool metadata.

    Args:
        row: Native assistant event.

    Returns:
        Text blocks emitted by the model, in native order.
    """
    message = row.get("message", {})
    if row.get("type") != "assistant" or not message.get("model") or not message.get("id"):
        return ""
    return "\n".join(part.get("text", "") for part in message.get("content", []) if part.get("type") == "text")


def completed_sequence(rows: list[dict], idle: int, spawn: dict, root: str, token: str) -> tuple[int, ...]:
    """Locate actual child output, matching notification and a subsequent root turn.

    Args:
        rows: Ordered native stream.
        idle: First completed root turn index.
        spawn: Native background task start.
        root: Root session identity.
        token: Controlled experiment sentinel.

    Returns:
        Child, notification, root assistant and root result indices, or an empty tuple.
    """
    child = notification = reply = -1
    prior_messages = {row.get("message", {}).get("id") for row in rows[: idle + 1] if row.get("type") == "assistant"}
    for index in range(idle + 1, len(rows)):
        row = rows[index]
        if row.get("type") == "user":
            return ()
        if row.get("session_id") != root:
            continue
        if (
            child < 0
            and row.get("parent_tool_use_id") == spawn["tool_use_id"]
            and row.get("agent_id") == spawn["task_id"]
            and token in model_text(row)
        ):
            child = index
            prior_messages.add(row["message"]["id"])
        elif (
            child >= 0
            and row.get("type") == "system"
            and row.get("subtype") == "task_notification"
            and row.get("task_id") == spawn["task_id"]
            and row.get("tool_use_id") == spawn["tool_use_id"]
            and row.get("run_id") == spawn.get("run_id")
            and row.get("status") == "completed"
            and row.get("summary") == model_text(rows[child])
        ):
            notification = index
        elif (
            notification >= 0
            and not row.get("parent_tool_use_id")
            and not row.get("agent_id")
            and model_text(row) == token
            and row["message"]["id"] not in prior_messages
        ):
            reply = index
        elif (
            reply >= 0
            and row.get("type") == "result"
            and row.get("subtype") == "success"
            and row.get("is_error") is False
            and not row.get("parent_tool_use_id")
            and not row.get("agent_id")
            and row.get("result") == token
        ):
            return child, notification, reply, index
    return ()


def observed_wake(fixture: dict) -> dict:
    """Compute idle-parent wake and actual result delivery without trusting verdict flags.

    Args:
        fixture: Recorder-produced native stream with redacted identities and source hashes.

    Returns:
        Evidence derived from native chronology; this is offline replay of a measured capture.
    """
    result = {
        "mechanism": "native",
        "parent_started": False,
        "result_delivered": False,
        "observation": "measured native Claude capture; offline replay, not fresh host execution",
    }
    provenance = fixture.get("provenance", {})
    artifacts = provenance.get("artifacts", [])
    if (
        not provenance.get("version")
        or {item.get("name") for item in artifacts} != ARTIFACT_NAMES
        or any(
            not re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", "")) or item.get("bytes", 0) <= 0 for item in artifacts
        )
    ):
        return result
    rows, root, token = fixture["wire"], fixture["root_session"], fixture["marker"]
    if not re.fullmatch(r"NATIVE_CLAUDE_WAKE_[A-Z0-9]+", token):
        return result
    for start, spawn in enumerate(rows):
        if (
            spawn.get("type") != "system"
            or spawn.get("subtype") != "task_started"
            or spawn.get("is_backgrounded") is not True
            or spawn.get("session_id") != root
            or not all(spawn.get(key) for key in ("task_id", "tool_use_id", "run_id"))
        ):
            continue
        idle = next(
            (
                index
                for index in range(start + 1, len(rows))
                if rows[index].get("type") == "result" and rows[index].get("session_id") == root
            ),
            None,
        )
        if (
            idle is None
            or rows[idle].get("subtype") != "success"
            or rows[idle].get("is_error") is not False
            or rows[idle].get("parent_tool_use_id")
            or rows[idle].get("agent_id")
        ):
            continue
        # Child output before idle proves waiting, even if another text event follows later.
        if any(
            (row.get("parent_tool_use_id") == spawn["tool_use_id"] and model_text(row))
            or (row.get("subtype") == "task_notification" and row.get("task_id") == spawn["task_id"])
            for row in rows[start + 1 : idle]
        ):
            continue
        sequence = completed_sequence(rows, idle, spawn, root, token)
        if sequence:
            result.update(
                parent_started=True,
                result_delivered=True,
                sequence={
                    "spawn": start,
                    "idle": idle,
                    "child": sequence[0],
                    "notification": sequence[1],
                    "reply": sequence[2],
                    "result": sequence[3],
                },
            )
            return result
    return result
