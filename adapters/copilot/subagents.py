"""Track native Copilot child activity and notify its parent once per silence period."""

import math
import time
from dataclasses import replace
from pathlib import Path

from adapters.copilot.tools import text
from core import events, state as state_mod, subagent_notes
from core.observation import BLOCK, NOTE, PASS, PROMPT, TOOL, Decision, Observation

RUNTIME = "copilot"
TRACKED = "children"
START = "subagentStart"
STOP = "subagentStop"
ACTIVITY = ("userPromptSubmitted", "postToolUse")


def clock() -> float:
    """Read the native activity clock.

    Returns:
        Epoch seconds.
    """
    return time.time()


def tracked(adapter: dict) -> dict[str, dict]:
    """Read valid child records from adapter state, ignoring corrupted entries.

    Args:
        adapter: Parent adapter state.

    Returns:
        Children with genuine stored identities and valid activity times.
    """
    children = adapter.get(TRACKED)
    if not isinstance(children, dict):
        return {}
    return {
        agent: record
        for agent, record in children.items()
        if text(agent)
        and isinstance(record, dict)
        and type(record.get("active")) in (int, float)
        and math.isfinite(record["active"])
        and record["active"] >= 0
    }


def track(event: str, payload: dict, directory: Path) -> None:
    """Track observed lifecycle and child activity without counting parent tool activity.

    Args:
        event: Native hook event from argv.
        payload: Validated native input, with lifecycle identity recovered when needed.
        directory: Adapter state directory.
    """
    agent, parent = text(payload.get("agentId")), text(payload.get("parentAgentId"))
    if not agent or not parent or agent == parent or event not in (START, STOP, *ACTIVITY):
        return
    now = clock()
    if not math.isfinite(now) or now < 0:
        return
    with state_mod.session_state(directory, RUNTIME, parent) as state:
        adapter = state.setdefault("adapter", {})
        children = tracked(adapter)
        if event == STOP:
            children.pop(agent, None)
        elif event == START:
            children.setdefault(
                agent,
                {
                    "active": now,
                    "role": text(payload.get("agentType")),
                    "transcript": text(payload.get("transcriptPath")),
                },
            )
        elif agent in children:
            children[agent].update(active=now)
            children[agent].pop("warned", None)
        adapter[TRACKED] = children


def adjust(obs: Observation, decision: Decision, state: dict, config: dict) -> Decision:
    """Append due silence notices within the policy state transaction.

    Args:
        obs: Root or child observation.
        decision: Policy decision including pending compaction context.
        state: Locked parent session state.
        config: Loaded configuration with subagents.silence_minutes.

    Returns:
        Decision with each newly silent child's note appended, otherwise unchanged.
    """
    if obs.is_subagent or obs.event not in (PROMPT, TOOL) or decision.kind == BLOCK:
        return decision
    threshold = config["subagents"]["silence_minutes"] * 60
    now, logged, notes = clock(), [], []
    adapter = state.setdefault("adapter", {})
    children = tracked(adapter)
    for agent, record in children.items():
        mark = record["active"]
        if now - mark < threshold or record.get("warned") == mark:
            continue
        minutes = int(now - mark) // 60
        notes.append(
            subagent_notes.silent_note(text(record.get("role")), agent, text(record.get("transcript")), minutes)
        )
        detail = {"role": text(record.get("role")), "agent": agent, "minutes": minutes}
        logged.append(events.record("subagent_silent", obs, detail=detail))
        record["warned"] = mark
    adapter[TRACKED] = children
    if not notes:
        return decision
    return replace(
        decision,
        kind=NOTE if decision.kind == PASS else decision.kind,
        message="\n\n".join(value for value in (decision.message, *notes) if value),
        events=[*decision.events, *logged],
    )
