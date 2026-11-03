"""Recorded hook payloads, native response contracts and telemetry writers shared by the test layers.

Fixtures under tests/fixtures/<runtime>/ come from scripts/record_payloads.py: payloads hold
{HOME}/{PROJECT} placeholders that materialize() fills with a temporary home. Response
contracts are the hook output schemas embedded in the codex binary (recorded beside the
payloads) and, for Cursor and Claude Code, the documented response fields as JSON schemas.
"""

import datetime as dt
import functools
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest import mock

from core import deadline

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUNTIMES = ("cursor", "codex", "genie", "claude")
HOOK_SECONDS = 30
WAKE_PARENT_ENV = "CONTEXT_GUARD_SUBAGENTS_WAKE_PARENT"
VARINT_PAYLOAD = 0x7F
STRING = {"type": "string"}
COMMON_CLAUDE = {
    "continue": {"type": "boolean"},
    "stopReason": STRING,
    "suppressOutput": {"type": "boolean"},
    "systemMessage": STRING,
}
BLOCK = {"decision": {"enum": ["block"]}, "reason": STRING}


def obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    """A closed JSON object schema.

    Args:
        properties: Property name to schema.
        required: Property names that must be present.

    Returns:
        The schema, rejecting any property not listed.
    """
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required)}


def claude_specific(event: str, **extra: object) -> dict:
    """The Claude Code `hookSpecificOutput` property for one event.

    Args:
        event: The hook event name the output must echo.
        **extra: Additional event-specific property schemas.

    Returns:
        A properties fragment holding the `hookSpecificOutput` schema.
    """
    return {
        "hookSpecificOutput": obj(
            {"hookEventName": {"const": event}, "additionalContext": STRING, **extra}, ("hookEventName",)
        )
    }


CURSOR_RESPONSES = {
    "sessionStart": obj({"env": {"type": "object"}, "additional_context": STRING}),
    "beforeSubmitPrompt": obj({"continue": {"type": "boolean"}, "user_message": STRING}, ("continue",)),
    "preToolUse": obj(
        {
            "permission": {"enum": ["allow", "deny", "ask"]},
            "user_message": STRING,
            "agent_message": STRING,
            "updated_input": {"type": "object"},
        }
    ),
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
    """One recorded hook payload fixture.

    Args:
        runtime: The runtime that produced the payload.
        name: The fixture file stem.
        event: The hook event the payload was delivered to.
        payload: The scrubbed payload with path placeholders.
        source: Where the recorder found the payload.
    """

    runtime: str
    name: str
    event: str
    payload: dict
    source: str

    @property
    def subagent(self) -> bool:
        """Whether the payload was sent from inside a subagent."""
        return bool(self.payload.get("agent_id") or self.payload.get("parent_tool_call_id"))


def read_fixture(runtime: str, name: str) -> dict:
    """Load one fixture file.

    Args:
        runtime: The fixture directory under tests/fixtures.
        name: The file stem, optionally with a subdirectory.

    Returns:
        The parsed fixture.
    """
    return json.loads((FIXTURES / runtime / f"{name}.json").read_text())


def recorded(runtime: str) -> list[Recorded]:
    """Every recorded payload fixture of a runtime, sorted by file name.

    Args:
        runtime: The fixture directory under tests/fixtures.

    Returns:
        The payload fixtures; telemetry and schema fixtures are skipped.
    """
    found = []
    for path in sorted((FIXTURES / runtime).glob("*.json")):
        data = json.loads(path.read_text())
        if "payload" in data:
            found.append(Recorded(runtime, path.stem, data["event"], data["payload"], data["source"]))
    return found


def materialize(value: object, home: Path, project: Path) -> object:
    """Replace the {HOME} and {PROJECT} placeholders throughout a JSON value.

    Args:
        value: The JSON value to rewrite.
        home: The path substituted for {HOME}.
        project: The path substituted for {PROJECT}.

    Returns:
        A copy of the value with every string filled in.
    """
    if isinstance(value, dict):
        return {key: materialize(item, home, project) for key, item in value.items()}
    if isinstance(value, list):
        return [materialize(item, home, project) for item in value]
    if isinstance(value, str):
        return value.replace("{PROJECT}", str(project)).replace("{HOME}", str(home))
    return value


def payload(runtime: str, name: str, home: Path, project: Path, **overrides: object) -> dict:
    """A recorded payload with real paths filled in.

    Args:
        runtime: The fixture directory under tests/fixtures.
        name: The fixture file stem.
        home: The path substituted for {HOME}.
        project: The path substituted for {PROJECT}.
        **overrides: Fields to replace; a None value removes the field.

    Returns:
        The materialized payload.
    """
    result = materialize(read_fixture(runtime, name)["payload"], home, project)
    for key, value in overrides.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def schema_problems(value: object, schema: object, root: dict, where: str = "$") -> list[str]:
    """Validate a value against the draft-07 subset the recorded schemas use.

    The subset is type, const, enum, properties, required, additionalProperties, $ref and allOf/anyOf.

    Args:
        value: The JSON value to check.
        schema: The schema, or a boolean schema.
        root: The document `$ref` pointers resolve against.
        where: The JSON path of `value`, used in messages.

    Returns:
        One message per violation; empty when the value conforms.
    """
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
        return [*problems, f"{where}: {type(value).__name__} is not {schema['type']}"]
    if isinstance(value, dict):
        problems += object_problems(value, schema, root, where)
    return problems


def object_problems(value: dict, schema: dict, root: dict, where: str) -> list[str]:
    """Validate an object's required, declared and additional properties.

    Args:
        value: The object to check.
        schema: The object schema.
        root: The document `$ref` pointers resolve against.
        where: The JSON path of `value`, used in messages.

    Returns:
        One message per violation; empty when the object conforms.
    """
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
    """Follow a local JSON pointer.

    Args:
        root: The schema document.
        ref: A `#/`-prefixed pointer into it.

    Returns:
        The referenced schema.
    """
    node = root
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def type_matches(value: object, expected: str | list) -> bool:
    """Whether a value has one of the JSON schema types.

    Args:
        value: The JSON value to check.
        expected: A type name or a list of type names.

    Returns:
        True when any listed type matches; booleans are not integers.
    """
    kinds = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}
    for kind in [expected] if isinstance(expected, str) else expected:
        if (kind == "integer" and type(value) is int) or (kind == "number" and type(value) in {int, float}):
            return True
        if kind in kinds and type(value) is kinds[kind]:
            return True
    return False


def compatible_schema(runtime: str, event: str, side: str) -> dict:
    """A Codex-compatible hook schema recorded from a runtime binary.

    Args:
        runtime: `codex` or `genie`.
        event: The hook event name, e.g. `UserPromptSubmit`.
        side: `input` or `output`.

    Returns:
        The JSON schema.
    """
    name = re.sub(r"(?<!^)([A-Z])", r"-\1", event).lower()
    return read_fixture(runtime, f"schemas/{name}.command.{side}")


def codex_schema(event: str, side: str) -> dict:
    """Return a recorded Codex hook schema.

    Args:
        event: The hook event name.
        side: `input` or `output`.

    Returns:
        The recorded JSON schema.
    """
    return compatible_schema("codex", event, side)


def response_schema(runtime: str, event: str) -> dict:
    """The native response schema of one hook event.

    Args:
        runtime: The runtime answering the hook.
        event: The hook event name.

    Returns:
        The JSON schema the hook's stdout must satisfy.
    """
    if runtime in ("codex", "genie"):
        return compatible_schema(runtime, event, "output")
    return (CURSOR_RESPONSES if runtime == "cursor" else CLAUDE_RESPONSES)[event]


def response_problems(runtime: str, event: str, response: object) -> list[str]:
    """Validate a hook response against its native schema.

    Args:
        runtime: The runtime answering the hook.
        event: The hook event name.
        response: The parsed hook stdout.

    Returns:
        One message per violation; empty when the response conforms.
    """
    schema = response_schema(runtime, event)
    return schema_problems(response, schema, schema)


def hook_env(home: Path, extra: dict | None = None) -> dict:
    """The process environment for a hook under a temporary home.

    Args:
        home: The temporary home, exported as HOME.
        extra: Variables added after the defaults.

    Returns:
        The current environment without context-guard variables, PYTHONPATH or runtime home overrides, with the
        Codex root waker off unless `extra` turns it on, so no hook starts a detached process.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CONTEXT_GUARD_")
        and key not in ("PYTHONPATH", "CODEX_HOME", "GENIE_HOME", "CLAUDE_CONFIG_DIR")
    }
    env.update({"HOME": str(home), WAKE_PARENT_ENV: "0", **(extra or {})})
    return env


@dataclass(frozen=True)
class HookRun:
    """The outcome of one hook process.

    Args:
        returncode: The process exit status.
        stdout: Everything the hook printed on stdout.
        stderr: Everything the hook printed on stderr.
    """

    returncode: int
    stdout: str
    stderr: str

    def response(self) -> object:
        """The parsed native response.

        Returns:
            stdout decoded as JSON.
        """
        return json.loads(self.stdout)


def run_hook(
    command: list[str] | str, data: object, home: Path, cwd: Path | None = None, env: dict | None = None
) -> HookRun:
    """Run one hook process the way a runtime does: payload on stdin, JSON on stdout.

    Args:
        command: An argv list, or a registered shell command line.
        data: The payload; a str is sent verbatim, anything else as JSON.
        home: The temporary home the hook sees.
        cwd: The working directory; defaults to `home`.
        env: Extra environment variables.

    Returns:
        The exit status and captured output.
    """
    stdin = data if isinstance(data, str) else json.dumps(data)
    result = subprocess.run(  # noqa: S603
        command,
        input=stdin,
        capture_output=True,
        text=True,
        shell=isinstance(command, str),
        cwd=cwd or home,
        env=hook_env(home, env),
        timeout=HOOK_SECONDS,
        check=False,
    )
    return HookRun(result.returncode, result.stdout, result.stderr)


def source_hook(runtime: str, event: str) -> list[str]:
    """The argv that runs a runtime's hook entry from the source tree.

    Args:
        runtime: The adapter to run.
        event: The hook event argument.

    Returns:
        The interpreter, the adapter's hooks.py and the event.
    """
    return [sys.executable, str(ROOT / "adapters" / runtime / "hooks.py"), event]


def cursor_db(home: Path) -> Path:
    """The Cursor IDE state database under a home, for the current platform.

    Args:
        home: The temporary home.

    Returns:
        The database path the Cursor adapter is configured to read.
    """
    configured = json.loads((ROOT / "adapters/cursor/config.json").read_text())["telemetry"]["platform_state_db"]
    return home / configured.get(sys.platform, configured["linux"]).removeprefix("~/")


def write_cursor_chat(home: Path, conversation: str, tokens: int, limit: int = 200_000) -> None:
    """Store a Cursor IDE composer record with the given context usage, updated now.

    Args:
        home: The temporary home.
        conversation: The composer (conversation) id.
        tokens: The context tokens used.
        limit: The context token limit.
    """
    record = dict(
        read_fixture("cursor", "composer-data")["record"],
        contextTokensUsed=tokens,
        contextTokenLimit=limit,
        contextUsagePercent=100 * tokens / limit,
        lastUpdatedAt=dt.datetime.now(dt.UTC).timestamp() * 1000,
    )
    database = cursor_db(home)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("create table if not exists cursorDiskKV (key text primary key, value blob)")
        connection.execute(
            "insert or replace into cursorDiskKV values (?, ?)", ("composerData:" + conversation, json.dumps(record))
        )
    connection.close()


def proto_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint.

    Args:
        value: The integer to encode.

    Returns:
        The varint bytes.
    """
    out = bytearray()
    while value > VARINT_PAYLOAD:
        out.append(value & VARINT_PAYLOAD | 0x80)
        value >>= 7
    return bytes(out + bytes([value]))


def proto_field(number: int, value: int | bytes) -> bytes:
    """Encode one protobuf field.

    Args:
        number: The field number.
        value: An int (varint wire type) or bytes (length-delimited).

    Returns:
        The tagged field bytes.
    """
    if isinstance(value, int):
        return proto_varint(number << 3) + proto_varint(value)
    return proto_varint(number << 3 | 2) + proto_varint(len(value)) + value


def cli_root(tokens: int | None = None, window: int | None = None) -> bytes:
    """A Cursor CLI root blob around the recorded usage message.

    Args:
        tokens: Context tokens appended to the usage message; the last value wins.
        window: Context window appended to the usage message; the last value wins.

    Returns:
        The encoded root blob.
    """
    usage = bytes.fromhex(read_fixture("cursor", "cli-store")["record"]["usage_hex"])
    usage += (proto_field(1, tokens) if tokens is not None else b"") + (
        proto_field(2, window) if window is not None else b""
    )
    blob_ids = b"".join(proto_field(1, bytes([index]) * 32) for index in range(2))
    return blob_ids + proto_field(5, usage) + proto_field(10, 1) + proto_field(26, 1_792_346_409_316)


def write_cli_chat(
    home: Path,
    conversation: str,
    tokens: int | None = None,
    window: int | None = None,
    meta: str | None = None,
    root: bytes | None = None,
) -> Path:
    """Write a Cursor CLI store.db under the default chats directory.

    Args:
        home: The temporary home.
        conversation: The agent (conversation) id.
        tokens: Context tokens in the root blob.
        window: Context window in the root blob.
        meta: Hex text replacing the valid meta record.
        root: Bytes replacing the valid root blob.

    Returns:
        The database path.
    """
    chats = json.loads((ROOT / "adapters/cursor/config.json").read_text())["telemetry"]["cli_chats"]
    database = home / chats.removeprefix("~/") / "0123456789abcdef0123456789abcdef" / conversation / "store.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "agentId": conversation,
        "latestRootBlobId": "f" * 64,
        "name": "<scrubbed>",
        "mode": "default",
        "createdAt": 1_792_346_000_000,
        "lastUsedModel": "default",
    }
    with sqlite3.connect(database) as connection:
        connection.execute("create table if not exists meta (key text primary key, value text)")
        connection.execute("create table if not exists blobs (id text primary key, data blob)")
        connection.execute(
            "insert or replace into meta values ('0', ?)",
            (json.dumps(record).encode().hex() if meta is None else meta,),
        )
        connection.execute(
            "insert or replace into blobs values (?, ?)", ("f" * 64, cli_root(tokens, window) if root is None else root)
        )
    connection.close()
    return database


def token_count(tokens: int, window: int | None = 258_400) -> dict:
    """A Codex rollout token_count record.

    Args:
        tokens: The last-turn input tokens.
        window: The model context window; None removes the field.

    Returns:
        A copy of the recorded record with those values.
    """
    record = json.loads(json.dumps(read_fixture("codex", "rollout-token-count")["record"]))
    info = record["payload"]["info"]
    info["last_token_usage"] = dict(
        info["last_token_usage"], input_tokens=tokens, output_tokens=0, cached_input_tokens=0, total_tokens=tokens
    )
    if window is None:
        info.pop("model_context_window")
    else:
        info["model_context_window"] = window
    return record


def write_rollout(path: Path, tokens: int, window: int | None = 258_400, *extra: dict | str) -> None:
    """Append a token_count record, then any extra records, to a Codex rollout.

    Args:
        path: The rollout file.
        tokens: The last-turn input tokens.
        window: The model context window; None omits it.
        *extra: Further records; a str is written verbatim (partial or corrupt lines).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for record in (token_count(tokens, window), *extra):
            handle.write((record if isinstance(record, str) else json.dumps(record)) + "\n")


def codex_child_rollout(parent: Path, agent_id: str) -> Path:
    """The rollout path of a Codex subagent beside its parent rollout.

    Args:
        parent: The parent rollout file.
        agent_id: The subagent id.

    Returns:
        The child rollout path.
    """
    return parent.parent / f"rollout-2026-10-18T10-00-00-{agent_id}.jsonl"


def assistant_usage(tokens: int, model: str | None = None, timestamp: str | None = None, **usage: int) -> dict:
    """A Claude Code assistant transcript record whose live context is `tokens`.

    Args:
        tokens: The input tokens; cache tokens default to zero.
        model: The model name; the recorded one when omitted.
        timestamp: The ISO timestamp; now when omitted.
        **usage: Overrides for other usage fields.

    Returns:
        A copy of the recorded record with those values.
    """
    record = json.loads(json.dumps(read_fixture("claude", "transcript-assistant")["record"]))
    record["timestamp"] = timestamp or dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    base = {"input_tokens": tokens, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    record["message"]["usage"] = {**record["message"]["usage"], **base, **usage}
    if model:
        record["message"]["model"] = model
    return record


def compact_boundary(trigger: str, pre_tokens: int, timestamp: str) -> dict:
    """A Claude Code compact_boundary transcript record.

    Args:
        trigger: `manual` or `auto`.
        pre_tokens: The context tokens before compaction.
        timestamp: The ISO timestamp.

    Returns:
        The system record.
    """
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": timestamp,
        "compactMetadata": {"trigger": trigger, "preTokens": pre_tokens},
    }


def append_transcript(path: Path, *records: dict) -> None:
    """Append JSON lines to a transcript, creating its directory.

    Args:
        path: The transcript file.
        *records: The records to append, one per line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def claude_child_transcript(parent: Path, agent_id: str) -> Path:
    """The transcript path of a Claude Code subagent under its parent session.

    Args:
        parent: The parent transcript file.
        agent_id: The subagent id.

    Returns:
        The child transcript path.
    """
    return parent.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"


def seed_handoff(home: Path, project: Path, title: str = "Recorded fixture task") -> Path:
    """Write an open handoff for a project into the home's handoff store.

    Args:
        home: The temporary home.
        project: The workspace the handoff names.
        title: The handoff title.

    Returns:
        The handoff file.
    """
    store = home / ".context-guard" / "handoffs"
    store.mkdir(parents=True, exist_ok=True)
    path = store / "recorded-fixture-task-0123456789abcdef.md"
    path.write_text(f"# {title}\nStatus: open\nWorkspace: {project}\n\n## Goal\nKeep the fixtures honest.\n")
    return path


def events(home: Path, runtime: str) -> list[dict]:
    """The event log a runtime's hooks wrote under a home.

    Args:
        home: The temporary home.
        runtime: The runtime whose state directory to read.

    Returns:
        The logged records in order; empty when nothing was logged.
    """
    path = home / ".context-guard" / "state" / runtime / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class RuntimeHookCase(unittest.TestCase):
    """A temporary home with a git project, the handoff store and the runtime's state directory."""

    runtime: str

    def setUp(self) -> None:
        """Create the temporary home and project; both are removed after the test."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.project = self.home / "dev" / "project"
        (self.project / ".git").mkdir(parents=True)
        self.store = self.home / ".context-guard" / "handoffs"
        self.state_dir = self.home / ".context-guard" / "state" / self.runtime

    def logged(self) -> list[dict]:
        """The runtime's logged events.

        Returns:
            The event records in order.
        """
        return events(self.home, self.runtime)

    def names(self) -> list[str]:
        """The names of the runtime's logged events.

        Returns:
            The event names in order.
        """
        return [record["event"] for record in self.logged()]


class HookBoundaryChecks:
    """Mixin for a RuntimeHookCase: the shared process entry passes on missing arguments, errors and overruns."""

    hook_module: ModuleType
    hook_event: str
    pass_response: dict

    def main(self, stdin: object, argv: list[str] | None = None) -> tuple[object, str]:
        """Run the hook module's process entry in this process.

        Args:
            stdin: The object patched in as sys.stdin.
            argv: Arguments after the script name; defaults to the hook event.

        Returns:
            The parsed stdout response and the stderr text.
        """
        out, err = io.StringIO(), io.StringIO()
        argv = ["hooks.py", self.hook_event] if argv is None else ["hooks.py", *argv]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.dict(os.environ, hook_env(self.home), clear=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            self.hook_module.main()
        return json.loads(out.getvalue()), err.getvalue()

    def test_missing_or_unknown_event_argument_passes(self) -> None:
        """A missing or unknown event argument answers an empty response."""
        for argv in ([], ["Unknown"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(io.TextIOWrapper(io.BytesIO(b"{}")), argv), ({}, ""))

    def test_unexpected_errors_print_the_pass_response_and_one_stderr_line(self) -> None:
        """An unexpected error answers the pass response and reports one stderr line."""
        with mock.patch.object(self.hook_module, "handle", side_effect=AttributeError("boom")):
            response, err = self.main(io.TextIOWrapper(io.BytesIO(b"{}")))
        self.assertEqual(response, self.pass_response)
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("AttributeError: boom", err)

    def test_stdin_is_read_inside_the_deadline(self) -> None:
        """A stalled stdin read is cut off by the deadline and the hook passes."""
        slow = mock.Mock(buffer=mock.Mock(read=lambda *_args: time.sleep(5) or b"{}"))
        started = time.monotonic()
        with mock.patch.object(deadline, "run_within", functools.partial(deadline.run_within, seconds=0.2)):
            response, _ = self.main(slow)
        self.assertEqual(response, self.pass_response)
        self.assertLess(time.monotonic() - started, 2)
