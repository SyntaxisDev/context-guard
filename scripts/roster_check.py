#!/usr/bin/env python3
"""Check that a role subagent spawned without a per-spawn model runs on its roster tier model.

scripts/plugin_live.sh runs one case per runtime. The parent runs on a different model than the
role's tier, spawns the role with no `model` argument and replies; the case passes when the
subagent's observed model equals the model adapters/<runtime>/config.json gives the role's tier
(Cursor: the tier's `call` slug). Cursor's policy has the parent pass the tier slug on every Task, so
a passed slug passes and any other passed model fails. A Genie case whose parent session ran on the
expected model cannot tell the agent file from the parent and is reported as `fail inconclusive ...`.
The command after `--` starts the runtime without a prompt; this script appends the prompt.
Every stdout line goes to the log. The verdict is one stdout line,
`ok <detail>` (exit 0) or `fail <reason>` (exit 1).

    roster_check.py claude --cwd DIR --log FILE [--plain-names] -- claude -p --output-format stream-json --verbose ...
    roster_check.py cursor --cwd DIR --log FILE -- cursor-agent -p --output-format stream-json ...
    roster_check.py codex  --cwd DIR --log FILE --sessions ~/.codex/sessions -- codex exec --json ...
    roster_check.py genie  --cwd DIR --log FILE --sessions ~/.genie/sessions -- genie exec --json ...

Observed model per runtime: Claude, the `message.model` of the subagent's assistant messages in the
stream-json output; Cursor, the completed Task call's `args.model`; Codex and Genie, the
`turn_context` model of the subagent rollout (`session_meta` role and cwd) written during the run.
Claude spawns the plugin's `context-guard:<role>` unless `--plain-names` (script install).
Cursor reads agents only from the workspace, so the rendered role files are copied to
`<cwd>/.cursor/agents/` first (the installer writes the same content to ~/.cursor/agents).
"""

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PRIVATE = "/private"
KILL_SECONDS = 10
CLOCK_SLACK = 2.0
# runtime -> role to spawn; its default tier is the expected one. Genie spawns a cheap role because
# its default session model is the standard/deep one.
# Cursor spawns the readonly scout, which needs no approval without --force.
ROLES = {"claude": "investigator", "cursor": "scout", "codex": "investigator", "genie": "runner"}
PROMPT = (
    "Spawn exactly one {role} subagent using the {tool} tool, with the task: 'Reply with exactly: ok'. "
    "Do not set a model, effort or any other optional parameter on the spawn{exception}. Wait for it, then "
    "end your turn by replying with only the word done."
)
# Codex and Genie's spawn tool asks for fork_turns; their policy sets it to "none".
FORK_EXCEPTION = ' except fork_turns, which you set to "none"'

TOOLS = {
    "claude": "Agent (subagent_type {name})",
    "cursor": "Task (subagent_type {name})",
    "codex": "spawn_agent (agent_type {name})",
    "genie": "spawn_agent (agent_type {name})",
}


class CheckError(Exception):
    """A step of the check failed; the message is the reason reported."""


def decode(line: str) -> dict | None:
    """Decode one output line.

    Args:
        line: A stdout or rollout line.

    Returns:
        The JSON object, or None for anything else.
    """
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def strip_private(path: str) -> str:
    """Drop macOS's `/private` prefix so both spellings of a temporary path compare equal.

    Args:
        path: An absolute path.

    Returns:
        The path without a leading `/private`.
    """
    return path.removeprefix(PRIVATE) if path.startswith(PRIVATE + "/") else path


def expected_model(runtime: str, role: str) -> str:
    """Read the model the role's default tier maps to in the runtime's adapter config.

    Args:
        runtime: claude, cursor, codex or genie.
        role: The role name.

    Returns:
        The tier's `model`, or its `call` slug for Cursor.

    Raises:
        CheckError: The role or tier is missing.
    """
    roles = json.loads((ROOT / "content" / "roles" / "roles.json").read_text())["roles"]
    tiers = json.loads((ROOT / "adapters" / runtime / "config.json").read_text())["tiers"]
    try:
        tier = roles[role]["default_tier"]
        return tiers[tier]["call" if runtime == "cursor" else "model"]
    except KeyError as error:
        msg = f"no tier model for role {role} in adapters/{runtime}/config.json: {error}"
        raise CheckError(msg) from error


def stream_messages(lines: Iterable[str]) -> list[dict]:
    """Decode the JSON object lines of a stream-json run.

    Args:
        lines: Output lines.

    Returns:
        The decoded objects in order.
    """
    return [message for line in lines if (message := decode(line)) is not None]


def claude_model(messages: Iterable[dict], role: str) -> str | None:
    """Find the model of the role's subagent in Claude Code stream-json messages.

    Args:
        messages: Decoded stream-json messages.
        role: The role name; the spawn's `subagent_type` ends with it (plugin roles are namespaced).

    Returns:
        The `message.model` of the first assistant message sent from inside that spawn, or None.
    """
    spawned: set[str] = set()
    for message in messages:
        body = message.get("message")
        if not isinstance(body, dict) or message.get("type") != "assistant":
            continue
        parent = message.get("parent_tool_use_id")
        if parent is None:
            for block in body.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                kind = (block.get("input") or {}).get("subagent_type")
                if isinstance(kind, str) and kind.split(":")[-1] == role:
                    spawned.add(block.get("id"))
        elif parent in spawned and isinstance(body.get("model"), str):
            return body["model"]
    return None


def cursor_model(messages: Iterable[dict], role: str, subtype: str = "completed") -> str | None:
    """Find the model of the role's Task call in Cursor stream-json messages.

    Args:
        messages: Decoded stream-json messages.
        role: The role name, `args.subagentType.custom.name` of the Task call.
        subtype: The record to read: "completed" (the resolved model) or "started" (the parent's argument).

    Returns:
        The call's `args.model`, or None.
    """
    for message in messages:
        if message.get("type") != "tool_call" or message.get("subtype") != subtype:
            continue
        call = message.get("tool_call")
        args = call.get("taskToolCall", {}).get("args") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        subagent = args.get("subagentType")
        custom = subagent.get("custom") if isinstance(subagent, dict) else None
        if isinstance(custom, dict) and custom.get("name") == role and isinstance(args.get("model"), str):
            return args["model"]
    return None


def cursor_started_model(messages: Iterable[dict], role: str) -> str | None:
    """Find the model the parent passed on the role's Task call, as the started record shows it.

    Args:
        messages: Decoded stream-json messages.
        role: The role name, `args.subagentType.custom.name` of the Task call.

    Returns:
        The started call's `args.model`, or None when the parent passed none.
    """
    return cursor_model(messages, role, subtype="started")


def rollout_model(lines: Iterable[str], role: str | None, cwd: Path) -> str | None:
    """Read the model a rollout ran on.

    A forked subagent rollout holds the parent's history after its own `session_meta`, so the role
    and workspace are decided by the first `session_meta` alone and the last `turn_context` wins
    (the subagent's own turn comes after the parent's copied ones).

    Args:
        lines: The rollout's lines.
        role: The `agent_role` the first `session_meta` must name; None for a root rollout.
        cwd: The workspace the first `session_meta` must name.

    Returns:
        The last `turn_context` model when the rollout matches, else None.
    """
    seen = matched = False
    model = None
    for record in filter(None, map(decode, lines)):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "session_meta":
            if seen:
                continue
            seen = True
            where = payload.get("cwd")
            matched = (
                payload.get("agent_role") == role
                and isinstance(where, str)
                and strip_private(str(Path(where).resolve())) == strip_private(str(cwd.resolve()))
            )
            if not matched:
                return None
        elif matched and record.get("type") == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
    return model


def find_rollout_model(sessions: Path, role: str | None, cwd: Path, since: float) -> str | None:
    """Look through rollouts written since the run started for the role's subagent.

    Args:
        sessions: The runtime's sessions directory.
        role: The role name; None finds the root (parent) rollout.
        cwd: The run's workspace.
        since: Epoch seconds; older rollouts are skipped.

    Returns:
        The subagent's model, or None.
    """
    for path in sorted(sessions.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.stat().st_mtime < since:
            break
        try:
            lines = path.read_text().splitlines()
        except OSError, UnicodeDecodeError:
            continue
        if (model := rollout_model(lines, role, cwd)) is not None:
            return model
    return None


def render_cursor_agents(cwd: Path) -> int:
    """Write the rendered role files to the workspace's `.cursor/agents/`.

    Args:
        cwd: The run's workspace.

    Returns:
        How many files were written.
    """
    from adapters.cursor import mapping  # noqa: PLC0415
    from core import config, render  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as home:
        sources = config.sources_for("cursor", Path(home), ROOT / "adapters" / "cursor" / "config.json")
        loaded = config.load_config(sources)
    prefix = f"{mapping.CURSOR_DIR}/agents/"
    count = 0
    for item in render.rendered_files(mapping.runtime_mapping(loaded)):
        if item.path.startswith(prefix):
            target = cwd / ".cursor" / "agents" / item.path.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.text)
            count += 1
    return count


def run(argv: list[str], cwd: Path, log: Path, timeout: float) -> list[str]:
    """Run the runtime command to completion, logging its stdout and stderr.

    Args:
        argv: The command, prompt included.
        cwd: Its working directory.
        log: The log file.
        timeout: Seconds before its process group is stopped.

    Returns:
        The stdout lines.

    Raises:
        CheckError: The command did not finish in time.
    """
    with log.open("w") as sink:
        process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=sink,
            text=True,
            start_new_session=True,
        )
        try:
            out, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, sig)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(KILL_SECONDS)
                    break
            msg = f"the run did not finish within {timeout:.0f}s"
            raise CheckError(msg) from error
        sink.write(out)
    return out.splitlines()


def check(args: argparse.Namespace) -> str:
    """Run one case and compare the observed subagent model with the tier model.

    Args:
        args: Parsed arguments.

    Returns:
        The success detail.

    Raises:
        CheckError: The run failed, no subagent was observed, or its model differs.
    """
    role = ROLES[args.runtime]
    expected = expected_model(args.runtime, role)
    passed = None
    if args.runtime == "cursor":
        render_cursor_agents(args.cwd)
    name = f"context-guard:{role}" if args.runtime == "claude" and not args.plain_names else role
    exception = FORK_EXCEPTION if args.runtime in {"codex", "genie"} else ""
    prompt = PROMPT.format(role=role, tool=TOOLS[args.runtime].format(name=name), exception=exception)
    started = time.time() - CLOCK_SLACK
    lines = run([*args.command, prompt], args.cwd, args.log, args.timeout)
    if args.runtime == "claude":
        observed = claude_model(stream_messages(lines), role)
    elif args.runtime == "cursor":
        messages = stream_messages(lines)
        # The policy tells Cursor parents to pass the tier slug on every Task (plugin agents lose their
        # model), so a passed slug is the product path; only a different one fails.
        passed = cursor_started_model(messages, role)
        if passed is not None and passed != expected:
            msg = f"the parent passed model {passed} on the {role} Task, expected the tier slug {expected}"
            raise CheckError(msg)
        observed = cursor_model(messages, role)
    else:
        observed = find_rollout_model(args.sessions, role, args.cwd, started)
    if observed is None:
        msg = f"no {role} subagent model observed (was it spawned, and are the role agents installed?)"
        raise CheckError(msg)
    # Claude reports dated ids ("claude-sonnet-5-5-2026..."), so its tier alias must be contained.
    if (expected not in observed) if args.runtime == "claude" else (observed != expected):
        msg = f"{role} ran on {observed}, expected {expected}"
        raise CheckError(msg)
    if args.runtime == "genie" and find_rollout_model(args.sessions, None, args.cwd, started) == observed:
        msg = f"inconclusive: the parent session ran on {observed} too, so a fallback looks identical"
        raise CheckError(msg)
    source = " (slug passed per policy)" if passed is not None else ""
    return f"{role} ran on {observed}{source}"


def parse(argv: list[str]) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments after the script.

    Returns:
        The arguments; `command` holds the runtime command after `--`.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runtime", choices=sorted(ROLES))
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, help="rollout directory (codex, genie)")
    parser.add_argument("--plain-names", action="store_true", help="claude: spawn the plain role name (script install)")
    parser.add_argument("--timeout", type=float, default=240.0)
    split = argv.index("--") if "--" in argv else len(argv)
    options, command = argv[:split], argv[split + 1 :]
    args = parser.parse_args(options)
    args.command = command
    if not args.command:
        parser.error("give the runtime command after --")
    if args.runtime in {"codex", "genie"} and args.sessions is None:
        parser.error("--sessions is required for codex and genie")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run one case and print its verdict.

    Args:
        argv: Arguments after the script; None reads `sys.argv`.

    Returns:
        0 when the subagent ran on its tier model, else 1.
    """
    args = parse(sys.argv[1:] if argv is None else argv)
    started = time.monotonic()
    try:
        detail = check(args)
    except (CheckError, OSError) as error:
        print(f"fail {error}")
        return 1
    print(f"ok {detail} in {int(time.monotonic() - started)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
