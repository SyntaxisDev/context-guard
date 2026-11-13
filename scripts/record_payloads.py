#!/usr/bin/env python3
"""Record scrubbed hook payload fixtures from real runtime artifacts; every source is read only.

- Cursor: hook stdin dumps in Cursor's hook output logs, one chat record (composerData)
  from its state.vscdb, and the context usage message of the newest Cursor CLI chat store
  (~/.cursor/chats/*/*/store.db), all opened immutable.
- Codex: hook stdin is not logged, so payloads are built from real rollout values (session,
  turn, model, spawn_agent arguments, subagent ids) and must validate against the hook
  input schemas embedded in the codex binary; those schemas (input and output) are stored too.
- Genie: payloads are shaped from real Genie root and subagent rollouts and validated against
  the hook schemas embedded in the Genie binary.
- Claude Code: hook stdin is not logged, so payloads are built from real transcript values
  (session, model, tool inputs and results, subagent ids) with the documented hook fields.
  --cursor-lifecycle-root selects only explicit Cursor CLI capture artifacts; no home or DB reads.
  --claude-lifecycle-root selects only an explicit sandbox stream capture with original source
  hashes, ordered native events and redacted experiment framing; it never reads default homes.
- Copilot: native `hook.start` inputs and `hook.end` outputs from an explicit sandbox home's
  `session-state/*/events.jsonl` (`--copilot-home`, never defaulted), telemetry templates from the
  same logs, and an output contract checked against the SDK hook contracts d.ts (`--copilot-contract`).
  Optional `--copilot-lifecycle-root` records measured native ACP wake with hashed source provenance;
  ordinary tests only replay the scrubbed fixture and never access those capture roots.

Output: tests/fixtures/<runtime>/<event>-<case>.json = {"source", "event", "payload"} plus
telemetry templates. Prompts, commands, file contents and tool output are replaced, paths
become {HOME}/{PROJECT} placeholders and ids become stable fakes; a recording that still
matches a sensitive pattern is refused.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import string
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.cursor.hooks import CLI_USAGE_FIELD, LENGTH_DELIMITED, cli_root_id, cli_usage, proto_fields
from adapters.registry import discover
from scripts.claude_lifecycle import record_claude_lifecycle
from scripts.cursor_lifecycle import record_cursor_lifecycle

FIXTURES = ROOT / "tests" / "fixtures"
RECORDER_RUNTIMES = frozenset({"cursor", "codex", "genie", "claude", "copilot"})
HOME_TOKEN, PROJECT_TOKEN = "{HOME}", "{PROJECT}"
SCRUBBED, PROMPT_TEXT, EMAIL = "<scrubbed>", "recorded prompt (scrubbed)", "user@example.com"
KEEP_KEYS = frozenset(
    {
        "hook_event_name",
        "model",
        "model_id",
        "composer_mode",
        "cursor_version",
        "tool_name",
        "status",
        "source",
        "permission_mode",
        "subagent_type",
        "agent_type",
        "reasoning_effort",
        "fork_turns",
        "trigger",
        "type",
        "value",
        "output_mode",
        "resolvedModel",
        "effort",
        "subagent_model",
        "subtype",
        "role",
        "unifiedMode",
    }
)
MODEL_PARAM_IDS = frozenset({"context", "effort", "fast"})
ID_KEYS = frozenset(
    {
        "conversation_id",
        "session_id",
        "generation_id",
        "tool_use_id",
        "parent_tool_call_id",
        "turn_id",
        "agent_id",
        "subagent_id",
        "call_id",
        "agentId",
        "parent_conversation_id",
        "tool_call_id",
    }
)
PATH_KEYS = frozenset(
    {
        "cwd",
        "transcript_path",
        "agent_transcript_path",
        "file_path",
        "path",
        "target_file",
        "notebook_path",
        "workspace_roots",
    }
)
SAFE_TOKEN = re.compile(r"[\w.:\-\[\]=,/ ]{1,60}")
STRUCTURAL = frozenset(
    {
        ".codex",
        "sessions",
        ".claude",
        "projects",
        "subagents",
        ".cursor",
        "agent-transcripts",
        ".context-guard",
        "handoffs",
    }
)
ROLLOUT_NAME = re.compile(r"(rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-)(.+)(\.jsonl)")
ID_LIKE = re.compile(r"(agent-)?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{12,})(\.jsonl)?", re.IGNORECASE)
SENSITIVE = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\."),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abprs]-"),
    re.compile(r"/Users/|/home/(?!user\b)|/root/|[A-Za-z]:[\\/]+Users[\\/]"),
)
CURSOR_LOGS = "Library/Application Support/Cursor/logs/*/window*/output_*/cursor.hooks.*.log"
CURSOR_DB = "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
CURSOR_CLI_STORES = ".cursor/chats/*/*/store.db"
CURSOR_CASES = (
    "sessionStart-root",
    "beforeSubmitPrompt-root",
    "preToolUse-task",
    "preToolUse-shell",
    "postToolUse-task",
    "postToolUse-read",
    "postToolUse-write",
    "postToolUse-shell",
    "postToolUse-grep",
    "postToolUse-mcp",
    "postToolUse-subagent-shell",
    "postToolUse-subagent-read",
    "stop-completed",
    "stop-aborted",
)
COMPOSER_KEYS = (
    "_v",
    "status",
    "lastUpdatedAt",
    "createdAt",
    "name",
    "unifiedMode",
    "contextUsagePercent",
    "contextTokensUsed",
    "contextTokenLimit",
    "isAgentic",
)
DEFAULT_TOOL_NAMESPACES = ("", "functions")
CODEX_SCHEMAS = ("session-start", "user-prompt-submit", "post-tool-use", "stop", "subagent-stop")
SCHEMA_MARK = re.compile(rb"\.command\.(input|output)\{")
PATCH = f"*** Begin Patch\n*** Update File: {PROJECT_TOKEN}/file.py\n@@\n-old\n+new\n*** End Patch\n"


class Scrubber:
    """Replace prompts, text, paths and ids; one instance keeps fake ids consistent across a recording.

    Args:
        home: The home directory the artifacts were read from; paths under it become `{HOME}`.
    """

    def __init__(self, home: Path) -> None:
        self.home = str(home)
        self.projects: list[str] = []
        self.emails: set[str] = set()

    def fake_id(self, real: str) -> str:
        """Return a stable fake id with the same shape as a real one.

        Args:
            real: The real id; any prefix up to its last `_` is kept.

        Returns:
            The id with its letters and digits replaced by a hash of the real id.
        """
        digest = hashlib.sha256(("context-guard-fixture:" + real).encode()).hexdigest() * 4
        prefix, sep, rest = real.rpartition("_") if "_" in real else ("", "", real)
        chars = iter(digest)
        body = "".join(next(chars) if ch in string.ascii_letters + string.digits else ch for ch in rest)
        return prefix + sep + body

    def segment(self, part: str) -> str:
        """Scrub one path segment.

        Args:
            part: The segment.

        Returns:
            Structural and date segments unchanged, ids and rollout names with fake ids, any other
            file as `file<suffix>` and any other directory as `dir`.
        """
        if part in STRUCTURAL or re.fullmatch(r"\d{2,4}", part):
            return part
        rollout = ROLLOUT_NAME.fullmatch(part)
        if rollout:
            return rollout[1] + self.fake_id(rollout[2]) + rollout[3]
        found = ID_LIKE.fullmatch(part)
        if found:
            return (found[1] or "") + self.fake_id(found[2]) + (found[3] or "")
        return "file" + Path(part).suffix if Path(part).suffix else "dir"

    def path(self, value: str) -> str:
        """Scrub a path, replacing the project or home root with its placeholder.

        Args:
            value: The path.

        Returns:
            The placeholder-rooted or segment-scrubbed path; `<scrubbed>` for a relative path outside both roots.
        """
        for root, token in [(project, PROJECT_TOKEN) for project in self.projects] + [(self.home, HOME_TOKEN)]:
            if value == root or value.startswith(root + "/"):
                rest = [self.segment(part) for part in value[len(root) :].split("/") if part]
                return "/".join([token, *rest])
        return (
            "/" + "/".join(self.segment(part) for part in value.split("/") if part)
            if value.startswith("/")
            else SCRUBBED
        )

    def text(self, key: str, value: str) -> str:
        """Scrub a string by the key that holds it.

        Args:
            key: The JSON key of the value.
            value: The string.

        Returns:
            A fake id, a scrubbed path, the placeholder email or prompt, a kept safe token, or `<scrubbed>`.
        """
        if not value:
            return value
        if key in ID_KEYS:
            return self.fake_id(value)
        if key in PATH_KEYS:
            return self.path(value)
        if key == "user_email":
            self.emails.add(value)
            return EMAIL
        if key == "prompt":
            return PROMPT_TEXT
        if key == "id" and value in MODEL_PARAM_IDS:
            return value
        if key in KEEP_KEYS and SAFE_TOKEN.fullmatch(value) and self.home not in value:
            return value
        return SCRUBBED

    def value(self, key: str, value: object) -> object:
        """Scrub a JSON value recursively.

        Args:
            key: The key of the value; list items inherit their list's key.
            value: The JSON value.

        Returns:
            The value with every string scrubbed by its key; other scalars unchanged.
        """
        if isinstance(value, dict):
            return {name: self.value(name, item) for name, item in value.items()}
        if isinstance(value, list):
            return [self.value(key, item) for item in value]
        if isinstance(value, str):
            return self.text(key, value)
        return value

    def payload(self, payload: dict, projects: list[str]) -> dict:
        """Scrub a hook payload.

        Args:
            payload: The payload.
            projects: Project roots that become `{PROJECT}`; the longest match wins.

        Returns:
            The scrubbed payload.
        """
        self.projects = sorted((p for p in projects if p), key=len, reverse=True)
        return self.value("", payload)

    def check(self, recorded: object) -> None:
        """Refuse a recording that still holds the home path, a seen email or a sensitive pattern.

        Args:
            recorded: The recording about to be written.

        Raises:
            SystemExit: When sensitive text remains.
        """
        text = json.dumps(recorded)
        leaks = [p.pattern for p in SENSITIVE if p.search(text)] + [e for e in self.emails if e in text]
        if self.home in text or leaks:
            msg = f"refusing to write a recording that still holds sensitive text: {leaks or self.home}"
            raise SystemExit(msg)


def projects_of(payload: dict) -> list[str]:
    """Return a payload's project roots.

    Args:
        payload: A hook payload.

    Returns:
        Its string `workspace_roots` and `cwd`.
    """
    roots = payload.get("workspace_roots") if isinstance(payload.get("workspace_roots"), list) else []
    return [root for root in [*roots, payload.get("cwd")] if isinstance(root, str)]


def entry(source: str, event: str, payload: dict) -> dict:
    """Build a payload fixture.

    Args:
        source: Where the payload came from.
        event: The native hook event name.
        payload: The scrubbed payload.

    Returns:
        The fixture object.
    """
    return {"source": source, "event": event, "payload": payload}


def cursor_inputs(paths: list[Path]) -> Iterator[dict]:
    """Yield the hook stdin payloads dumped in Cursor hook logs.

    Args:
        paths: Hook log files, oldest first; unreadable files are skipped.

    Yields:
        Each JSON object after an `INPUT:` line that names a hook event.
    """
    decoder = json.JSONDecoder()
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"^INPUT:\n", text, re.MULTILINE):
            try:
                payload, _ = decoder.raw_decode(text, match.end())
            except ValueError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("hook_event_name"), str):
                yield payload


def cursor_case(payload: dict) -> str:
    """Name the fixture case of a Cursor payload.

    Args:
        payload: A Cursor hook payload.

    Returns:
        `<event>-<tool>` for tool events (with `subagent-` inside subagents), `stop-<status>` or `<event>-root`.
    """
    event = payload["hook_event_name"]
    if event in ("preToolUse", "postToolUse"):
        name = str(payload.get("tool_name", ""))
        tool = "mcp" if name.startswith("MCP:") else name.lower()
        return f"{event}-{'subagent-' if payload.get('parent_tool_call_id') else ''}{tool}"
    if event == "stop":
        return f"stop-{payload.get('status', 'unknown')}"
    return f"{event}-root"


def record_cursor_capture(path: Path, scrub: Scrubber) -> dict[str, dict]:
    """Record the actual compaction recipe from an explicit hook capture only.

    Args:
        path: Captured native hook JSONL; no runtime home is discovered.
        scrub: Stable identity and content scrubber.

    Returns:
        Independently hashed, scrubbed payloads for initiation, completion and next Read.

    Raises:
        ValueError: The capture lacks a matching completed compaction and subsequent Read.
    """
    rows = list(rollout_records(path))
    for index, row in enumerate(rows):
        if row.get("event") != "preCompact":
            continue
        pre = row["payload"]
        for following, stop in enumerate(rows[index + 1 :], index + 1):
            payload = stop.get("payload", {})
            if (
                stop.get("event") == "stop"
                and payload.get("status") == "completed"
                and all(payload.get(key) == pre.get(key) for key in ("conversation_id", "session_id", "generation_id"))
            ):
                tool = next(
                    (
                        item["payload"]
                        for item in rows[following + 1 :]
                        if item.get("event") == "postToolUse"
                        and item.get("payload", {}).get("tool_name") == "Read"
                        and item["payload"].get("session_id") == pre.get("session_id")
                    ),
                    None,
                )
                if tool:
                    source = (
                        "Captured Cursor CLI hook JSONL; original capture sha256 "
                        + hashlib.sha256(path.read_bytes()).hexdigest()
                    )
                    return {
                        name: entry(source, data["hook_event_name"], scrub.payload(data, projects_of(data)))
                        for name, data in (
                            ("preCompact-root", pre),
                            ("stop-compaction-completed", payload),
                            ("postToolUse-recovery-read", tool),
                        )
                    }
    msg = "capture lacks a completed compaction/Read recipe"
    raise ValueError(msg)


def record_cursor(home: Path, scrub: Scrubber) -> dict[str, dict]:
    """Record Cursor fixtures: the newest payload per case, one composer record and one CLI usage record.

    Args:
        home: The home directory to read from.
        scrub: The recording's scrubber.

    Returns:
        Fixture names mapped to their content.
    """
    paths = sorted(home.glob(CURSOR_LOGS), key=lambda p: p.stat().st_mtime)
    latest = {}
    for payload in cursor_inputs(paths):
        case = cursor_case(payload)
        if case in CURSOR_CASES:
            latest[case] = payload
    source = "Cursor hook log stdin dump (cursor.hooks.*.log), scrubbed"
    found = {
        case: entry(source, payload["hook_event_name"], scrub.payload(payload, projects_of(payload)))
        for case, payload in latest.items()
    }
    composer = cursor_composer(home / CURSOR_DB)
    if composer:
        found["composer-data"] = {
            "source": "Cursor state.vscdb cursorDiskKV composerData:<id>, scrubbed",
            "record": composer,
        }
    usage = cursor_cli_usage(home)
    if usage:
        found["cli-store"] = {
            "source": "Cursor CLI store.db root blob field 5 (context usage), numbers and category labels only",
            "record": usage,
        }
    return found


def cli_store_root(database: Path) -> bytes | None:
    """Read the root blob of a Cursor CLI chat store, opened immutable.

    Args:
        database: The chat's `store.db`.

    Returns:
        The root blob, or None when it is missing or the database cannot be read.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        meta = connection.execute("select value from meta where key = '0'").fetchone()
        root = cli_root_id(meta[0] if meta else None)
        row = connection.execute("select data from blobs where id = ?", (root,)).fetchone() if root else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return row[0] if row and isinstance(row[0], bytes) else None


def cursor_cli_usage(home: Path) -> dict | None:
    """Return the newest CLI chat's usage message, kept as hex with its decoded tokens and window.

    Args:
        home: The home directory to read from.

    Returns:
        The usage record from the newest of up to 50 chat stores that has one, or None.
    """
    stores = sorted(home.glob(CURSOR_CLI_STORES), key=lambda p: p.stat().st_mtime, reverse=True)
    for database in stores[:50]:
        root = cli_store_root(database)
        try:
            usage = [
                value
                for number, wire, value in proto_fields(root or b"")
                if number == CLI_USAGE_FIELD and wire == LENGTH_DELIMITED
            ]
            tokens, window = cli_usage(root) if usage else (None, None)
        except ValueError:
            continue
        if tokens is not None and window is not None:
            return {"usage_hex": usage[-1].hex(), "tokens": tokens, "window": window}
    return None


def cursor_composer(database: Path) -> dict | None:
    """Return the newest composer record with token usage from Cursor's state database, opened immutable.

    Args:
        database: Cursor's `state.vscdb`.

    Returns:
        The record's `COMPOSER_KEYS` with the chat name scrubbed, or None when there is none.
    """
    if not database.is_file():
        return None
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        rows = connection.execute(
            "select value from cursorDiskKV where key like 'composerData:%' order by rowid desc limit 500"
        ).fetchall()
    finally:
        connection.close()
    for (value,) in rows:
        try:
            record = json.loads(value)
        except TypeError, ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("contextTokensUsed"), int):
            kept = {key: record[key] for key in COMPOSER_KEYS if key in record}
            return {key: (SCRUBBED if isinstance(item, str) and key == "name" else item) for key, item in kept.items()}
    return None


def codex_schemas(binary: Path) -> dict[str, dict]:
    """Extract the hook schemas embedded in the codex binary as `<name>.command.<side>{...}`.

    Args:
        binary: The codex executable.

    Returns:
        Schemas keyed by their decoded `title`, such as `stop.command.input`.
    """
    data = binary.read_bytes()
    decoder, found = json.JSONDecoder(), {}
    for match in SCHEMA_MARK.finditer(data):
        start = match.end() - 1
        try:
            schema, _ = decoder.raw_decode(data[start : start + 200_000].decode("utf-8", "replace"))
        except ValueError:
            continue
        title = schema.get("title") if isinstance(schema, dict) else None
        if isinstance(title, str) and title.endswith(".command." + match[1].decode()):
            found[title] = schema
    return found


def rollout_records(path: Path) -> Iterator[dict]:
    """Yield the JSON object lines of a rollout or transcript.

    Args:
        path: The JSON-lines file.

    Yields:
        Each line that parses as a JSON object.
    """
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def rollout_facts(path: Path) -> dict:
    """Collect the values a Codex rollout offers for payloads.

    Args:
        path: The rollout file.

    Returns:
        `path` plus, when present, the first `meta` and the last `turn`, `token_count`, `spawn` and `call`.
    """
    facts: dict = {"path": str(path)}
    for record in rollout_records(path):
        kind, body = record.get("type"), record.get("payload")
        if not isinstance(body, dict):
            continue
        if kind == "session_meta" and "meta" not in facts:
            facts["meta"] = body
        elif kind == "turn_context":
            facts["turn"] = body
        elif kind == "event_msg" and body.get("type") == "token_count" and isinstance(body.get("info"), dict):
            facts["token_count"] = record
        elif body.get("type") == "function_call" and body.get("name") == "spawn_agent":
            facts["spawn"] = body
        elif body.get("type") in ("custom_tool_call", "function_call"):
            facts["call"] = body
    return facts


def codex_rollouts(home: Path) -> tuple[dict, dict]:
    """Find the newest root rollout with a spawn_agent call and the newest subagent rollout.

    Args:
        home: The home directory to read from.

    Returns:
        The root and subagent rollout facts, searched among the 200 newest rollouts.

    Raises:
        SystemExit: When either rollout is missing.
    """
    paths = sorted(home.glob(".codex/sessions/*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    root = child = None
    for path in paths[:200]:
        facts = rollout_facts(path)
        source = facts.get("meta", {}).get("thread_source")
        if root is None and source == "user" and {"spawn", "turn", "token_count"} <= facts.keys():
            root = facts
        elif child is None and source == "subagent" and "turn" in facts:
            child = facts
        if root and child:
            return root, child
    msg = "no Codex root rollout with spawn_agent and no subagent rollout found"
    raise SystemExit(msg)


def genie_rollouts(home: Path) -> tuple[dict, dict]:
    """Find the newest Genie root rollout with a spawn and newest subagent rollout.

    Args:
        home: The home directory to read from.

    Returns:
        The root and subagent rollout facts.

    Raises:
        SystemExit: When either rollout is missing.
    """
    paths = sorted(home.glob(".genie/sessions/*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    root = child = None
    for path in paths[:200]:
        facts = rollout_facts(path)
        source = facts.get("meta", {}).get("thread_source")
        if root is None and source == "user" and {"call", "turn", "token_count"} <= facts.keys():
            root = facts
        elif child is None and source == "subagent" and {"meta", "turn"} <= facts.keys():
            child = facts
        if root and child:
            return root, child
    msg = "no Genie root rollout with a tool call and no subagent rollout found"
    raise SystemExit(msg)


def genie_pool(root: dict, child: dict) -> dict:
    """Gather values used to shape Genie hook payload fixtures.

    Args:
        root: The root rollout facts.
        child: The subagent rollout facts.

    Returns:
        Root fields and the subagent fields under `child`.
    """
    turn, meta, call = root["turn"], root["meta"], root["call"]
    return {
        "session_id": meta["id"],
        "transcript_path": root["path"],
        "cwd": meta.get("cwd") or turn.get("cwd"),
        "model": turn.get("model"),
        "turn_id": turn.get("turn_id"),
        "permission_mode": "bypassPermissions" if turn.get("approval_policy") == "never" else "default",
        "prompt": PROMPT_TEXT,
        "stop_hook_active": False,
        "last_assistant_message": SCRUBBED,
        "tool_response": SCRUBBED,
        "tool_use_id": call.get("call_id", "call_0"),
        "tool_name": call.get("name") or "exec",
        "tool_input": call.get("input") or SCRUBBED,
        "child_path": child["path"],
        "child": {
            "agent_id": child["meta"]["id"],
            "agent_type": child["meta"].get("agent_role") or "default",
            "turn_id": child["turn"].get("turn_id"),
            "model": child["turn"].get("model") or turn.get("model"),
        },
    }


def genie_cases(pool: dict) -> dict[str, tuple[str, dict]]:
    """List fixture cases observed through Genie's hooks.

    Args:
        pool: Values from `genie_pool`.

    Returns:
        Case names mapped to hook events and field overrides.
    """
    agent = pool["child"]
    tool = {"tool_name": pool["tool_name"], "tool_input": pool["tool_input"]}
    return {
        "SessionStart-startup": ("SessionStart", {"source": "startup"}),
        "SessionStart-resume": ("SessionStart", {"source": "resume"}),
        "SessionStart-compact": ("SessionStart", {"source": "compact"}),
        "UserPromptSubmit-root": ("UserPromptSubmit", {}),
        "UserPromptSubmit-subagent": ("UserPromptSubmit", agent),
        "PostToolUse-exec": ("PostToolUse", tool),
        "PostToolUse-subagent-exec": ("PostToolUse", {**tool, **agent}),
        "Stop-root": ("Stop", {}),
        "SubagentStop-subagent": ("SubagentStop", {**agent, "agent_transcript_path": pool["child_path"]}),
    }


def hook_tool_name(call: dict) -> str:
    """Name a rollout function call the way Codex names it in hook payloads.

    Codex joins a non-default namespace and the tool name without a separator.

    Args:
        call: The rollout `function_call` payload.

    Returns:
        The bare name for the default namespace, else namespace and name joined.
    """
    namespace, name = call.get("namespace") or "", call.get("name") or ""
    return name if namespace in DEFAULT_TOOL_NAMESPACES else namespace + name


def codex_pool(root: dict, child: dict) -> dict:
    """Gather the real values Codex payloads are built from.

    Args:
        root: The root rollout facts.
        child: The subagent rollout facts.

    Returns:
        Payload field values, the spawn_agent arguments under `spawn`, the subagent fields under `child`
        and its rollout under `child_path`.
    """
    turn, meta = root["turn"], root["meta"]
    spawn_args = json.loads(root["spawn"].get("arguments") or "{}")
    return {
        "session_id": meta["id"],
        "transcript_path": root["path"],
        "cwd": meta.get("cwd") or turn.get("cwd"),
        "model": turn.get("model"),
        "turn_id": turn.get("turn_id"),
        "permission_mode": "bypassPermissions" if turn.get("approval_policy") == "never" else "default",
        "prompt": PROMPT_TEXT,
        "stop_hook_active": False,
        "last_assistant_message": SCRUBBED,
        "tool_response": SCRUBBED,
        "tool_use_id": (root.get("call") or root["spawn"]).get("call_id", "call_0"),
        "spawn": spawn_args,
        "spawn_tool": hook_tool_name(root["spawn"]),
        "child_path": child["path"],
        "child": {
            "agent_id": child["meta"]["id"],
            "agent_type": child["meta"].get("agent_role") or "default",
            "turn_id": child["turn"].get("turn_id"),
            "model": child["turn"].get("model") or turn.get("model"),
        },
    }


def codex_cases(pool: dict) -> dict[str, tuple[str, dict]]:
    """List the Codex fixture cases.

    Args:
        pool: The values from `codex_pool`.

    Returns:
        Case names mapped to their hook event and the fields that override the pool.
    """
    agent = pool["child"]
    bash = {"tool_name": "Bash", "tool_input": {"command": SCRUBBED}}
    return {
        "SessionStart-startup": ("SessionStart", {"source": "startup"}),
        "SessionStart-resume": ("SessionStart", {"source": "resume"}),
        "SessionStart-compact": ("SessionStart", {"source": "compact"}),
        "UserPromptSubmit-root": ("UserPromptSubmit", {}),
        "UserPromptSubmit-subagent": ("UserPromptSubmit", agent),
        "PostToolUse-bash": ("PostToolUse", bash),
        "PostToolUse-apply-patch": ("PostToolUse", {"tool_name": "apply_patch", "tool_input": {"command": PATCH}}),
        "PostToolUse-spawn-agent": ("PostToolUse", {"tool_name": pool["spawn_tool"], "tool_input": pool["spawn"]}),
        "PostToolUse-subagent-bash": ("PostToolUse", {**bash, **agent}),
        "Stop-root": ("Stop", {}),
        "SubagentStop-subagent": ("SubagentStop", {**agent, "agent_transcript_path": pool["child_path"]}),
    }


def schema_payload(schema: dict, event: str, pool: dict, extra: dict) -> dict:
    """Build a payload from exactly the schema's properties that real values exist for.

    Args:
        schema: The event's input schema from the codex binary.
        event: The hook event name.
        pool: The values from `codex_pool`.
        extra: Case fields that override the pool.

    Returns:
        The unscrubbed payload.

    Raises:
        SystemExit: When a required property has no recorded value.
    """
    values = {**pool, **extra, "hook_event_name": event}
    payload = {key: values[key] for key in schema["properties"] if key in values and values[key] is not None}
    missing = [key for key in schema.get("required", []) if key not in payload]
    if missing:
        msg = f"Codex {event}: no recorded value for required fields {missing}"
        raise SystemExit(msg)
    return payload


def record_codex(home: Path, binary: Path | None, scrub: Scrubber) -> dict[str, dict]:
    """Record Codex fixtures: schema-shaped payloads, the hook schemas and one token-count record.

    Args:
        home: The home directory to read from.
        binary: The codex executable whose embedded schemas shape the payloads, or None when not found.
        scrub: The recording's scrubber.

    Returns:
        Fixture names mapped to their content.

    Raises:
        SystemExit: When the codex binary is missing.
    """
    if binary is None or not binary.is_file():
        msg = "codex binary not found; pass --codex-binary"
        raise SystemExit(msg)
    schemas = codex_schemas(binary)
    root, child = codex_rollouts(home)
    pool = codex_pool(root, child)
    source = "Codex rollout values (session_meta, turn_context, spawn_agent call) shaped by the codex binary schema"
    found = {}
    for case, (event, extra) in codex_cases(pool).items():
        schema = schemas[f"{kebab(event)}.command.input"]
        payload = scrub.payload(schema_payload(schema, event, pool, extra), [pool["cwd"]])
        if extra.get("tool_input", {}).get("command") == PATCH:
            payload["tool_input"]["command"] = PATCH
        found[case] = entry(source, event, payload)
    for name in (f"{event}.command.{side}" for event in CODEX_SCHEMAS for side in ("input", "output")):
        found[f"schemas/{name}"] = schemas[name]
    found["rollout-token-count"] = {
        "source": "Codex rollout event_msg token_count, numbers kept",
        "record": token_count_record(root["token_count"]),
    }
    return found


def record_genie(home: Path, binary: Path | None, scrub: Scrubber) -> dict[str, dict]:
    """Record Genie payloads, schemas and token telemetry from local artifacts.

    Args:
        home: The home directory to read from.
        binary: The Genie executable whose embedded schemas shape the payloads.
        scrub: The recording's scrubber.

    Returns:
        Fixture names mapped to their content.

    Raises:
        SystemExit: When the Genie binary is missing.
    """
    if binary is None or not binary.is_file():
        msg = "genie binary not found; pass --genie-binary"
        raise SystemExit(msg)
    schemas = codex_schemas(binary)
    root, child = genie_rollouts(home)
    pool = genie_pool(root, child)
    source = "Genie rollout values (session_meta, turn_context, tool call) shaped by the Genie binary schema"
    found = {}
    for case, (event, extra) in genie_cases(pool).items():
        schema = schemas[f"{kebab(event)}.command.input"]
        payload = scrub.payload(schema_payload(schema, event, pool, extra), [pool["cwd"]])
        found[case] = entry(source, event, payload)
    for name in (f"{event}.command.{side}" for event in CODEX_SCHEMAS for side in ("input", "output")):
        found[f"schemas/{name}"] = schemas[name]
    found["rollout-token-count"] = {
        "source": "Genie rollout event_msg token_count, numbers kept",
        "record": token_count_record(root["token_count"]),
    }
    return found


def kebab(event: str) -> str:
    """Convert a CamelCase event name to kebab case.

    Args:
        event: The event name, such as `UserPromptSubmit`.

    Returns:
        The kebab-case name, such as `user-prompt-submit`.
    """
    return re.sub(r"(?<!^)([A-Z])", r"-\1", event).lower()


def token_count_record(record: dict) -> dict:
    """Reduce a rollout token_count event to its usage numbers.

    Args:
        record: The rollout `event_msg` record.

    Returns:
        The record with only the token usage and context window kept.
    """
    info = record["payload"]["info"]
    kept = {key: info[key] for key in ("total_token_usage", "last_token_usage", "model_context_window") if key in info}
    return {"timestamp": record.get("timestamp"), "type": "event_msg", "payload": {"type": "token_count", "info": kept}}


def transcript_facts(path: Path) -> dict:
    """Collect the values a Claude Code transcript offers for payloads.

    Args:
        path: The transcript file.

    Returns:
        `path`, the session, cwd, permission mode and model, the last assistant record with
        usage, tool_use blocks by tool name, tool results by tool_use id and any compact boundary.
    """
    facts: dict = {"path": str(path), "tools": {}, "results": {}}
    for record in rollout_records(path):
        facts.setdefault("session", record.get("sessionId"))
        facts["cwd"] = record.get("cwd") or facts.get("cwd")
        facts["mode"] = record.get("permissionMode") or facts.get("mode")
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        if record.get("type") == "assistant" and isinstance(message.get("usage"), dict):
            facts["assistant"], facts["model"] = record, message.get("model")
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                facts["tools"][block.get("name")] = block
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                facts["results"][block.get("tool_use_id")] = record.get("toolUseResult")
        if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            facts["boundary"] = record
    return facts


def claude_transcripts(home: Path) -> tuple[dict, dict, Path]:
    """Find the newest transcript with an Agent call and a subagent transcript that used a tool.

    Args:
        home: The home directory to read from.

    Returns:
        The main and subagent transcript facts and the main transcript's path.

    Raises:
        SystemExit: When no such transcript exists.
    """
    paths = sorted(home.glob(".claude/projects/*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        agents = sorted(path.with_suffix("").glob("subagents/agent-*.jsonl"))
        if not agents:
            continue
        facts = transcript_facts(path)
        if "Agent" in facts["tools"] and facts.get("assistant"):
            child = next((transcript_facts(a) for a in agents if transcript_facts(a)["tools"]), None)
            if child:
                return facts, child, path
    msg = "no Claude transcript with an Agent call and a subagent transcript found"
    raise SystemExit(msg)


def claude_tool(facts: dict, name: str) -> dict | None:
    """Build the tool fields of a PostToolUse payload from a transcript's tool call.

    Args:
        facts: Transcript facts.
        name: The tool name.

    Returns:
        The tool name, input, response (`<scrubbed>` when none was recorded) and tool_use id, or
        None when the transcript has no call of that tool.
    """
    block = facts["tools"].get(name)
    if block is None:
        return None
    result = facts["results"].get(block.get("id"))
    return {
        "tool_name": name,
        "tool_input": block.get("input") or {},
        "tool_response": result if result is not None else SCRUBBED,
        "tool_use_id": block.get("id"),
    }


def claude_cases(main: dict, child: dict) -> dict[str, tuple[str, dict]]:
    """List the Claude Code fixture cases.

    Args:
        main: The main transcript facts.
        child: The subagent transcript facts.

    Returns:
        Case names mapped to their hook event and event-specific fields.
    """
    agent_id = Path(child["path"]).stem.removeprefix("agent-")
    agent_type = (main["tools"]["Agent"].get("input") or {}).get("subagent_type") or "general-purpose"
    agent = {"agent_id": agent_id, "agent_type": agent_type}
    cases = {
        "SessionStart-startup": ("SessionStart", {"source": "startup", "model": main.get("model")}),
        "SessionStart-compact": ("SessionStart", {"source": "compact", "model": main.get("model")}),
        "UserPromptSubmit-root": ("UserPromptSubmit", {"prompt": PROMPT_TEXT}),
        "Stop-root": ("Stop", {"stop_hook_active": False, "last_assistant_message": SCRUBBED}),
        "SubagentStop-subagent": (
            "SubagentStop",
            {
                **agent,
                "agent_transcript_path": child["path"],
                "stop_hook_active": False,
                "last_assistant_message": SCRUBBED,
            },
        ),
        "PreCompact-auto": ("PreCompact", {"trigger": "auto", "custom_instructions": ""}),
        "PostCompact-auto": ("PostCompact", {"trigger": "auto", "compact_summary": SCRUBBED}),
    }
    for name in ("Read", "Write", "Edit", "Bash", "Agent"):
        tool = claude_tool(main, name)
        if tool:
            cases[f"PostToolUse-{name.lower()}"] = ("PostToolUse", tool)
    name = next((tool for tool in ("Read", "Bash", "Grep") if tool in child["tools"]), min(child["tools"]))
    cases[f"PostToolUse-subagent-{name.lower()}"] = ("PostToolUse", {**claude_tool(child, name), **agent})
    return cases


def record_claude(home: Path, scrub: Scrubber) -> dict[str, dict]:
    """Record Claude Code fixtures: payloads with the documented fields, one assistant record and any compact boundary.

    Args:
        home: The home directory to read from.
        scrub: The recording's scrubber.

    Returns:
        Fixture names mapped to their content.
    """
    main, child, path = claude_transcripts(home)
    common = {"session_id": main["session"], "transcript_path": str(path), "cwd": main["cwd"]}
    source = "Claude Code transcript values (session, model, tool_use input and toolUseResult) with documented fields"
    found = {}
    for case, (event, extra) in claude_cases(main, child).items():
        mode = {} if event in ("SessionStart", "PreCompact", "PostCompact") else {"permission_mode": main["mode"]}
        payload = {**common, **mode, "hook_event_name": event, **extra}
        found[case] = entry(source, event, scrub.payload(payload, [main["cwd"]]))
    found["transcript-assistant"] = {
        "source": "Claude transcript assistant record, usage numbers kept",
        "record": assistant_record(main["assistant"]),
    }
    boundary = next((f["boundary"] for f in map(transcript_facts, boundary_candidates(home)) if "boundary" in f), None)
    if boundary:
        meta = boundary.get("compactMetadata") or {}
        found["transcript-compact-boundary"] = {
            "source": "Claude transcript compact_boundary record",
            "record": {
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": boundary.get("timestamp"),
                "compactMetadata": {key: meta.get(key) for key in ("trigger", "preTokens")},
            },
        }
    return found


def mentions(path: Path, needle: bytes) -> bool:
    """Return whether any line of a file contains a byte string.

    Args:
        path: The file.
        needle: The bytes to find.

    Returns:
        True when found; False when absent or the file cannot be read.
    """
    try:
        with path.open("rb") as handle:
            return any(needle in line for line in handle)
    except OSError:
        return False


def boundary_candidates(home: Path, limit: int = 20) -> list[Path]:
    """Find Claude Code transcripts that mention a compact boundary.

    Args:
        home: The home directory to read from.
        limit: The most transcripts to return.

    Returns:
        Matching transcripts in path order.
    """
    found = []
    for path in sorted((home / ".claude/projects").rglob("*.jsonl")):
        if mentions(path, b"compact_boundary"):
            found.append(path)
        if len(found) == limit:
            break
    return found


def assistant_record(record: dict) -> dict:
    """Reduce a transcript assistant record to its model and integer usage numbers.

    Args:
        record: The assistant transcript record.

    Returns:
        The record with empty content.
    """
    message = record["message"]
    usage = {key: value for key, value in message["usage"].items() if type(value) is int}
    return {
        "type": "assistant",
        "timestamp": record.get("timestamp"),
        "message": {"model": message.get("model"), "role": "assistant", "content": [], "usage": usage},
    }


COPILOT_IDS = frozenset(
    {"sessionId", "toolCallId", "parentToolCallId", "parentAgentId", "agentId", "compactionAttemptId"}
)
COPILOT_PATHS = frozenset({"transcriptPath"})
COPILOT_KEEP = frozenset(
    {
        "toolName",
        "agentType",
        "agentName",
        "stopReason",
        "notificationType",
        "resultType",
        "model",
        "outcome",
        "execution",
        "copilotVersion",
    }
)
COPILOT_STRUCTURAL = frozenset({"session-state", "events.jsonl"})
COPILOT_USAGE_KEYS = (
    "interactionType",
    "model",
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "reasoningTokens",
    "maxPromptTokens",
)
COPILOT_SESSION_KEYS = ("copilotVersion", "selectedModel", "contextTier")
COPILOT_COMPACTION_KEYS = {
    "session.compaction_start": (
        "model",
        "systemTokens",
        "conversationTokens",
        "toolDefinitionsTokens",
        "currentTokens",
        "tokenLimit",
        "trigger",
        "execution",
    ),
    "session.compaction_complete": (
        "success",
        "outcome",
        "preCompactionMessagesLength",
        "messagesRemoved",
        "preCompactionTokens",
        "postCompactionTokens",
        "tokensRemoved",
        "tokenAccountingBasis",
    ),
}
COPILOT_CONTRACT_FILE = "sdk-hook-contracts.d.ts"
# Hand-maintained from the SDK hook contracts d.ts; recorded only after each field is found there.
COPILOT_OUTPUT_CONTRACT = {
    "sessionStart": ("SessionStartHookOutput", ("additionalContext", "modifiedConfig")),
    "userPromptSubmitted": (
        "UserPromptSubmittedHookOutput",
        ("modifiedPrompt", "additionalContext", "suppressOutput", "decision", "reason"),
    ),
    "userPromptTransformed": ("UserPromptTransformedHookOutput", ("modifiedTransformedPrompt",)),
    "postToolUse": ("PostToolUseHookOutput", ("modifiedResult", "additionalContext", "suppressOutput")),
    "postToolUseFailure": ("PostToolUseFailureHookOutput", ("additionalContext",)),
    "agentStop": ("AgentStopHookOutput", ("decision", "reason")),
    "subagentStart": ("SubagentStartHookOutput", ("additionalContext",)),
    "subagentStop": ("SubagentStopHookOutput", ("decision", "reason", "modifiedResponse")),
    "sessionEnd": ("SessionEndHookOutput", ("suppressOutput", "cleanupActions", "sessionSummary")),
}
COPILOT_BLOCK_EVENTS = {
    "userPromptSubmitted": "refuses the prompt; no model turn",
    "agentStop": "continues the agent; reason becomes the next user message",
    "subagentStop": "continues the child; reason becomes its next message",
}


class CopilotScrubber(Scrubber):
    """Scrubber that also understands Copilot's camelCase ids, paths and structural values."""

    def segment(self, part: str) -> str:
        """Scrub one path segment, keeping the Copilot session-state layout.

        Args:
            part: The segment.

        Returns:
            The segment unchanged when structural, otherwise as `Scrubber.segment`.
        """
        return part if part in COPILOT_STRUCTURAL else super().segment(part)

    def text(self, key: str, value: str) -> str:
        """Scrub a string by the camelCase key that holds it.

        Args:
            key: The JSON key of the value.
            value: The string.

        Returns:
            A fake id, scrubbed path or kept structural token; otherwise as `Scrubber.text`.
        """
        if value and key in COPILOT_IDS:
            return self.fake_id(value)
        if value and key in COPILOT_PATHS:
            return self.path(value)
        if key == "reason" and value in {"complete", "error", "abort", "timeout", "user_exit"}:
            return value
        if key == "toolArgs" and value.startswith("*** Begin Patch"):
            return self.patch(value)
        if value and key in COPILOT_KEEP and SAFE_TOKEN.fullmatch(value) and self.home not in value:
            return value
        return super().text(key, value)

    def patch(self, value: str) -> str:
        """Preserve native patch headers and operation shape while removing file contents.

        Args:
            value: Recorded raw patch argument.

        Returns:
            Patch structure with scrubbed paths, context and file contents.
        """
        lines = []
        for line in value.splitlines():
            if line in {"*** Begin Patch", "*** End Patch"}:
                lines.append(line)
            elif line.startswith(("*** Add File: ", "*** Update File: ")):
                header, path = line.split(": ", 1)
                lines.append(f"{header}: {self.path(path)}")
            elif line.startswith("@@"):
                lines.append("@@")
            else:
                prefix = line[0] if line and line[0] in "+- " else " "
                lines.append(prefix + SCRUBBED)
        return "\n".join(lines) + "\n"

    def value(self, key: str, value: object) -> object:
        """Scrub a JSON value, treating each `toolCalls` entry's `id`, `name` and `args`.

        Args:
            key: The key of the value.
            value: The JSON value.

        Returns:
            The scrubbed value.
        """
        if key == "toolCalls" and isinstance(value, list):
            return [{name: self.tool_call_field(name, item) for name, item in call.items()} for call in value]
        return super().value(key, value)

    def tool_call_field(self, name: str, item: object) -> object:
        """Scrub one field of a `toolCalls` entry.

        Args:
            name: The field name.
            item: The field value.

        Returns:
            A fake id, the tool name when a safe token, or the scrubbed value.
        """
        if name == "id" and isinstance(item, str):
            return self.fake_id(item)
        if name == "name" and isinstance(item, str) and SAFE_TOKEN.fullmatch(item):
            return item
        return super().value(name, item)


def copilot_events(home: Path) -> Iterator[tuple[str, dict]]:
    """Yield every record of every session event log under a Copilot home, oldest first.

    Args:
        home: The Copilot home (`session-state/<id>/events.jsonl` below it), read only.

    Yields:
        The Copilot version of the log's session start and each record. Unreadable lines are skipped.
    """
    records = []
    for path in sorted((home / "session-state").glob("*/events.jsonl")):
        version = ""
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and isinstance(record.get("data"), dict):
                if record.get("type") == "session.start":
                    version = str(record["data"].get("copilotVersion", ""))
                records.append((str(record.get("timestamp", "")), path.name, index, version, record))
    for _, _, _, version, record in sorted(records, key=lambda item: item[:3]):
        yield version, record


def copilot_case(hook: str, data: dict) -> str:
    """Name the recording case of a hook.start record from its structural values.

    Args:
        hook: The native hook event name.
        data: The hook.start `data`; its `input` is read.

    Returns:
        The case: the discriminating input value, or `root` or `subagent` by who fired it.
    """
    given = data["input"]
    if hook == "sessionStart":
        found = given.get("source")
    elif hook == "sessionEnd":
        found = given.get("reason")
    elif hook == "notification":
        found = given.get("notificationType")
    elif hook == "preCompact":
        found = given.get("trigger")
    elif hook in ("postToolUse", "postToolUseFailure", "permissionRequest"):
        found = given.get("toolName")
        if found == "apply_patch" and isinstance(given.get("toolArgs"), str):
            patch = given["toolArgs"]
            operation = "add" if "*** Add File: " in patch else "update" if "*** Update File: " in patch else "other"
            return f"apply_patch-{operation}"
    elif hook in ("subagentStart", "subagentStop"):
        found = given.get("agentType")
    else:
        found = None
    scope = "subagent" if given.get("agentId") or data.get("parentToolCallId") else "root"
    parts = [str(found) if found else "", scope if hook in ("postToolUse", "preToolUse", "permissionRequest") else ""]
    case = "-".join(part for part in parts if part) or scope
    return re.sub(r"[^A-Za-z0-9]+", "-", case).strip("-")


def copilot_hooks(home: Path, scrub: Scrubber) -> dict[str, dict]:
    """Record the first observed hook.start input and hook.end output per hook and case.

    Args:
        home: The Copilot home to read.
        scrub: The recording's scrubber.

    Returns:
        Fixture names mapped to their content: `<hook>-<case>` inputs and `output-<hook>-<case>` outputs.
    """
    found: dict[str, dict] = {}
    pending: dict[str, tuple[str, str, str]] = {}
    for version, record in copilot_events(home):
        data = record["data"]
        if record["type"] == "hook.start" and isinstance(data.get("input"), dict):
            hook, case = str(data["hookType"]), copilot_case(str(data["hookType"]), data)
            name = f"{hook}-{case}"
            if name in found:
                continue
            source = f"Copilot {version} events.jsonl hook.start input (native, unmodified except scrubbing)"
            found[name] = entry(source, hook, scrub.payload(data["input"], projects_of(data["input"])))
            pending[str(data.get("hookInvocationId"))] = (hook, name, version)
        elif record["type"] == "hook.end" and str(data.get("hookInvocationId")) in pending:
            hook, name, started = pending.pop(str(data["hookInvocationId"]))
            if isinstance(data.get("output"), dict):
                source = f"Copilot {started} events.jsonl hook.end output of the same invocation (observed template)"
                output = scrub.value("", data["output"])
                found[f"output-{name}"] = {"source": source, "event": hook, "output": output}
    return found


def pick(data: dict, keys: tuple[str, ...]) -> dict:
    """Select the present keys of a mapping.

    Args:
        data: The mapping.
        keys: The keys to keep.

    Returns:
        The mapping reduced to those of `keys` it holds.
    """
    return {key: data[key] for key in keys if key in data}


def copilot_telemetry(home: Path, scrub: Scrubber) -> dict[str, dict]:
    """Record the observed telemetry templates: usage, compaction and session start events.

    Args:
        home: The Copilot home to read.
        scrub: Scrubber that consistently replaces native event and agent identities.

    Returns:
        Fixture names mapped to records holding only structural and numeric fields.
    """
    found: dict[str, dict] = {}
    for version, record in copilot_events(home):
        kind, data = record["type"], record["data"]
        if kind == "session.usage_record" and isinstance(data.get("usage"), dict):
            name, kept = "events-usage-record", {"usage": pick(data["usage"], COPILOT_USAGE_KEYS)}
            if record.get("agentId"):
                name = "events-subagent-usage-record"
        elif kind in COPILOT_COMPACTION_KEYS:
            name, kept = "events-" + kind.split(".")[1].replace("_", "-"), pick(data, COPILOT_COMPACTION_KEYS[kind])
        elif kind == "session.start":
            name, kept = "events-session-start", pick(data, COPILOT_SESSION_KEYS)
        else:
            continue
        native = {"type": kind, "data": kept}
        for key in ("id", "agentId"):
            if isinstance(record.get(key), str):
                native[key] = scrub.fake_id(record[key])
        found.setdefault(
            name,
            {
                "source": f"Copilot {version} events.jsonl {kind} record, numeric and structural fields only",
                "record": native,
            },
        )
    return found


def copilot_contract(contract: Path) -> dict[str, dict]:
    """Record the hand-maintained native output contract after verifying it against the SDK d.ts.

    Args:
        contract: The SDK hook contracts d.ts.

    Returns:
        The one `output-contract` fixture with the d.ts digest as provenance.

    Raises:
        SystemExit: When the d.ts does not declare a maintained output type or field.
    """
    text = contract.read_text()
    outputs = {}
    for event, (name, fields) in COPILOT_OUTPUT_CONTRACT.items():
        start = re.search(rf"export (?:interface|type) {name}\b", text)
        if not start:
            msg = f"{contract.name} does not declare {name}"
            raise SystemExit(msg)
        region = text[start.start() :].split("\nexport ")[0]
        absent = [field for field in fields if not re.search(rf"\b{field}\??:", region)]
        if absent:
            msg = f"{contract.name} {name} lacks maintained fields: {absent}"
            raise SystemExit(msg)
        outputs[event] = {"type": name, "fields": list(fields)}
    source = (
        f"Maintained from the Copilot SDK hook contracts d.ts ({COPILOT_CONTRACT_FILE}, "
        f"sha256 {hashlib.sha256(text.encode()).hexdigest()}); independent of adapters/copilot"
    )
    return {"output-contract": {"source": source, "outputs": outputs, "block_semantics": COPILOT_BLOCK_EVENTS}}


def record_copilot(home: Path, contract: Path | None, scrub: Scrubber) -> dict[str, dict]:
    """Record Copilot fixtures from an explicitly given Copilot home's native event logs.

    Args:
        home: The Copilot home to read; never defaulted to a live home.
        contract: The SDK hook contracts d.ts, or None to omit the output contract fixture.
        scrub: The recording's scrubber.

    Returns:
        Fixture names mapped to their content.

    Raises:
        SystemExit: When the home holds no hook.start input.
    """
    found = copilot_hooks(home, scrub)
    if not found:
        msg = f"no Copilot hook.start inputs under {home}/session-state"
        raise SystemExit(msg)
    found.update(copilot_telemetry(home, scrub))
    if contract:
        found.update(copilot_contract(contract))
    return found


LIFECYCLE_MARKERS = ("PARENT_IDLE_ACP", "CHILD_RETURNED_ACP", "S_CHILD_4P6C", "TOOL_READ_OK")
LIFECYCLE_TYPES = frozenset(
    {
        "assistant.message",
        "assistant.turn_start",
        "assistant.idle",
        "subagent.started",
        "subagent.completed",
        "user.message",
    }
)


def lifecycle_text(text: str) -> str:
    """Keep only the experiment's controlled sentinels from native model text.

    Args:
        text: Native model output, never user input.

    Returns:
        Sentinel occurrences in their original order, with all other text removed.
    """
    return "".join(match.group() for match in re.finditer("|".join(LIFECYCLE_MARKERS), text))


def lifecycle_event(record: dict, scrub: Scrubber) -> dict:
    """Reduce a native journal or ACP event to independent lifecycle evidence.

    Args:
        record: Native event object.
        scrub: Stable identity scrubber shared across all evidence sources.

    Returns:
        Actual native event fields with identities aliased and content redacted.
    """
    result = {key: record[key] for key in ("type", "timestamp")}
    for key in ("id", "sessionId", "agentId"):
        if record.get(key):
            result[key] = scrub.fake_id(record[key])
    data = record.get("data", {})
    kept = {
        key: scrub.fake_id(data[key])
        for key in ("messageId", "interactionId", "toolCallId", "sessionId")
        if data.get(key)
    }
    for key in ("executionMode", "turnId", "model"):
        if isinstance(data.get(key), str) and SAFE_TOKEN.fullmatch(data[key]):
            kept[key] = data[key]
    if record["type"] == "assistant.message" and isinstance(data.get("content"), str):
        kept["content"] = lifecycle_text(data["content"])
    if record["type"] == "user.message":
        kept["content"] = PROMPT_TEXT
        if "source" in data:
            kept["source"] = "system" if data["source"] == "system" else SCRUBBED
    result["data"] = kept
    return result


def lifecycle_chunk_masks(rows: list[dict]) -> dict[int, str]:
    """Redact ACP text without losing original marker splits across native chunks.

    Args:
        rows: Original ACP wire messages in capture order.

    Returns:
        Native wire indices mapped to their sentinel-only chunk text.
    """
    groups: dict[str, list[tuple[int, str]]] = {}
    for index, row in enumerate(rows):
        update = row.get("message", {}).get("params", {}).get("update", {})
        if update.get("sessionUpdate") == "agent_message_chunk":
            groups.setdefault(update["messageId"], []).append((index, update["content"].get("text", "")))
    kept = {}
    for chunks in groups.values():
        text = "".join(chunk for _, chunk in chunks)
        offsets = {
            position
            for match in re.finditer("|".join(LIFECYCLE_MARKERS), text)
            for position in range(match.start(), match.end())
        }
        offset = 0
        for index, chunk in chunks:
            kept[index] = "".join(char for position, char in enumerate(chunk, offset) if position in offsets)
            offset += len(chunk)
    return kept


def lifecycle_wire(rows: list[dict], scrub: Scrubber) -> list[dict]:
    """Retain submissions, native lifecycle notifications and model-output chunks.

    Args:
        rows: Original ACP wire messages.
        scrub: The capture's shared stable identity scrubber.

    Returns:
        Scrubbed native messages retaining their original wire sequence indices.
    """
    chunks = lifecycle_chunk_masks(rows)
    kept = []
    for index, row in enumerate(rows):
        message, direction = row["message"], row["direction"]
        method, params = message.get("method"), message.get("params", {})
        if direction == "send" and method == "session/prompt":
            value = {"sessionId": scrub.fake_id(params["sessionId"]), "prompt": PROMPT_TEXT}
        elif direction == "receive" and method == "github.com/copilot/sessionEvent":
            value = lifecycle_event(params, scrub)
        elif direction == "receive" and index in chunks:
            update = params["update"]
            native = {
                "sessionUpdate": update["sessionUpdate"],
                "messageId": scrub.fake_id(update["messageId"]),
                "content": {"type": "text", "text": chunks[index]},
            }
            agent = update.get("_meta", {}).get("github.com/copilot", {}).get("agentId")
            if agent:
                native["_meta"] = {"github.com/copilot": {"agentId": scrub.fake_id(agent)}}
            value = {"sessionId": scrub.fake_id(params["sessionId"]), "update": native}
        else:
            continue
        kept.append({"sequence": index, "direction": direction, "message": {"method": method, "params": value}})
    return kept


def lifecycle_artifact(path: Path, name: str) -> dict:
    """Describe a read-only capture by logical name and original byte digest.

    Args:
        path: Explicit capture file to hash.
        name: Nonpersonal logical artifact identity.

    Returns:
        Source byte count and SHA-256 provenance, without filesystem paths.
    """
    data = path.read_bytes()
    return {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def record_copilot_lifecycle(root: Path, scrub: Scrubber) -> dict[str, dict]:
    """Record measured idle-parent wake from explicitly supplied native spike captures.

    Args:
        root: Explicit capture directory; never inferred from a runtime home.
        scrub: Shared native identity scrubber.

    Returns:
        One lifecycle fixture, without inferred or precomputed success flags.

    Raises:
        SystemExit: The captured session identity or runtime provenance does not match.
    """
    session = (root / "acp-lifecycle-session-id").read_text().strip()
    if not re.fullmatch(r"[a-f0-9-]{36}", session):
        msg = "invalid captured ACP lifecycle session identity"
        raise SystemExit(msg)
    paths = {
        "journal": root / "home/session-state" / session / "events.jsonl",
        "lifecycle": root / "acp-lifecycle-events.json",
        "wire": root / "acp-lifecycle-wire.jsonl",
        "manifest": root / "source-provenance.json",
    }
    journal = list(rollout_records(paths["journal"]))
    lifecycle = json.loads(paths["lifecycle"].read_text())
    wire = list(rollout_records(paths["wire"]))
    manifest = json.loads(paths["manifest"].read_text())
    starts = [row["data"] for row in journal if row.get("type") == "session.start"]
    if not any(
        data.get("sessionId") == session and data.get("copilotVersion") == manifest["version"] for data in starts
    ):
        msg = "captured journal identity/version does not match lifecycle provenance"
        raise SystemExit(msg)
    provenance = {
        "version": manifest["version"],
        "build_commit": manifest["buildCommit"],
        "artifacts": [lifecycle_artifact(path, name) for name, path in paths.items()],
        "runtime_sources": [
            {"name": Path(item["path"]).name, "sha256": item["sha256"], "bytes": item["bytes"]}
            for item in manifest["files"]
        ],
    }
    fixture = {
        "source": "Measured Copilot native ACP idle-parent wake; offline captured observation, not fresh execution",
        "provenance": provenance,
        "root_session": scrub.fake_id(session),
        "journal": [lifecycle_event(record, scrub) for record in journal if record.get("type") in LIFECYCLE_TYPES],
        "lifecycle": [lifecycle_event(record, scrub) for record in lifecycle],
        "wire": lifecycle_wire(wire, scrub),
    }
    return {"lifecycle/idle-parent-wake": fixture}


def write(found: dict[str, dict], runtime: str, out: Path, *, dry_run: bool, scrub: Scrubber) -> None:
    """Check and write one runtime's fixtures, printing each path.

    Args:
        found: Fixture names mapped to their content.
        runtime: The runtime whose directory under `out` receives them.
        out: The fixtures root.
        dry_run: Print the paths without writing.
        scrub: The recording's scrubber, which refuses sensitive content.
    """
    for name, data in sorted(found.items()):
        scrub.check(data)
        path = out / runtime / f"{name}.json"
        print(
            f"{'would write' if dry_run else 'wrote'} {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}"
        )
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def default_codex_binary() -> Path | None:
    """Locate the codex binary on PATH.

    Returns:
        Its resolved path, or None when it is not on PATH.
    """
    found = shutil.which("codex")
    return Path(os.path.realpath(found)) if found else None


def default_genie_binary() -> Path | None:
    """Locate the Genie binary on PATH.

    Returns:
        Its resolved path, or None when it is not on PATH.
    """
    found = shutil.which("genie")
    return Path(os.path.realpath(found)) if found else None


def record_copilot_inputs(args: argparse.Namespace, scrub: Scrubber) -> dict[str, dict]:
    """Record all explicitly supplied Copilot artifact roots.

    Args:
        args: Parsed explicit native source paths and recording options.
        scrub: Primary native identity scrubber.

    Returns:
        Hook, telemetry and optional measured lifecycle fixtures.
    """
    found = record_copilot(args.copilot_home, args.copilot_contract, scrub)
    if args.copilot_lifecycle_root is not None:
        found.update(record_copilot_lifecycle(args.copilot_lifecycle_root, scrub))
    for extra_home in args.copilot_extra_home:
        extra_scrub = CopilotScrubber(extra_home)
        extra = record_copilot(extra_home, None, extra_scrub)
        for name, fixture in extra.items():
            extra_scrub.check(fixture)
            found.setdefault(name, fixture)
    return found


def validate_cursor_capture_route(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Keep the explicit lifecycle route isolated from runtime-home discovery.

    Args:
        args: Parsed recorder arguments.
        parser: Parser reporting invalid combinations before any capture reads.
    """
    if args.cursor_lifecycle_root is not None and (args.runtime != "cursor" or args.cursor_capture is not None):
        parser.error("--cursor-lifecycle-root requires --runtime cursor and excludes --cursor-capture")


def main(argv: list[str] | None = None) -> int:
    """Record the selected runtimes' fixtures.

    Args:
        argv: Command-line arguments, or None for `sys.argv`.

    Returns:
        0; recording problems exit through SystemExit.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", choices=(*sorted({*discover(), "copilot"}), "all"), default="all")
    parser.add_argument("--home", type=Path, default=Path.home(), help="home to read artifacts from (read only)")
    parser.add_argument(
        "--copilot-home",
        type=Path,
        default=None,
        help="explicit Copilot home or sandbox to read (read only); Copilot is never recorded from a default home",
    )
    parser.add_argument(
        "--copilot-lifecycle-root",
        type=Path,
        default=None,
        help="explicit native ACP lifecycle capture directory (read only)",
    )
    parser.add_argument(
        "--claude-lifecycle-root",
        type=Path,
        help="explicit native Claude stream capture (read only; records lifecycle only)",
    )
    parser.add_argument("--copilot-contract", type=Path, default=None, help="SDK hook contracts d.ts (read only)")
    parser.add_argument(
        "--copilot-extra-home", type=Path, action="append", default=[], help="additional explicit native capture root"
    )
    parser.add_argument("--cursor-lifecycle-root", type=Path, help="explicit Cursor lifecycle capture only (read only)")
    parser.add_argument("--cursor-capture", type=Path, help="explicit captured native hook JSONL; capture only")
    parser.add_argument("--codex-binary", type=Path, default=None)
    parser.add_argument("--genie-binary", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=FIXTURES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    validate_cursor_capture_route(args, parser)
    runtimes = tuple(discover()) if args.runtime == "all" else (args.runtime,)
    if args.runtime == "all" and args.copilot_home is None:
        runtimes = tuple(runtime for runtime in runtimes if runtime != "copilot")
    missing = set(runtimes) - RECORDER_RUNTIMES
    if missing:
        parser.error(f"no native fixture recorder for: {', '.join(sorted(missing))}")
    if "copilot" in runtimes and args.copilot_home is None:
        parser.error("--runtime copilot requires an explicit --copilot-home")
    for runtime in runtimes:
        scrub = CopilotScrubber(args.copilot_home) if runtime == "copilot" else Scrubber(args.home)
        if runtime == "copilot":
            found = record_copilot_inputs(args, scrub)
        elif runtime == "cursor":
            found = (
                record_cursor_lifecycle(args.cursor_lifecycle_root, scrub.fake_id)
                if args.cursor_lifecycle_root is not None
                else (
                    record_cursor_capture(args.cursor_capture, scrub)
                    if args.cursor_capture
                    else record_cursor(args.home, scrub)
                )
            )
        elif runtime == "codex":
            found = record_codex(args.home, args.codex_binary or default_codex_binary(), scrub)
        elif runtime == "genie":
            found = record_genie(args.home, args.genie_binary or default_genie_binary(), scrub)
        elif runtime == "claude":
            found = (
                record_claude_lifecycle(args.claude_lifecycle_root, scrub.fake_id)
                if args.claude_lifecycle_root is not None
                else record_claude(args.home, scrub)
            )
        write(found, runtime, args.out, dry_run=args.dry_run, scrub=scrub)
    return 0


if __name__ == "__main__":
    sys.exit(main())
