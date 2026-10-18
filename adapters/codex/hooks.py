"""Codex lifecycle hooks: `python hooks.py <event>` reads the hook payload on stdin and prints the response.

Subagent hooks carry the parent's session_id and transcript_path, so leaf state is keyed by agent_id
and tokens come from the subagent's own rollout. Every failure path prints `{}` (fail open).
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from adapters.codex import mapping, rollout, runner, tools
from core import config as config_mod, deadline, policy, render, state
from core.runner import service
from core.runner.provider import INTERNAL_ENV
from core.observation import BLOCK, COMPACT, CONTEXT, PASS, PROMPT, SESSION, STOP, TOOL, Decision, Observation

RUNTIME = mapping.RUNTIME
ADAPTER_CONFIG = Path(__file__).resolve().parent / "config.json"
SESSION_START = mapping.SESSION_START
PROMPT_SUBMIT = "UserPromptSubmit"
POST_TOOL = "PostToolUse"
STOP_EVENT = "Stop"
SUBAGENT_STOP = "SubagentStop"
EVENTS = {SESSION_START: SESSION, PROMPT_SUBMIT: PROMPT, POST_TOOL: TOOL, STOP_EVENT: STOP, SUBAGENT_STOP: STOP}
CONTEXT_EVENTS = (SESSION_START, PROMPT_SUBMIT, POST_TOOL)
COMPACT_SOURCE = "compact"
TEXT_FIELDS = ("hook_event_name", "session_id", "cwd", "transcript_path", "prompt", "model", "source",
               "agent_id", "agent_type", "agent_transcript_path")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def check_payload(event_name: str, payload: dict) -> None:
    for field in TEXT_FIELDS:
        value = payload.get(field)
        if value is not None and not config_mod.valid_text(value):
            raise ValueError(f"{field} must be text")
    if payload.get("hook_event_name") not in (None, event_name):
        raise ValueError("payload event does not match the registered event")


def rollout_dirs(parent: Path) -> list[Path]:
    """The parent rollout's sessions/<yyyy>/<mm>/<dd> directory and the days beside it."""
    directory = parent.parent
    try:
        day = dt.date(*(int(part) for part in directory.parts[-3:]))
    except (TypeError, ValueError):
        return [directory]
    root = directory.parent.parent.parent
    return [root / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}"
            for when in (day + dt.timedelta(days=offset) for offset in (0, 1, -1))]


def subagent_transcript(payload: dict, agent_id: str) -> str | None:
    """SubagentStop names the subagent rollout; other subagent hooks only name the parent's, and a
    subagent started after midnight writes under the next day's directory."""
    if payload.get("agent_transcript_path"):
        return payload["agent_transcript_path"]
    parent = payload.get("transcript_path")
    if not parent:
        return None
    try:
        matches = sorted(match for directory in rollout_dirs(Path(parent))
                         for match in directory.glob(f"rollout-*{agent_id}.jsonl"))
    except OSError:
        return None
    return str(matches[0]) if len(matches) == 1 else None


def observe(event_name: str, payload: dict, config: dict, home: Path) -> Observation:
    check_payload(event_name, payload)
    agent = payload.get("agent_id") or None
    subagent = agent is not None or event_name == SUBAGENT_STOP
    if subagent and (agent is None or not AGENT_ID.fullmatch(agent)):
        raise ValueError("subagent hooks need a valid agent_id")
    transcript = subagent_transcript(payload, agent) if subagent else payload.get("transcript_path")
    usage = rollout.latest_usage(transcript, config["rollout_tail_bytes"])
    compacted = event_name == SESSION_START and payload.get("source") == COMPACT_SOURCE
    event = COMPACT if compacted else EVENTS[event_name]
    cwd = payload.get("cwd") or ""
    tool = (tools.classify(payload, cwd, home, config_mod.handoff_dir(config, home), config["tiers"])
            if event == TOOL else None)
    return Observation(
        RUNTIME, agent if subagent else payload.get("session_id") or "", event,
        tokens=usage.tokens if usage else None, window=usage.window if usage else None,
        model=payload.get("model") or None, is_subagent=subagent,
        role=(payload.get("agent_type") or None) if subagent else None, compacted=compacted, cwd=cwd,
        prompt=payload.get("prompt") if event == PROMPT else None, tool=tool)


def session_policy(config: dict) -> str:
    try:
        return mapping.policy_text(config)
    except render.RenderError:
        return ""


def respond(decision: Decision, event_name: str) -> dict:
    message = decision.message
    if decision.kind == PASS or not message:
        return {}
    if decision.kind == BLOCK and event_name == PROMPT_SUBMIT:
        return {"decision": "block", "reason": message, "systemMessage": message}
    if decision.kind == CONTEXT and event_name == STOP_EVENT:
        return {"decision": "block", "reason": message}
    if decision.kind != BLOCK and event_name in CONTEXT_EVENTS:
        return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": message}}
    return {}


def handle(event_name: str, payload: object, home: Path) -> dict:
    if os.environ.get(INTERNAL_ENV) == "1" or config_mod.foreign_home(KIT) \
            or event_name not in EVENTS or not isinstance(payload, dict):
        return {}
    if event_name == STOP_EVENT and payload.get("stop_hook_active") is True:
        return {}
    config = config_mod.load_config(config_mod.sources_for(RUNTIME, home, ADAPTER_CONFIG))
    try:
        obs = observe(event_name, payload, config, home)
    except ValueError:
        return {}
    text = ""
    if obs.event in (SESSION, COMPACT):
        state.prune(config_mod.state_dir(config, home, RUNTIME), config["paths"]["prune_days"])
        text = session_policy(config)
    routing = service.route(obs, config, lambda: runner.services(home))
    if routing.blocked:
        return respond(service.combine(Decision(), routing), event_name)
    decision = service.combine(policy.evaluate(obs, config, home, text), routing)
    response = respond(decision, event_name)
    if "hookSpecificOutput" in response:
        policy.log_delivered(obs, decision, config, home)
    return response


def read_payload(stream) -> object:
    try:
        return json.load(stream)
    except (OSError, ValueError):
        return None


def run(event_name: str, stream, home: Path) -> dict:
    """Unreadable configuration, state or telemetry fails open."""
    payload = read_payload(stream)
    try:
        return handle(event_name, payload, home)
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    home = config_mod.resolve_home(KIT)
    if service.is_worker_call(args):
        raise SystemExit(runner.services(home).work(args))
    event_name, fallback = (args[0] if args else ""), {}
    response = deadline.boundary(
        lambda: deadline.run_within(lambda: run(event_name, sys.stdin, home), fallback), fallback, event_name)
    print(json.dumps(response), flush=True)
    service.confirm_staged(response, fallback)


if __name__ == "__main__":
    main()
