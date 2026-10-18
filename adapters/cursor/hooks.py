"""Cursor hook entry: `python hooks.py <hookEventName>` reads the payload on stdin.

Token counts come from Cursor's own chat record (composerData in state.vscdb). Cursor CLI
chats are not in that database; they keep their own store at
`<cli_chats>/<workspace hash>/<conversation_id>/store.db`, whose latest root blob (protobuf)
holds the context usage message in field 5: tokens used (1) and context window (2). Missing or
changed telemetry and invalid payloads fail open.
"""
import json
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[2]
if str(KIT_ROOT) not in sys.path:
    sys.path.insert(0, str(KIT_ROOT))

from adapters.cursor import runner
from core import config as config_mod, deadline, events, handoffs, policy, state as state_mod
from core.observation import (BLOCK, CONTEXT, NOTE, OTHER, PASS, PROMPT, READ, SESSION, SPAWN, STOP, TOOL, WRITE,
                              Decision, Observation, ToolUse)
from core.config import valid_text
from core.render import RenderError, load_roles
from core.runner import service
from core.runner.provider import INTERNAL_ENV

RUNTIME = "cursor"
PROMPT_HOOK = "beforeSubmitPrompt"
ADAPTER_CONFIG = Path(__file__).resolve().parent / "config.json"
COMPOSER_KEY = "composerData:"
TEXT_FIELDS = ("conversation_id", "session_id", "generation_id", "subagent_id", "parent_tool_call_id", "tool_use_id",
               "prompt", "model", "status", "tool_name", "cwd")
PATH_FIELDS = ("file_path", "path", "target_file")
EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")
PENDING_NOTE, PENDING_TIER = "pending_note", "pending_tier"
CLI_ID = re.compile(r"[\w-]+")
CLI_META_KEY, CLI_ROOT_KEY = "0", "latestRootBlobId"
CLI_USAGE_FIELD, CLI_TOKENS_FIELD, CLI_WINDOW_FIELD = 5, 1, 2
VARINT, LENGTH_DELIMITED = 0, 2
FIXED_SIZES = {1: 8, 5: 4}
SPAWN_RECORD = "spawn\0"


@dataclass(frozen=True)
class Telemetry:
    tokens: int | None
    window: int | None
    updated_ms: float | None


NO_TELEMETRY = Telemetry(None, None, None)


def count(value: object, integer: bool = True) -> bool:
    if type(value) is int:
        return value >= 0
    return not integer and type(value) is float and math.isfinite(value) and value >= 0


def validate_payload(payload: object) -> dict:
    """Reject payload shapes the hook cannot interpret; the caller fails open."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    for key in TEXT_FIELDS:
        if payload.get(key) is not None and not valid_text(payload[key]):
            raise ValueError(f"{key} must be text")
    roots = payload.get("workspace_roots")
    if roots is not None and (not isinstance(roots, list) or not all(valid_text(root) for root in roots)):
        raise ValueError("workspace_roots must be a list of text")
    if payload.get("tool_input") is not None and not isinstance(payload["tool_input"], dict):
        raise ValueError("tool_input must be an object")
    if "loop_count" in payload and not count(payload["loop_count"]):
        raise ValueError("loop_count must be a non-negative int")
    return payload


def event_spec(config: dict, native: str) -> dict:
    spec = config["hook_events"].get(native)
    if not isinstance(spec, dict):
        raise ValueError(f"unknown Cursor hook event {native!r}")
    return spec


def state_db_path(config: dict, home: Path) -> Path | None:
    conf = config["telemetry"]
    configured = conf["state_db"] or conf["platform_state_db"].get(sys.platform, "")
    return config_mod.expand_path(configured, home) if configured else None


def parse_chat(value: object) -> Telemetry | None:
    """Validate Cursor's chat record; any changed field shape means no telemetry."""
    if not isinstance(value, dict):
        return None
    tokens, window, updated = (value.get(key) for key in ("contextTokensUsed", "contextTokenLimit", "lastUpdatedAt"))
    if any(item is not None and not count(item) for item in (tokens, window)):
        return None
    if updated is not None and not count(updated, integer=False):
        return None
    return Telemetry(tokens, window or None, updated or None)


def read_chat(database: Path | None, conversation_id: str, timeout: float) -> Telemetry | None:
    if database is None or not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("select value from cursorDiskKV where key = ?",
                                 (COMPOSER_KEY + conversation_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not row or not isinstance(row[0], (str, bytes)):
        return None
    try:
        return parse_chat(json.loads(row[0]))
    except (ValueError, UnicodeError):
        return None


def cli_chats_path(config: dict, home: Path) -> Path | None:
    configured = config["telemetry"]["cli_chats"]
    return config_mod.expand_path(configured, home) if configured else None


def varint(data: bytes, index: int) -> tuple[int, int]:
    value = shift = 0
    while index < len(data) and shift < 64:
        byte = data[index]
        value, index, shift = value | (byte & 0x7F) << shift, index + 1, shift + 7
        if byte < 0x80:
            return value, index
    raise ValueError("truncated varint")


def proto_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """(field number, wire type, value) for every field; an unknown wire type or truncation raises."""
    fields, index = [], 0
    while index < len(data):
        key, index = varint(data, index)
        number, wire = key >> 3, key & 7
        if wire == VARINT:
            value, index = varint(data, index)
        elif wire == LENGTH_DELIMITED or wire in FIXED_SIZES:
            size, index = varint(data, index) if wire == LENGTH_DELIMITED else (FIXED_SIZES[wire], index)
            if index + size > len(data):
                raise ValueError("truncated field")
            value, index = data[index:index + size], index + size
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.append((number, wire, value))
    return fields


def cli_usage(root: bytes) -> tuple[int | None, int | None] | None:
    """Tokens and window from the root blob's usage message; a zero count is omitted on the wire."""
    usage = [value for number, wire, value in proto_fields(root)
             if number == CLI_USAGE_FIELD and wire == LENGTH_DELIMITED]
    if not usage:
        return None
    counts = {number: value for number, wire, value in proto_fields(usage[-1]) if wire == VARINT}
    return counts.get(CLI_TOKENS_FIELD), counts.get(CLI_WINDOW_FIELD) or None


def cli_root_id(meta: object) -> str | None:
    if not isinstance(meta, str):
        return None
    try:
        record = json.loads(bytes.fromhex(meta))
    except ValueError:
        return None
    root = record.get(CLI_ROOT_KEY) if isinstance(record, dict) else None
    return root if valid_text(root) and root else None


def read_cli_root(database: Path, timeout: float) -> bytes | None:
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
    if chats is None or not CLI_ID.fullmatch(conversation_id) or not chats.is_dir():
        return None
    return next(iter(sorted(chats.glob(f"*/{conversation_id}/store.db"))), None)


def updated_ms(database: Path) -> float:
    """Writes land in the WAL during a session; the main file changes only at checkpoints."""
    paths = (database, database.with_name(database.name + "-wal"))
    return max(path.stat().st_mtime for path in paths if path.exists()) * 1000


def read_cli_chat(chats: Path | None, conversation_id: str, timeout: float) -> Telemetry | None:
    database = cli_store(chats, conversation_id)
    root = read_cli_root(database, timeout) if database else None
    if root is None:
        return None
    try:
        usage = cli_usage(root)
        return Telemetry(*usage, updated_ms(database)) if usage else None
    except (ValueError, OSError):
        return None


def spawn_tier(config: dict, role: str, model: str) -> str | None:
    """The tier whose call slug was passed, else the role's default (its agent file's model)."""
    if model:
        return next((tier for tier, profile in config["tiers"].items() if profile.get("call") == model), None)
    try:
        found = load_roles().get(role)
    except RenderError:
        return None
    return found.default_tier if found else None


def spawn_detail(config: dict, tool_input: dict) -> dict:
    role = tool_input.get("subagent_type") if valid_text(tool_input.get("subagent_type")) else ""
    model = tool_input.get("model") if valid_text(tool_input.get("model")) else ""
    effort = EFFORT_SUFFIX.search(model)
    return {"role": role or None, "tier": spawn_tier(config, role, model), "model": model or None,
            "effort": effort[1] if effort else None}


def shell_use(config: dict, payload: dict, home: Path) -> ToolUse:
    tool_input = payload["tool_input"] if isinstance(payload.get("tool_input"), dict) else {}
    command = tool_input.get("command")
    access = valid_text(command) and handoffs.shell_access(
        command, workspace(payload), home, config_mod.handoff_dir(config, home))
    return ToolUse(payload["tool_name"], *access) if access else ToolUse(payload["tool_name"])


def tool_use(config: dict, payload: dict, spawn_only: bool, home: Path) -> ToolUse | None:
    """Classify the native tool; spawns are logged once, before the subagent runs."""
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


def session_id(payload: dict) -> str:
    return payload.get("conversation_id") or payload.get("session_id") or ""


def workspace(payload: dict) -> str:
    roots = payload.get("workspace_roots") or []
    return roots[0] if roots else payload.get("cwd") or ""


def stop_skipped(payload: dict) -> bool:
    """Only a completed turn that is not our own follow-up loop gets bookkeeping."""
    return payload.get("status", "completed") != "completed" or payload.get("loop_count", 0) > 0


def session_file(directory: Path, conversation: str) -> Path:
    return directory / f"{state_mod.session_key(RUNTIME, conversation)}.json"


def telemetry_for(config: dict, home: Path, event: str, spawn_only: bool, payload: dict) -> Telemetry:
    if event == SESSION or spawn_only:
        return NO_TELEMETRY
    conversation, timeout = session_id(payload), config["telemetry"]["timeout_seconds"]
    return (read_chat(state_db_path(config, home), conversation, timeout)
            or read_cli_chat(cli_chats_path(config, home), conversation, timeout) or NO_TELEMETRY)


def spawn_record(directory: Path, call: str) -> Path:
    return session_file(directory, SPAWN_RECORD + call)


def remember_spawn(directory: Path, payload: dict, tool: ToolUse) -> None:
    """Subagent tool hooks carry only the parent's Task `tool_use_id`, so the spawn keeps the role under it."""
    call, role = payload.get("tool_use_id"), tool.detail.get("role")
    if call and role:
        with state_mod.session_state(directory, RUNTIME, SPAWN_RECORD + call) as record:
            record.setdefault("adapter", {})["role"] = role


def spawned_role(directory: Path, payload: dict) -> str | None:
    call = payload.get("parent_tool_call_id")
    if not call:
        return None
    role = state_mod.validated(state_mod.read_json(spawn_record(directory, call))).get("adapter", {}).get("role")
    return role if valid_text(role) and role else None


def observe(native: str, payload: object, config: dict, home: Path, now: float | None = None) -> Observation | None:
    """Translate one Cursor hook payload; None means there is nothing to evaluate."""
    spec, payload = event_spec(config, native), validate_payload(payload)
    event, spawn_only = spec["event"], bool(spec.get("spawn_only"))
    tool = tool_use(config, payload, spawn_only, home) if event == TOOL else None
    if (event == TOOL and tool is None) or (event == STOP and stop_skipped(payload)):
        return None
    conversation = session_id(payload)
    current = time.time() if now is None else now
    chat = telemetry_for(config, home, event, spawn_only, payload)
    fallback = config["telemetry"]["window_fallback"] or None
    idle = max(0.0, current - chat.updated_ms / 1000) if chat.updated_ms else None
    return Observation(RUNTIME, conversation, event, tokens=chat.tokens, window=chat.window or fallback,
                       model=payload.get("model") or None, idle_seconds=idle,
                       is_subagent=bool(payload.get("parent_tool_call_id")),
                       role=spawned_role(config_mod.state_dir(config, home, RUNTIME), payload), cwd=workspace(payload), prompt=payload.get("prompt") if event == PROMPT else None, tool=tool)


def take_pending(state: dict) -> tuple[str, str] | None:
    """The waiting prompt note and its tier; a note whose tier was re-armed since is dropped."""
    adapter = state.setdefault("adapter", {})
    message, tier = adapter.pop(PENDING_NOTE, None), adapter.pop(PENDING_TIER, None)
    if not isinstance(message, str) or "tier" not in state:
        return None
    return message, tier if isinstance(tier, str) else state["tier"]


def carry_prompt_note(obs: Observation, decision: Decision, directory: Path, config: dict) -> Decision:
    """beforeSubmitPrompt cannot add model context, so a prompt note waits for the next tool result or
    session start (stop cannot carry it either); its delivery is logged as `note_delivered`."""
    if obs.is_subagent or obs.event not in (PROMPT, TOOL, SESSION) or (obs.tool and obs.tool.action == SPAWN):
        return decision
    if obs.event == PROMPT and decision.kind != NOTE:
        return decision
    with state_mod.session_state(directory, obs.runtime, obs.session_id, commit=True) as state:
        if obs.event == PROMPT:
            state.setdefault("adapter", {}).update({PENDING_NOTE: decision.message, PENDING_TIER: decision.tier})
            return decision
        pending = take_pending(state)
    if pending is None or policy.RANK.get(decision.tier, 0) >= policy.RANK.get(pending[1], 0) > 0:
        return decision
    policy.log_events(config, directory, [events.record("note_delivered", obs, pending[1])])
    message = "\n\n".join(part for part in (pending[0], decision.message) if part)
    return replace(decision, kind=NOTE if obs.event == TOOL else CONTEXT, message=message)


def respond(decision: Decision, event: str) -> dict:
    """Render a decision as the Cursor hook response for its core event."""
    if event == PROMPT:
        if decision.kind == BLOCK:
            return {"continue": False, "user_message": decision.message}
        return {"continue": True}
    if decision.kind == PASS or not decision.message:
        return {}
    if event == SESSION and decision.kind == CONTEXT:
        return {"additional_context": decision.message}
    if event == TOOL and decision.kind == NOTE:
        return {"additional_context": decision.message}
    if event == STOP and decision.kind == CONTEXT:
        return {"followup_message": decision.message}
    return {}


def handle(native: str, payload: object, home: Path, config: dict) -> dict:
    obs = observe(native, payload, config, home)
    if obs is None:
        return respond(Decision(), event_spec(config, native)["event"])
    directory = config_mod.state_dir(config, home, RUNTIME)
    if obs.event == SESSION and not obs.is_subagent:
        state_mod.prune(directory, config["paths"]["prune_days"])
    if obs.tool is not None and obs.tool.action == SPAWN:
        remember_spawn(directory, payload, obs.tool)
    routing = service.route(obs, config, lambda: runner.services(home))
    if routing.blocked:
        return respond(service.combine(Decision(), routing), obs.event)
    decision = service.combine(policy.evaluate(obs, config, home), routing)
    response = respond(carry_prompt_note(obs, decision, directory, config), obs.event)
    if obs.event == TOOL and "additional_context" in response:
        policy.log_delivered(obs, decision, config, home)
    return response


def load(home: Path, environ: dict | None = None) -> dict:
    return config_mod.load_config(config_mod.sources_for(RUNTIME, home, ADAPTER_CONFIG), environ)


def passing(native: str) -> dict:
    return {"continue": True} if native == PROMPT_HOOK else {}


def evaluate(native: str, payload: object, home: Path, environ: dict | None = None) -> dict:
    """Evaluate one hook; invalid input or I/O failure passes."""
    environment = os.environ if environ is None else environ
    if environment.get(INTERNAL_ENV) == "1" or config_mod.foreign_home(KIT_ROOT, environment):
        return passing(native)
    try:
        return handle(native, payload, home, load(home, environ))
    except (ValueError, OSError):
        return passing(native)


def run(native: str, payload: object, home: Path, environ: dict | None = None,
        seconds: float = deadline.HOOK_WORK_SECONDS) -> dict:
    """Evaluate one hook inside the deadline."""
    return deadline.run_within(lambda: evaluate(native, payload, home, environ), passing(native), seconds)


def read_payload(stream) -> object:
    try:
        return json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return None


def main() -> None:
    home = config_mod.resolve_home(KIT_ROOT)
    if service.is_worker_call(sys.argv[1:]):
        raise SystemExit(runner.services(home).work(sys.argv[1:]))
    native = sys.argv[1] if len(sys.argv) > 1 else ""
    fallback = passing(native)
    response = deadline.boundary(
        lambda: deadline.run_within(lambda: evaluate(native, read_payload(sys.stdin), home), fallback),
        fallback, native)
    print(json.dumps(response), flush=True)
    service.confirm_staged(response, fallback)


if __name__ == "__main__":
    main()
