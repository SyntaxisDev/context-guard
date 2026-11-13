"""Independent observation of recorded native ACP wake, without executing a host.

The capture links journal model message ids to ACP chunks, and native idle/restart
notifications to journal turns. Controlled child context and result sentinels prove
model-visible delivery; a completion notification or a prompt marker alone cannot.
"""

ARTIFACT_NAMES = frozenset({"journal", "lifecycle", "wire"})


def model_chunks(wire: list[dict]) -> dict[tuple[str, str | None], dict]:
    """Aggregate actual ACP model chunks by message id and native child metadata.

    Args:
        wire: Scrubbed native wire messages in their captured order.

    Returns:
        Model messages with their text and first/last native sequence indices.
    """
    messages: dict[tuple[str, str | None], dict] = {}
    for row in wire:
        params = row["message"]["params"]
        update = params.get("update", {})
        if row["direction"] != "receive" or update.get("sessionUpdate") != "agent_message_chunk":
            continue
        agent = update.get("_meta", {}).get("github.com/copilot", {}).get("agentId")
        key = (update["messageId"], agent)
        message = messages.setdefault(
            key, {"text": "", "first": row["sequence"], "last": row["sequence"], "session": params["sessionId"]}
        )
        message["text"] += update["content"]["text"]
        message["last"] = row["sequence"]
    return messages


def wire_event(fixture: dict, event: dict) -> int | None:
    """Cross-check a lifecycle notification against its actual ACP wire message.

    Args:
        fixture: Captured native lifecycle fixture.
        event: The lifecycle event to locate independently on the wire.

    Returns:
        The original sequence index, or None without matching native evidence.
    """
    for row in fixture["wire"]:
        message = row["message"]
        if (
            row["direction"] == "receive"
            and message["method"] == "github.com/copilot/sessionEvent"
            and message["params"] == event
        ):
            return row["sequence"]
    return None


def matching_model(record: dict, messages: dict, root: str) -> dict | None:
    """Match actual journal model text with independently aggregated ACP output.

    Args:
        record: Native journal assistant message.
        messages: Aggregated wire messages.
        root: Captured root session identity.

    Returns:
        Matched wire output, or None for synthetic or unmatched journal messages.
    """
    data = record["data"]
    message = messages.get((data.get("messageId"), record.get("agentId")))
    if not data.get("model") or not message or message["session"] != root:
        return None
    return message if message["text"] == data.get("content") else None


def observed_wake(fixture: dict) -> dict:
    """Derive wake and model-visible result delivery from captured native boundaries.

    Args:
        fixture: Scrubbed journal, lifecycle notifications, wire and source provenance.

    Returns:
        Computed parent-start and result-delivery evidence, explicitly labeled as
        offline replay of a measured native observation rather than fresh execution.
    """
    result = {
        "parent_started": False,
        "result_delivered": False,
        "observation": "measured native ACP capture; offline replay, not fresh host execution",
    }
    root, journal = fixture["root_session"], fixture["journal"]
    if (
        not fixture["provenance"].get("version")
        or {item["name"] for item in fixture["provenance"].get("artifacts", [])} != ARTIFACT_NAMES
    ):
        return result
    messages = model_chunks(fixture["wire"])
    for idle in fixture["lifecycle"]:
        if idle["type"] != "assistant.idle" or idle.get("agentId") or idle.get("sessionId") != root:
            continue
        idle_index = wire_event(fixture, idle)
        if idle_index is None:
            continue
        for complete in journal:
            if complete["type"] != "subagent.completed" or not complete.get("agentId"):
                continue
            agent = complete["agentId"]
            started = any(
                row["type"] == "subagent.started"
                and row.get("agentId") == agent
                and row["data"].get("executionMode") == "background"
                and row["timestamp"] < idle["timestamp"]
                for row in journal
            )
            if not started or complete["timestamp"] <= idle["timestamp"]:
                continue
            observe_restart(fixture, idle, idle_index, complete, messages, result)
            if result["result_delivered"]:
                return result
    return result


def observe_restart(fixture: dict, idle: dict, idle_index: int, complete: dict, messages: dict, result: dict) -> None:
    """Observe a root restart and child result without any new human submission.

    Args:
        fixture: Native captured evidence.
        idle: Root idle notification.
        idle_index: Original ACP idle sequence index.
        complete: Native child completion after idle.
        messages: Independently aggregated native model messages.
        result: Computed evidence updated only when the native sequence is established.
    """
    root, journal = fixture["root_session"], fixture["journal"]
    child_outputs = [
        row
        for row in journal
        if row["type"] == "assistant.message"
        and row.get("agentId") == complete["agentId"]
        and idle["timestamp"] < row["timestamp"] < complete["timestamp"]
        and all(marker in row["data"].get("content", "") for marker in ("S_CHILD_4P6C", "TOOL_READ_OK"))
        and matching_model(row, messages, root)
    ]
    for turn in fixture["lifecycle"]:
        if (
            turn["type"] != "assistant.turn_start"
            or turn.get("agentId")
            or turn.get("sessionId") != root
            or turn["timestamp"] <= complete["timestamp"]
        ):
            continue
        turn_index = wire_event(fixture, turn)
        journal_turn = any(
            row["type"] == turn["type"]
            and not row.get("agentId")
            and row["timestamp"] == turn["timestamp"]
            and row["data"] == turn["data"]
            for row in journal
        )
        earlier_turn = any(
            row["type"] == "assistant.turn_start"
            and not row.get("agentId")
            and idle["timestamp"] < row["timestamp"] < turn["timestamp"]
            for row in fixture["lifecycle"]
        )
        if turn_index is None or turn_index <= idle_index or not journal_turn or earlier_turn:
            continue
        for output in journal:
            if (
                output["type"] != "assistant.message"
                or output.get("agentId")
                or output["timestamp"] <= turn["timestamp"]
            ):
                continue
            wire_output = matching_model(output, messages, root)
            if not wire_output or wire_output["first"] <= turn_index:
                continue
            if human_submission(fixture, idle, idle_index, output, wire_output["last"]):
                continue
            result["parent_started"] = True
            delivered_child = any(
                idle_index < matching_model(row, messages, root)["first"]
                and matching_model(row, messages, root)["last"] < turn_index
                for row in child_outputs
            )
            if delivered_child and all(
                marker in output["data"].get("content", "")
                for marker in ("CHILD_RETURNED_ACP", "S_CHILD_4P6C", "TOOL_READ_OK")
            ):
                result["result_delivered"] = True
                result["idle_at"], result["restarted_at"] = idle["timestamp"], turn["timestamp"]
                result["model_message"] = output["data"]["messageId"]
                return


def human_submission(fixture: dict, idle: dict, idle_index: int, output: dict, last_index: int) -> bool:
    """Detect an intervening human prompt independently in journal and ACP requests.

    Args:
        fixture: Scrubbed captured native evidence.
        idle: Root idle notification.
        idle_index: Native wire position of idle.
        output: Root journal model output under evaluation.
        last_index: Native wire position of its final output chunk.

    Returns:
        True when a root human message or ACP submission intervenes.
    """
    human = any(
        row["type"] == "user.message"
        and not row.get("agentId")
        and row["data"].get("source") != "system"
        and idle["timestamp"] < row["timestamp"] <= output["timestamp"]
        for row in fixture["journal"]
    )
    submitted = any(
        row["direction"] == "send"
        and row["message"]["method"] == "session/prompt"
        and idle_index < row["sequence"] <= last_index
        for row in fixture["wire"]
    )
    return human or submitted
