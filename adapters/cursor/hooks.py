"""Cursor hook entry: `python hooks.py <hookEventName>` reads the payload on stdin.

Token counts come from Cursor's own chat record (composerData in state.vscdb). Cursor CLI
chats are not in that database; they keep their own store at
`<cli_chats>/<workspace hash>/<conversation_id>/store.db`, whose latest root blob (protobuf)
holds the context usage message in field 5: tokens used (1) and context window (2). Missing or
changed telemetry and invalid payloads fail open.
"""

import json
import re
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

KIT_ROOT = Path(__file__).resolve().parents[2]
if str(KIT_ROOT) not in sys.path:
    sys.path.insert(0, str(KIT_ROOT))

from adapters.cursor import mapping, recovery, runner, subagents
from core import config as config_mod, events, handoffs, hook, policy, spawn, state as state_mod
from core.config import valid_text
from core.observation import (
    BLOCK,
    CONTEXT,
    NOTE,
    OTHER,
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
from core.runner import service

RUNTIME = "cursor"
PROMPT_HOOK = "beforeSubmitPrompt"
ADAPTER_CONFIG = Path(__file__).resolve().parent / "config.json"
COMPOSER_KEY = "composerData:"
TEXT_FIELDS = (
    "conversation_id",
    "session_id",
    "generation_id",
    "subagent_id",
    "parent_tool_call_id",
    "tool_use_id",
    "prompt",
    "model",
    "status",
    "trigger",
    "tool_name",
    "cwd",
)
PATH_FIELDS = ("file_path", "path", "target_file")
EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")
PENDING_NOTE, PENDING_TIER = "pending_note", "pending_tier"
CLI_ID = re.compile(r"[\w-]+")
CLI_META_KEY, CLI_ROOT_KEY = "0", "latestRootBlobId"
CLI_USAGE_FIELD, CLI_TOKENS_FIELD, CLI_WINDOW_FIELD = 5, 1, 2
VARINT, LENGTH_DELIMITED = 0, 2
VARINT_BITS, VARINT_CONTINUATION = 64, 0x80
FIXED_SIZES = {1: 8, 5: 4}


@dataclass(frozen=True)
class Telemetry:
    """Context usage read from a Cursor chat record.

    Attributes:
        tokens: Tokens in the live context, or None when unknown.
        window: Context window in tokens, or None when unknown.
    """

    tokens: int | None
    window: int | None


NO_TELEMETRY = Telemetry(None, None)


def count(value: object) -> bool:
    """Tell whether an untrusted JSON value is a token or loop count.

    Args:
        value: Untrusted JSON value.

    Returns:
        True for a non-negative int (bools excluded), else False.
    """
    return type(value) is int and value >= 0


def validate_payload(payload: object) -> dict[str, object]:
    """Reject Cursor payload shapes the hook cannot interpret.

    Args:
        payload: Decoded stdin JSON of one Cursor hook call.

    Returns:
        The payload, unchanged, once its text, list and count fields have the expected types.

    Raises:
        ValueError: The payload is not an object or a known field has the wrong type; the hook
            guard treats ValueError as a reason to pass, so the hook fails open.
    """
    if not isinstance(payload, dict):
        msg = "payload must be an object"
        raise ValueError(msg)  # noqa: TRY004
    for key in TEXT_FIELDS:
        if payload.get(key) is not None and not valid_text(payload[key]):
            msg = f"{key} must be text"
            raise ValueError(msg)
    roots = payload.get("workspace_roots")
    if roots is not None and (not isinstance(roots, list) or not all(valid_text(root) for root in roots)):
        msg = "workspace_roots must be a list of text"
        raise ValueError(msg)
    if payload.get("tool_input") is not None and not isinstance(payload["tool_input"], dict):
        msg = "tool_input must be an object"
        raise ValueError(msg)
    if "loop_count" in payload and not count(payload["loop_count"]):
        msg = "loop_count must be a non-negative int"
        raise ValueError(msg)
    return payload


def event_spec(native: str) -> mapping.HookEvent:
    """Look up the registration of a native Cursor hook event.

    Args:
        native: Cursor hook event name from the command line, e.g. `postToolUse`.

    Returns:
        The core event and options the native event maps to.

    Raises:
        ValueError: The event is not one context-guard registers.
    """
    spec = mapping.HOOK_EVENTS.get(native)
    if spec is None:
        msg = f"unknown Cursor hook event {native!r}"
        raise ValueError(msg)
    return spec


def state_db_path(config: dict, home: Path) -> Path | None:
    """Resolve Cursor's state.vscdb: the configured path, else the platform default.

    Args:
        config: Loaded Cursor adapter configuration.
        home: Home directory that `~` expands against.

    Returns:
        The database path, or None when neither the configuration nor the platform names one.
    """
    conf = config["telemetry"]
    configured = conf["state_db"] or conf["platform_state_db"].get(sys.platform, "")
    return config_mod.expand_path(configured, home) if configured else None


def parse_chat(value: object) -> Telemetry | None:
    """Read context usage from a decoded composerData chat record.

    Args:
        value: Decoded JSON of Cursor's chat record.

    Returns:
        Tokens used and context limit (a zero limit reads as unknown), or None when the record
        or either field has a changed shape.
    """
    if not isinstance(value, dict):
        return None
    tokens, window = value.get("contextTokensUsed"), value.get("contextTokenLimit")
    if any(item is not None and not count(item) for item in (tokens, window)):
        return None
    return Telemetry(tokens, window or None)


def read_chat(database: Path | None, conversation_id: str, timeout: float) -> Telemetry | None:
    """Read an IDE chat's context usage from state.vscdb, opened read-only.

    Args:
        database: Path of state.vscdb, or None when unknown.
        conversation_id: Cursor conversation id of the chat.
        timeout: Seconds SQLite waits for a locked database.

    Returns:
        The chat's telemetry, or None when the database, the row or its shape is missing or unreadable.
    """
    if database is None or not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "select value from cursorDiskKV where key = ?", (COMPOSER_KEY + conversation_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not row or not isinstance(row[0], (str, bytes)):
        return None
    try:
        return parse_chat(json.loads(row[0]))
    except ValueError, UnicodeError:
        return None


def cli_chats_path(config: dict, home: Path) -> Path | None:
    """Resolve the Cursor CLI chat store directory.

    Args:
        config: Loaded Cursor adapter configuration.
        home: Home directory that `~` expands against.

    Returns:
        The configured directory, or None when the configuration leaves it empty.
    """
    configured = config["telemetry"]["cli_chats"]
    return config_mod.expand_path(configured, home) if configured else None


def varint(data: bytes, index: int) -> tuple[int, int]:
    """Decode one protobuf base-128 varint.

    Args:
        data: Encoded protobuf message.
        index: Offset of the varint's first byte.

    Returns:
        The decoded value and the offset just past the varint.

    Raises:
        ValueError: The data ends inside the varint or it exceeds 64 bits.
    """
    value = shift = 0
    while index < len(data) and shift < VARINT_BITS:
        byte = data[index]
        value, index, shift = value | (byte & 0x7F) << shift, index + 1, shift + 7
        if byte < VARINT_CONTINUATION:
            return value, index
    msg = "truncated varint"
    raise ValueError(msg)


def proto_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """Split a protobuf message into its top-level fields.

    Args:
        data: Encoded protobuf message.

    Returns:
        (field number, wire type, value) for every field in order; varints decode to int,
        length-delimited and fixed-size fields stay bytes.

    Raises:
        ValueError: A field is truncated or uses a group wire type.
    """
    fields, index = [], 0
    while index < len(data):
        key, index = varint(data, index)
        number, wire = key >> 3, key & 7
        if wire == VARINT:
            value, index = varint(data, index)
        elif wire == LENGTH_DELIMITED or wire in FIXED_SIZES:
            size, index = varint(data, index) if wire == LENGTH_DELIMITED else (FIXED_SIZES[wire], index)
            if index + size > len(data):
                msg = "truncated field"
                raise ValueError(msg)
            value, index = data[index : index + size], index + size
        else:
            msg = f"unsupported wire type {wire}"
            raise ValueError(msg)
        fields.append((number, wire, value))
    return fields


def cli_usage(root: bytes) -> tuple[int | None, int | None] | None:
    """Read tokens and window from a CLI root blob's latest usage message.

    A malformed blob raises ValueError from `proto_fields`.

    Args:
        root: Encoded root blob of a Cursor CLI chat.

    Returns:
        Tokens used and context window, either None when absent (protobuf omits a zero count),
        or None when the blob holds no usage message.
    """
    usage = [
        value for number, wire, value in proto_fields(root) if number == CLI_USAGE_FIELD and wire == LENGTH_DELIMITED
    ]
    if not usage:
        return None
    counts = {number: value for number, wire, value in proto_fields(usage[-1]) if wire == VARINT}
    return counts.get(CLI_TOKENS_FIELD), counts.get(CLI_WINDOW_FIELD) or None


def cli_root_id(meta: object) -> str | None:
    """Extract the latest root blob id from a CLI store's meta value.

    Args:
        meta: The meta row value: hex-encoded JSON, or anything else when the schema changed.

    Returns:
        The root blob id, or None when the value cannot be decoded or names no root.
    """
    if not isinstance(meta, str):
        return None
    try:
        record = json.loads(bytes.fromhex(meta))
    except ValueError:
        return None
    root = record.get(CLI_ROOT_KEY) if isinstance(record, dict) else None
    return root if valid_text(root) and root else None


def read_cli_root(database: Path, timeout: float) -> bytes | None:
    """Read the latest root blob of a Cursor CLI chat store, opened read-only.

    Args:
        database: Path of the chat's store.db.
        timeout: Seconds SQLite waits for a locked database.

    Returns:
        The root blob bytes, or None when the store, its meta row or the blob is missing or unreadable.
    """
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error:
        return None
    try:
        meta = connection.execute("select value from meta where key = ?", (CLI_META_KEY,)).fetchone()
        root = cli_root_id(meta[0] if meta else None)
        row = connection.execute("select data from blobs where id = ?", (root,)).fetchone() if root else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return row[0] if row and isinstance(row[0], bytes) else None


def cli_store(chats: Path | None, conversation_id: str) -> Path | None:
    """Find a conversation's store.db under any workspace hash of the CLI chat directory.

    Args:
        chats: Cursor CLI chat directory, or None when unknown.
        conversation_id: Cursor conversation id; ids that are not plain word characters match nothing.

    Returns:
        The first matching store in sorted order, or None.
    """
    if chats is None or not CLI_ID.fullmatch(conversation_id) or not chats.is_dir():
        return None
    return next(iter(sorted(chats.glob(f"*/{conversation_id}/store.db"))), None)


def read_cli_chat(chats: Path | None, conversation_id: str, timeout: float) -> Telemetry | None:
    """Read a Cursor CLI chat's context usage from its store.

    Args:
        chats: Cursor CLI chat directory, or None when unknown.
        conversation_id: Cursor conversation id of the chat.
        timeout: Seconds SQLite waits for a locked database.

    Returns:
        The chat's telemetry, or None when the store or its usage message is missing or malformed.
    """
    database = cli_store(chats, conversation_id)
    root = read_cli_root(database, timeout) if database else None
    if root is None:
        return None
    try:
        usage = cli_usage(root)
    except ValueError:
        return None
    return Telemetry(*usage) if usage else None


def agent_profile(profile: dict) -> tuple[str | None, str | None]:
    """Read a tier's agent-file model and its effort, e.g. `claude-opus-5-5-high`.

    Args:
        profile: One tier profile from the configuration.

    Returns:
        The tier's `call` slug and the effort in its suffix, each None when absent.
    """
    call = profile.get("call")
    if not valid_text(call) or not call:
        return None, None
    effort = EFFORT_SUFFIX.search(call)
    return call, effort[1] if effort else None


def spawn_detail(config: dict, tool_input: dict[str, object]) -> dict[str, str | None]:
    """Describe a Task spawn by role, tier, model and effort.

    A Task without `model` runs the role's agent file, i.e. its default tier.

    Args:
        config: Loaded Cursor adapter configuration.
        tool_input: The Task tool input from the payload.

    Returns:
        The spawn detail logged with the event; the tier is None when the model matches no tier.
    """
    role = tool_input.get("subagent_type") if valid_text(tool_input.get("subagent_type")) else ""
    model = tool_input.get("model") if valid_text(tool_input.get("model")) else ""
    if not model:
        return spawn.default_detail(role or None, config["tiers"], agent_profile)
    tier = next((tier for tier, profile in config["tiers"].items() if profile.get("call") == model), None)
    effort = EFFORT_SUFFIX.search(model)
    return {"role": role or None, "tier": tier, "model": model, "effort": effort[1] if effort else None}


def shell_cwd(tool_input: dict[str, object], payload: dict[str, object]) -> str:
    """Resolve the directory a shell command runs in.

    Args:
        tool_input: The shell tool input; its optional `cwd` is relative to the workspace.
        payload: The validated hook payload.

    Returns:
        The tool's `cwd` joined to the workspace, else the workspace itself.
    """
    cwd = tool_input.get("cwd")
    return str(Path(workspace(payload)) / cwd) if valid_text(cwd) and cwd else workspace(payload)


def shell_use(config: dict, payload: dict[str, object], home: Path) -> ToolUse:
    """Classify a shell tool call by the handoff it reads or writes, if any.

    Args:
        config: Loaded Cursor adapter configuration.
        payload: The validated hook payload of a shell tool.
        home: Home directory that the handoff store resolves against.

    Returns:
        A read or write of the handoff path the command names, else an unclassified tool use.
    """
    tool_input = payload["tool_input"] if isinstance(payload.get("tool_input"), dict) else {}
    command = tool_input.get("command")
    access = valid_text(command) and handoffs.shell_access(
        command, shell_cwd(tool_input, payload), home, config_mod.handoff_dir(config, home)
    )
    return ToolUse(payload["tool_name"], *access) if access else ToolUse(payload["tool_name"])


def tool_use(config: dict, payload: dict[str, object], home: Path, *, spawn_only: bool) -> ToolUse | None:
    """Classify the native tool of a tool hook; spawns are logged once, before the subagent runs.

    Args:
        config: Loaded Cursor adapter configuration.
        payload: The validated hook payload.
        home: Home directory that the handoff store resolves against.
        spawn_only: Whether the hook only observes spawns (preToolUse) rather than results.

    Returns:
        The classified tool use, or None for a non-spawn tool seen by a spawn-only hook.
    """
    name, tool_input, tools = payload.get("tool_name") or "", payload.get("tool_input") or {}, config["tools"]
    if name in tools["spawn"]:
        return ToolUse(name, SPAWN, detail=spawn_detail(config, tool_input)) if spawn_only else ToolUse(name)
    if spawn_only:
        return None
    path = next((tool_input[key] for key in PATH_FIELDS if valid_text(tool_input.get(key))), None)
    if name in tools["read"]:
        return ToolUse(name, READ, path)
    if name in tools["write"]:
        return ToolUse(name, WRITE, path)
    if name in tools["shell"]:
        return shell_use(config, payload, home)
    return ToolUse(name, OTHER)


def session_id(payload: dict[str, object]) -> str:
    """Pick the chat id of a payload.

    Args:
        payload: The validated hook payload.

    Returns:
        The conversation id, else the session id, else an empty string.
    """
    return payload.get("conversation_id") or payload.get("session_id") or ""


def workspace(payload: dict[str, object]) -> str:
    """Pick the workspace directory of a payload.

    Args:
        payload: The validated hook payload.

    Returns:
        The first workspace root, else the payload's `cwd`, else an empty string.
    """
    roots = payload.get("workspace_roots") or []
    return roots[0] if roots else payload.get("cwd") or ""


def stop_skipped(payload: dict[str, object]) -> bool:
    """Tell whether a stop hook skips policy evaluation; spawn bookkeeping runs regardless.

    Args:
        payload: The validated stop hook payload.

    Returns:
        True unless the turn completed and is not our own follow-up loop.
    """
    return payload.get("status", "completed") != "completed" or payload.get("loop_count", 0) > 0


def telemetry_for(config: dict, home: Path, event: str, payload: dict[str, object], *, spawn_only: bool) -> Telemetry:
    """Read context usage for an event from the IDE database, else the CLI chat store.

    Args:
        config: Loaded Cursor adapter configuration.
        home: Home directory that configured paths expand against.
        event: Core event of the hook.
        payload: The validated hook payload.
        spawn_only: Whether the hook only observes spawns.

    Returns:
        The chat's telemetry; unknown for session starts, spawn-only hooks and unreadable stores.
    """
    if event == SESSION or spawn_only:
        return NO_TELEMETRY
    conversation, timeout = session_id(payload), config["telemetry"]["timeout_seconds"]
    return (
        read_chat(state_db_path(config, home), conversation, timeout)
        or read_cli_chat(cli_chats_path(config, home), conversation, timeout)
        or NO_TELEMETRY
    )


def remember_parent(directory: Path, payload: dict[str, object]) -> None:
    """Record a subagent's parent Task call in its session state.

    Only subagent tool hooks carry `parent_tool_call_id`; its stop and prompt hooks do not,
    so this is how those learn they belong to a subagent.

    Args:
        directory: Cursor state directory.
        payload: The validated hook payload.
    """
    call = payload.get("parent_tool_call_id")
    if call and recorded_parent(directory, session_id(payload)) != call:
        with state_mod.session_state(directory, RUNTIME, session_id(payload)) as state:
            state.setdefault("adapter", {})[subagents.PARENT_CALL] = call


def recorded_parent(directory: Path, conversation: str) -> str | None:
    """Look up the parent Task call recorded for a conversation.

    Args:
        directory: Cursor state directory.
        conversation: The conversation id.

    Returns:
        The parent's `tool_use_id`, or None for a main chat or a subagent not seen on a tool hook.
    """
    state = state_mod.validated(state_mod.read_json(subagents.state_path(directory, conversation)))
    call = state.get("adapter", {}).get(subagents.PARENT_CALL)
    return call if valid_text(call) and call else None


def observe(native: str, payload: object, config: dict, home: Path) -> Observation | None:
    """Translate one Cursor hook payload into an observation.

    An unknown event or an invalid payload raises ValueError from `event_spec` or
    `validate_payload`.

    Args:
        native: Cursor hook event name.
        payload: Decoded stdin JSON of the hook call.
        config: Loaded Cursor adapter configuration.
        home: Home directory that configured paths expand against.

    Returns:
        The observation to evaluate, or None when there is nothing to evaluate (an ignored
        tool, or a stop that is incomplete or our own follow-up).
    """
    spec, payload = event_spec(native), validate_payload(payload)
    event, spawn_only = spec.event, spec.spawn_only
    tool = tool_use(config, payload, home, spawn_only=spawn_only) if event == TOOL else None
    if (event == TOOL and tool is None) or (event == STOP and stop_skipped(payload)):
        return None
    chat = telemetry_for(config, home, event, payload, spawn_only=spawn_only)
    fallback = config["telemetry"]["window_fallback"] or None
    directory = config_mod.state_dir(config, home, RUNTIME)
    parent = payload.get("parent_tool_call_id") or recorded_parent(directory, session_id(payload))
    return Observation(
        RUNTIME,
        session_id(payload),
        event,
        tokens=chat.tokens,
        rearm_on_usage=False,
        window=chat.window or fallback,
        model=payload.get("model") or None,
        is_subagent=bool(parent),
        role=subagents.spawned_role(directory, parent),
        cwd=workspace(payload),
        prompt=payload.get("prompt") if event == PROMPT else None,
        tool=tool,
    )


def take_pending(state: dict) -> tuple[str, str | None] | None:
    """Remove the waiting prompt note from session state.

    Args:
        state: Mutable session state; the note is popped from its `adapter` section.

    Returns:
        The note and its tier (the session's current tier when the field was absent), or None when
        no note waits or the session's tier was re-armed since.
    """
    adapter = state.setdefault("adapter", {})
    missing = object()
    message, tier = adapter.pop(PENDING_NOTE, None), adapter.pop(PENDING_TIER, missing)
    if not isinstance(message, str) or (
        tier is not missing and tier is not None and (not isinstance(tier, str) or "tier" not in state)
    ):
        return None
    return message, state.get("tier") if tier is missing else tier


def handoff_written(decision: Decision) -> bool:
    """Tell whether a decision credited a handoff write.

    Args:
        decision: The evaluated decision.

    Returns:
        True when its events include `handoff_written`.
    """
    return any(record.get("event") == "handoff_written" for record in decision.events)


def carry_note(obs: Observation, decision: Decision, state: dict) -> Decision:
    """Compose a deferred warning inside the policy transaction.

    Args:
        obs: Current observation.
        decision: Policy decision.
        state: Locked session state.

    Returns:
        Decision with any deliverable warning attached.
    """
    if obs.is_subagent or (obs.tool and obs.tool.action == SPAWN):
        return decision
    if (obs.event == PROMPT and decision.kind != NOTE) or (obs.event == STOP and decision.kind != CONTEXT):
        return decision
    if obs.event == PROMPT:
        state.setdefault("adapter", {}).update({PENDING_NOTE: decision.message, PENDING_TIER: decision.tier})
        return decision
    pending = take_pending(state)
    if (
        obs.event == STOP
        or handoff_written(decision)
        or pending is None
        or policy.RANK.get(decision.tier, 0) >= policy.RANK.get(pending[1], 0) > 0
    ):
        return decision
    message = "\n\n".join(part for part in (pending[0], decision.message) if part)
    return replace(
        decision,
        kind=NOTE if obs.event == TOOL else CONTEXT,
        message=message,
        events=[*decision.events, events.record("note_delivered", obs, pending[1])],
    )


def respond(decision: Decision, event: str) -> dict[str, object]:
    """Render a decision as the Cursor hook response for its core event.

    Args:
        decision: The decision to render.
        event: Core event of the hook.

    Returns:
        The native response: `continue` for prompts, `additional_context` for session starts (always,
        led by `mapping.SESSION_MARKER`) and tool notes, `followup_message` for stops, or `{}` when
        the event cannot carry the decision.
    """
    if event == PROMPT:
        if decision.kind == BLOCK:
            return {"continue": False, "user_message": decision.message}
        return {"continue": True}
    if event == SESSION:
        message = decision.message if decision.kind == CONTEXT else ""
        return {"additional_context": "\n\n".join(part for part in (mapping.SESSION_MARKER, message) if part)}
    if decision.kind == PASS or not decision.message:
        return {}
    if event == TOOL and decision.kind == NOTE:
        return {"additional_context": decision.message}
    if event == STOP and decision.kind == CONTEXT:
        return {"followup_message": decision.message}
    return {}


def handle(native: str, payload: object, config: dict, home: Path) -> dict[str, object]:
    """Observe, evaluate and answer one Cursor hook call.

    Invalid payloads raise ValueError from `observe`; `run` turns that into a pass.

    Args:
        native: Cursor hook event name.
        payload: Decoded stdin JSON of the hook call.
        config: Loaded Cursor adapter configuration.
        home: Home directory of the context-guard install.

    Returns:
        The native hook response.
    """
    payload = validate_payload(payload)
    event_spec(native)
    directory = config_mod.state_dir(config, home, RUNTIME)
    remember_parent(directory, payload)
    subagents.track(config, directory, native, payload)
    if recovery.lifecycle(native, payload, config, home):
        return {}
    obs = observe(native, payload, config, home)
    if obs is None:
        return respond(Decision(), event_spec(native).event)
    if obs.tool is not None and obs.tool.action == SPAWN:
        subagents.remember_spawn(directory, payload, obs.tool.detail.get("role"))
    responses: list[tuple[Decision, dict]] = []
    routes: list[service.Routing] = []

    def routed_prompt(prompt: Observation) -> service.Routing:
        """Retain prompt routing for transactional deferred delivery.

        Args:
            prompt: The root prompt observation.

        Returns:
            The prompt service's routing decision.
        """
        routing = runner.services(home).prompt(prompt)
        routes.append(routing)
        return routing if routing.blocked else service.Routing()

    def compose(decision: Decision, state: dict) -> Decision:
        """Render the composed decision before its state transaction commits.

        Args:
            decision: Shared policy decision.
            state: Locked session state.

        Returns:
            The composed decision whose native response is retained for delivery.

        Raises:
            ValueError: Model context could not be rendered for delivery.
        """
        combined = service.combine(decision, routes[0]) if routes else decision
        decision = subagents.deliver_state(
            obs, recovery.deliver(obs, carry_note(obs, combined, state), state, config, home), state
        )
        response = respond(decision, obs.event)
        if decision.message and obs.event in (TOOL, SESSION) and not response.get("additional_context"):
            msg = "Cursor model context response was not rendered"
            raise ValueError(msg)
        responses.append((decision, response))
        return decision

    return hook.evaluate(
        obs,
        config,
        home,
        lambda: SimpleNamespace(prompt=routed_prompt),
        lambda decision: responses[0][1] if responses and decision == responses[0][0] else respond(decision, obs.event),
        lambda response: obs.event == TOOL and "additional_context" in response,
        adjust=compose,
    )


def load(home: Path, environ: Mapping[str, str] | None = None) -> dict:
    """Load the Cursor configuration: defaults, adapter config, user config, environment.

    Args:
        home: Home directory holding the user configuration.
        environ: Environment overrides; None reads the process environment.

    Returns:
        The merged configuration.
    """
    return config_mod.load_config(config_mod.sources_for(RUNTIME, home, ADAPTER_CONFIG), environ)


def passing(native: str) -> dict[str, object]:
    """Build the response that lets a Cursor hook pass.

    Args:
        native: Cursor hook event name.

    Returns:
        `{"continue": true}` for beforeSubmitPrompt, `{}` for every other event.
    """
    return {"continue": True} if native == PROMPT_HOOK else {}


def run(native: str, payload: object, home: Path, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Answer one Cursor hook call, failing open.

    Args:
        native: Cursor hook event name.
        payload: Decoded stdin JSON of the hook call.
        home: Home directory of the context-guard install.
        environ: Environment overrides; None reads the process environment.

    Returns:
        The native response, or the passing response for internal runs, foreign homes and
        unreadable configuration, state, telemetry or payloads.
    """
    return hook.guarded(KIT_ROOT, lambda: handle(native, payload, load(home, environ), home), passing(native), environ)


def main() -> None:
    """Run the hook process: argv names the event, stdin holds the payload, stdout gets one response."""
    hook.main(KIT_ROOT, sys.argv[1:], runner.services, run, passing)


if __name__ == "__main__":
    main()
