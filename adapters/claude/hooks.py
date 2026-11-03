"""Claude Code hooks: `hooks.py <HookEvent>` reads the hook payload on stdin and prints the response.

Payloads carrying agent_id come from subagents: they run in leaf mode on the subagent's own
transcript; of the parent session they only touch its background spawn bookkeeping
(`subagents.py`). The context window is the learned
auto-compaction point when known, else the configured window for the model.
"""

import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[2]
if str(KIT_ROOT) not in sys.path:
    sys.path.insert(0, str(KIT_ROOT))

from adapters.claude import mapping, runner, subagents, transcript
from core import config as config_mod, handoffs, hook, install, spawn, state as state_mod
from core.observation import (
    BLOCK,
    COMPACT,
    PASS,
    PROMPT,
    READ,
    SESSION,
    SPAWN,
    STOP,
    TOOL,
    WRITE,
    Decision,
    Observation,
    ToolUse,
)
from core.package import LAUNCHER

RUNTIME = "claude"
ADAPTER_CONFIG = Path(__file__).resolve().parent / "config.json"
OBSERVED = {"SessionStart": SESSION, "UserPromptSubmit": PROMPT, "PostToolUse": TOOL, "Stop": STOP}
SUBAGENT_STOP = "SubagentStop"
BOOKKEEPING = ("PreCompact", "PostCompact", SUBAGENT_STOP)
HOOK_EVENTS = (*OBSERVED, *BOOKKEEPING)
LEAF_EVENTS = ("UserPromptSubmit", "PostToolUse")
COMPACT_SOURCE = "compact"
SPAWN_TOOL = "Agent"
SHELL_TOOL = "Bash"
READ_TOOLS = {"Read": "file_path"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
TEXT_FIELDS = (
    "session_id",
    "cwd",
    "transcript_path",
    "prompt",
    "model",
    "source",
    "trigger",
    "tool_name",
    "agent_id",
    "agent_type",
    "agent_transcript_path",
)
FOREIGN_HOST_KEYS = ("cursor_version",)
WINDOW_ENTRY_SIZE = 2
LEARNED_FILE = Path("learned", "windows.json")
PLUGIN_NAME = "context-guard"
PLUGIN_POLICY_FILE = "policy/context-guard.md"
ACCESS_SCRIPT = "adapters/claude/access.py install"
ACCESS_OFFER = (
    "context-guard: handoffs are written outside the project, so Claude Code asks before each handoff "
    "write. In your reply, offer the user once to run `{command}`, which allows edits in the handoff "
    "directory only (an `Edit` allow rule in {settings}) so handoff writes skip that prompt; "
    "a new session loads it. This note is not shown again."
)


def load_config(home: Path, environ: Mapping[str, str] | None = None) -> dict:
    """Load the Claude Code configuration: defaults, adapter config, user config, environment.

    Args:
        home: Home directory holding the user configuration.
        environ: Environment overrides; None reads the process environment.

    Returns:
        The merged configuration.
    """
    return config_mod.load_config(config_mod.sources_for(RUNTIME, home, ADAPTER_CONFIG), environ)


def valid_payload(payload: object) -> bool:
    """Tell whether a Claude Code hook payload has fields the hook can interpret.

    Cursor imports Claude Code plugins and runs their hooks with its own payloads; those
    belong to the Cursor adapter, so a payload naming another host is not interpreted.

    Args:
        payload: Decoded stdin JSON of the hook call.

    Returns:
        True for an object without another host's marker whose known text fields are text
        and whose `stop_hook_active` is absent or a bool.
    """
    if not isinstance(payload, dict) or any(key in payload for key in FOREIGN_HOST_KEYS):
        return False
    if any(payload.get(key) is not None and not config_mod.valid_text(payload[key]) for key in TEXT_FIELDS):
        return False
    return payload.get("stop_hook_active") is None or type(payload["stop_hook_active"]) is bool


def text(value: object) -> str | None:
    """Validate an untrusted JSON text field.

    Args:
        value: Untrusted JSON value.

    Returns:
        The value when it is a non-empty string without NUL, else None.
    """
    return value if isinstance(value, str) and value and "\0" not in value else None


def tier_of_model(model: str, tiers: dict) -> str | None:
    """Find the tier an Agent `model` argument selects.

    Args:
        model: The spawn's model, an alias (`call`) or a full model id.
        tiers: Tier profiles from the configuration.

    Returns:
        The first tier whose `call` or `model` equals it, else None.
    """
    return next(
        (
            tier
            for tier, profile in tiers.items()
            if isinstance(profile, dict) and model in (profile.get("call"), profile.get("model"))
        ),
        None,
    )


def role_name(value: object) -> str | None:
    """Read a role from an agent type; plugin agents are namespaced `context-guard:<role>`.

    Args:
        value: Untrusted `subagent_type` or `agent_type` value.

    Returns:
        The agent type without the plugin prefix, or None when it is not text.
    """
    name = text(value)
    return (name.removeprefix(f"{PLUGIN_NAME}:") or None) if name else None


def profile_model(profile: dict) -> tuple[str | None, str | None]:
    """Read a tier profile's model and effort.

    Args:
        profile: One tier profile from the configuration.

    Returns:
        Model and effort, each None when not text.
    """
    return text(profile.get("model")), text(profile.get("effort"))


def spawn_detail(data: dict[str, object], tiers: dict) -> dict[str, str | None]:
    """Describe an Agent spawn by role, tier, model and effort.

    The Agent tool takes no effort: effort is the agent definition's, i.e. its default tier's.

    Args:
        data: The Agent tool input from the payload.
        tiers: Tier profiles from the configuration.

    Returns:
        The spawn detail logged with the event; the tier is None when the model matches no tier.
    """
    role, model = role_name(data.get("subagent_type")), text(data.get("model"))
    default = spawn.default_detail(role, tiers, profile_model)
    if model is None:
        return default
    return {"role": role, "tier": tier_of_model(model, tiers), "model": model, "effort": default["effort"]}


def tool_use(payload: dict[str, object], config: dict, home: Path) -> ToolUse | None:
    """Classify the tool of a PostToolUse payload.

    Args:
        payload: Validated hook payload.
        config: Loaded Claude Code adapter configuration.
        home: Home directory that the handoff store resolves against.

    Returns:
        A spawn, a read or write of the tool's path, a handoff a Bash command names, or an
        unclassified tool use; None without a tool name.
    """
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
    access = command and handoffs.shell_access(
        command, payload.get("cwd") or "", home, config_mod.handoff_dir(config, home)
    )
    return ToolUse(name, *access) if access else ToolUse(name)


def configured_window(models: tuple[str | None, ...], patterns: list[object]) -> int | None:
    """Find the configured window of the first [substring, window] pair matching a model.

    Args:
        models: Known model names of the session; None entries are skipped.
        patterns: Configured [substring, window] pairs; malformed entries are skipped.

    Returns:
        The window of the first matching pair, else None.
    """
    for entry in patterns:
        if not (
            isinstance(entry, list)
            and len(entry) == WINDOW_ENTRY_SIZE
            and text(entry[0])
            and type(entry[1]) is int
            and entry[1] > 0
        ):
            continue
        if any(model and entry[0] in model for model in models):
            return entry[1]
    return None


def learned_window(directory: Path, model: str | None, minimum: int) -> int | None:
    """Look up the auto-compaction point learned for a model.

    Args:
        directory: Claude Code state directory.
        model: The session's model, or None when unknown.
        minimum: Smallest window that counts as learned.

    Returns:
        The learned window, or None when unknown or below the minimum.
    """
    value = state_mod.read_json(directory / LEARNED_FILE).get(model) if model else None
    return value if type(value) is int and value >= minimum else None


def remember_window(directory: Path, model: str, tokens: int) -> None:
    """Store a model's learned window under the learned-windows lock.

    Args:
        directory: Claude Code state directory.
        model: Model the compaction happened on.
        tokens: Tokens at the automatic compaction.
    """
    path = directory / LEARNED_FILE
    with state_mod.file_lock(path.with_name(path.name + ".lock")):
        data = state_mod.read_json(path)
        data[model] = tokens
        state_mod.write_json(path, data)


def learn(session: dict, directory: Path, model: str | None, tokens: int | None, config: dict) -> None:
    """Make an automatic compaction point this session's and this model's window.

    Counts below the configured minimum and repeats of the session's window are ignored.

    Args:
        session: Mutable session state.
        directory: Claude Code state directory.
        model: Model the compaction happened on, or None when unknown.
        tokens: Tokens at the automatic compaction, or None when unknown.
        config: Loaded Claude Code adapter configuration.
    """
    if tokens is None or tokens < config["transcript"]["min_learned_window"] or session.get("learned_window") == tokens:
        return
    session["learned_window"] = tokens
    if model:
        remember_window(directory, model, tokens)


def window(session: dict, model: str | None, directory: Path, config: dict) -> int | None:
    """Pick the context window of the main session.

    Args:
        session: Session state; its `adapter` section must exist.
        model: The session's latest model, or None when unknown.
        directory: Claude Code state directory.
        config: Loaded Claude Code adapter configuration.

    Returns:
        The session's learned window, else the model's learned window, else the configured
        window for the start or latest model, else None.
    """
    minimum = config["transcript"]["min_learned_window"]
    if session.get("learned_window", 0) >= minimum:
        return session["learned_window"]
    start = session["adapter"].get("start_model")
    return learned_window(directory, model, minimum) or configured_window(
        (text(start), model), config["context_windows"]
    )


def read_main(payload: dict[str, object], adapter: dict, config: dict) -> transcript.Reading:
    """Read the main session's transcript after its latest compaction.

    Args:
        payload: Validated hook payload.
        adapter: The session state's `adapter` section.
        config: Loaded Claude Code adapter configuration.

    Returns:
        The transcript reading; unknown fields stay None.
    """
    since_bytes = adapter.get("compacted_bytes")
    since_time = adapter.get("compacted_at")
    return transcript.read(
        payload.get("transcript_path"),
        config["transcript"]["tail_bytes"],
        since_bytes if type(since_bytes) is int else 0,
        since_time if type(since_time) in (int, float) else None,
    )


def observed_event(event: str, payload: dict[str, object]) -> str:
    """Map a native observed event to its core event.

    Args:
        event: Native event, one of the observed ones.
        payload: Validated hook payload.

    Returns:
        The core event; a SessionStart after compaction is a compaction.
    """
    if event == "SessionStart" and payload.get("source") == COMPACT_SOURCE:
        return COMPACT
    return OBSERVED[event]


def observe_main(event: str, payload: dict[str, object], config: dict, directory: Path, home: Path) -> Observation:
    """Observe the main session, updating its model, learned window and compaction reset.

    Args:
        event: Native observed event.
        payload: Validated hook payload with a session_id.
        config: Loaded Claude Code adapter configuration.
        directory: Claude Code state directory.
        home: Home directory that the handoff store resolves against.

    Returns:
        The observation of the main session.
    """
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
    return Observation(
        RUNTIME,
        session_id,
        observed_event(event, payload),
        tokens=reading.tokens,
        window=size,
        model=model,
        compacted=compacted,
        cwd=payload.get("cwd") or "",
        prompt=payload.get("prompt") if event == "UserPromptSubmit" else None,
        tool=tool_use(payload, config, home) if event == "PostToolUse" else None,
    )


def observe_leaf(event: str, payload: dict[str, object], config: dict, home: Path) -> Observation:
    """Observe a subagent from its own transcript, without touching session state.

    Args:
        event: Native leaf event.
        payload: Validated subagent hook payload with an agent_id.
        config: Loaded Claude Code adapter configuration.
        home: Home directory that the handoff store resolves against.

    Returns:
        The subagent's observation, keyed by its agent id, with no window.
    """
    reading = transcript.read(transcript.subagent_path(payload), config["transcript"]["tail_bytes"])
    return Observation(
        RUNTIME,
        payload["agent_id"],
        OBSERVED[event],
        tokens=reading.tokens,
        model=reading.model,
        is_subagent=True,
        role=role_name(payload.get("agent_type")),
        cwd=payload.get("cwd") or "",
        prompt=payload.get("prompt") if event == "UserPromptSubmit" else None,
        tool=tool_use(payload, config, home) if event == "PostToolUse" else None,
    )


def on_precompact(payload: dict[str, object], config: dict, directory: Path) -> None:
    """Learn the window from an automatic compaction about to happen; manual ones are ignored.

    Args:
        payload: Validated PreCompact payload with a session_id.
        config: Loaded Claude Code adapter configuration.
        directory: Claude Code state directory.
    """
    if payload.get("trigger") != transcript.AUTO_TRIGGER:
        return
    with state_mod.session_state(directory, RUNTIME, payload["session_id"]) as session:
        adapter = session.setdefault("adapter", {})
        reading = read_main(payload, adapter, config)
        learn(session, directory, reading.model or text(adapter.get("model")), reading.tokens, config)


def on_postcompact(payload: dict[str, object], directory: Path) -> None:
    """Mark the transcript up to now as compacted.

    Usage recorded before the compaction is obsolete; the next observation resets fired tiers.

    Args:
        payload: Validated PostCompact payload with a session_id.
        directory: Claude Code state directory.
    """
    with state_mod.session_state(directory, RUNTIME, payload["session_id"]) as session:
        session.setdefault("adapter", {}).update(
            compacted_bytes=transcript.size(payload.get("transcript_path")), compacted_at=time.time(), reset=True
        )


def clip(message: str, limit: int) -> str:
    """Shorten a message to a character limit, at a line break when there is one.

    Args:
        message: Message to clip.
        limit: Maximum length, including the trailing ellipsis.

    Returns:
        The message, or its clipped prefix ending in an ellipsis.
    """
    if len(message) <= limit:
        return message
    cut = message[: limit - 1]
    return (cut.rsplit("\n", 1)[0] if "\n" in cut else cut) + "…"


def respond(decision: Decision, event: str, config: dict) -> dict[str, object]:
    """Render a decision as the Claude Code hook response for a native event.

    Args:
        decision: The decision to render.
        event: Native event being answered.
        config: Loaded Claude Code adapter configuration with the output limits.

    Returns:
        `decision: block` for blocks and Stop continuations, clipped additional context
        otherwise, or `{}` for a pass.
    """
    if decision.kind == PASS or not decision.message:
        return {}
    if decision.kind == BLOCK or event == "Stop":
        return {"decision": "block", "reason": decision.message}
    limits = config["hook_output"]
    limit = limits["tool_note_chars"] if event == "PostToolUse" else limits["context_chars"]
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": clip(decision.message, limit)}}


def lacks_access(config: dict, home: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Tell whether settings.json lacks the handoff allow rule where it could be added.

    Args:
        config: Loaded Claude Code adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment to read; None reads the process environment.

    Returns:
        True when settings.json is missing or has no rule and no foreign `permissions.allow`;
        False when it holds the rule or cannot be read or parsed.
    """
    try:
        data = json.loads((mapping.claude_home(home, environ) / "settings.json").read_text())
    except FileNotFoundError:
        return True
    except OSError, ValueError:
        return False
    permissions = data.get("permissions", {}) if isinstance(data, dict) else None
    allow = permissions.get("allow", []) if isinstance(permissions, dict) else None
    return isinstance(allow, list) and mapping.access_rule(config_mod.handoff_dir(config, home)) not in allow


def access_offer(config: dict, home: Path, environ: Mapping[str, str] | None = None) -> str:
    """Offer the handoff write access once ever, recording the offer before it is made.

    No offer is made when `handoff_access.enabled` is false, an install manifest shows the access
    was installed (by access.py or the script install), the offer marker exists, settings.json
    already allows it or cannot be read, or the marker cannot be written.

    Args:
        config: Loaded Claude Code adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment to read; None reads the process environment.

    Returns:
        The one-time offer note naming the absolute command, or an empty string.
    """
    if not config["handoff_access"]["enabled"]:
        return ""
    installed = (
        install.manifest_path(home, mapping.install_identity(home, name, environ)).is_file()
        for name in (RUNTIME, mapping.PLUGIN_ACCESS)
    )
    identity = mapping.install_identity(home, mapping.PLUGIN_ACCESS, environ)
    if any(installed) or install.offer_marker(home, identity).exists():
        return ""
    if not lacks_access(config, home, environ):
        return ""
    try:
        install.record_offer(home, identity)
    except OSError:
        return ""
    return ACCESS_OFFER.format(
        command=f'sh "{KIT_ROOT}/{LAUNCHER}" {ACCESS_SCRIPT}',
        settings=mapping.claude_home(home, environ) / "settings.json",
    )


def plugin_policy(config: dict, home: Path, environ: Mapping[str, str] | None = None) -> str:
    """Read the packaged policy that a plugin puts into the session context.

    Only a packaged plugin ships the rendered policy; an installed kit uses the rules file.

    Args:
        config: Loaded Claude Code adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment to read; None reads the process environment.

    Returns:
        The policy text, followed once ever by the offer from `access_offer`, or an empty string
        outside a plugin.
    """
    try:
        policy = (KIT_ROOT / PLUGIN_POLICY_FILE).read_text()
    except FileNotFoundError:
        return ""
    offer = access_offer(config, home, environ)
    return f"{policy.rstrip()}\n\n{offer}\n" if offer else policy


def observed(event: str, payload: dict[str, object], config: dict, home: Path) -> Observation | None:
    """Observe a hook call, or apply its bookkeeping.

    Args:
        event: Registered native event.
        payload: Validated hook payload.
        config: Loaded Claude Code adapter configuration.
        home: Home directory of the context-guard install.

    Returns:
        The observation to evaluate; None when the event only maintains state or must pass.
    """
    if payload.get("agent_id"):
        return observe_leaf(event, payload, config, home) if event in LEAF_EVENTS else None
    if not payload.get("session_id") or event == SUBAGENT_STOP:
        return None
    directory = config_mod.state_dir(config, home, RUNTIME)
    if event == "PreCompact":
        on_precompact(payload, config, directory)
        return None
    if event == "PostCompact":
        on_postcompact(payload, directory)
        return None
    if event == "Stop" and payload.get("stop_hook_active"):
        return None
    return observe_main(event, payload, config, directory, home)


def handle(
    event: str, payload: object, config: dict, home: Path, environ: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Observe, evaluate and answer one Claude Code hook call.

    Args:
        event: Native event from the command line.
        payload: Decoded stdin JSON of the hook call.
        config: Loaded Claude Code adapter configuration.
        home: Home directory of the context-guard install.
        environ: Environment to read; None reads the process environment.

    Returns:
        The native response; `{}` for unknown events, invalid payloads and bookkeeping events.
    """
    if event not in HOOK_EVENTS or not valid_payload(payload):
        return {}
    mapping.claude_home(home, environ)
    directory = config_mod.state_dir(config, home, RUNTIME)
    data = payload.get("tool_input")
    subagents.track(event, payload, role_name(data.get("subagent_type")) if isinstance(data, dict) else None, directory)
    obs = observed(event, payload, config, home)
    if obs is None:
        return {}
    return hook.evaluate(
        obs,
        config,
        home,
        lambda: runner.services(home),
        lambda decision: respond(decision, event, config),
        lambda response: "hookSpecificOutput" in response,
        lambda: plugin_policy(config, home, environ),
        lambda decision: subagents.deliver(obs, decision, config, directory),
    )


def run(event: str, payload: object, home: Path, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Answer one Claude Code hook call, failing open.

    Args:
        event: Native event from the command line.
        payload: Decoded stdin JSON of the hook call.
        home: Home directory of the context-guard install.
        environ: Environment overrides; None reads the process environment.

    Returns:
        The native response, or `{}` for internal runs, foreign homes and unreadable
        configuration, state or telemetry.
    """
    return hook.guarded(
        KIT_ROOT, lambda: handle(event, payload, load_config(home, environ), home, environ), {}, environ
    )


def main() -> None:
    """Run the hook process: argv names the event, stdin holds the payload, stdout gets one response."""
    hook.main(KIT_ROOT, sys.argv[1:], runner.services, run, lambda _event: {})


if __name__ == "__main__":
    main()
