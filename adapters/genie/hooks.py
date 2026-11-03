"""Genie lifecycle hooks: `python hooks.py <event>` reads the hook payload on stdin and prints the response.

Subagent hooks carry the parent's session_id and transcript_path, so leaf state is keyed by agent_id
and tokens come from the subagent's own rollout. SubagentStop validates and passes because Genie
has no public command that can wake an idle parent. Every failure path prints `{}` (fail open).
"""

import datetime as dt
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from adapters.genie import mapping, rollout, runner, tools
from core import config as config_mod, hook, install, render
from core.observation import BLOCK, COMPACT, CONTEXT, PASS, PROMPT, SESSION, STOP, TOOL, Decision, Observation
from core.package import LAUNCHER

RUNTIME = mapping.RUNTIME
ADAPTER_CONFIG = Path(__file__).resolve().parent / "config.json"
SESSION_START = mapping.SESSION_START
PROMPT_SUBMIT = "UserPromptSubmit"
POST_TOOL = "PostToolUse"
STOP_EVENT = "Stop"
EVENTS = {SESSION_START: SESSION, PROMPT_SUBMIT: PROMPT, POST_TOOL: TOOL, STOP_EVENT: STOP}
CONTEXT_EVENTS = (SESSION_START, PROMPT_SUBMIT, POST_TOOL)
COMPACT_SOURCE = "compact"
TEXT_FIELDS = (
    "hook_event_name",
    "session_id",
    "cwd",
    "transcript_path",
    "prompt",
    "model",
    "source",
    "agent_id",
    "agent_type",
)
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
PLUGIN_NAME = "context-guard"
PLUGIN_POLICY_FILE = "policy/context-guard.md"
PLUGIN_ROLELESS_POLICY_FILE = "policy/context-guard-roleless.md"
PLUGIN_ROOT_TOKEN = "${PLUGIN_ROOT}"  # noqa: S105
AGENTS_OFFER = (
    "context-guard: in your reply, offer the user once to install the named subagents with the "
    "command under Named subagents above. This note is not shown again."
)
ACCESS_COMMAND = f'sh "{PLUGIN_ROOT_TOKEN}/{LAUNCHER}" adapters/genie/access.py install'
ACCESS_OFFER = (
    "context-guard: handoffs are written outside the workspace, so the Genie sandbox asks before each "
    "handoff write. In your reply, offer the user once to run `{command}`, which adds only the handoff "
    "directory to the sandbox's writable roots in config.toml so handoff writes skip that approval; a "
    "new session loads it. This note is not shown again."
)


def check_payload(event_name: str, payload: dict[str, object]) -> None:
    """Reject Genie payloads whose fields the hook cannot interpret.

    Args:
        event_name: Native event the hook was registered for.
        payload: Decoded hook payload object.

    Raises:
        ValueError: A known field is not text, or the payload names a different event.
    """
    for field in TEXT_FIELDS:
        value = payload.get(field)
        if value is not None and not config_mod.valid_text(value):
            msg = f"{field} must be text"
            raise ValueError(msg)
    if payload.get("hook_event_name") not in (None, event_name):
        msg = "payload event does not match the registered event"
        raise ValueError(msg)


def rollout_dirs(parent: Path) -> list[Path]:
    """List the rollout directories a subagent of this parent may write to.

    Args:
        parent: The parent session's rollout file.

    Returns:
        The parent's sessions/<yyyy>/<mm>/<dd> directory and the days after and before it, or
        only the parent's directory when it is not a dated one.
    """
    directory = parent.parent
    try:
        day = dt.date(*(int(part) for part in directory.parts[-3:]))
    except TypeError, ValueError:
        return [directory]
    root = directory.parent.parent.parent
    return [
        root / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}"
        for when in (day + dt.timedelta(days=offset) for offset in (0, 1, -1))
    ]


def subagent_transcript(payload: dict[str, object], agent_id: str) -> str | None:
    """Find a subagent's own rollout.

    Subagent hooks only name the parent's rollout, and a subagent started after midnight writes
    under the next day's directory.

    Args:
        payload: Validated subagent hook payload.
        agent_id: Validated subagent id that its rollout file name ends with.

    Returns:
        The rollout path when exactly one matches, else None.
    """
    parent = payload.get("transcript_path")
    if not parent:
        return None
    try:
        matches = sorted(
            match for directory in rollout_dirs(Path(parent)) for match in directory.glob(f"rollout-*{agent_id}.jsonl")
        )
    except OSError:
        return None
    return str(matches[0]) if len(matches) == 1 else None


def observe(event_name: str, payload: dict[str, object], config: dict, home: Path) -> Observation:
    """Translate one Genie hook payload into an observation.

    Invalid fields raise ValueError from `check_payload`.

    Args:
        event_name: Registered native event.
        payload: Decoded hook payload object.
        config: Loaded Genie adapter configuration.
        home: Home directory that the handoff store resolves against.

    Returns:
        The observation, keyed by agent id for subagents and by session id otherwise; tokens
        and window are None when the rollout gives no usable count.

    Raises:
        ValueError: A subagent payload carries an invalid agent_id.
    """
    check_payload(event_name, payload)
    agent = payload.get("agent_id") or None
    subagent = agent is not None
    if subagent and not AGENT_ID.fullmatch(agent):
        msg = "subagent hooks need a valid agent_id"
        raise ValueError(msg)
    transcript = subagent_transcript(payload, agent) if subagent else payload.get("transcript_path")
    usage = rollout.latest_usage(transcript, config["rollout"]["tail_bytes"])
    compacted = event_name == SESSION_START and payload.get("source") == COMPACT_SOURCE
    event = COMPACT if compacted else EVENTS[event_name]
    cwd = payload.get("cwd") or ""
    tool = (
        tools.classify(payload, cwd, home, config_mod.handoff_dir(config, home), config["tiers"])
        if event == TOOL
        else None
    )
    return Observation(
        RUNTIME,
        agent if subagent else payload.get("session_id") or "",
        event,
        tokens=usage.tokens if usage else None,
        window=usage.window if usage else None,
        model=payload.get("model") or None,
        is_subagent=subagent,
        role=(payload.get("agent_type") or None) if subagent else None,
        compacted=compacted,
        cwd=cwd,
        prompt=payload.get("prompt") if event == PROMPT else None,
        tool=tool,
    )


def plugin_policy(config: dict, home: Path, environ: Mapping[str, str]) -> str:
    """Read the packaged policy that matches the role agents in the user's Genie home.

    Plugins cannot ship agents, so the role-less policy, which spawns Genie's default agent
    with the packaged role brief, is used unless every role's agent TOML is installed.

    Args:
        config: Loaded Genie adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment that may set GENIE_HOME.

    Returns:
        The named-agent policy, or the role-less one with the plugin root filled in and, once ever,
        the agents offer from `agents_offer`; either may end with the offer from `access_offer`.
    """
    agents = mapping.agents_dir(home, environ)
    names = [agent.name for agent in render.agents(mapping.runtime_mapping(config))]
    if all((agents / f"{name}.toml").is_file() for name in names):
        policy, offers = (KIT / PLUGIN_POLICY_FILE).read_text(), []
    else:
        policy = (KIT / PLUGIN_ROLELESS_POLICY_FILE).read_text().replace(PLUGIN_ROOT_TOKEN, str(KIT))
        offers = [agents_offer(home, environ)]
    notes = "\n\n".join(offer for offer in [*offers, access_offer(config, home, environ)] if offer)
    return f"{policy.rstrip()}\n\n{notes}\n" if notes else policy


def agents_offer(home: Path, environ: Mapping[str, str] | None = None) -> str:
    """Offer the named role agents once ever, recording the offer before it is made.

    No offer is made when an install manifest shows the agents were installed (by agents.py or the
    script install), when the offer marker exists, or when the marker cannot be written.

    Args:
        home: Home directory of the context-guard install.
        environ: Environment that may set GENIE_HOME; None reads the process environment.

    Returns:
        The one-time offer note, or an empty string.
    """
    identity = mapping.install_identity(home, mapping.PLUGIN_AGENTS, environ)
    installed = (
        install.manifest_path(home, mapping.install_identity(home, name, environ)).is_file()
        for name in (RUNTIME, mapping.PLUGIN_AGENTS)
    )
    if any(installed) or install.offer_marker(home, identity).exists():
        return ""
    try:
        install.record_offer(home, identity)
    except OSError:
        return ""
    return AGENTS_OFFER


def lacks_access(config: dict, home: Path, environ: Mapping[str, str]) -> bool:
    """Tell whether Genie's config.toml lacks the handoff directory in its sandbox writable roots.

    Args:
        config: Loaded Genie adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment that may set GENIE_HOME.

    Returns:
        True when config.toml is missing or parses without the directory; False when it holds
        it or cannot be read or parsed.
    """
    try:
        text = (mapping.genie_home(home, environ) / "config.toml").read_text()
    except FileNotFoundError:
        return True
    except OSError, UnicodeDecodeError:
        return False
    try:
        return not mapping.has_root(tomllib.loads(text), config_mod.handoff_dir(config, home))
    except tomllib.TOMLDecodeError:
        return False


def access_offer(config: dict, home: Path, environ: Mapping[str, str]) -> str:
    """Offer the handoff write access once ever, recording the offer before it is made.

    No offer is made when `handoff_access.enabled` is false, an install manifest shows the access
    was installed (by access.py or the script install), the offer marker exists, config.toml
    already grants it or cannot be read, or the marker cannot be written.

    Args:
        config: Loaded Genie adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment that may set GENIE_HOME.

    Returns:
        The one-time offer note naming the absolute command, or an empty string.
    """
    if not config["handoff_access"]["enabled"]:
        return ""
    identity = mapping.install_identity(home, mapping.PLUGIN_ACCESS, environ)
    installed = (
        install.manifest_path(home, mapping.install_identity(home, name, environ)).is_file()
        for name in (RUNTIME, mapping.PLUGIN_ACCESS)
    )
    if any(installed) or install.offer_marker(home, identity).exists():
        return ""
    if not lacks_access(config, home, environ):
        return ""
    try:
        install.record_offer(home, identity)
    except OSError:
        return ""
    return ACCESS_OFFER.format(command=ACCESS_COMMAND.replace(PLUGIN_ROOT_TOKEN, str(KIT)))


def session_policy(config: dict, home: Path, environ: Mapping[str, str] | None = None) -> str:
    """Render the policy that SessionStart puts into the session context.

    A packaged plugin ships its policies as files; an installed kit renders the policy.

    Args:
        config: Loaded Genie adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment that may set GENIE_HOME; None reads the process environment.

    Returns:
        The policy, or an empty string when it cannot be rendered or read.
    """
    try:
        if (KIT / PLUGIN_POLICY_FILE).is_file():
            return plugin_policy(config, home, os.environ if environ is None else environ)
        return mapping.policy_text(config)
    except ValueError, OSError:
        return ""


def respond(decision: Decision, event_name: str) -> dict[str, object]:
    """Render a decision as the Genie hook response for a native event.

    Args:
        decision: The decision to render.
        event_name: Native event being answered.

    Returns:
        A prompt block, a Stop continuation (`decision: block`), additional context for
        context-carrying events, or `{}` when the event cannot carry the decision.
    """
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


def load_config(home: Path, environ: Mapping[str, str] | None = None) -> dict:
    """Load the Genie configuration: defaults, adapter config, user config, environment.

    Args:
        home: Home directory holding the user configuration.
        environ: Environment overrides; None reads the process environment.

    Returns:
        The merged configuration.
    """
    return config_mod.load_config(config_mod.sources_for(RUNTIME, home, ADAPTER_CONFIG), environ)


def subagent_stop(payload: dict[str, object]) -> dict[str, object]:
    """Validate a finished subagent payload and pass.

    Invalid payload fields raise ValueError from `check_payload`; `run` turns that into a pass.

    Args:
        payload: Decoded SubagentStop payload object.

    Returns:
        `{}`: the stop itself is never held.
    """
    check_payload(mapping.SUBAGENT_STOP, payload)
    return {}


def handle(
    event_name: str, payload: object, config: dict, home: Path, environ: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Observe, evaluate and answer one Genie hook call.

    Invalid payload fields raise ValueError from `observe`; `run` turns that into a pass.

    Args:
        event_name: Native event from the command line.
        payload: Decoded stdin JSON of the hook call.
        config: Loaded Genie adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment that may set GENIE_HOME; None reads the process environment.

    Returns:
        The native response; `{}` for SubagentStop, unknown events, non-object payloads and Stop
        continuations already in progress.
    """
    if event_name == mapping.SUBAGENT_STOP and isinstance(payload, dict):
        return subagent_stop(payload)
    if event_name not in EVENTS or not isinstance(payload, dict):
        return {}
    if event_name == STOP_EVENT and payload.get("stop_hook_active") is True:
        return {}
    return hook.evaluate(
        observe(event_name, payload, config, home),
        config,
        home,
        lambda: runner.services(home),
        lambda decision: respond(decision, event_name),
        lambda response: "hookSpecificOutput" in response,
        lambda: session_policy(config, home, environ),
    )


def run(event_name: str, payload: object, home: Path, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Answer one Genie hook call, failing open.

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
