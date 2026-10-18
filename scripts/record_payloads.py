#!/usr/bin/env python3
"""Record scrubbed hook payload fixtures from real runtime artifacts; every source is read only.

- Cursor: hook stdin dumps in Cursor's hook output logs, one chat record (composerData)
  from its state.vscdb, and the context usage message of the newest Cursor CLI chat store
  (~/.cursor/chats/*/*/store.db), all opened immutable.
- Codex: hook stdin is not logged, so payloads are built from real rollout values (session,
  turn, model, spawn_agent arguments, subagent ids) and must validate against the hook
  input schemas embedded in the codex binary; those schemas (input and output) are stored too.
- Claude Code: hook stdin is not logged, so payloads are built from real transcript values
  (session, model, tool inputs and results, subagent ids) with the documented hook fields.

Output: tests/fixtures/<runtime>/<event>-<case>.json = {"source", "event", "payload"} plus
telemetry templates. Prompts, commands, file contents and tool output are replaced, paths
become {HOME}/{PROJECT} placeholders and ids become stable fakes; a recording that still
matches a sensitive pattern is refused.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import string
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.cursor.hooks import CLI_USAGE_FIELD, LENGTH_DELIMITED, cli_root_id, cli_usage, proto_fields

FIXTURES = ROOT / "tests" / "fixtures"
HOME_TOKEN, PROJECT_TOKEN = "{HOME}", "{PROJECT}"
SCRUBBED, PROMPT_TEXT, EMAIL = "<scrubbed>", "recorded prompt (scrubbed)", "user@example.com"
KEEP_KEYS = frozenset({
    "hook_event_name", "model", "model_id", "composer_mode", "cursor_version", "tool_name", "status", "source",
    "permission_mode", "subagent_type", "agent_type", "reasoning_effort", "fork_turns", "trigger", "type", "id",
    "value", "output_mode", "resolvedModel", "effort", "subagent_model", "subtype", "role", "unifiedMode"})
ID_KEYS = frozenset({
    "conversation_id", "session_id", "generation_id", "tool_use_id", "parent_tool_call_id", "turn_id", "agent_id",
    "subagent_id", "call_id", "agentId", "parent_conversation_id", "tool_call_id"})
PATH_KEYS = frozenset({"cwd", "transcript_path", "agent_transcript_path", "file_path", "path", "target_file",
                       "notebook_path", "workspace_roots"})
SAFE_TOKEN = re.compile(r"[\w.:\-\[\]=,/ ]{1,60}")
STRUCTURAL = frozenset({".codex", "sessions", ".claude", "projects", "subagents", ".cursor", "agent-transcripts",
                        ".context-guard", "handoffs"})
ROLLOUT_NAME = re.compile(r"(rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-)(.+)(\.jsonl)")
ID_LIKE = re.compile(r"(agent-)?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{12,})(\.jsonl)?", re.IGNORECASE)
SENSITIVE = (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), re.compile(r"eyJ[A-Za-z0-9_-]{10,}\."),
             re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), re.compile(r"AKIA[0-9A-Z]{16}"), re.compile(r"xox[abprs]-"),
             re.compile(r"/Users/|/home/(?!user\b)"))
CURSOR_LOGS = "Library/Application Support/Cursor/logs/*/window*/output_*/cursor.hooks.*.log"
CURSOR_DB = "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
CURSOR_CLI_STORES = ".cursor/chats/*/*/store.db"
CURSOR_CASES = ("sessionStart-root", "beforeSubmitPrompt-root", "preToolUse-task", "preToolUse-shell",
                "postToolUse-task", "postToolUse-read", "postToolUse-write", "postToolUse-shell", "postToolUse-grep",
                "postToolUse-mcp", "postToolUse-subagent-shell", "postToolUse-subagent-read", "stop-completed",
                "stop-aborted")
COMPOSER_KEYS = ("_v", "status", "lastUpdatedAt", "createdAt", "name", "unifiedMode", "contextUsagePercent",
                 "contextTokensUsed", "contextTokenLimit", "isAgentic")
CODEX_SCHEMAS = ("session-start", "user-prompt-submit", "post-tool-use", "stop", "subagent-stop")
SCHEMA_MARK = re.compile(rb"\.command\.(input|output)\{")
PATCH = f"*** Begin Patch\n*** Update File: {PROJECT_TOKEN}/file.py\n@@\n-old\n+new\n*** End Patch\n"


class Scrubber:
    """Replace prompts, text, paths and ids; one instance keeps fake ids consistent across a recording."""

    def __init__(self, home: Path):
        self.home = str(home)
        self.projects: list[str] = []
        self.emails: set[str] = set()

    def fake_id(self, real: str) -> str:
        digest = hashlib.sha256(("context-guard-fixture:" + real).encode()).hexdigest() * 4
        prefix, sep, rest = real.rpartition("_") if "_" in real else ("", "", real)
        chars = iter(digest)
        body = "".join(next(chars) if ch in string.ascii_letters + string.digits else ch for ch in rest)
        return prefix + sep + body

    def segment(self, part: str) -> str:
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
        for root, token in [(project, PROJECT_TOKEN) for project in self.projects] + [(self.home, HOME_TOKEN)]:
            if value == root or value.startswith(root + "/"):
                rest = [self.segment(part) for part in value[len(root):].split("/") if part]
                return "/".join([token, *rest])
        return "/" + "/".join(self.segment(part) for part in value.split("/") if part) if value.startswith("/") \
            else SCRUBBED

    def text(self, key: str, value: str) -> str:
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
        if key in KEEP_KEYS and SAFE_TOKEN.fullmatch(value) and self.home not in value:
            return value
        return SCRUBBED

    def value(self, key: str, value: object) -> object:
        if isinstance(value, dict):
            return {name: self.value(name, item) for name, item in value.items()}
        if isinstance(value, list):
            return [self.value(key, item) for item in value]
        if isinstance(value, str):
            return self.text(key, value)
        return value

    def payload(self, payload: dict, projects: list[str]) -> dict:
        self.projects = sorted((p for p in projects if p), key=len, reverse=True)
        return self.value("", payload)

    def check(self, recorded: object) -> None:
        text = json.dumps(recorded)
        leaks = [p.pattern for p in SENSITIVE if p.search(text)] + [e for e in self.emails if e in text]
        if self.home in text or leaks:
            raise SystemExit(f"refusing to write a recording that still holds sensitive text: {leaks or self.home}")


def projects_of(payload: dict) -> list[str]:
    roots = payload.get("workspace_roots") if isinstance(payload.get("workspace_roots"), list) else []
    return [root for root in [*roots, payload.get("cwd")] if isinstance(root, str)]


def entry(source: str, event: str, payload: dict) -> dict:
    return {"source": source, "event": event, "payload": payload}


def cursor_inputs(paths: list[Path]) -> Iterator[dict]:
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
    event = payload["hook_event_name"]
    if event in ("preToolUse", "postToolUse"):
        name = str(payload.get("tool_name", ""))
        tool = "mcp" if name.startswith("MCP:") else name.lower()
        return f"{event}-{'subagent-' if payload.get('parent_tool_call_id') else ''}{tool}"
    if event == "stop":
        return f"stop-{payload.get('status', 'unknown')}"
    return f"{event}-root"


def record_cursor(home: Path, scrub: Scrubber) -> dict[str, dict]:
    paths = sorted((Path(p) for p in glob.glob(str(home / CURSOR_LOGS))), key=lambda p: p.stat().st_mtime)
    latest = {}
    for payload in cursor_inputs(paths):
        case = cursor_case(payload)
        if case in CURSOR_CASES:
            latest[case] = payload
    source = "Cursor hook log stdin dump (cursor.hooks.*.log), scrubbed"
    found = {case: entry(source, payload["hook_event_name"], scrub.payload(payload, projects_of(payload)))
             for case, payload in latest.items()}
    composer = cursor_composer(home / CURSOR_DB)
    if composer:
        found["composer-data"] = {"source": "Cursor state.vscdb cursorDiskKV composerData:<id>, scrubbed",
                                  "record": composer}
    usage = cursor_cli_usage(home)
    if usage:
        found["cli-store"] = {"source": "Cursor CLI store.db root blob field 5 (context usage), numbers and "
                                        "category labels only", "record": usage}
    return found


def cli_store_root(database: Path) -> bytes | None:
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
    """The newest CLI chat's usage message, kept as hex with its decoded tokens and window."""
    stores = sorted((Path(p) for p in glob.glob(str(home / CURSOR_CLI_STORES))),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    for database in stores[:50]:
        root = cli_store_root(database)
        try:
            usage = [value for number, wire, value in proto_fields(root or b"")
                     if number == CLI_USAGE_FIELD and wire == LENGTH_DELIMITED]
            tokens, window = cli_usage(root) if usage else (None, None)
        except ValueError:
            continue
        if tokens is not None and window is not None:
            return {"usage_hex": usage[-1].hex(), "tokens": tokens, "window": window}
    return None


def cursor_composer(database: Path) -> dict | None:
    if not database.is_file():
        return None
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        rows = connection.execute("select value from cursorDiskKV where key like 'composerData:%' "
                                  "order by rowid desc limit 500").fetchall()
    finally:
        connection.close()
    for (value,) in rows:
        try:
            record = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("contextTokensUsed"), int):
            kept = {key: record[key] for key in COMPOSER_KEYS if key in record}
            return {key: (SCRUBBED if isinstance(item, str) and key == "name" else item) for key, item in kept.items()}
    return None


def codex_schemas(binary: Path) -> dict[str, dict]:
    """The schemas are stored as `<name>.command.<side>{...}`; the decoded `title` names each one."""
    data = binary.read_bytes()
    decoder, found = json.JSONDecoder(), {}
    for match in SCHEMA_MARK.finditer(data):
        start = match.end() - 1
        try:
            schema, _ = decoder.raw_decode(data[start:start + 200_000].decode("utf-8", "replace"))
        except ValueError:
            continue
        title = schema.get("title") if isinstance(schema, dict) else None
        if isinstance(title, str) and title.endswith(".command." + match[1].decode()):
            found[title] = schema
    return found


def rollout_records(path: Path) -> Iterator[dict]:
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def rollout_facts(path: Path) -> dict:
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
    paths = sorted((Path(p) for p in glob.glob(str(home / ".codex/sessions/*/*/*/rollout-*.jsonl"))),
                   key=lambda p: p.stat().st_mtime, reverse=True)
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
    raise SystemExit("no Codex root rollout with spawn_agent and no subagent rollout found")


def codex_pool(root: dict, child: dict) -> dict:
    turn, meta = root["turn"], root["meta"]
    spawn_args = json.loads(root["spawn"].get("arguments") or "{}")
    return {
        "session_id": meta["id"], "transcript_path": root["path"], "cwd": meta.get("cwd") or turn.get("cwd"),
        "model": turn.get("model"), "turn_id": turn.get("turn_id"),
        "permission_mode": "bypassPermissions" if turn.get("approval_policy") == "never" else "default",
        "prompt": PROMPT_TEXT, "stop_hook_active": False, "last_assistant_message": SCRUBBED,
        "tool_response": SCRUBBED, "tool_use_id": (root.get("call") or root["spawn"]).get("call_id", "call_0"),
        "spawn": spawn_args,
        "child": {"agent_id": child["meta"]["id"], "agent_type": child["meta"].get("agent_role") or "default",
                  "turn_id": child["turn"].get("turn_id"), "model": child["turn"].get("model") or turn.get("model")},
        "agent_transcript_path": child["path"]}


def codex_cases(pool: dict) -> dict[str, tuple[str, dict]]:
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
        "PostToolUse-spawn-agent": ("PostToolUse", {"tool_name": "spawn_agent", "tool_input": pool["spawn"]}),
        "PostToolUse-subagent-bash": ("PostToolUse", {**bash, **agent}),
        "Stop-root": ("Stop", {}),
        "SubagentStop-subagent": ("SubagentStop", {**agent, "agent_transcript_path": pool["agent_transcript_path"]}),
    }


def schema_payload(schema: dict, event: str, pool: dict, extra: dict) -> dict:
    """Exactly the schema's properties that real values exist for; a missing required one is an error."""
    values = {**{k: v for k, v in pool.items() if k != "agent_transcript_path"}, **extra, "hook_event_name": event}
    payload = {key: values[key] for key in schema["properties"] if key in values and values[key] is not None}
    missing = [key for key in schema.get("required", []) if key not in payload]
    if missing:
        raise SystemExit(f"Codex {event}: no recorded value for required fields {missing}")
    return payload


def record_codex(home: Path, binary: Path | None, scrub: Scrubber) -> dict[str, dict]:
    if binary is None or not binary.is_file():
        raise SystemExit("codex binary not found; pass --codex-binary")
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
    found["rollout-token-count"] = {"source": "Codex rollout event_msg token_count, numbers kept",
                                    "record": token_count_record(root["token_count"])}
    return found


def kebab(event: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"-\1", event).lower()


def token_count_record(record: dict) -> dict:
    info = record["payload"]["info"]
    kept = {key: info[key] for key in ("total_token_usage", "last_token_usage", "model_context_window") if key in info}
    return {"timestamp": record.get("timestamp"), "type": "event_msg", "payload": {"type": "token_count", "info": kept}}


def transcript_facts(path: Path) -> dict:
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
    paths = sorted((Path(p) for p in glob.glob(str(home / ".claude/projects/*/*.jsonl"))),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        agents = sorted(path.with_suffix("").glob("subagents/agent-*.jsonl"))
        if not agents:
            continue
        facts = transcript_facts(path)
        if "Agent" in facts["tools"] and facts.get("assistant"):
            child = next((transcript_facts(a) for a in agents if transcript_facts(a)["tools"]), None)
            if child:
                return facts, child, path
    raise SystemExit("no Claude transcript with an Agent call and a subagent transcript found")


def claude_tool(facts: dict, name: str) -> dict | None:
    block = facts["tools"].get(name)
    if block is None:
        return None
    result = facts["results"].get(block.get("id"))
    return {"tool_name": name, "tool_input": block.get("input") or {},
            "tool_response": result if result is not None else SCRUBBED, "tool_use_id": block.get("id")}


def claude_cases(main: dict, child: dict) -> dict[str, tuple[str, dict]]:
    agent_id = Path(child["path"]).stem.removeprefix("agent-")
    agent_type = (main["tools"]["Agent"].get("input") or {}).get("subagent_type") or "general-purpose"
    agent = {"agent_id": agent_id, "agent_type": agent_type}
    cases = {
        "SessionStart-startup": ("SessionStart", {"source": "startup", "model": main.get("model")}),
        "SessionStart-compact": ("SessionStart", {"source": "compact", "model": main.get("model")}),
        "UserPromptSubmit-root": ("UserPromptSubmit", {"prompt": PROMPT_TEXT}),
        "Stop-root": ("Stop", {"stop_hook_active": False, "last_assistant_message": SCRUBBED}),
        "SubagentStop-subagent": ("SubagentStop", {**agent, "agent_transcript_path": child["path"],
                                                   "stop_hook_active": False, "last_assistant_message": SCRUBBED}),
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
    main, child, path = claude_transcripts(home)
    common = {"session_id": main["session"], "transcript_path": str(path), "cwd": main["cwd"]}
    source = "Claude Code transcript values (session, model, tool_use input and toolUseResult) with documented fields"
    found = {}
    for case, (event, extra) in claude_cases(main, child).items():
        mode = {} if event in ("SessionStart", "PreCompact", "PostCompact") else {"permission_mode": main["mode"]}
        payload = {**common, **mode, "hook_event_name": event, **extra}
        found[case] = entry(source, event, scrub.payload(payload, [main["cwd"]]))
    found["transcript-assistant"] = {"source": "Claude transcript assistant record, usage numbers kept",
                                     "record": assistant_record(main["assistant"])}
    boundary = next((f["boundary"] for f in map(transcript_facts, boundary_candidates(home)) if "boundary" in f), None)
    if boundary:
        meta = boundary.get("compactMetadata") or {}
        found["transcript-compact-boundary"] = {
            "source": "Claude transcript compact_boundary record",
            "record": {"type": "system", "subtype": "compact_boundary", "timestamp": boundary.get("timestamp"),
                       "compactMetadata": {key: meta.get(key) for key in ("trigger", "preTokens")}}}
    return found


def boundary_candidates(home: Path) -> list[Path]:
    found = subprocess.run(["rg", "-l", "--max-count", "1", "compact_boundary", str(home / ".claude/projects")],
                           capture_output=True, text=True, check=False).stdout.split()
    return [Path(p) for p in found[:20]]


def assistant_record(record: dict) -> dict:
    message = record["message"]
    usage = {key: value for key, value in message["usage"].items() if type(value) is int}
    return {"type": "assistant", "timestamp": record.get("timestamp"),
            "message": {"model": message.get("model"), "role": "assistant", "content": [], "usage": usage}}


def write(found: dict[str, dict], runtime: str, out: Path, dry_run: bool, scrub: Scrubber) -> None:
    for name, data in sorted(found.items()):
        scrub.check(data)
        path = out / runtime / f"{name}.json"
        print(f"{'would write' if dry_run else 'wrote'} {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def default_codex_binary() -> Path | None:
    found = subprocess.run(["which", "codex"], capture_output=True, text=True, check=False).stdout.strip()
    return Path(os.path.realpath(found)) if found else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", choices=("cursor", "codex", "claude", "all"), default="all")
    parser.add_argument("--home", type=Path, default=Path.home(), help="home to read artifacts from (read only)")
    parser.add_argument("--codex-binary", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=FIXTURES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    runtimes = ("cursor", "codex", "claude") if args.runtime == "all" else (args.runtime,)
    for runtime in runtimes:
        scrub = Scrubber(args.home)
        if runtime == "cursor":
            found = record_cursor(args.home, scrub)
        elif runtime == "codex":
            found = record_codex(args.home, args.codex_binary or default_codex_binary(), scrub)
        else:
            found = record_claude(args.home, scrub)
        write(found, runtime, args.out, args.dry_run, scrub)
    return 0


if __name__ == "__main__":
    sys.exit(main())
