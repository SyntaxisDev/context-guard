"""Copilot lifecycle hooks: `python hooks.py <event>` reads the hook payload on stdin and prints the response.

Payloads are camelCase and carry no event name, so the event comes from the command line. Tokens come
from the session's `events.jsonl`. Every failure path prints `{}` (fail open).
"""

import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from adapters.copilot import events, mapping, runner, subagents, tools
from core import config as config_mod, hook, policy, state as state_mod
from core.observation import BLOCK, COMPACT, CONTEXT, NOTE, PASS, PROMPT, SESSION, STOP, TOOL, Decision, Observation
from core.runner import service

RUNTIME = mapping.RUNTIME
ADAPTER_CONFIG = Path(__file__).resolve().parent / "config.json"
EVENTS = {
    mapping.SESSION_START: SESSION,
    mapping.PROMPT_SUBMITTED: PROMPT,
    mapping.AGENT_STOP: STOP,
    mapping.POST_TOOL: TOOL,
}
TEXT_FIELDS = (
    "sessionId",
    "cwd",
    "prompt",
    "transcriptPath",
    "source",
    "agentId",
    "agentType",
    "parentAgentId",
    "toolName",
)
STOP_ACTIVE_KEYS = ("stop_hook_active", "stopHookActive")


def check_payload(payload: dict[str, object]) -> None:
    """Reject Copilot payloads whose fields the hook cannot interpret.

    Args:
        payload: Decoded hook payload object.

    Raises:
        ValueError: A known text field is not text or a stop guard is not a bool.
    """
    for key in STOP_ACTIVE_KEYS:
        if payload.get(key) is not None and type(payload[key]) is not bool:
            msg = f"{key} must be a bool"
            raise ValueError(msg)
    for field in TEXT_FIELDS:
        value = payload.get(field)
        if value is not None and not config_mod.valid_text(value):
            msg = f"{field} must be text"
            raise ValueError(msg)


def observe(event_name: str, payload: dict[str, object], config: dict, copilot_home: Path, home: Path) -> Observation:
    """Translate one Copilot hook payload into an observation.

    Invalid fields raise ValueError from `check_payload`.

    Args:
        event_name: Registered native event.
        payload: Decoded hook payload object.
        config: Loaded Copilot adapter configuration.
        copilot_home: The Copilot home directory that holds session state.
        home: User home for handoff path expansion.

    Returns:
        The root or isolated child observation; tokens, window and model are None when the event log
        gives no usable usage record.
    """
    check_payload(payload)
    agent = tools.text(payload.get("agentId"))
    session = agent or payload.get("sessionId") or ""
    parent = tools.text(payload.get("parentAgentId"))
    path = events.events_path(copilot_home, parent or session, payload.get("transcriptPath"))
    usage = events.latest_usage(path, config["events"]["tail_bytes"], agent if parent else None)
    event = EVENTS[event_name]
    return Observation(
        RUNTIME,
        session,
        event,
        tokens=usage.tokens if usage else None,
        window=usage.window if usage else None,
        model=usage.model if usage else None,
        is_subagent=bool(agent and payload.get("parentAgentId")),
        role=tools.role_name(payload.get("agentType")),
        tool=tools.tool_use(payload, config, home) if event == TOOL else None,
        cwd=payload.get("cwd") or "",
        prompt=payload.get("prompt") if event == PROMPT else None,
    )


def remember_agent(payload: dict, config: dict, home: Path) -> None:
    """Remember native child identity for later tool payloads that only carry sessionId.

    Args:
        payload: Recorded subagentStart input.
        config: Loaded adapter configuration.
        home: User home for adapter state.
    """
    agent = tools.text(payload.get("agentId"))
    parent = tools.text(payload.get("parentAgentId"))
    if not agent or not parent or agent == parent:
        return
    directory = config_mod.state_dir(config, home, RUNTIME)
    with state_mod.session_state(directory, RUNTIME, agent) as state:
        state.setdefault("adapter", {})["identity"] = {
            key: payload[key] for key in ("agentId", "parentAgentId", "agentType") if key in payload
        }


def with_identity(payload: dict, config: dict, home: Path) -> dict:
    """Recover a child tool call's native identity without modifying the supplied payload.

    Args:
        payload: Validated native hook input.
        config: Loaded configuration.
        home: Home holding adapter state.

    Returns:
        Payload with recorded identity when present; otherwise the original payload.
    """
    session = tools.text(payload.get("sessionId"))
    if payload.get("agentId") or not session:
        return payload
    directory = config_mod.state_dir(config, home, RUNTIME)
    with state_mod.session_state(directory, RUNTIME, session) as state:
        identity = state.get("adapter", {}).get("identity")
    return {**payload, **identity} if isinstance(identity, dict) else payload


def recovery(obs: Observation, path: Path | None, config: dict, home: Path) -> tuple[str, Observation] | None:
    """Find an applied native root compaction whose context still needs evaluation.

    Args:
        obs: Current observation.
        path: Root native event log.
        config: Loaded configuration.
        home: Home holding adapter state.

    Returns:
        The native compaction identity and observation when not consumed, otherwise None.
    """
    if obs.is_subagent or obs.event not in (PROMPT, TOOL, SESSION):
        return None
    identity = events.latest_compaction(path, config["events"]["tail_bytes"])
    if not identity:
        return None
    directory = config_mod.state_dir(config, home, RUNTIME)
    with state_mod.session_state(directory, RUNTIME, obs.session_id) as state:
        adapter = state.setdefault("adapter", {})
        if adapter.get("compaction") == identity:
            return None
    return identity, replace(obs, event=COMPACT, compacted=True, tokens=None, tool=None)


def session_policy(config: dict) -> str:
    """Render the policy that sessionStart puts into the session context.

    Args:
        config: Loaded Copilot adapter configuration.

    Returns:
        The policy, or an empty string when it cannot be rendered.
    """
    try:
        policy = mapping.policy_text(config)
        if os.environ.get("CONTEXT_GUARD_PLUGIN_ROOT"):
            for name in mapping.subagent_settings(config):
                policy = policy.replace(f"`{name}`", f"`{mapping.PLUGIN_NAME}:{name}`")
    except ValueError, OSError:
        return ""
    return policy


def respond(decision: Decision, event_name: str) -> dict[str, object]:
    """Render a decision as the Copilot hook response for a native event.

    Args:
        decision: The decision to render.
        event_name: Native event being answered.

    Returns:
        A prompt refusal (`decision: block`), an agentStop continuation (`decision: block` whose
        reason becomes the next user message), `additionalContext` for sessionStart and
        userPromptSubmitted, or `{}` when the event cannot carry the decision.
    """
    message = decision.message
    if decision.kind == PASS or not message:
        return {}
    if decision.kind == BLOCK and event_name == mapping.PROMPT_SUBMITTED:
        return {"decision": "block", "reason": message}
    if decision.kind == CONTEXT and event_name == mapping.AGENT_STOP:
        return {"decision": "block", "reason": message}
    if decision.kind != BLOCK and event_name in (mapping.SESSION_START, mapping.PROMPT_SUBMITTED, mapping.POST_TOOL):
        return {"additionalContext": message}
    return {}


def load_config(home: Path, environ: Mapping[str, str] | None = None) -> dict:
    """Load the Copilot configuration: defaults, adapter config, user config, environment.

    Args:
        home: Home directory holding the user configuration.
        environ: Environment overrides; None reads the process environment.

    Returns:
        The merged configuration.
    """
    return config_mod.load_config(config_mod.sources_for(RUNTIME, home, ADAPTER_CONFIG), environ)


def delivered(response: dict[str, object]) -> bool:
    """Tell whether a response hands a note to the model.

    Args:
        response: The native response.

    Returns:
        True for `additionalContext`, which the CLI adds to the model's context.
    """
    return "additionalContext" in response


def handle(
    event_name: str, payload: object, config: dict, home: Path, environ: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Observe, evaluate and answer one Copilot hook call.

    Invalid payload fields raise ValueError from `observe`; `run` turns that into a pass.

    Args:
        event_name: Native event from the command line.
        payload: Decoded stdin JSON of the hook call.
        config: Loaded Copilot adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment that may set COPILOT_HOME; None reads the process environment.

    Returns:
        The native response; `{}` for unknown events, non-object payloads and agentStop
        continuations already in progress.
    """
    if not isinstance(payload, dict):
        return {}
    check_payload(payload)
    if event_name == mapping.SUBAGENT_START:
        remember_agent(payload, config, home)
        subagents.track(event_name, payload, config_mod.state_dir(config, home, RUNTIME))
        return {}
    if event_name == subagents.STOP:
        subagents.track(event_name, payload, config_mod.state_dir(config, home, RUNTIME))
        return {}
    if event_name not in EVENTS:
        return {}
    if event_name == mapping.AGENT_STOP and any(payload.get(key) is True for key in STOP_ACTIVE_KEYS):
        return {}
    payload = with_identity(payload, config, home)
    subagents.track(event_name, payload, config_mod.state_dir(config, home, RUNTIME))
    copilot_home = mapping.copilot_home(home, os.environ if environ is None else environ)
    obs = observe(event_name, payload, config, copilot_home, home)
    path = events.events_path(copilot_home, payload.get("sessionId"), payload.get("transcriptPath"))
    found = recovery(obs, path, config, home)
    identity, compact = found or (None, None)
    return evaluate(obs, event_name, config, home, compact=compact, identity=identity)


def carry_recovery(obs: Observation, decision: Decision, state: dict, identity: str | None) -> Decision:
    """Include pending compaction context in the policy transaction.

    Args:
        obs: Current root or child observation.
        decision: Current policy decision, or the compaction decision for a new compaction.
        state: Locked session state.
        identity: Newly evaluated compaction identity, or None for existing pending context.

    Returns:
        Decision with pending context when delivery is possible.
    """
    if obs.is_subagent or obs.event not in (SESSION, PROMPT, TOOL):
        return decision
    adapter = state.setdefault("adapter", {})
    pending = adapter.pop("recovery_context", "")
    message = decision.message if identity else pending
    if identity:
        adapter["compaction"] = identity
    if not isinstance(message, str) or not message:
        return decision
    if decision.kind == BLOCK:
        adapter["recovery_context"] = message
        return decision
    if identity:
        return decision
    return replace(
        decision,
        kind=NOTE if decision.kind == PASS else decision.kind,
        message="\n\n".join(value for value in (message, decision.message) if value),
    )


def evaluate(
    obs: Observation,
    event_name: str,
    config: dict,
    home: Path,
    *,
    compact: Observation | None = None,
    identity: str | None = None,
) -> dict:
    """Evaluate one observation with the shared policy and native response renderer.

    Args:
        obs: Root or child observation.
        event_name: Native response event.
        config: Loaded configuration.
        home: User home for state.
        compact: Newly completed root compaction, when present.
        identity: Native compaction identity, when present.

    Returns:
        Native response for the decision.
    """
    routing = service.route(obs, config, lambda: runner.services(home)) if compact else service.Routing()
    if routing.blocked:
        return respond(service.combine(Decision(), routing), event_name)
    text = session_policy(config) if compact else ""

    def adjust(decision: Decision, state: dict) -> Decision:
        """Complete native recovery and silence state before the policy commit.

        Args:
            decision: First policy decision.
            state: Locked root or child session state.

        Returns:
            Final policy decision and its events.
        """
        recovery_text = decision.message if compact else ""
        if compact and obs.event != SESSION:
            ctx = policy.Context(config, config_mod.handoff_dir(config, home), home, text)
            current = policy.decide(obs, state, ctx)
            decision = replace(current, events=[*decision.events, *current.events])
            decision = service.combine(decision, routing)
        if compact:
            adapter = state.setdefault("adapter", {})
            adapter["compaction"] = identity
            if obs.event != SESSION:
                adapter.pop("recovery_context", None)
                if decision.kind == BLOCK:
                    adapter["recovery_context"] = recovery_text
                elif recovery_text:
                    decision = replace(
                        decision,
                        kind=NOTE if decision.kind == PASS else decision.kind,
                        message="\n\n".join(value for value in (recovery_text, decision.message) if value),
                    )
        else:
            decision = carry_recovery(obs, decision, state, None)
        return subagents.adjust(obs, decision, state, config)

    return hook.evaluate(
        compact or obs,
        config,
        home,
        lambda: runner.services(home),
        lambda decision: respond(decision, event_name),
        delivered,
        (lambda: text) if compact else (lambda: session_policy(config)),
        adjust=adjust,
    )


def run(event_name: str, payload: object, home: Path, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Answer one Copilot hook call, failing open.

    Args:
        event_name: Native event from the command line.
        payload: Decoded stdin JSON of the hook call.
        home: Home directory of the context-guard install.
        environ: Environment overrides; None reads the process environment.

    Returns:
        The native response, or `{}` for internal runs, foreign homes and unreadable
        configuration, state, telemetry or payloads.
    """
    return hook.guarded(
        KIT, lambda: handle(event_name, payload, load_config(home, environ), home, environ), {}, environ
    )


def main(argv: list[str] | None = None) -> None:
    """Run the hook process: argv names the event, stdin holds the payload, stdout gets one response.

    Args:
        argv: Command-line arguments after the script; None reads `sys.argv`.
    """
    hook.main(KIT, sys.argv[1:] if argv is None else argv, runner.services, run, lambda _event_name: {})


if __name__ == "__main__":
    main()
