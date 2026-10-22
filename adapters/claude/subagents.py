"""Track Claude Code background subagents so a parent learns about one that has gone silent.

Claude Code wakes the parent itself with a `<task-notification>` when a background subagent
stops, so a finished subagent needs no note; a hung or killed one never notifies. An Agent
PostToolUse whose `tool_response.status` is `async_launched` records the spawn in the parent's
session state under its `agentId` (role, spawn time, transcript). The subagent's own PostToolUse
hooks carry the parent's `session_id` plus `agent_id` and refresh its last activity, kept to
ACTIVITY_SECONDS so most tool hooks do not write state; its SubagentStop ends tracking. On each
parent PostToolUse, UserPromptSubmit and SessionStart, a spawn silent past the configured
threshold adds one note per silence period to that same response.
"""

import time
from dataclasses import replace
from pathlib import Path

from adapters.claude import transcript
from core import events, policy, state as state_mod, subagent_notes
from core.config import valid_text
from core.observation import BLOCK, COMPACT, CONTEXT, NOTE, PROMPT, SESSION, TOOL, Decision, Observation

RUNTIME = "claude"
BACKGROUND = "background_agents"
ASYNC_STATUS = "async_launched"
ACTIVITY_SECONDS = 60
SPAWN_TOOL = "Agent"
DELIVERING = (TOOL, PROMPT, SESSION, COMPACT)


def clock() -> float:
    """Read the current time.

    Returns:
        Epoch seconds.
    """
    return time.time()


def tracked(adapter: dict) -> dict[str, dict]:
    """Read the background spawns from the adapter section of a parent's state.

    Args:
        adapter: The adapter section of the parent's session state.

    Returns:
        Spawn records by agent id; empty when missing or not an object.
    """
    items = adapter.get(BACKGROUND)
    if not isinstance(items, dict):
        return {}
    return {agent: record for agent, record in items.items() if valid_text(agent) and isinstance(record, dict)}


def read_tracked(directory: Path, session: str) -> dict[str, dict]:
    """Read a parent's background spawns without locking its state.

    Args:
        directory: Claude Code state directory.
        session: The parent's session id.

    Returns:
        Spawn records by agent id.
    """
    path = directory / f"{state_mod.session_key(RUNTIME, session)}.json"
    return tracked(state_mod.validated(state_mod.read_json(path)).get("adapter", {}))


def update(directory: Path, session: str, agent: str, record: dict | None) -> None:
    """Replace or drop one background spawn in a parent's state.

    Args:
        directory: Claude Code state directory.
        session: The parent's session id.
        agent: The subagent's agent id.
        record: The new spawn record, or None to stop tracking it.
    """
    with state_mod.session_state(directory, RUNTIME, session) as state:
        adapter = state.setdefault("adapter", {})
        spawns = tracked(adapter)
        if record is None:
            spawns.pop(agent, None)
        else:
            spawns[agent] = record
        adapter[BACKGROUND] = spawns


def remember_spawn(directory: Path, payload: dict[str, object], role: str | None, now: float) -> None:
    """Start tracking a background spawn from the parent's Agent PostToolUse.

    A foreground result, or one without an agent id, is not tracked: its result is the tool result.

    Args:
        directory: Claude Code state directory.
        payload: The validated PostToolUse payload of the parent's Agent call.
        role: The spawned role, if named.
        now: Current time in epoch seconds.
    """
    response = payload.get("tool_response")
    response = response if isinstance(response, dict) else {}
    agent = response.get("agentId")
    if response.get("status") != ASYNC_STATUS or not valid_text(agent) or not agent:
        return
    path = transcript.subagent_path({"transcript_path": payload.get("transcript_path"), "agent_id": agent})
    fields = {"role": role, "spawned": int(now), "transcript": path}
    update(directory, payload["session_id"], agent, {key: value for key, value in fields.items() if value})


def touch(directory: Path, payload: dict[str, object], now: float) -> None:
    """Record a tracked subagent's latest activity from its own PostToolUse.

    Args:
        directory: Claude Code state directory.
        payload: The validated PostToolUse payload of a subagent.
        now: Current time in epoch seconds.
    """
    session, agent = payload.get("session_id"), payload.get("agent_id")
    record = read_tracked(directory, session).get(agent) if session and agent else None
    active = record.get("active") if record else None
    if record is None or (isinstance(active, int) and now - active < ACTIVITY_SECONDS):
        return
    update(directory, session, agent, {**record, "active": int(now)})


def finish(directory: Path, payload: dict[str, object]) -> None:
    """Stop tracking a subagent once its SubagentStop fires.

    Args:
        directory: Claude Code state directory.
        payload: The validated SubagentStop payload.
    """
    session, agent = payload.get("session_id"), payload.get("agent_id")
    if session and agent and agent in read_tracked(directory, session):
        update(directory, session, agent, None)


def track(event: str, payload: dict[str, object], role: str | None, directory: Path) -> None:
    """Update background spawn bookkeeping for one hook call, whatever the hook answers.

    Args:
        event: Native Claude Code hook event.
        payload: The validated hook payload.
        role: The spawned role of a parent's Agent call, if named.
        directory: Claude Code state directory.
    """
    if not payload.get("session_id"):
        return
    if event == "SubagentStop":
        finish(directory, payload)
    elif event == "PostToolUse" and payload.get("agent_id"):
        touch(directory, payload, clock())
    elif event == "PostToolUse" and payload.get("tool_name") == SPAWN_TOOL:
        remember_spawn(directory, payload, role, clock())


def silent(spawns: dict[str, dict], threshold: float, now: float) -> list[tuple[str, dict, int]]:
    """Find spawns silent past the threshold and not yet warned about in this silence period.

    A spawn with no activity yet is measured from its spawn time.

    Args:
        spawns: Spawn records by agent id.
        threshold: Seconds of silence that raise a note.
        now: Current time in epoch seconds.

    Returns:
        (agent id, record, silence mark) for each spawn to warn about.
    """
    found = []
    for agent, record in spawns.items():
        mark = record.get("active", record.get("spawned"))
        if isinstance(mark, int) and now - mark >= threshold and record.get("warned") != mark:
            found.append((agent, record, mark))
    return found


def silence_notes(config: dict, directory: Path, obs: Observation, now: float) -> list[str]:
    """Mark each newly silent spawn of a parent as warned, log it as `subagent_silent` and word its note.

    Args:
        config: Loaded Claude Code adapter configuration.
        directory: Claude Code state directory.
        obs: The parent's observation.
        now: Current time in epoch seconds.

    Returns:
        One note per newly silent spawn.
    """
    threshold = config["subagents"]["silence_minutes"] * 60
    found = silent(read_tracked(directory, obs.session_id), threshold, now)
    for agent, record, mark in found:
        update(directory, obs.session_id, agent, {**record, "warned": mark})
    notes, logged = [], []
    for agent, record, mark in found:
        minutes = int(now - mark) // 60
        notes.append(subagent_notes.silent_note(record.get("role"), agent, record.get("transcript"), minutes))
        detail = {"role": record.get("role"), "agent": agent, "minutes": minutes}
        logged.append(events.record("subagent_silent", obs, detail=detail))
    policy.log_events(config, directory, logged)
    return notes


def deliver(obs: Observation, decision: Decision, config: dict, directory: Path) -> Decision:
    """Append silent-subagent notes to a parent's tool result, prompt or session start.

    A blocked prompt is answered as is; its notes wait for the next delivering hook.

    Args:
        obs: The observation being answered.
        decision: The decision to render.
        config: Loaded Claude Code adapter configuration.
        directory: Claude Code state directory.

    Returns:
        The decision with the notes appended to its message, or unchanged when none are due.
    """
    if obs.is_subagent or obs.event not in DELIVERING or decision.kind == BLOCK:
        return decision
    notes = silence_notes(config, directory, obs, clock())
    if not notes:
        return decision
    message = "\n\n".join(part for part in (decision.message, *notes) if part)
    return replace(decision, kind=NOTE if obs.event in (TOOL, PROMPT) else CONTEXT, message=message)
