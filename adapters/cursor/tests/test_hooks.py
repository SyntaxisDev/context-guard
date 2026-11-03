import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from adapters.cursor import hooks, mapping, subagents
from core import policy
from tests import harness

START = 1_800_000_000
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
    """sessionStart marks the hooks as active and lists open handoffs, without policy text."""

    def test_session_start_lists_handoffs_after_the_marker_without_policy_text(self) -> None:
        self.store.mkdir(parents=True)
        handoff = self.store / "project-task-0123456789abcdef.md"
        handoff.write_text(f"# Project task\nStatus: open · Updated: now\nWorkspace: {self.project}\n")
        response = self.hook("sessionStart")
        self.assertTrue(response["additional_context"].startswith(mapping.SESSION_MARKER + "\n\n"))
        self.assertIn(str(handoff), response["additional_context"])
        self.assertIn("(likely match)", response["additional_context"])
        self.assertNotIn("Orchestrator mode", response["additional_context"])

    def test_session_start_without_handoffs_adds_only_the_marker(self) -> None:
        self.assertEqual(self.hook("sessionStart"), {"additional_context": mapping.SESSION_MARKER})


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

    def test_subagent_stop_without_parent_id_gets_no_follow_up_once_a_tool_hook_named_its_parent(self) -> None:
        self.tokens("sub", 170000, limit=1000000)
        self.hook("postToolUse", conversation_id="sub", parent_tool_call_id="toolu_1", tool_name="Shell")
        self.assertEqual(self.hook("stop", conversation_id="sub", status="completed", loop_count=0), {})
        self.assertNotIn("handoff_requested", self.names())


class SubagentTrackingTests(CursorHookCase):
    """Spawned subagents whose result is lost, orphaned or silent leave one note per occurrence for the parent."""

    parent = harness.read_fixture("cursor", "preToolUse-task")["payload"]["conversation_id"]
    call = harness.read_fixture("cursor", "preToolUse-task")["payload"]["tool_use_id"]
    agent = harness.read_fixture("cursor", "postToolUse-subagent-read")["payload"]["conversation_id"]

    def recorded(self, name: str, **fields: object) -> dict:
        payload = harness.payload("cursor", name, self.home, self.project, **fields)
        return hooks.run(payload["hook_event_name"], payload, self.home, {})

    def task_result(self, *, background: bool = False) -> dict:
        output = {"status": "success", "agentId": self.agent, "durationMs": 5, "isBackground": background}
        return self.recorded("postToolUse-task", tool_use_id=self.call, tool_output=json.dumps(output))

    def parent_tool(self) -> dict:
        return self.recorded("postToolUse-shell", conversation_id=self.parent, session_id=self.parent)

    def unreturned(self) -> list[dict]:
        return self.details("subagent_unreturned")

    def details(self, event: str) -> list[dict]:
        return [record["detail"] for record in self.logged() if record["event"] == event]

    def at(self, minutes: float, name: str, **fields: object) -> dict:
        with mock.patch.object(subagents, "clock", return_value=START + minutes * 60):
            return self.recorded(name, **fields)

    def parent_tool_at(self, minutes: float, duration_ms: float = 1000) -> dict:
        return self.at(
            minutes, "postToolUse-shell", conversation_id=self.parent, session_id=self.parent, duration=duration_ms
        )

    def transcript(self) -> Path:
        return self.home / ".cursor/projects/dir/agent-transcripts" / self.parent / "subagents" / f"{self.agent}.jsonl"

    def background_spawn(self) -> None:
        self.at(0, "preToolUse-task")
        self.at(1, "postToolUse-subagent-read")
        with mock.patch.object(subagents, "clock", return_value=START + 60):
            self.task_result(background=True)

    def test_aborted_parent_gets_the_note_on_its_next_tool_result_once(self) -> None:
        self.recorded("preToolUse-task")
        self.recorded("postToolUse-subagent-read")
        self.recorded("postToolUse-subagent-shell")
        self.assertEqual(self.recorded("stop-aborted"), {})
        self.assertEqual(self.recorded("postToolUse-subagent-read"), {})
        note = self.parent_tool()["additional_context"]
        transcripts = self.home / ".cursor/projects/dir/agent-transcripts" / self.parent / "subagents"
        self.assertIn(f"Subagent `implementer` (agent {self.agent}) did not return its result", note)
        self.assertRegex(note, r"last activity \d\d:\d\d UTC")
        self.assertIn(f"Transcript: {transcripts / f'{self.agent}.jsonl'}", note)
        self.assertIn("resume it by agent id", note)
        self.assertEqual(self.parent_tool(), {})
        self.assertEqual(self.unreturned(), [{"role": "implementer", "agent": self.agent, "status": "aborted"}])

    def test_returned_or_background_subagent_leaves_no_note(self) -> None:
        for background in (False, True):
            with self.subTest(background=background):
                self.recorded("preToolUse-task")
                self.recorded("postToolUse-subagent-read")
                self.task_result(background=background)
                self.recorded("stop-aborted")
                self.assertEqual(self.parent_tool(), {})
        self.assertEqual(self.unreturned(), [])

    def test_subagent_that_never_ran_a_tool_is_reported_at_session_start(self) -> None:
        self.recorded("preToolUse-task")
        self.recorded("stop-completed")
        context = self.recorded("sessionStart-root")["additional_context"]
        self.assertIn("Subagent `implementer` did not return its result. Transcript:", context)
        self.assertIn("re-spawn it from its progress file", context)
        self.assertNotIn("(agent", context)
        self.assertEqual(self.parent_tool(), {})

    def test_note_merges_with_the_tier_note(self) -> None:
        self.recorded("preToolUse-task")
        self.recorded("stop-aborted")
        self.tokens(self.parent, 190000)
        note = self.parent_tool()["additional_context"]
        self.assertIn("190k", note)
        self.assertIn("did not return its result", note)

    def test_background_subagent_still_running_is_not_unreturned_at_parent_stop(self) -> None:
        self.background_spawn()
        self.assertEqual(self.at(2, "stop-completed"), {})
        self.assertEqual(self.parent_tool_at(3), {})
        self.assertEqual(self.unreturned(), [])

    def test_silent_background_subagent_gets_one_note_per_silence_period(self) -> None:
        self.background_spawn()
        self.assertEqual(self.parent_tool_at(10), {})
        note = self.parent_tool_at(11.5)["additional_context"]
        self.assertIn(f"Subagent `implementer` (agent {self.agent}) has been silent for 10 minutes", note)
        self.assertIn(f"Transcript: {self.transcript()}", note)
        self.assertIn("It may have died or be stuck", note)
        self.assertIn("cancel its jobs before re-spawning", note)
        self.assertEqual(self.parent_tool_at(30), {})
        self.at(31, "postToolUse-subagent-read")
        self.assertEqual(self.parent_tool_at(40), {})
        self.assertIn("silent for 10 minutes", self.parent_tool_at(41)["additional_context"])
        self.assertEqual(
            self.details("subagent_silent"), [{"role": "implementer", "agent": self.agent, "minutes": 10}] * 2
        )

    def test_silence_threshold_follows_the_user_config(self) -> None:
        self.user_config({"subagents": {"silence_minutes": 3}})
        self.background_spawn()
        self.assertIn("silent for 3 minutes", self.parent_tool_at(4)["additional_context"])

    def test_spawn_without_activity_is_silent_from_its_spawn_time_at_session_start(self) -> None:
        self.at(0, "preToolUse-task")
        context = self.at(10, "sessionStart-root")["additional_context"]
        self.assertIn("Subagent `implementer` has been silent for 10 minutes", context)

    def test_background_subagent_closed_by_its_completed_stop_raises_no_silence_note(self) -> None:
        self.background_spawn()
        self.at(2, "stop-completed", conversation_id=self.agent, session_id=self.agent)
        self.assertEqual(self.parent_tool_at(30), {})
        self.assertEqual(self.details("subagent_silent"), [])

    def test_background_subagent_whose_transcript_ended_raises_no_silence_note(self) -> None:
        self.background_spawn()
        self.transcript().parent.mkdir(parents=True)
        lines = [{"role": "assistant", "message": {"content": []}}, {"type": "turn_ended", "status": "success"}]
        self.transcript().write_text("".join(json.dumps(line) + "\n" for line in lines))
        self.assertEqual(self.parent_tool_at(5), {})
        self.transcript().unlink()
        self.assertEqual(self.parent_tool_at(30), {})
        self.assertEqual(self.details("subagent_silent"), [])

    def test_ended_transcript_retires_a_background_spawn_whose_task_payloads_name_no_transcript(self) -> None:
        self.at(0, "preToolUse-task", transcript_path=None)
        self.at(1, "postToolUse-subagent-read", transcript_path=None)
        output = json.dumps({"status": "success", "agentId": self.agent, "durationMs": 144, "isBackground": True})
        self.at(1, "postToolUse-task", tool_use_id=self.call, tool_output=output, transcript_path=None)
        self.transcript().parent.mkdir(parents=True)
        lines = [{"role": "assistant", "message": {"content": []}}, {"type": "turn_ended", "status": "success"}]
        self.transcript().write_text("".join(json.dumps(line) + "\n" for line in lines))
        self.assertEqual(self.parent_tool_at(13), {})
        self.assertEqual(self.parent_tool_at(30), {})
        self.assertEqual(self.details("subagent_silent"), [])

    def test_parent_moving_on_without_a_foreground_result_gets_one_orphan_note(self) -> None:
        self.at(0, "preToolUse-task")
        self.at(1, "postToolUse-subagent-read")
        note = self.parent_tool_at(3)["additional_context"]
        self.assertIn(f"Subagent `implementer` (agent {self.agent}) was orphaned", note)
        self.assertIn(f"It may still be running. Transcript: {self.transcript()}", note)
        self.assertEqual(self.parent_tool_at(4), {})
        self.assertEqual(self.at(5, "stop-aborted"), {})
        self.assertEqual(self.parent_tool_at(6), {})
        self.assertEqual(self.details("subagent_orphaned"), [{"role": "implementer", "agent": self.agent}])
        self.assertEqual(self.unreturned(), [])

    def test_parallel_tool_and_task_results_raise_no_orphan_note(self) -> None:
        second = "toolu_second"
        self.at(0, "preToolUse-task")
        self.at(0, "preToolUse-task", tool_use_id=second)
        self.assertEqual(self.parent_tool_at(4, duration_ms=4 * 60000 - 500), {})
        output = json.dumps({"status": "success", "agentId": self.agent, "durationMs": 5, "isBackground": False})
        for call in (self.call, second):
            self.assertEqual(self.at(5, "postToolUse-task", tool_use_id=call, tool_output=output), {})
        self.assertEqual(self.parent_tool_at(6), {})
        self.assertEqual(self.at(7, "stop-completed"), {})
        self.assertEqual(self.parent_tool_at(8), {})
        self.assertEqual(self.details("subagent_orphaned"), [])
        self.assertEqual(self.unreturned(), [])


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
                {"role": "scout", "tier": "cheap", "model": "claude-haiku-5-5-thinking-low", "effort": "low"},
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
