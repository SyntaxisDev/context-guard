"""Confirmed compaction recovery using native generation identities."""

from dataclasses import replace
from pathlib import Path

from adapters.cursor import mapping
from core import config as config_mod, policy, render, state as state_mod
from core.observation import COMPACT, CONTEXT, NOTE, SESSION, SPAWN, TOOL, Decision, Observation

RECOVERY = "compaction_recovery"


def lifecycle(native: str, payload: dict, config: dict, home: Path) -> bool:
    """Record compaction initiation or matching termination without evaluating warning policy.

    Args:
        native: Native hook name.
        payload: Validated hook payload.
        config: Loaded configuration.
        home: Explicit install home.

    Returns:
        Whether this hook is a compaction lifecycle event and has been handled.
    """
    if native not in ("preCompact", "stop") or payload.get("parent_tool_call_id"):
        return native == "preCompact"
    session = payload.get("conversation_id") or payload.get("session_id")
    generation = payload.get("generation_id")
    if not session or not generation:
        return native == "preCompact"
    directory = config_mod.state_dir(config, home, "cursor")
    if native == "stop":
        path = directory / f"{state_mod.session_key('cursor', session)}.json"
        existing = state_mod.read_json(path).get("adapter", {}).get(RECOVERY, {})
        if existing.get("pending") != generation:
            return False
    with state_mod.session_state(directory, "cursor", session, commit=True) as state:
        adapter = state.setdefault("adapter", {})
        recovery = adapter.setdefault(RECOVERY, {})
        seen = recovery.setdefault("seen", [])
        if native == "preCompact":
            if generation not in seen:
                seen.append(generation)
                if not recovery.get("pending") and not recovery.get("ready"):
                    recovery["pending"] = generation
            return True
        if generation != recovery.get("pending"):
            return False
        recovery.pop("pending")
        if payload.get("status") == "completed" and payload.get("loop_count", 0) == 0:
            recovery["ready"] = generation
        return True


def deliver(obs: Observation, decision: Decision, state: dict, config: dict, home: Path) -> Decision:
    """Compose recovery facts and rearm warnings only when the native response carries context.

    Args:
        obs: Current observation.
        decision: Policy decision, including any deferred warning.
        state: Locked session state; consumption commits with the response.
        config: Loaded configuration.
        home: Explicit install home.

    Returns:
        The composed native-context decision, or the unchanged decision when delivery is unsupported.
    """
    if obs.is_subagent or obs.event not in (TOOL, SESSION) or (obs.tool and obs.tool.action == SPAWN):
        return decision
    adapter = state.setdefault("adapter", {})
    recovery = adapter.get(RECOVERY, {})
    if not recovery.get("ready"):
        return decision
    text = render.render_policy(mapping.runtime_mapping(config))
    ctx = policy.Context(config, config_mod.handoff_dir(config, home), home, text)
    context = policy.session_context(replace(obs, event=COMPACT), ctx)
    message = "\n\n".join(
        part
        for part in (
            (
                "[context-guard] Recovery context after confirmed compaction. "
                "These are hook-provided policy and handoff facts."
            ),
            context.message,
            decision.message,
        )
        if part
    )
    recovery.pop("ready")
    for key in ("tier", "blocked_prompt"):
        state.pop(key, None)
    for key in ("pending_note", "pending_tier"):
        adapter.pop(key, None)
    return replace(decision, kind=NOTE if obs.event == TOOL else CONTEXT, message=message)
