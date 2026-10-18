"""Recorded hook payloads, native response contracts and telemetry writers shared by the test layers.

Fixtures under tests/fixtures/<runtime>/ come from scripts/record_payloads.py: payloads hold
{HOME}/{PROJECT} placeholders that materialize() fills with a temporary home. Response
contracts are the hook output schemas embedded in the codex binary (recorded beside the
payloads) and, for Cursor and Claude Code, the documented response fields as JSON schemas.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUNTIMES = ("cursor", "codex", "claude")
HOOK_SECONDS = 30
STRING = {"type": "string"}
COMMON_CLAUDE = {"continue": {"type": "boolean"}, "stopReason": STRING, "suppressOutput": {"type": "boolean"},
                 "systemMessage": STRING}
BLOCK = {"decision": {"enum": ["block"]}, "reason": STRING}


def obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required)}


def claude_specific(event: str, **extra) -> dict:
    return {"hookSpecificOutput": obj({"hookEventName": {"const": event}, "additionalContext": STRING, **extra},
                                      ("hookEventName",))}


CURSOR_RESPONSES = {
    "sessionStart": obj({"env": {"type": "object"}, "additional_context": STRING}),
    "beforeSubmitPrompt": obj({"continue": {"type": "boolean"}, "user_message": STRING}, ("continue",)),
    "preToolUse": obj({"permission": {"enum": ["allow", "deny", "ask"]}, "user_message": STRING,
                       "agent_message": STRING, "updated_input": {"type": "object"}}),
    "postToolUse": obj({"additional_context": STRING, "updated_mcp_tool_output": {}}),
    "stop": obj({"followup_message": STRING}),
}
CLAUDE_RESPONSES = {
    "SessionStart": obj({**COMMON_CLAUDE, **claude_specific("SessionStart")}),
    "UserPromptSubmit": obj({**COMMON_CLAUDE, **BLOCK, **claude_specific("UserPromptSubmit")}),
    "PostToolUse": obj({**COMMON_CLAUDE, **BLOCK, **claude_specific("PostToolUse", updatedMCPToolOutput={})}),
    "Stop": obj({**COMMON_CLAUDE, **BLOCK}),
    "SubagentStop": obj({**COMMON_CLAUDE, **BLOCK}),
    "PreCompact": obj(COMMON_CLAUDE),
    "PostCompact": obj(COMMON_CLAUDE),
}


@dataclass(frozen=True)
class Recorded:
    runtime: str
    name: str
    event: str
    payload: dict
    source: str

    @property
    def subagent(self) -> bool:
        return bool(self.payload.get("agent_id") or self.payload.get("parent_tool_call_id"))


def read_fixture(runtime: str, name: str) -> dict:
    return json.loads((FIXTURES / runtime / f"{name}.json").read_text())


def recorded(runtime: str) -> list[Recorded]:
    found = []
    for path in sorted((FIXTURES / runtime).glob("*.json")):
        data = json.loads(path.read_text())
        if "payload" in data:
            found.append(Recorded(runtime, path.stem, data["event"], data["payload"], data["source"]))
    return found


def materialize(value: object, home: Path, project: Path) -> object:
    if isinstance(value, dict):
        return {key: materialize(item, home, project) for key, item in value.items()}
    if isinstance(value, list):
        return [materialize(item, home, project) for item in value]
    if isinstance(value, str):
        return value.replace("{PROJECT}", str(project)).replace("{HOME}", str(home))
    return value


def payload(runtime: str, name: str, home: Path, project: Path, **overrides) -> dict:
    """A recorded payload with real paths filled in; overrides replace or (with None) remove fields."""
    result = materialize(read_fixture(runtime, name)["payload"], home, project)
    for key, value in overrides.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def schema_problems(value: object, schema: object, root: dict, where: str = "$") -> list[str]:
    """The draft-07 subset the recorded schemas use: type, const, enum, properties, required,
    additionalProperties, $ref and allOf/anyOf."""
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [f"{where}: not allowed"]
    if "$ref" in schema:
        return schema_problems(value, resolve(root, schema["$ref"]), root, where)
    problems = [p for part in schema.get("allOf", []) for p in schema_problems(value, part, root, where)]
    if "anyOf" in schema and all(schema_problems(value, part, root, where) for part in schema["anyOf"]):
        problems.append(f"{where}: matches no anyOf branch")
    if "const" in schema and value != schema["const"]:
        problems.append(f"{where}: {value!r} != {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where}: {value!r} not in {schema['enum']}")
    if "type" in schema and not type_matches(value, schema["type"]):
        return problems + [f"{where}: {type(value).__name__} is not {schema['type']}"]
    if isinstance(value, dict):
        problems += object_problems(value, schema, root, where)
    return problems


def object_problems(value: dict, schema: dict, root: dict, where: str) -> list[str]:
    properties = schema.get("properties", {})
    problems = [f"{where}: missing {key}" for key in schema.get("required", []) if key not in value]
    extra = schema.get("additionalProperties", True)
    for key, item in value.items():
        if key in properties:
            problems += schema_problems(item, properties[key], root, f"{where}.{key}")
        elif extra is False:
            problems.append(f"{where}: unexpected key {key!r}")
        elif isinstance(extra, dict):
            problems += schema_problems(item, extra, root, f"{where}.{key}")
    return problems


def resolve(root: dict, ref: str) -> dict:
    node = root
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def type_matches(value: object, expected: str | list) -> bool:
    kinds = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}
    for kind in [expected] if isinstance(expected, str) else expected:
        if kind == "integer" and type(value) is int or kind == "number" and type(value) in (int, float):
            return True
        if kind in kinds and type(value) is kinds[kind]:
            return True
    return False


def codex_schema(event: str, side: str) -> dict:
    name = re.sub(r"(?<!^)([A-Z])", r"-\1", event).lower()
    return read_fixture("codex", f"schemas/{name}.command.{side}")


def response_schema(runtime: str, event: str) -> dict:
    if runtime == "codex":
        return codex_schema(event, "output")
    return (CURSOR_RESPONSES if runtime == "cursor" else CLAUDE_RESPONSES)[event]


def response_problems(runtime: str, event: str, response: object) -> list[str]:
    schema = response_schema(runtime, event)
    return schema_problems(response, schema, schema)


def hook_env(home: Path, extra: dict | None = None) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("CONTEXT_GUARD_") and key != "PYTHONPATH"}
    env.update({"HOME": str(home), **(extra or {})})
    return env


@dataclass(frozen=True)
class HookRun:
    returncode: int
    stdout: str
    stderr: str

    def response(self) -> object:
        return json.loads(self.stdout)


def run_hook(command: list[str] | str, data: object, home: Path, cwd: Path | None = None,
             env: dict | None = None) -> HookRun:
    """Run one hook process the way a runtime does: payload on stdin, JSON on stdout."""
    stdin = data if isinstance(data, str) else json.dumps(data)
    result = subprocess.run(command, input=stdin, capture_output=True, text=True, shell=isinstance(command, str),
                            cwd=cwd or home, env=hook_env(home, env), timeout=HOOK_SECONDS, check=False)
    return HookRun(result.returncode, result.stdout, result.stderr)


def source_hook(runtime: str, event: str) -> list[str]:
    return [sys.executable, str(ROOT / "adapters" / runtime / "hooks.py"), event]


def cursor_db(home: Path) -> Path:
    configured = json.loads((ROOT / "adapters/cursor/config.json").read_text())["telemetry"]["platform_state_db"]
    return home / configured.get(sys.platform, configured["linux"]).removeprefix("~/")


def write_cursor_chat(home: Path, conversation: str, tokens: int, limit: int = 200_000) -> None:
    record = dict(read_fixture("cursor", "composer-data")["record"], contextTokensUsed=tokens,
                  contextTokenLimit=limit, contextUsagePercent=100 * tokens / limit,
                  lastUpdatedAt=dt.datetime.now().timestamp() * 1000)
    database = cursor_db(home)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("create table if not exists cursorDiskKV (key text primary key, value blob)")
        connection.execute("insert or replace into cursorDiskKV values (?, ?)",
                           ("composerData:" + conversation, json.dumps(record)))
    connection.close()


def proto_varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    return bytes(out + bytes([value]))


def proto_field(number: int, value: int | bytes) -> bytes:
    if isinstance(value, int):
        return proto_varint(number << 3) + proto_varint(value)
    return proto_varint(number << 3 | 2) + proto_varint(len(value)) + value


def cli_root(tokens: int | None = None, window: int | None = None) -> bytes:
    """A CLI root blob around the recorded usage message; given counts are appended, and the last value wins."""
    usage = bytes.fromhex(read_fixture("cursor", "cli-store")["record"]["usage_hex"])
    usage += (proto_field(1, tokens) if tokens is not None else b"") + (
        proto_field(2, window) if window is not None else b"")
    blob_ids = b"".join(proto_field(1, bytes([index]) * 32) for index in range(2))
    return blob_ids + proto_field(5, usage) + proto_field(10, 1) + proto_field(26, 1_792_346_409_316)


def write_cli_chat(home: Path, conversation: str, tokens: int | None = None, window: int | None = None,
                   meta: str | None = None, root: bytes | None = None) -> Path:
    """A Cursor CLI store.db under the default chats dir; meta (hex text) and root override the valid ones."""
    chats = json.loads((ROOT / "adapters/cursor/config.json").read_text())["telemetry"]["cli_chats"]
    database = home / chats.removeprefix("~/") / "0123456789abcdef0123456789abcdef" / conversation / "store.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    record = {"agentId": conversation, "latestRootBlobId": "f" * 64, "name": "<scrubbed>", "mode": "default",
              "createdAt": 1_792_346_000_000, "lastUsedModel": "default"}
    with sqlite3.connect(database) as connection:
        connection.execute("create table if not exists meta (key text primary key, value text)")
        connection.execute("create table if not exists blobs (id text primary key, data blob)")
        connection.execute("insert or replace into meta values ('0', ?)",
                           (json.dumps(record).encode().hex() if meta is None else meta,))
        connection.execute("insert or replace into blobs values (?, ?)",
                           ("f" * 64, cli_root(tokens, window) if root is None else root))
    connection.close()
    return database


def token_count(tokens: int, window: int | None = 258_400) -> dict:
    record = json.loads(json.dumps(read_fixture("codex", "rollout-token-count")["record"]))
    info = record["payload"]["info"]
    info["last_token_usage"] = dict(info["last_token_usage"], input_tokens=tokens, output_tokens=0,
                                    cached_input_tokens=0, total_tokens=tokens)
    if window is None:
        info.pop("model_context_window")
    else:
        info["model_context_window"] = window
    return record


def write_rollout(path: Path, tokens: int, window: int | None = 258_400, *extra: dict | str) -> None:
    """Append a token_count then any extra records; a str extra is written verbatim (partial or corrupt lines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for record in (token_count(tokens, window), *extra):
            handle.write((record if isinstance(record, str) else json.dumps(record)) + "\n")


def codex_child_rollout(parent: Path, agent_id: str) -> Path:
    return parent.parent / f"rollout-2026-10-18T10-00-00-{agent_id}.jsonl"


def assistant_usage(tokens: int, model: str | None = None, timestamp: str | None = None, **usage: int) -> dict:
    """An assistant record whose live context is `tokens`; keyword arguments override other usage fields."""
    record = json.loads(json.dumps(read_fixture("claude", "transcript-assistant")["record"]))
    record["timestamp"] = timestamp or dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    base = {"input_tokens": tokens, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    record["message"]["usage"] = {**record["message"]["usage"], **base, **usage}
    if model:
        record["message"]["model"] = model
    return record


def compact_boundary(trigger: str, pre_tokens: int, timestamp: str) -> dict:
    return {"type": "system", "subtype": "compact_boundary", "timestamp": timestamp,
            "compactMetadata": {"trigger": trigger, "preTokens": pre_tokens}}


def append_transcript(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def claude_child_transcript(parent: Path, agent_id: str) -> Path:
    return parent.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"


def seed_handoff(home: Path, project: Path, title: str = "Recorded fixture task") -> Path:
    store = home / ".context-guard" / "handoffs"
    store.mkdir(parents=True, exist_ok=True)
    path = store / "recorded-fixture-task-0123456789abcdef.md"
    path.write_text(f"# {title}\nStatus: open\nWorkspace: {project}\n\n## Goal\nKeep the fixtures honest.\n")
    return path


def events(home: Path, runtime: str) -> list[dict]:
    path = home / ".context-guard" / "state" / runtime / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class RuntimeHookCase(unittest.TestCase):
    """A temporary home with a git project, the handoff store and the runtime's state directory."""
    runtime: str

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.project = self.home / "dev" / "project"
        (self.project / ".git").mkdir(parents=True)
        self.store = self.home / ".context-guard" / "handoffs"
        self.state_dir = self.home / ".context-guard" / "state" / self.runtime

    def logged(self) -> list[dict]:
        return events(self.home, self.runtime)
