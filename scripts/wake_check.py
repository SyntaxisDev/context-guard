#!/usr/bin/env python3
"""Check that a parent agent wakes on its own when its background subagent finishes.

scripts/plugin_live.sh runs one case per runtime against the installed plugin. The parent spawns a
background subagent that runs `sleep` and then answers with a one-time token, and ends its turn.
The case passes when the parent then starts a new turn without user input and that turn's reply
quotes the token, within the timeout. The command after `--` starts the runtime; every line it
prints goes to the log. The verdict is one stdout line, `ok <detail>` (exit 0) or `fail <reason>`
(exit 1); `clean-handoffs` deletes the handoffs written for a workspace and prints how many.

    wake_check.py codex --token T --cwd DIR --log FILE --waker-log FILE -- codex app-server ...
    wake_check.py claude --token T --cwd DIR --log FILE -- claude -p --input-format stream-json ...
    wake_check.py cursor --token T --cwd DIR --log FILE -- cursor-agent -p --output-format stream-json ...
    wake_check.py clean-handoffs --store DIR --workspace DIR
"""

import argparse
import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from pathlib import Path

SLEEP_SECONDS = 20
EXIT_SECONDS = 10
PRIVATE = "/private"
WORKSPACE_LINE = "Workspace:"
FIRST_TURN = "the parent waited for the subagent in its first turn"
CODEX_PROMPT = (
    "Use spawn_agent to start one background subagent named waker with the task: 'Run the shell command: "
    "sleep {sleep}. Then reply with exactly: {token}'. Do NOT call wait_agent; end your turn now by replying "
    "with only the word spawned. In any later turn, reply with one short line quoting the subagent's final "
    "answer; never call wait_agent."
)
CLAUDE_PROMPT = (
    "Use the Agent tool with run_in_background set to true to start one subagent with the task: 'Run the "
    "shell command: sleep {sleep}. Then reply with exactly: {token}'. Do not wait for it or check on it: end "
    "your turn now by replying with only the word spawned. When its result arrives, reply with one short "
    "line quoting it."
)
CURSOR_PROMPT = (
    "Use the Task tool to start one subagent in the background (do not wait for it) with the task: 'Run the "
    "shell command: sleep {sleep}. Then reply with exactly: {token}'. End your turn now by replying with "
    "only the word spawned. When its result arrives, reply with one short line quoting it."
)


class CheckError(Exception):
    """A step of the check failed; the message is the reason reported."""


class Session:
    """A runtime process whose stdout lines are read in the background and appended to a log.

    The process runs in its own process group, which `close` stops with everything it started.

    Args:
        argv: The command.
        cwd: Its working directory.
        log: The log file; stdout lines and stderr are appended.
    """

    def __init__(self, argv: list[str], cwd: Path, log: Path) -> None:
        self.log = log.open("a", buffering=1)
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        threading.Thread(target=self.read, daemon=True).start()

    def read(self) -> None:
        """Copy stdout lines to the log and the line queue; None marks the end."""
        for line in self.process.stdout:
            self.log.write(line)
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message: dict) -> None:
        """Write one JSON line to the process's stdin.

        Args:
            message: The JSON object.
        """
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def messages(self, deadline: float) -> Iterator[dict]:
        """Yield each JSON object the process prints until it exits or the deadline passes.

        Args:
            deadline: `time.monotonic()` value to stop at.

        Yields:
            Each stdout line that decodes to a JSON object; other lines are skipped.
        """
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                return
            if line is None:
                return
            message = decode(line)
            if message is not None:
                yield message

    def close(self) -> None:
        """Close stdin, give the process EXIT_SECONDS to exit, then stop its process group."""
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(EXIT_SECONDS)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.process.pid, sig)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(EXIT_SECONDS)
                break
        self.log.close()


class Rpc:
    """JSON-RPC over a Session's stdio; notifications that arrive while a request waits are kept.

    Args:
        session: The app server process.
        deadline: `time.monotonic()` value every wait stops at.
    """

    def __init__(self, session: Session, deadline: float) -> None:
        self.session = session
        self.deadline = deadline
        self.backlog: deque[dict] = deque()
        self.last_id = 0

    def request(self, method: str, params: dict) -> dict:
        """Send a request and wait for its result.

        Args:
            method: The method name.
            params: Its parameters.

        Returns:
            The result object.

        Raises:
            CheckError: The server answered with an error, or not before it exited or the deadline.
        """
        self.last_id += 1
        self.session.send({"id": self.last_id, "method": method, "params": params})
        for message in self.session.messages(self.deadline):
            if message.get("id") == self.last_id and "method" not in message:
                if "error" in message:
                    msg = f"{method} failed: {message['error']}"
                    raise CheckError(msg)
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            self.keep(message)
        msg = f"{method} got no answer"
        raise CheckError(msg)

    def keep(self, message: dict) -> None:
        """Answer a server request with an accept decision, else keep the notification.

        Args:
            message: A message that is not the awaited response.
        """
        if "id" in message and "method" in message:
            self.session.send({"id": message["id"], "result": {"decision": "accept"}})
        elif "method" in message:
            self.backlog.append(message)

    def notifications(self) -> Iterator[dict]:
        """Yield the kept notifications, then new ones until the server exits or the deadline.

        Yields:
            Each notification; server requests are answered and not yielded.
        """
        while self.backlog:
            yield self.backlog.popleft()
        for message in self.session.messages(self.deadline):
            self.keep(message)
            while self.backlog:
                yield self.backlog.popleft()


def decode(line: str) -> dict | None:
    """Decode one output line.

    Args:
        line: A stdout line.

    Returns:
        The JSON object, or None for anything else.
    """
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def turn_text(turn: object) -> str:
    """Join the agent message texts of a completed app-server turn.

    Args:
        turn: The `turn` object of a `turn/completed` notification.

    Returns:
        The texts, one per line; empty for anything else.
    """
    items = turn.get("items") if isinstance(turn, dict) else None
    texts = [item.get("text") for item in items or [] if isinstance(item, dict) and item.get("type") == "agentMessage"]
    return "\n".join(text for text in texts if isinstance(text, str))


def codex_wake(notifications: Iterable[dict], thread: str, token: str) -> str:
    """Wait for a root turn after the first one that quotes the token.

    Args:
        notifications: App-server notifications in order.
        thread: The root thread id.
        token: The subagent's answer.

    Returns:
        Which root turn quoted the token.

    Raises:
        CheckError: The first turn quoted it, or no later turn did before the notifications ended.
    """
    turns = 0
    for message in notifications:
        params = message.get("params")
        if (
            message.get("method") != "turn/completed"
            or not isinstance(params, dict)
            or params.get("threadId") != thread
        ):
            continue
        turns += 1
        quoted = token in turn_text(params.get("turn"))
        if quoted and turns == 1:
            raise CheckError(FIRST_TURN)
        if quoted:
            return f"root turn {turns} quoted the token"
    msg = "no woken turn quoted the token" if turns else "the first turn never finished"
    raise CheckError(msg)


def queue_turn(rollout: Path) -> bool:
    """Tell whether the root rollout records a turn started by `codex queue`.

    Args:
        rollout: The root rollout file.

    Returns:
        True when a `task_started` record has turn trigger `queue`.
    """
    try:
        lines = rollout.read_text().splitlines()
    except OSError, UnicodeDecodeError:
        return False
    for record in filter(None, map(decode, lines)):
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "task_started":
            continue
        attribution = payload.get("turn_attribution")
        trigger = attribution.get("turn_trigger") if isinstance(attribution, dict) else payload.get("turn_trigger")
        if trigger == "queue":
            return True
    return False


def waker_sent(log: Path, thread: str) -> bool:
    """Tell whether the context-guard waker log has a `sent` line for the root thread.

    Args:
        log: The waker log.
        thread: The root thread id.

    Returns:
        True when a line names `root=<thread>` and ends with `sent`.
    """
    try:
        lines = log.read_text().splitlines()
    except OSError, UnicodeDecodeError:
        return False
    return any(f" root={thread} " in line and line.rstrip().endswith(" sent") for line in lines)


def check_codex(args: argparse.Namespace, deadline: float) -> str:
    """Run the Codex case through an app server that keeps the root thread loaded.

    Args:
        args: Parsed arguments.
        deadline: `time.monotonic()` value to give up at.

    Returns:
        The success detail.

    Raises:
        CheckError: A step failed.
    """
    session = Session(args.command, args.cwd, args.log)
    rpc, thread = Rpc(session, deadline), ""
    try:
        rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "context-guard-wake-check", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        session.send({"method": "initialized"})
        started = rpc.request(
            "thread/start", {"cwd": str(args.cwd), "approvalPolicy": "never", "sandbox": "workspace-write"}
        )
        thread, rollout = started.get("thread", {}).get("id", ""), started.get("thread", {}).get("path")
        if not thread:
            msg = "thread/start returned no thread id"
            raise CheckError(msg)
        prompt = CODEX_PROMPT.format(sleep=SLEEP_SECONDS, token=args.token)
        rpc.request(
            "turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt, "text_elements": []}]}
        )
        detail = codex_wake(rpc.notifications(), thread, args.token)
        if not (isinstance(rollout, str) and queue_turn(Path(rollout))):
            msg = "the woken turn was not started by codex queue"
            raise CheckError(msg)
        if not waker_sent(args.waker_log, thread):
            msg = f"{args.waker_log} has no sent line for thread {thread}"
            raise CheckError(msg)
        return f"{detail} (codex queue turn; waker sent)"
    finally:
        if thread:
            with contextlib.suppress(CheckError, OSError):
                rpc.deadline = time.monotonic() + EXIT_SECONDS
                rpc.request("thread/archive", {"threadId": thread})
        session.close()


def result_wake(messages: Iterable[dict], token: str) -> str:
    """Wait for a turn result after the first one that quotes the token.

    Used for Claude Code stream-json output, where each finished main turn prints one `result`
    message (subagent messages never do).

    Args:
        messages: Stream-json messages in order.
        token: The subagent's answer.

    Returns:
        Which turn result quoted the token.

    Raises:
        CheckError: The first result quoted it, or no later result did before the stream ended.
    """
    results = 0
    for message in messages:
        if message.get("type") != "result":
            continue
        results += 1
        text = message.get("result")
        quoted = isinstance(text, str) and token in text
        if quoted and results == 1:
            raise CheckError(FIRST_TURN)
        if quoted:
            return f"turn {results} quoted the token"
    msg = "no woken turn quoted the token" if results else "the first turn never finished"
    raise CheckError(msg)


def check_claude(args: argparse.Namespace, deadline: float) -> str:
    """Run the Claude Code case in one streaming-input print session kept open until the wake.

    Args:
        args: Parsed arguments.
        deadline: `time.monotonic()` value to give up at.

    Returns:
        The success detail.
    """
    session = Session(args.command, args.cwd, args.log)
    try:
        prompt = CLAUDE_PROMPT.format(sleep=SLEEP_SECONDS, token=args.token)
        session.send(
            {
                "type": "user",
                "session_id": "",
                "parent_tool_use_id": None,
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            }
        )
        return result_wake(session.messages(deadline), args.token)
    finally:
        session.close()


def background_task(tool_call: object) -> bool:
    """Tell whether a Cursor tool call is a Task whose result reports a background subagent.

    Args:
        tool_call: The `tool_call` object of a stream-json `tool_call` message.

    Returns:
        True when it holds `taskToolCall` with `isBackground: true` anywhere in it.
    """
    if not isinstance(tool_call, dict) or "taskToolCall" not in tool_call:
        return False
    pending = [tool_call]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if value.get("isBackground") is True:
                return True
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def cursor_wake(messages: Iterable[dict], token: str) -> str:
    """Check a Cursor print run: a background Task returned, then the final reply quotes the token.

    Print mode waits for background subagents and runs the parent's follow-up turn itself, then
    prints one `result` with the parent's reply.

    Args:
        messages: Stream-json messages in order.
        token: The subagent's answer.

    Returns:
        The success detail.

    Raises:
        CheckError: No Task ran in the background, or the reply never quoted the token.
    """
    background = False
    for message in messages:
        if message.get("type") == "tool_call" and message.get("subtype") == "completed":
            background = background or background_task(message.get("tool_call"))
        elif message.get("type") == "result":
            text = message.get("result")
            if not background:
                msg = "no Task subagent ran in the background"
                raise CheckError(msg)
            if isinstance(text, str) and token in text:
                return "the parent's reply after the background subagent quoted the token"
            msg = "the parent's final reply did not quote the token"
            raise CheckError(msg)
    msg = "cursor-agent printed no result" if background else "no Task subagent ran in the background"
    raise CheckError(msg)


def check_cursor(args: argparse.Namespace, deadline: float) -> str:
    """Run the Cursor case as one `cursor-agent -p` run with the prompt appended.

    Args:
        args: Parsed arguments.
        deadline: `time.monotonic()` value to give up at.

    Returns:
        The success detail.
    """
    prompt = CURSOR_PROMPT.format(sleep=SLEEP_SECONDS, token=args.token)
    session = Session([*args.command, prompt], args.cwd, args.log)
    try:
        return cursor_wake(session.messages(deadline), args.token)
    finally:
        session.close()


def strip_private(path: str) -> str:
    """Drop macOS's `/private` prefix so both spellings of a temporary path compare equal.

    Args:
        path: An absolute path.

    Returns:
        The path without a leading `/private`.
    """
    return path.removeprefix(PRIVATE) if path.startswith(PRIVATE + "/") else path


def workspace_handoffs(store: Path, workspace: Path) -> list[Path]:
    """Find the handoffs whose `Workspace:` line names the workspace or a path inside it.

    Args:
        store: The handoff directory; only its top-level `*.md` files are read.
        workspace: The run's workspace.

    Returns:
        The matching handoff files.
    """
    root = strip_private(str(workspace.resolve()))
    found = []
    for path in sorted(store.glob("*.md")):
        try:
            lines = path.read_text().splitlines()
        except OSError, UnicodeDecodeError:
            continue
        named = (
            strip_private(line.removeprefix(WORKSPACE_LINE).strip(" `"))
            for line in lines
            if line.startswith(WORKSPACE_LINE)
        )
        if any(value == root or value.startswith(root + "/") for value in named):
            found.append(path)
    return found


def clean_handoffs(args: argparse.Namespace) -> int:
    """Delete the handoffs written for the workspace and print how many.

    Args:
        args: Parsed arguments.

    Returns:
        0.
    """
    removed = 0
    for path in workspace_handoffs(args.store, args.workspace):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed += 1
    print(f"ok removed {removed} handoff{'' if removed == 1 else 's'}")
    return 0


def parse(argv: list[str]) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments after the script.

    Returns:
        The arguments; `command` holds the runtime command after `--`.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="case", required=True)
    for name in ("codex", "claude", "cursor"):
        case = commands.add_parser(name)
        case.add_argument("--token", required=True)
        case.add_argument("--cwd", type=Path, required=True)
        case.add_argument("--log", type=Path, required=True)
        case.add_argument("--timeout", type=float, default=180.0)
        if name == "codex":
            case.add_argument("--waker-log", type=Path, required=True)
        case.add_argument("command", nargs=argparse.REMAINDER)
    clean = commands.add_parser("clean-handoffs")
    clean.add_argument("--store", type=Path, required=True)
    clean.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.case != "clean-handoffs":
        args.command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not args.command:
            parser.error("give the runtime command after --")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run one case and print its verdict.

    Args:
        argv: Arguments after the script; None reads `sys.argv`.

    Returns:
        0 when the parent woke and quoted the token, else 1.
    """
    args = parse(sys.argv[1:] if argv is None else argv)
    if args.case == "clean-handoffs":
        return clean_handoffs(args)
    checks = {"codex": check_codex, "claude": check_claude, "cursor": check_cursor}
    started = time.monotonic()
    try:
        detail = checks[args.case](args, started + args.timeout)
    except (CheckError, OSError) as error:
        print(f"fail {error}")
        return 1
    print(f"ok {detail} in {int(time.monotonic() - started)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
