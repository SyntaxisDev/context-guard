#!/usr/bin/env python3
"""Claude Code hooks: `hooks.py <HookEvent>` reads the hook payload on stdin and prints the response.

Payloads carrying agent_id come from subagents: they run in leaf mode on the subagent's own
transcript and never touch the parent session. The context window is the learned
auto-compaction point when known, else the configured window for the model.
"""
import json
import os
import sys
import time
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[2]
if str(KIT_ROOT) not in sys.path:
    sys.path.insert(0, str(KIT_ROOT))

from adapters.claude import runner, transcript
from core import config as config_mod, deadline, handoffs, policy, render, state as state_mod
from core.runner import service
from core.runner.provider import INTERNAL_ENV
from core.observation import (BLOCK, COMPACT, PASS, PROMPT, READ, SESSION, SPAWN, STOP, TOOL, WRITE,
                              Decision, Observation, ToolUse)

RUNTIME = "claude"
ADAPTER_CONFIG = Path(__file__).resolve().parent / "config.json"
OBSERVED = {"SessionStart": SESSION, "UserPromptSubmit": PROMPT, "PostToolUse": TOOL, "Stop": STOP}
BOOKKEEPING = ("PreCompact", "PostCompact")
HOOK_EVENTS = (*OBSERVED, *BOOKKEEPING)
LEAF_EVENTS = ("UserPromptSubmit", "PostToolUse")
COMPACT_SOURCE = "compact"
SPAWN_TOOL = "Agent"
SHELL_TOOL = "Bash"
READ_TOOLS = {"Read": "file_path"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}
TEXT_FIELDS = ("session_id", "cwd", "transcript_path", "prompt", "model", "source", "trigger",
               "tool_name", "agent_id", "agent_type", "agent_transcript_path")
LEARNED_FILE = Path("learned", "windows.json")
PLUGIN_NAME = "context-guard"
PLUGIN_POLICY_FILE = "policy/context-guard.md"


def load_config(home: Path, environ=None) -> dict:
    return config_mod.load_config(config_mod.sources_for(RUNTIME, home, ADAPTER_CONFIG), environ)


def valid_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if any(payload.get(key) is not None and not config_mod.valid_text(payload[key]) for key in TEXT_FIELDS):
        return False
    return payload.get("stop_hook_active") is None or type(payload["stop_hook_active"]) is bool


def text(value: object) -> str | None:
    return value if isinstance(value, str) and value and "\0" not in value else None


def tier_of_model(model: str, tiers: dict) -> str | None:
    return next((tier for tier, profile in tiers.items()
                 if isinstance(profile, dict) and model in (profile.get("call"), profile.get("model"))), None)


def role_name(value: object) -> str | None:
    """Plugin agents are namespaced `context-guard:<role>`; the role is the part after the prefix."""
    name = text(value)
    return (name.removeprefix(f"{PLUGIN_NAME}:") or None) if name else None


def role_default_tier(role: str | None) -> str | None:
    if role is None:
        return None
    try:
        found = render.load_roles().get(role)
    except render.RenderError:
        return None
    return found.default_tier if found else None


def spawn_detail(data: dict, tiers: dict) -> dict:
    """The Agent tool takes no effort: effort is the agent definition's, i.e. its default tier's."""
    role, model = role_name(data.get("subagent_type")), text(data.get("model"))
    default = role_default_tier(role)
    tier = tier_of_model(model, tiers) if model else default
    profile, defined = tiers.get(tier) or {}, tiers.get(default) or {}
    return {"role": role, "tier": tier, "model": model or profile.get("model"), "effort": defined.get("effort")}


def tool_use(payload: dict, config: dict, home: Path) -> ToolUse | None:
    name = text(payload.get("tool_name"))
    if name is None:
        return None
    data = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if name == SPAWN_TOOL:
        return ToolUse(name, SPAWN, detail=spawn_detail(data, config["tiers"]))
    if name in READ_TOOLS:
        return ToolUse(name, READ, text(data.get(READ_TOOLS[name])))
    if name in WRITE_TOOLS:
        return ToolUse(name, WRITE, text(data.get(WRITE_TOOLS[name])))
    command = text(data.get("command")) if name == SHELL_TOOL else None
    access = command and handoffs.shell_access(command, payload.get("cwd") or "", home,
                                               config_mod.handoff_dir(config, home))
    return ToolUse(name, *access) if access else ToolUse(name)


def configured_window(models: tuple, patterns: list) -> int | None:
    """First [substring, window] pair matching any known model name."""
    for entry in patterns:
        if not (isinstance(entry, list) and len(entry) == 2 and text(entry[0])
                and type(entry[1]) is int and entry[1] > 0):
            continue
        if any(model and entry[0] in model for model in models):
            return entry[1]
    return None


def learned_window(directory: Path, model: str | None, minimum: int) -> int | None:
    value = state_mod.read_json(directory / LEARNED_FILE).get(model) if model else None
    return value if type(value) is int and value >= minimum else None


def remember_window(directory: Path, model: str, tokens: int) -> None:
    path = directory / LEARNED_FILE
    with state_mod.file_lock(path.with_name(path.name + ".lock")):
        data = state_mod.read_json(path)
        data[model] = tokens
        state_mod.write_json(path, data)


def learn(session: dict, directory: Path, model: str | None, tokens: int | None, config: dict) -> None:
    """An automatic compaction point becomes this session's and this model's window."""
    if tokens is None or tokens < config["transcript"]["min_learned_window"] \
            or session.get("learned_window") == tokens:
        return
    session["learned_window"] = tokens
    if model:
        remember_window(directory, model, tokens)


def window(session: dict, model: str | None, directory: Path, config: dict) -> int | None:
    minimum = config["transcript"]["min_learned_window"]
    if session.get("learned_window", 0) >= minimum:
        return session["learned_window"]
    start = session["adapter"].get("start_model")
    return learned_window(directory, model, minimum) or configured_window(
        (text(start), model), config["context_windows"])


def read_main(payload: dict, adapter: dict, config: dict) -> transcript.Reading:
    since_bytes = adapter.get("compacted_bytes")
    since_time = adapter.get("compacted_at")
    return transcript.read(payload.get("transcript_path"), config["transcript"]["tail_bytes"],
                           since_bytes if type(since_bytes) is int else 0,
                           since_time if type(since_time) in (int, float) else None)


def observed_event(event: str, payload: dict) -> str:
    if event == "SessionStart" and payload.get("source") == COMPACT_SOURCE:
        return COMPACT
    return OBSERVED[event]


def observe_main(event: str, payload: dict, config: dict, directory: Path, home: Path) -> Observation:
    session_id = payload["session_id"]
    with state_mod.session_state(directory, RUNTIME, session_id) as session:
        adapter = session.setdefault("adapter", {})
        if event == "SessionStart" and text(payload.get("model")):
            adapter["start_model"] = payload["model"]
        reading = read_main(payload, adapter, config)
        if reading.model:
            adapter["model"] = reading.model
        model = text(adapter.get("model"))
        learn(session, directory, model, reading.compaction, config)
        compacted = adapter.pop("reset", False) is True
        size = window(session, model, directory, config)
    return Observation(RUNTIME, session_id, observed_event(event, payload), tokens=reading.tokens,
                       window=size, model=model, compacted=compacted, cwd=payload.get("cwd") or "",
                       prompt=payload.get("prompt") if event == "UserPromptSubmit" else None,
                       tool=tool_use(payload, config, home) if event == "PostToolUse" else None)


def observe_leaf(event: str, payload: dict, config: dict, home: Path) -> Observation:
    reading = transcript.read(transcript.subagent_path(payload), config["transcript"]["tail_bytes"])
    return Observation(RUNTIME, payload["agent_id"], OBSERVED[event], tokens=reading.tokens,
                       model=reading.model, is_subagent=True, role=role_name(payload.get("agent_type")),
                       cwd=payload.get("cwd") or "",
                       prompt=payload.get("prompt") if event == "UserPromptSubmit" else None,
                       tool=tool_use(payload, config, home) if event == "PostToolUse" else None)


def on_precompact(payload: dict, config: dict, directory: Path) -> None:
    if payload.get("trigger") != transcript.AUTO_TRIGGER:
        return
    with state_mod.session_state(directory, RUNTIME, payload["session_id"]) as session:
        adapter = session.setdefault("adapter", {})
        reading = read_main(payload, adapter, config)
        learn(session, directory, reading.model or text(adapter.get("model")), reading.tokens, config)


def on_postcompact(payload: dict, directory: Path) -> None:
    """Usage recorded before the compaction is obsolete; the next observation resets fired tiers."""
    with state_mod.session_state(directory, RUNTIME, payload["session_id"]) as session:
        session.setdefault("adapter", {}).update(
            compacted_bytes=transcript.size(payload.get("transcript_path")), compacted_at=time.time(), reset=True)


def clip(message: str, limit: int) -> str:
    if len(message) <= limit:
        return message
    cut = message[:limit - 1]
    return (cut.rsplit("\n", 1)[0] if "\n" in cut else cut) + "…"


def respond(decision: Decision, event: str, config: dict) -> dict:
    if decision.kind == PASS or not decision.message:
        return {}
    if decision.kind == BLOCK or event == "Stop":
        return {"decision": "block", "reason": decision.message}
    limits = config["hook_output"]
    limit = limits["tool_note_chars"] if event == "PostToolUse" else limits["context_chars"]
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": clip(decision.message, limit)}}


def plugin_policy() -> str:
    """Only a packaged plugin ships the rendered policy; an installed kit uses the rules file."""
    try:
        return (KIT_ROOT / PLUGIN_POLICY_FILE).read_text()
    except FileNotFoundError:
        return ""


def leaf_response(event: str, payload: dict, config: dict, home: Path) -> dict:
    if event not in LEAF_EVENTS:
        return {}
    return respond(policy.evaluate(observe_leaf(event, payload, config, home), config, home), event, config)


def handle(event: str, payload: object, config: dict, home: Path) -> dict:
    if os.environ.get(INTERNAL_ENV) == "1" or config_mod.foreign_home(KIT_ROOT) \
            or event not in HOOK_EVENTS or not valid_payload(payload):
        return {}
    if payload.get("agent_id"):
        return leaf_response(event, payload, config, home)
    if not payload.get("session_id"):
        return {}
    directory = config_mod.state_dir(config, home, RUNTIME)
    if event == "PreCompact":
        on_precompact(payload, config, directory)
        return {}
    if event == "PostCompact":
        on_postcompact(payload, directory)
        return {}
    if event == "Stop" and payload.get("stop_hook_active"):
        return {}
    obs = observe_main(event, payload, config, directory, home)
    policy_text = ""
    if obs.event in (SESSION, COMPACT):
        state_mod.prune(directory, config["paths"]["prune_days"])
        policy_text = plugin_policy()
    routing = service.route(obs, config, lambda: runner.services(home))
    if routing.blocked:
        return respond(service.combine(Decision(), routing), event, config)
    decision = service.combine(policy.evaluate(obs, config, home, policy_text), routing)
    response = respond(decision, event, config)
    if "hookSpecificOutput" in response:
        policy.log_delivered(obs, decision, config, home)
    return response


def run(event: str, payload: object, home: Path) -> dict:
    """Unreadable state, configuration or telemetry fails open."""
    try:
        return handle(event, payload, load_config(home), home)
    except (OSError, ValueError):
        return {}


def read_payload(stream) -> object:
    try:
        return json.loads(stream.read())
    except (OSError, ValueError):
        return None


def main() -> None:
    home = config_mod.resolve_home(KIT_ROOT)
    if service.is_worker_call(sys.argv[1:]):
        raise SystemExit(runner.services(home).work(sys.argv[1:]))
    event, fallback = (sys.argv[1] if len(sys.argv) == 2 else ""), {}
    response = deadline.boundary(
        lambda: deadline.run_within(lambda: run(event, read_payload(sys.stdin.buffer), home), fallback),
        fallback, event)
    print(json.dumps(response), flush=True)
    service.confirm_staged(response, fallback)


if __name__ == "__main__":
    main()
