"""Track native Copilot child activity and notify its parent once per silence period."""

import math
import time
from pathlib import Path

from adapters.copilot.tools import text
from core import events, policy, state as state_mod, subagent_notes
from core.observation import PROMPT, TOOL, Observation

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


def deliver(obs: Observation, response: dict, config: dict, directory: Path) -> dict:
    """Append due silence notes to a model-facing parent prompt or tool response.

    Blocked prompts retain their pending notices. State and logs are updated only after
    the returned response contains the shared note text.

    Args:
        obs: Root or child observation.
        response: Native response already produced by policy and compaction recovery.
        config: Loaded configuration with subagents.silence_minutes.
        directory: Adapter state directory.

    Returns:
        Native response with each newly silent child's note appended, otherwise unchanged.
    """
    if obs.is_subagent or obs.event not in (PROMPT, TOOL) or "decision" in response:
        return response
    threshold = config["subagents"]["silence_minutes"] * 60
    now, logged, notes = clock(), [], []
    with state_mod.session_state(directory, RUNTIME, obs.session_id, commit=True) as state:
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
        if notes:
            response = {
                **response,
                "additionalContext": "\n\n".join(
                    value for value in (response.get("additionalContext", ""), *notes) if value
                ),
            }
        adapter[TRACKED] = children
    policy.log_events(config, directory, logged)
    return response
