"""scripts/wake_check.py judges the live wake cases; these tests run it against stub runtimes.

Each stub is a small Python program standing in for the runtime command after `--`: it prints the
stream-json or app-server messages a real run produces, so the verdict logic runs end to end
without a model. The handoff cleanup runs on a temporary store.
"""

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests import harness

SCRIPT = harness.ROOT / "scripts" / "wake_check.py"
ANSWER = "WAKE-OK-42"
CODEX_STUB = """
import json, sys
rollout, token = sys.argv[1], sys.argv[2]
def out(message):
    print(json.dumps(message), flush=True)
def turn(thread, text):
    items = [{"type": "agentMessage", "text": text}]
    out({"method": "turn/completed", "params": {"threadId": thread, "turn": {"items": items}}})
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "thread/start":
        out({"id": message["id"], "result": {"thread": {"id": "root-1", "path": rollout}}})
    else:
        out({"id": message["id"], "result": {}})
    if method == "turn/start":
        turn("root-1", "spawned")
        turn("child-1", token)
        turn("root-1", "got " + token)
"""


class WakeCheckTests(unittest.TestCase):
    """Each runtime's verdict follows the turn that quotes the token; cleanup removes only the run's handoffs."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def stub(self, name: str, text: str) -> Path:
        """Write a stub runtime program.

        Returns:
            Its path.
        """
        path = self.root / f"{name}.py"
        path.write_text(textwrap.dedent(text))
        return path

    def lines_stub(self, name: str, *messages: dict) -> list[str]:
        """Build a stub command that prints the messages as JSON lines.

        Returns:
            The command.
        """
        text = "".join(f"print({json.dumps(json.dumps(message))}, flush=True)\n" for message in messages)
        return [sys.executable, str(self.stub(name, text))]

    def check(self, case: str, command: list[str], *extra: str) -> tuple[int, str]:
        """Run one wake case against a stub command.

        Returns:
            The exit code and the verdict line.
        """
        args = [case, "--token", ANSWER, "--cwd", str(self.root), "--log", str(self.root / "log"), "--timeout", "30"]
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args, *extra, "--", *command],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout.strip()

    def test_claude_passes_when_a_later_turn_result_quotes_the_token(self) -> None:
        command = self.lines_stub(
            "claude",
            {
                "type": "assistant",
                "parent_tool_use_id": "t1",
                "message": {"content": [{"type": "text", "text": ANSWER}]},
            },
            {"type": "result", "result": "spawned"},
            {"type": "result", "result": f"The subagent said {ANSWER}"},
        )
        code, verdict = self.check("claude", command)
        self.assertEqual(code, 0, verdict)
        self.assertRegex(verdict, r"^ok turn 2 quoted the token")

    def test_claude_fails_when_the_first_turn_already_has_the_answer(self) -> None:
        code, verdict = self.check("claude", self.lines_stub("claude", {"type": "result", "result": ANSWER}))
        self.assertEqual((code, verdict), (1, "fail the parent waited for the subagent in its first turn"))

    def test_cursor_needs_a_background_task_before_the_quoting_result(self) -> None:
        task = {"taskToolCall": {"result": {"success": {"isBackground": True, "agentId": "a1"}}}}
        done = {"type": "tool_call", "subtype": "completed", "tool_call": task}
        code, verdict = self.check("cursor", self.lines_stub("cursor", done, {"type": "result", "result": ANSWER}))
        self.assertEqual(code, 0, verdict)
        code, verdict = self.check("cursor", self.lines_stub("cursor2", {"type": "result", "result": ANSWER}))
        self.assertEqual((code, verdict), (1, "fail no Task subagent ran in the background"))

    def test_codex_passes_on_a_queued_root_turn_with_a_waker_sent_line(self) -> None:
        rollout = self.root / "rollout.jsonl"
        started = {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_attribution": {"turn_trigger": "queue"}},
        }
        rollout.write_text(json.dumps(started) + "\n")
        waker = self.root / "waker.log"
        waker.write_text("2026-10-22T00:00:00.000+00:00 root=root-1 agent=child-1 skipped: root is in a turn\n")
        command = [sys.executable, str(self.stub("codex", CODEX_STUB)), str(rollout), ANSWER]
        code, verdict = self.check("codex", command, "--waker-log", str(waker))
        self.assertEqual(code, 1)
        self.assertIn("has no sent line for thread root-1", verdict)
        waker.write_text("2026-10-22T00:00:00.000+00:00 root=root-1 agent=child-1 sent\n")
        code, verdict = self.check("codex", command, "--waker-log", str(waker))
        self.assertEqual(code, 0, verdict)
        self.assertRegex(verdict, r"^ok root turn 2 quoted the token")

    def test_clean_handoffs_removes_only_handoffs_of_the_workspace(self) -> None:
        store, work = self.root / "handoffs", self.root / "run" / "work"
        store.mkdir()
        work.mkdir(parents=True)
        mine = store / "read-readme-1.md"
        mine.write_text(f"# Smoke\n\nWorkspace: {str(work).removeprefix('/private')}\n")
        nested = store / "read-readme-2.md"
        nested.write_text(f"Status: done\nWorkspace: `{work}/sub`\n")
        other = store / "real-task.md"
        other.write_text(f"Workspace: {work}-other\n")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "clean-handoffs", "--store", str(store), "--workspace", str(work)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "ok removed 2 handoffs")
        self.assertEqual(sorted(path.name for path in store.iterdir()), ["real-task.md"])


if __name__ == "__main__":
    unittest.main()
