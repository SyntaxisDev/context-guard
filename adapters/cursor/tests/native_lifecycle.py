"""Independently verify measured Cursor Task wake from native captured evidence."""

import re


def actual_text(row: dict) -> str:
    """Read actual assistant text, excluding reasoning and tool arguments.

    Args:
        row: Allowlisted native event.

    Returns:
        Native text blocks in order, or empty text for other roles.
    """
    if row.get("role") != "assistant":
        return ""
    return "\n".join(p.get("text", "") for p in row.get("content", []) if p.get("type") == "text")


def hook_boundaries(fixture: dict) -> tuple[dict, dict, dict, list[dict]] | None:
    """Find a background Task, completed idle, actual child read and root receipts.

    Args:
        fixture: Recorder-produced capture.

    Returns:
        Correlated hook boundaries, or no valid sequence.
    """
    hooks, root = fixture["hooks"], fixture["root_session"]
    for spawn in hooks:
        if (
            spawn.get("session_id") != root
            or spawn.get("event") != "postToolUse"
            or spawn.get("tool_name") != "Task"
            or spawn.get("parent_tool_call_id")
            or spawn.get("agent_id")
            or spawn.get("run_in_background") is not True
            or spawn.get("output", {}).get("isBackground") is not True
            or spawn.get("output", {}).get("status") != "success"
        ):
            continue
        candidate = candidate_boundaries(fixture, spawn)
        if candidate is not None:
            return candidate
    return None


def candidate_boundaries(fixture: dict, spawn: dict) -> tuple[dict, dict, dict, list[dict]] | None:
    """Correlate one background Task with completed root and child hook boundaries.

    Args:
        fixture: Captured native hook stream.
        spawn: Candidate root background Task result.

    Returns:
        Background, idle, actual read and root result boundaries, or None.
    """
    hooks, root, token = fixture["hooks"], fixture["root_session"], fixture["marker"]
    child = spawn["output"].get("agentId")
    if not child or child == root or not spawn.get("tool_use_id"):
        return None
    stops = [h for h in hooks if h.get("session_id") == root and h.get("event") == "stop"]
    if not stops or any(
        h.get("status") != "completed" or h.get("loop_count") != 0 or h.get("parent_tool_call_id") or h.get("agent_id")
        for h in stops
    ):
        return None
    if any(
        h.get("session_id") == root
        and h.get("event") in {"preToolUse", "postToolUse"}
        and (h.get("tool_name") != "Task" or h.get("tool_use_id") != spawn["tool_use_id"])
        for h in hooks
    ):
        return None
    if any(
        h.get("session_id") == child
        and (
            h.get("parent_tool_call_id") not in (None, "", spawn["tool_use_id"])
            or h.get("agent_id") not in (None, "", child)
        )
        for h in hooks
    ):
        return None
    idle = stops[0]
    if idle.get("generation_id") != spawn.get("generation_id") or idle["ns"] <= spawn["ns"]:
        return None
    reads = [
        h
        for h in hooks
        if h.get("session_id") == child
        and h.get("event") == "postToolUse"
        and h.get("command") == "cat"
        and h.get("tool_name") == "Shell"
        and h.get("output", {}).get("exitCode") == 0
        and token in h.get("output", {}).get("text", "")
    ]
    if len(reads) != 1 or reads[0]["ns"] <= idle["ns"]:
        return None
    read = reads[0]
    if not paired_hook(hooks, read, idle):
        return None
    if len({h.get("generation_id") for h in stops}) != len(stops):
        return None
    if any(not h.get("generation_id") for h in stops):
        return None
    return spawn, idle, reads[0], stops


def child_order_valid(rows: list[dict], child: str, token: str, parent: str) -> bool:
    """Require unique native Shell calls, correlated results and final child text.

    Args:
        rows: Native projected messages in captured per-session order.
        child: Background agent identity.
        token: Controlled sentinel.
        parent: Task tool identity, when native child ancestry is supplied.

    Returns:
        Whether one actual cat result precedes final child text without false identities.
    """
    calls, results = {}, set()
    output = text = -1
    for index, row in enumerate(rows):
        if row.get("session_id") != child:
            continue
        if row.get("agent_id") not in (None, "", child) or row.get("parent_tool_call_id") not in (None, "", parent):
            return False
        for part in row.get("content", []):
            kind, identity = part.get("type"), part.get("toolCallId")
            if kind in {"tool-call", "tool-result"} and not record_child_tool(
                part, row.get("role"), index, calls, results
            ):
                return False
            if kind == "tool-result" and token in part.get("result", ""):
                if calls[identity][1] != "cat" or output >= 0:
                    return False
                output = index
        if token in actual_text(row):
            if not 0 <= output < index or text >= 0:
                return False
            text = index
    return output >= 0 and text > output and len([v for v in calls.values() if v[1] == "cat"]) == 1


def record_child_tool(part: dict, role: str, index: int, calls: dict, results: set) -> bool:
    """Validate and retain one uniquely identified native child tool boundary.

    Args:
        part: Native tool call or result.
        role: Native message role.
        index: Position in the ordered native message stream.
        calls: Previously accepted call identities and their kind, position and name.
        results: Call identities with an accepted result.

    Returns:
        Whether this boundary has the correct role and unambiguous identity.
    """
    identity = part.get("toolCallId")
    if part["type"] == "tool-call":
        if role != "assistant" or not identity or identity in calls or part.get("operation") != "Shell":
            return False
        calls[identity] = (index, part.get("command"), part.get("toolName"))
        return True
    call = calls.get(identity)
    if role != "tool" or call is None or identity in results or call[0] >= index or part.get("toolName") != call[2]:
        return False
    results.add(identity)
    return True


def paired_hook(hooks: list[dict], result: dict, idle: dict) -> bool:
    """Correlate the timed child result with its native pre-tool hook.

    Args:
        hooks: Captured hook boundaries.
        result: Completed native cat hook.
        idle: Root idle boundary preceding child execution.

    Returns:
        Whether a unique matching native invocation occurred after the parent became idle.
    """
    before = [
        h
        for h in hooks
        if h.get("event") == "preToolUse"
        and all(
            h.get(k) == result.get(k) for k in ("session_id", "generation_id", "tool_use_id", "command", "tool_name")
        )
    ]
    return len(before) == 1 and idle["ns"] < before[0]["ns"] < result["ns"]


def transcripts_valid(fixture: dict, child: str) -> bool:
    """Require both independently captured transcripts and their controlled text framing.

    Args:
        fixture: Recorder-produced native capture.
        child: Observed background agent identity.

    Returns:
        Whether both sessions corroborate actual token receipt and token-free initial prompts.
    """
    sessions, transcripts = fixture.get("transcript_sessions", []), fixture.get("transcripts", [])
    if (
        len(sessions) != len({fixture["root_session"], child})
        or set(sessions) != {fixture["root_session"], child}
        or len(transcripts) != len(sessions)
    ):
        return False
    for rows in transcripts:
        if not rows or rows[0].get("role") != "user" or fixture["marker"] in rows[0].get("text", ""):
            return False
        if not any(r.get("role") == "assistant" and fixture["marker"] in r.get("text", "") for r in rows):
            return False
        if rows[-1].get("type") != "turn_ended" or rows[-1].get("status") != "success":
            return False
    return True


def native_receipt(fixture: dict, spawn: dict, stops: list[dict]) -> dict | None:
    """Require actual child output, matching Task notification and distinct root reply.

    Args:
        fixture: Captured native events.
        spawn: Correlated background hook result.
        stops: Completed root generations in chronological order.

    Returns:
        Matching root receipt boundary, or no valid delivery.
    """
    root, token = fixture["root_session"], fixture["marker"]
    child = spawn["output"]["agentId"]
    rows = fixture["native"]
    child_text = [actual_text(r) for r in rows if r.get("session_id") == child and token in actual_text(r)]
    child_results = [
        p
        for r in rows
        if r.get("session_id") == child and r.get("role") == "tool"
        for p in r.get("content", [])
        if token in p.get("result", "")
    ]
    if (
        len(child_text) != 1
        or len(child_results) != 1
        or not child_order_valid(rows, child, token, spawn["tool_use_id"])
    ):
        return None
    launches = [
        p
        for r in rows
        if r.get("session_id") == root and r.get("role") == "tool"
        for p in r.get("content", [])
        if p.get("type") == "tool-result" and p.get("toolCallId") == spawn["tool_use_id"] and p.get("agent_id") == child
    ]
    task_calls = [
        p
        for r in rows
        if r.get("session_id") == root and r.get("role") == "assistant"
        for p in r.get("content", [])
        if p.get("type") == "tool-call"
        and p.get("operation") == "Task"
        and p.get("run_in_background") is True
        and p.get("toolCallId") == spawn["tool_use_id"]
    ]
    if len(launches) != 1 or len(task_calls) != 1:
        return None
    if launches[0].get("toolName") != task_calls[0].get("toolName"):
        return None
    root_rows = [r for r in rows if r.get("session_id") == root]
    replies = [r for r in root_rows if actual_text(r)]
    if not root_boundaries_valid(fixture, spawn, root_rows, replies, stops):
        return None
    for index, row in enumerate(root_rows):
        notice = row.get("notification", {})
        if (
            notice.get("kind") != "subagent"
            or notice.get("status") != "success"
            or notice.get("task_id") != child
            or notice.get("detail") != child_text[0]
        ):
            continue
        if any(token in str(r) for r in root_rows[:index]):
            continue
        if not any(actual_text(r) for r in root_rows[:index]):
            continue
        following = next((r for r in root_rows[index + 1 :] if actual_text(r)), None)
        if (
            following is None
            or token not in actual_text(following)
            or following.get("parent_tool_call_id")
            or following.get("agent_id")
        ):
            continue
        return stops[replies.index(following)]
    return None


def root_boundaries_valid(
    fixture: dict, spawn: dict, root_rows: list[dict], replies: list[dict], stops: list[dict]
) -> bool:
    """Check root ancestry, prompt framing and distinct native messages.

    Args:
        fixture: Recorder-produced capture with transcript corroboration.
        spawn: Matched background Task.
        root_rows: Native root messages in order.
        replies: Native root assistant replies in order.
        stops: Native completed root hook boundaries.

    Returns:
        Whether root identity and independent corroboration are valid.
    """
    token, child = fixture["marker"], spawn["output"]["agentId"]
    if any(r.get("agent_id") or r.get("parent_tool_call_id") for r in root_rows):
        return False
    root_calls = [p for r in root_rows for p in r.get("content", []) if p.get("type") == "tool-call"]
    if len(root_calls) != 1 or root_calls[0].get("operation") != "Task" or not transcripts_valid(fixture, child):
        return False
    if any(token in h.get("prompt", "") for h in fixture["hooks"] if h.get("event") == "beforeSubmitPrompt"):
        return False
    if (
        len(replies) != len(stops)
        or token in actual_text(replies[0])
        or any(r.get("agent_id") or r.get("parent_tool_call_id") for r in replies)
    ):
        return False
    ids = [r.get("message_id") for r in fixture["native"]]
    return not (any(not identity for identity in ids) or len(set(ids)) != len(ids))


def chronology_valid(fixture: dict, spawn: dict, idle: dict, read: dict, receipt: dict) -> bool:
    """Validate cross-stream ordering and absence of human input through receipt.

    Args:
        fixture: Recorder-produced chronology.
        spawn: Background Task result.
        idle: Initial completed root generation.
        read: Actual child token read.
        receipt: Completed new root generation delivering token.

    Returns:
        Whether timestamps and independently captured input boundaries agree.
    """
    if not spawn["ns"] < idle["ns"] < read["ns"] < receipt["ns"]:
        return False
    for name in ("hooks", "ordered", "terminal"):
        rows = fixture[name]
        times = [r.get("ns", 0) for r in rows]
        if times != sorted(times) or not times or times[0] <= 0:
            return False
    for hook in (spawn, idle, read, receipt):
        if not any(
            all(row.get(k) == hook.get(k) for k in ("ns", "event", "session_id", "generation_id", "tool_use_id"))
            for row in fixture["ordered"]
            if row.get("kind") == "hook"
        ):
            return False
    for name in ("terminal", "ordered"):
        inputs = [r["ns"] for r in fixture[name] if r.get("kind") == "input"]
        if not inputs or max(inputs) >= spawn["ns"]:
            return False
    submissions = [h for h in fixture["hooks"] if h.get("event") == "beforeSubmitPrompt"]
    return len(submissions) == 1 and submissions[0]["ns"] < spawn["ns"]


def observed_wake(fixture: dict) -> dict:
    """Derive wake from native boundaries rather than trusting captured verdicts.

    Args:
        fixture: Scrubbed recorder artifact.

    Returns:
        Independently verified wake evidence with the strict-delay control retained.
    """
    result = {
        "mechanism": "native",
        "parent_started": False,
        "result_delivered": False,
        "observation": "Measured Cursor CLI capture; offline replay, not fresh host execution or IDE proof",
    }
    artifacts = fixture.get("provenance", {}).get("artifacts", [])
    required = {
        "hooks.jsonl",
        "native-allowlisted-events.json",
        "terminal-chronology.jsonl",
        "ordered-events.json",
        "transcript-0",
        "transcript-1",
    }
    if (
        {a.get("name") for a in artifacts} != required
        or any(not re.fullmatch(r"[a-f0-9]{64}", a.get("sha256", "")) or a.get("bytes", 0) <= 0 for a in artifacts)
        or not re.fullmatch(r"NATIVE_CURSOR_WAKE_[a-f0-9]+", fixture.get("marker", ""))
    ):
        return result
    boundaries = hook_boundaries(fixture)
    if boundaries is None:
        return result
    spawn, idle, read, stops = boundaries
    receipt = native_receipt(fixture, spawn, stops)
    if receipt is None or not chronology_valid(fixture, spawn, idle, read, receipt):
        return result
    sleep = [
        h
        for h in fixture["hooks"]
        if h.get("session_id") == read["session_id"] and h.get("event") == "postToolUse" and h.get("command") == "sleep"
    ]
    result.update(
        parent_started=True,
        result_delivered=True,
        sequence={"idle_ns": idle["ns"], "child_read_ns": read["ns"], "receipt_ns": receipt["ns"]},
        strict_delay_control="satisfied" if sleep and sleep[0]["ns"] < read["ns"] else "FAILED",
    )
    return result
