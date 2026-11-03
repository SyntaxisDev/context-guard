"""scripts/roster_check.py reads the observed subagent model; these tests feed it inline run records."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from tests import harness

SPEC = importlib.util.spec_from_file_location("roster_check", harness.ROOT / "scripts" / "roster_check.py")
roster = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(roster)


def assistant(model: str, parent: str | None, *blocks: dict) -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"model": model, "content": list(blocks)}}


class RosterCheckTests(unittest.TestCase):
    """Each runtime's observed model comes from its own record shape."""

    def test_claude_takes_the_model_of_messages_inside_the_role_spawn(self) -> None:
        spawn = {"type": "tool_use", "id": "t1", "input": {"subagent_type": "context-guard:investigator"}}
        messages = [
            assistant("claude-haiku-5-5", None, spawn),
            assistant("claude-sonnet-5-5", "other"),
            assistant("claude-sonnet-5-5-2026", "t1"),
        ]
        self.assertEqual(roster.claude_model(messages, "investigator"), "claude-sonnet-5-5-2026")
        self.assertIsNone(roster.claude_model(messages, "scout"))

    def test_cursor_takes_the_completed_task_call_of_the_role(self) -> None:
        def call(subtype: str, name: str) -> dict:
            args = {"model": "claude-sonnet-5-5-medium", "subagentType": {"custom": {"name": name}}}
            return {"type": "tool_call", "subtype": subtype, "tool_call": {"taskToolCall": {"args": args}}}

        messages = [call("started", "investigator"), call("completed", "scout"), call("completed", "investigator")]
        self.assertEqual(roster.cursor_model(messages, "investigator"), "claude-sonnet-5-5-medium")
        self.assertIsNone(roster.cursor_model(messages[:2], "investigator"))

    def test_rollout_model_needs_the_role_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory).resolve()
            meta = {"type": "session_meta", "payload": {"agent_role": "runner", "cwd": str(cwd)}}
            context = {"type": "turn_context", "payload": {"model": "gpt-5.6-luna"}}
            lines = [json.dumps(meta), json.dumps(context)]
            self.assertEqual(roster.rollout_model(lines, "runner", cwd), "gpt-5.6-luna")
            self.assertIsNone(roster.rollout_model(lines, "scout", cwd))
            self.assertIsNone(roster.rollout_model(lines, "runner", cwd / "elsewhere"))

    def test_cursor_started_call_shows_a_model_the_parent_passed(self) -> None:
        def call(subtype: str, **extra: str) -> dict:
            args = {**extra, "subagentType": {"custom": {"name": "scout"}}}
            return {"type": "tool_call", "subtype": subtype, "tool_call": {"taskToolCall": {"args": args}}}

        self.assertEqual(roster.cursor_started_model([call("started", model="m")], "scout"), "m")
        self.assertIsNone(roster.cursor_started_model([call("started"), call("completed", model="m")], "scout"))

    def test_forked_rollout_takes_the_last_turn_and_decides_the_role_on_the_first_meta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory).resolve()

            def meta(role: str | None) -> str:
                return json.dumps({"type": "session_meta", "payload": {"agent_role": role, "cwd": str(cwd)}})

            def turn(model: str) -> str:
                return json.dumps({"type": "turn_context", "payload": {"model": model}})

            lines = [meta("runner"), meta(None), turn("gpt-parent"), turn("gpt-5.6-luna")]
            self.assertEqual(roster.rollout_model(lines, "runner", cwd), "gpt-5.6-luna")
            self.assertIsNone(roster.rollout_model(lines, None, cwd))
            self.assertEqual(roster.rollout_model([meta(None), turn("gpt-parent")], None, cwd), "gpt-parent")

    def test_expected_model_follows_the_adapter_config(self) -> None:
        config = json.loads((harness.ROOT / "adapters" / "cursor" / "config.json").read_text())
        self.assertEqual(roster.expected_model("cursor", "investigator"), config["tiers"]["standard"]["call"])
        config = json.loads((harness.ROOT / "adapters" / "genie" / "config.json").read_text())
        self.assertEqual(roster.expected_model("genie", "runner"), config["tiers"]["cheap"]["model"])


if __name__ == "__main__":
    unittest.main()
