"""Track child hook activity and deliver silence notes on the next root opportunity."""

import time
from pathlib import Path

from core import events, state as state_mod, subagent_notes
from core.observation import BLOCK, COMPACT, CONTEXT, PASS, PROMPT, SESSION, TOOL, Decision, Observation

RUNTIME = "genie"
TRACKED = "children"
STOP = "SubagentStop"


def clock() -> float:
    """Read the child activity clock.

    Returns:
        Epoch seconds.
    """
    return time.time()


def tracked(adapter: dict) -> dict[str, dict]:
    """Filter corrupt tracking data before using it.

    Args:
        adapter: Stored adapter state.

    Returns:
        Valid child identities and activity records.
    """
    children = adapter.get(TRACKED)
    if not isinstance(children, dict):
        return {}
    return {
        agent: record
        for agent, record in children.items()
        if isinstance(agent, str)
        and agent
        and isinstance(record, dict)
        and state_mod.valid_field(record.get("active"), float)
    }


def track(event: str, payload: dict, obs: Observation, directory: Path, transcript: str | None) -> None:
    """Record genuine child activity under its reported parent, retiring completed children.

    Args:
        event: Native hook event.
        payload: Validated hook payload.
        obs: Observation retaining the child's ancestry and role.
        directory: Runtime state directory.
        transcript: Resolved child rollout, if available.
    """
    parent = payload.get("session_id")
    agent = payload.get("agent_id")
    if not agent or not parent or agent == parent:
        return
    now = clock()
    if not state_mod.valid_field(now, float):
        return
    with state_mod.session_state(directory, RUNTIME, parent) as state:
        adapter = state.setdefault("adapter", {})
        children = tracked(adapter)
        if event == STOP:
            children.pop(agent, None)
        else:
            children[agent] = {"active": now, "role": obs.role, "transcript": transcript}
        adapter[TRACKED] = children


def refresh(record: dict, now: float) -> None:
    """Rearm silence when the child's rollout changes, without counting parent activity.

    Args:
        record: Stored child activity, updated in place.
        now: Current epoch seconds.
    """
    transcript = record.get("transcript")
    if not isinstance(transcript, str) or not transcript or "\0" in transcript:
        return
    try:
        modified = Path(transcript).stat().st_mtime
    except OSError:
        return
    if record["active"] < modified <= now:
        record["active"] = modified
        record.pop("warned", None)


def carry(obs: Observation, decision: Decision, state: dict, config: dict) -> Decision:
    """Compose silence notices and their markers in the pending policy transaction.

    Blocked prompts keep pending notices for the next root opportunity. The caller
    commits both policy and silence state once, after all activity reads finish.

    Args:
        obs: Root or child observation.
        decision: Uncommitted policy decision.
        state: Locked session state, updated in place.
        config: Loaded runtime configuration.

    Returns:
        Decision containing each newly due notice once per inactivity period.
    """
    if obs.is_subagent or obs.event not in (PROMPT, TOOL, SESSION, COMPACT) or decision.kind == BLOCK:
        return decision
    now, notes, logged = clock(), [], []
    if not state_mod.valid_field(now, float):
        return decision
    threshold = config["subagents"]["silence_minutes"] * 60
    adapter = state.setdefault("adapter", {})
    children = tracked(adapter)
    for agent, record in children.items():
        refresh(record, now)
        mark = record["active"]
        if now - mark < threshold or record.get("warned") == mark:
            continue
        minutes = int(now - mark) // 60
        notes.append(subagent_notes.silent_note(record.get("role"), agent, record.get("transcript"), minutes))
        detail = {"role": record.get("role"), "agent": agent, "minutes": minutes}
        logged.append(events.record("subagent_silent", obs, detail=detail))
        record["warned"] = mark
    adapter[TRACKED] = children
    if not notes:
        return decision
    message = "\n\n".join(value for value in (decision.message, *notes) if value)
    return Decision(
        CONTEXT if decision.kind == PASS else decision.kind, message, decision.tier, decision.events + logged
    )
