import json
import sqlite3
import unittest

from adapters.cursor import hooks
from core import policy
from tests import harness

RECORDED = {
    "sessionStart": "sessionStart-root",
    "beforeSubmitPrompt": "beforeSubmitPrompt-root",
    "preToolUse": "preToolUse-task",
    "postToolUse": "postToolUse-shell",
    "stop": "stop-completed",
}


class CursorHookCase(harness.RuntimeHookCase):
    """A Cursor home with its chat state database and helpers that record usage and run hooks on recorded payloads."""

    runtime = "cursor"

    def setUp(self) -> None:
        super().setUp()
        self.config = hooks.load(self.home, {})
        self.database = hooks.state_db_path(self.config, self.home)
        if self.database is None:
            self.skipTest("no Cursor state database location for this platform")

    def chat(self, conversation: str, value: str | dict | list) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as database:
            database.execute("create table if not exists cursorDiskKV (key text primary key, value text)")
            database.execute(
                "insert or replace into cursorDiskKV values (?, ?)",
                ("composerData:" + conversation, value if isinstance(value, str) else json.dumps(value)),
            )
        database.close()

    def tokens(self, conversation: str, used: int, limit: int = 200000) -> None:
        harness.write_cursor_chat(self.home, conversation, used, limit)

    def hook(self, native: str, **fields: object) -> dict:
        recorded = harness.payload("cursor", RECORDED.get(native, "postToolUse-shell"), self.home, self.project)
        payload = {
            **recorded,
            "conversation_id": "c1",
            "session_id": "c1",
            "hook_event_name": native,
            "workspace_roots": [str(self.project)],
            **fields,
        }
        return hooks.run(native, payload, self.home, {})

    def user_config(self, values: dict) -> None:
        (self.home / ".context-guard").mkdir(exist_ok=True)
        (self.home / ".context-guard" / "config.json").write_text(json.dumps(values))

    def prompted(self, conversation: str = "c1") -> tuple[int | None, int | None]:
        """Submit a prompt and read back what the hook logged for the chat.

        Args:
            conversation: The chat id to prompt.

        Returns:
            The tokens and window the logged prompt record read.
        """
        self.hook("beforeSubmitPrompt", conversation_id=conversation, prompt="next")
        records = [r for r in self.logged() if r["event"] == "prompt" and r["session"] == conversation]
        return records[-1]["tokens"], records[-1]["window"]


class SessionTests(CursorHookCase):
    """sessionStart lists open handoffs and nothing else."""

    def test_session_start_lists_handoffs_without_policy_text(self) -> None:
        self.store.mkdir(parents=True)
        handoff = self.store / "project-task-0123456789abcdef.md"
        handoff.write_text(f"# Project task\nStatus: open · Updated: now\nWorkspace: {self.project}\n")
        response = self.hook("sessionStart")
        self.assertIn(str(handoff), response["additional_context"])
        self.assertIn("(likely match)", response["additional_context"])
        self.assertNotIn("Orchestrator mode", response["additional_context"])

    def test_session_start_without_handoffs_adds_nothing(self) -> None:
        self.assertEqual(self.hook("sessionStart"), {})


class PromptTests(CursorHookCase):
    """Prompt notes and blocks, and delivery of held notes on the next tool result or session."""

    def test_block_reports_chat_tokens_and_the_chat_limit(self) -> None:
        self.tokens("c1", 190000)
        blocked = self.hook("beforeSubmitPrompt", prompt="continue the work")
        self.assertEqual(set(blocked), {"continue", "user_message"})
        self.assertFalse(blocked["continue"])
        self.assertIn("190k", blocked["user_message"])
        self.assertIn("200k", blocked["user_message"])

    def test_prompt_note_reaches_the_model_on_the_next_tool_result_once(self) -> None:
        self.tokens("c1", 120000)
        self.assertEqual(self.hook("beforeSubmitPrompt", prompt="go on"), {"continue": True})
        note = self.hook("postToolUse", tool_name="Shell", tool_input={"command": "ls"})
        self.assertIn("120k", note["additional_context"])
        self.assertEqual(self.hook("postToolUse", tool_name="Shell", tool_input={}), {})
        self.assertEqual(self.hook("beforeSubmitPrompt", prompt="again"), {"continue": True})
        self.assertEqual(self.hook("postToolUse", tool_name="Shell", tool_input={}), {})
        self.assertEqual(self.names().count("note"), 1)
        self.assertEqual(self.names().count("note_delivered"), 1)

    def test_prompt_note_survives_a_stop_without_follow_up_and_reaches_the_next_tool_result(self) -> None:
        self.tokens("c1", 120000)
        self.hook("beforeSubmitPrompt", prompt="quick question")
        self.assertEqual(self.hook("stop", status="aborted", loop_count=0), {})
        self.assertNotIn("note_delivered", self.names())
        note = self.hook("postToolUse", tool_name="Shell", tool_input={})
        self.assertIn("120k", note["additional_context"])
        self.assertEqual(
            [(r["event"], r.get("tier")) for r in self.logged() if r["event"] == "note_delivered"],
            [("note_delivered", "note")],
        )

    def test_held_note_is_dropped_once_the_handoff_is_written(self) -> None:
        self.tokens("c1", 120000)
        self.hook("beforeSubmitPrompt", prompt="quick question")
        self.store.mkdir(parents=True)
        path = str(self.store / "task-0123456789abcdef.md")
        self.assertEqual(self.hook("postToolUse", tool_name="Write", tool_input={"file_path": path}), {})
        self.assertEqual(self.hook("postToolUse", tool_name="Shell", tool_input={}), {})
        self.assertNotIn("note_delivered", self.names())

    def test_stop_follow_up_supersedes_the_held_note(self) -> None:
        self.tokens("c1", 120000)
        self.hook("beforeSubmitPrompt", prompt="quick question")
        self.assertIn("followup_message", self.hook("stop", status="completed", loop_count=0))
        self.store.mkdir(parents=True)
        path = str(self.store / "task-0123456789abcdef.md")
        self.assertEqual(self.hook("postToolUse", tool_name="Write", tool_input={"file_path": path}), {})
        self.assertEqual(self.hook("postToolUse", tool_name="Shell", tool_input={}), {})
        self.assertEqual(self.names(), ["prompt", "note", "handoff_requested", "handoff_written"])

    def test_prompt_note_is_delivered_at_the_next_session_start(self) -> None:
        self.tokens("c1", 120000)
        self.hook("beforeSubmitPrompt", prompt="quick question")
        self.hook("stop", status="aborted", loop_count=0)
        started = self.hook("sessionStart")
        self.assertIn("120k", started["additional_context"])
        self.assertEqual(self.hook("postToolUse", tool_name="Shell", tool_input={}), {})

    def test_tool_notes_cap_at_firm_and_prompt_still_blocks(self) -> None:
        self.tokens("c1", 190000)
        firm = self.hook("postToolUse", tool_name="Shell", tool_input={})
        self.assertIn("190k", firm["additional_context"])
        self.assertEqual([r["tier"] for r in self.logged() if r["event"] == "note"], ["firm"])
        self.assertEqual([r["tier"] for r in self.logged() if r["event"] == "note_delivered"], ["firm"])
        self.assertFalse(self.hook("beforeSubmitPrompt", prompt="next")["continue"])


class TelemetryTests(CursorHookCase):
    """Missing or changed chat telemetry and invalid payloads pass; the window falls back without a chat limit."""

    def test_missing_or_changed_telemetry_passes(self) -> None:
        self.assertEqual(self.hook("beforeSubmitPrompt", prompt="x"), {"continue": True})
        self.assertEqual(self.hook("stop", status="completed"), {})
        cases = {
            "no row": None,
            "not json": "{",
            "text tokens": {"contextTokensUsed": "190000"},
            "negative": {"contextTokensUsed": -1},
            "list": [190000],
        }
        for label, value in cases.items():
            with self.subTest(label):
                conversation = "c-" + label.replace(" ", "-")
                if value is not None:
                    self.chat(conversation, value)
                else:
                    self.chat("other", {"contextTokensUsed": 250000})
                self.assertEqual(
                    self.hook("beforeSubmitPrompt", conversation_id=conversation, prompt="x"), {"continue": True}
                )
                self.assertEqual(self.hook("postToolUse", conversation_id=conversation, tool_name="Shell"), {})
        self.assertNotIn("block", self.names())
        self.assertNotIn("note", self.names())

    def test_window_fallback_applies_without_chat_limit(self) -> None:
        self.chat("c1", {"contextTokensUsed": 5000})
        self.assertEqual(self.prompted(), (5000, None))
        self.user_config({"telemetry": {"window_fallback": 400000}})
        self.assertEqual(self.prompted(), (5000, 400000))

    def test_invalid_payloads_pass(self) -> None:
        self.tokens("c1", 250000)
        for payload in (
            None,
            [],
            {"conversation_id": 5},
            {"conversation_id": "c1", "workspace_roots": "x"},
            {"conversation_id": "c1", "prompt": "a\x00b"},
            {"conversation_id": "c1", "tool_input": "x"},
            {"conversation_id": "c1", "loop_count": -1},
            {},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(hooks.run("beforeSubmitPrompt", payload, self.home, {}), {"continue": True})
                self.assertEqual(hooks.run("postToolUse", payload, self.home, {}), {})
        self.assertEqual(hooks.run("unknownEvent", {"conversation_id": "c1"}, self.home, {}), {})
        self.assertEqual(self.logged(), [])


class CliTelemetryTests(CursorHookCase):
    """The Cursor CLI chat store as a telemetry source behind the IDE record."""

    def test_recorded_cli_store_gives_tokens_and_window(self) -> None:
        recorded = harness.read_fixture("cursor", "cli-store")["record"]
        harness.write_cli_chat(self.home, "c1")
        self.assertEqual(self.prompted(), (recorded["tokens"], recorded["window"]))

    def test_cli_store_raises_the_note_without_a_gui_record(self) -> None:
        harness.write_cli_chat(self.home, "c1", 160000, 200000)
        self.assertEqual(self.hook("beforeSubmitPrompt", prompt="next"), {"continue": True})
        note = self.hook("postToolUse", tool_name="Shell", tool_input={})
        self.assertIn("160k", note["additional_context"])
        self.assertEqual([r["tokens"] for r in self.logged() if r["event"] == "note"], [160000])

    def test_gui_record_wins_over_the_cli_store(self) -> None:
        self.tokens("c1", 20000, limit=272000)
        harness.write_cli_chat(self.home, "c1", 190000, 200000)
        self.assertEqual(self.prompted(), (20000, 272000))

    def test_malformed_corrupt_or_missing_cli_store_fails_open(self) -> None:
        truncated = harness.cli_root(160000, 200000)
        cases = {
            "bad hex": {"meta": "zz"},
            "meta not an object": {"meta": b"[1]".hex()},
            "no root id": {"meta": b"{}".hex()},
            "truncated protobuf": {"root": truncated[:-3]},
            "unknown wire type": {"root": truncated + bytes([0x0B])},
            "no usage field": {"root": harness.proto_field(1, bytes(32))},
            "not a database": None,
        }
        for label, override in cases.items():
            with self.subTest(label):
                conversation = "c-" + label.replace(" ", "-")
                database = harness.write_cli_chat(self.home, conversation, 160000, 200000, **(override or {}))
                if override is None:
                    database.write_bytes(b"not a database")
                self.assertEqual(self.prompted(conversation), (None, None))
        self.assertEqual(self.prompted("missing"), (None, None))
        self.assertEqual(self.prompted("../c-not-a-database"), (None, None))


class LeafTests(CursorHookCase):
    """Subagents get one budget message for the role recorded at spawn and no tier notes."""

    def test_subagent_gets_one_budget_message_and_no_tier_notes(self) -> None:
        self.tokens("sub", 190000)
        first = self.hook("postToolUse", conversation_id="sub", parent_tool_call_id="toolu_1", tool_name="Shell")
        self.assertEqual(first["additional_context"], policy.BUDGET_MESSAGE.format(tokens=190, budget=80))
        self.assertEqual(
            self.hook("postToolUse", conversation_id="sub", parent_tool_call_id="toolu_1", tool_name="Shell"), {}
        )
        self.assertEqual(self.names().count("subagent_budget"), 1)
        self.assertNotIn("note", self.names())

    def test_subagent_budget_follows_the_role_recorded_at_spawn(self) -> None:
        self.hook("preToolUse", tool_name="Task", tool_use_id="toolu_r", tool_input={"subagent_type": "reviewer"})
        self.tokens("rev", 100000)
        self.tokens("other", 100000)
        self.assertEqual(
            self.hook("postToolUse", conversation_id="rev", parent_tool_call_id="toolu_r", tool_name="Shell"), {}
        )
        self.hook("postToolUse", conversation_id="other", parent_tool_call_id="toolu_x", tool_name="Shell")
        self.tokens("rev", 125000)
        self.hook("postToolUse", conversation_id="rev", parent_tool_call_id="toolu_r", tool_name="Shell")
        budgets = [(r["session"], r["detail"]) for r in self.logged() if r["event"] == "subagent_budget"]
        self.assertEqual(
            budgets, [("other", {"role": None, "budget": 80_000}), ("rev", {"role": "reviewer", "budget": 120_000})]
        )

    def test_unregistered_subagent_events_pass(self) -> None:
        self.store.mkdir(parents=True)
        (self.store / "task-0123456789abcdef.md").write_text(f"# Task\nWorkspace: {self.project}\n")
        self.assertEqual(self.hook("subagentStart", subagent_id="sub", subagent_type="scout"), {})
        self.assertEqual(self.logged(), [])


class StopTests(CursorHookCase):
    """stop asks for a handoff refresh only on the first completed stop."""

    def test_refresh_follow_up_only_on_completed_first_stop(self) -> None:
        self.tokens("c1", 170000, limit=1000000)
        follow = self.hook("stop", status="completed", loop_count=0)
        self.assertTrue(follow["followup_message"].startswith("[context-guard]"))
        self.assertIn(str(self.store), follow["followup_message"])
        self.tokens("c2", 170000, limit=1000000)
        self.assertEqual(self.hook("stop", conversation_id="c2", status="completed", loop_count=1), {})
        self.assertEqual(self.hook("stop", conversation_id="c2", status="aborted"), {})
        self.assertEqual(self.names().count("handoff_requested"), 1)


class ToolEventTests(CursorHookCase):
    """Task spawns and handoff reads and writes are logged from tool events."""

    def spawns(self) -> list[dict]:
        return [record["detail"] for record in self.logged() if record["event"] == "subagent_spawn"]

    def test_task_spawn_is_logged_once_with_role_tier_model_and_effort(self) -> None:
        task = {
            "description": "review",
            "prompt": "p",
            "subagent_type": "reviewer",
            "model": "claude-sonnet-5-5-medium",
        }
        self.assertEqual(self.hook("preToolUse", tool_name="Task", tool_input=task), {})
        self.assertEqual(self.hook("postToolUse", tool_name="Task", tool_input=task), {})
        self.assertEqual(self.hook("preToolUse", tool_name="Task", tool_input={"subagent_type": "scout"}), {})
        self.assertEqual(
            self.spawns(),
            [
                {"role": "reviewer", "tier": "standard", "model": "claude-sonnet-5-5-medium", "effort": "medium"},
                {"role": "scout", "tier": "cheap", "model": "claude-haiku-5-5", "effort": "low"},
            ],
        )

    def test_pre_tool_use_ignores_other_tools(self) -> None:
        self.tokens("c1", 250000)
        self.assertEqual(self.hook("preToolUse", tool_name="Shell", tool_input={"command": "ls"}), {})
        self.assertEqual(self.logged(), [])

    def test_handoff_reads_and_writes_in_the_store_are_logged(self) -> None:
        self.store.mkdir(parents=True)
        handoff = str(self.store / "task-0123456789abcdef.md")
        self.hook("postToolUse", tool_name="Write", tool_input={"file_path": handoff, "content": "x"})
        self.hook("postToolUse", tool_name="Read", tool_input={"file_path": handoff})
        self.hook("postToolUse", tool_name="Read", tool_input={"file_path": str(self.project / "README.md")})
        handoff_events = [
            (record["event"], record["detail"]["path"])
            for record in self.logged()
            if record["event"].startswith("handoff_")
        ]
        self.assertEqual(handoff_events, [("handoff_written", handoff), ("handoff_read", handoff)])

    def test_shell_writes_credit_the_turn_so_stop_does_not_refresh(self) -> None:
        self.store.mkdir(parents=True)
        path = self.store / "existing-task-0123456789abcdef.md"
        writes = [f"cat > {path} <<'EOF'\n# Task\nEOF", f"printf '# Task\\n' | tee {path}"]
        for index, command in enumerate(writes):
            with self.subTest(command=command):
                self.tokens(f"w{index}", 170000, limit=1000000)
                self.hook(
                    "postToolUse", conversation_id=f"w{index}", tool_name="Shell", tool_input={"command": command}
                )
                self.assertEqual(self.hook("stop", conversation_id=f"w{index}", status="completed"), {})
        self.tokens("r1", 170000, limit=1000000)
        self.hook("postToolUse", conversation_id="r1", tool_name="Shell", tool_input={"command": "echo done"})
        follow = self.hook("stop", conversation_id="r1", status="completed")["followup_message"]
        self.assertIn(str(self.store), follow)
        self.assertEqual(
            [r["detail"] for r in self.logged() if r["event"] == "handoff_requested"], [{"source": "unwritten"}]
        )

    def test_relative_shell_writes_resolve_against_the_tool_cwd(self) -> None:
        self.store.mkdir(parents=True)
        self.hook(
            "postToolUse",
            tool_name="Shell",
            tool_input={"command": "printf x > task-0123456789abcdef.md", "cwd": str(self.store)},
        )
        self.assertEqual(
            [(r["event"], r["detail"]["path"]) for r in self.logged()],
            [("handoff_written", str(self.store / "task-0123456789abcdef.md"))],
        )

    def test_shell_handoff_reads_are_logged(self) -> None:
        self.store.mkdir(parents=True)
        path = self.store / "task-0123456789abcdef.md"
        self.hook("postToolUse", tool_name="Shell", tool_input={"command": f"sed -n 1,5p {path}"})
        self.hook("postToolUse", tool_name="Shell", tool_input={"command": "cat README.md"})
        self.assertEqual(
            [(record["event"], record["detail"]["path"]) for record in self.logged()], [("handoff_read", str(path))]
        )


class EntryPointTests(harness.HookBoundaryChecks, CursorHookCase):
    """The Cursor hook process entry passes on missing arguments, errors, overruns and bad user tiers."""

    hook_module, hook_event, pass_response = hooks, "beforeSubmitPrompt", {"continue": True}

    def test_malformed_user_tiers_pass(self) -> None:
        self.user_config({"tiers": {"huge": "opus"}})
        task = {"subagent_type": "reviewer", "model": "opus"}
        self.assertEqual(self.hook("preToolUse", tool_name="Task", tool_input=task), {})
        self.assertEqual(self.logged()[0]["detail"]["tier"], None)


if __name__ == "__main__":
    unittest.main()
