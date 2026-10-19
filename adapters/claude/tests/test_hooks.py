"""Claude Code hook contracts with temporary homes and fixture transcripts."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.claude import hooks, transcript
from core import policy
from core.runner.provider import INTERNAL_ENV
from tests import harness

RECORDED = {
    "SessionStart": "SessionStart-startup",
    "UserPromptSubmit": "UserPromptSubmit-root",
    "PostToolUse": "PostToolUse-read",
    "Stop": "Stop-root",
    "PreCompact": "PreCompact-auto",
    "PostCompact": "PostCompact-auto",
}
FUTURE = "2099-01-01T00:00:00Z"
MODEL = "claude-sonnet-5-5"


class HookTestCase(harness.RuntimeHookCase):
    """A Claude Code home with a session transcript and helpers that append usage and run hooks in process."""

    runtime = "claude"

    def setUp(self) -> None:
        super().setUp()
        self.transcript = self.home / ".claude" / "projects" / "p" / "session-1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop(INTERNAL_ENV, None)
        self.config = hooks.load_config(self.home, environ={})

    def append(self, record: dict, path: Path | None = None) -> None:
        harness.append_transcript(path or self.transcript, record)

    def usage(
        self, tokens: int, path: Path | None = None, model: str = MODEL, timestamp: str = FUTURE, **extra: int
    ) -> None:
        self.append(harness.assistant_usage(tokens, model, timestamp, output_tokens=9_000_000, **extra), path)

    def boundary(self, trigger: str, pre_tokens: int, path: Path | None = None) -> None:
        self.append(harness.compact_boundary(trigger, pre_tokens, FUTURE), path)

    def hook(self, event: str, session: str = "s1", path: Path | None = None, **extra: object) -> dict:
        recorded = harness.payload("claude", RECORDED[event], self.home, self.project)
        payload = {
            **recorded,
            "session_id": session,
            "cwd": str(self.project),
            "hook_event_name": event,
            "transcript_path": str(path or self.transcript),
            **extra,
        }
        return hooks.handle(event, payload, self.config, self.home)

    def context(self, response: dict, event: str) -> str:
        self.assertEqual(set(response), {"hookSpecificOutput"})
        self.assertEqual(set(response["hookSpecificOutput"]), {"hookEventName", "additionalContext"})
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], event)
        return response["hookSpecificOutput"]["additionalContext"]

    def handoff(self, name: str, title: str, status: str = "open", goal: str = "") -> Path:
        self.store.mkdir(parents=True, exist_ok=True)
        path = self.store / f"{name}-0123456789abcdef.md"
        path.write_text(f"# {title}\nStatus: {status}\nWorkspace: {self.project}\n\n## Goal\n{goal}\n")
        return path


class SessionContextTests(HookTestCase):
    """SessionStart lists open handoffs for the workspace within the context cap."""

    def test_session_start_lists_open_handoffs_ranked(self) -> None:
        self.handoff("open-task", "Open task")
        (self.store / "done.md").write_text(f"# Finished task\nStatus: done\nWorkspace: {self.project}\n")
        text = self.context(self.hook("SessionStart", source="startup", model=MODEL), "SessionStart")
        self.assertIn("Open task", text)
        self.assertIn("(likely match)", text)
        self.assertNotIn("Finished task", text)
        self.assertNotIn("Orchestrator mode", text)

    def test_session_context_is_capped_at_ten_thousand_characters(self) -> None:
        self.config["handoff_list"]["limit"] = 500
        for index in range(150):
            path = self.handoff(f"task-{index}", f"Task number {index}", goal="g" * 150)
            path.rename(path.with_name(f"task-{index}-{index:016x}.md"))
        text = self.context(self.hook("SessionStart", source="startup"), "SessionStart")
        self.assertLessEqual(len(text), 10_000)
        self.assertTrue(text.endswith("…"))

    def test_no_handoffs_means_no_output(self) -> None:
        self.assertEqual(self.hook("SessionStart", source="startup"), {})


class PromptAndToolTests(HookTestCase):
    """Prompt, tool and Stop hooks note, block or refresh by transcript usage and fail open on bad input."""

    def test_note_window_then_block_shape(self) -> None:
        self.usage(120_000)
        note = self.context(self.hook("UserPromptSubmit", prompt="implement feature"), "UserPromptSubmit")
        self.assertIn("200k", note)
        self.usage(190_000)
        blocked = self.hook("UserPromptSubmit", prompt="next step")
        self.assertEqual(set(blocked), {"decision", "reason"})
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("190k", blocked["reason"])

    def test_tool_note_caps_at_firm_once(self) -> None:
        self.usage(190_000)
        text = self.context(self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "true"}), "PostToolUse")
        self.assertIn("190k", text)
        self.assertEqual([entry["tier"] for entry in self.logged() if entry["event"] == "note"], ["firm"])
        self.assertEqual([entry["tier"] for entry in self.logged() if entry["event"] == "note_delivered"], ["firm"])
        self.assertLessEqual(len(text), 2_000)
        self.assertEqual(self.hook("PostToolUse", tool_name="Bash"), {})

    def test_stop_refresh_continues_agent_and_respects_loop_guard(self) -> None:
        self.usage(130_000)
        requested = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(set(requested), {"decision", "reason"})
        self.assertEqual(
            [entry["detail"] for entry in self.logged() if entry["event"] == "handoff_requested"],
            [{"source": "refresh"}],
        )
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {})
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})

    def test_latest_usage_counts_cache_but_not_output_or_child_records(self) -> None:
        self.usage(80_000)
        self.usage(1_000, cache_creation_input_tokens=2_000, cache_read_input_tokens=3_000)
        self.append(
            {"type": "assistant", "parent_tool_use_id": "child", "message": {"usage": {"input_tokens": 900_000}}}
        )
        with self.transcript.open("a") as handle:
            handle.write('{"type":')
        reading = transcript.read(str(self.transcript), 2_097_152)
        self.assertEqual((reading.tokens, reading.model), (6_000, MODEL))

    def test_missing_or_changed_telemetry_passes(self) -> None:
        for event in ("UserPromptSubmit", "PostToolUse", "Stop"):
            self.assertEqual(self.hook(event, prompt="go", tool_name="Read"), {})
        self.usage(250_000)
        self.append({"type": "assistant", "message": {"model": MODEL, "usage": {"input_tokens": "250000"}}})
        self.assertEqual(self.hook("UserPromptSubmit", prompt="go"), {})
        self.usage(250_000)
        self.append({"type": "assistant", "message": {"model": MODEL}})
        self.assertEqual(self.hook("PostToolUse", tool_name="Read"), {})
        self.config["transcript"]["tail_bytes"] = 0
        self.usage(250_000)
        self.assertEqual(self.hook("UserPromptSubmit", prompt="go"), {})

    def test_malformed_payloads_and_internal_runs_fail_open(self) -> None:
        self.usage(250_000)
        payload = {
            "session_id": "s1",
            "cwd": str(self.project),
            "transcript_path": str(self.transcript),
            "prompt": "go",
        }
        for bad in (
            None,
            [],
            "text",
            {**payload, "session_id": 1},
            {**payload, "cwd": "a\0b"},
            {**payload, "stop_hook_active": "false"},
            {**payload, "session_id": ""},
        ):
            with self.subTest(payload=bad):
                self.assertEqual(hooks.handle("UserPromptSubmit", bad, self.config, self.home), {})
        self.assertEqual(hooks.handle("Notification", payload, self.config, self.home), {})
        with patch.dict(os.environ, {INTERNAL_ENV: "1"}):
            self.assertEqual(hooks.run("UserPromptSubmit", payload, self.home), {})
        self.assertEqual(self.hook("UserPromptSubmit", prompt="go")["decision"], "block")


class CompactionTests(HookTestCase):
    """Auto compaction teaches the window; compaction discards stale usage and relists handoffs."""

    def test_auto_compaction_point_replaces_configured_window(self) -> None:
        self.usage(150_000)
        self.boundary("auto", 160_000)
        self.usage(100_000)
        note = self.context(self.hook("UserPromptSubmit", prompt="go"), "UserPromptSubmit")
        self.assertIn("160k", note)
        other = self.transcript.with_name("session-2.jsonl")
        self.usage(100_000, path=other)
        note = self.context(self.hook("UserPromptSubmit", session="s2", path=other, prompt="go"), "UserPromptSubmit")
        self.assertIn("160k", note)
        third = self.transcript.with_name("session-3.jsonl")
        self.usage(100_000, path=third, model="claude-opus-5-5")
        self.assertEqual(self.hook("UserPromptSubmit", session="s3", path=third, prompt="go"), {})

    def test_manual_compaction_is_not_learned(self) -> None:
        self.usage(150_000)
        self.boundary("manual", 150_000)
        self.usage(100_000)
        self.assertEqual(self.hook("UserPromptSubmit", prompt="go"), {})

    def test_precompact_learns_latest_usage_only_for_auto_trigger(self) -> None:
        self.usage(90_000)
        self.assertEqual(self.hook("PreCompact", trigger="manual"), {})
        self.usage(170_000)
        self.assertEqual(self.hook("PreCompact", trigger="auto"), {})
        self.assertEqual(self.hook("PostCompact", trigger="auto"), {})
        self.usage(100_000)
        note = self.context(self.hook("UserPromptSubmit", prompt="go"), "UserPromptSubmit")
        self.assertIn("170k", note)

    def test_postcompact_discards_stale_usage_and_rearms_tiers(self) -> None:
        self.usage(120_000)
        self.context(self.hook("UserPromptSubmit", prompt="first"), "UserPromptSubmit")
        self.assertEqual(self.hook("PostCompact", trigger="manual"), {})
        self.assertEqual(self.hook("UserPromptSubmit", prompt="second"), {})
        self.usage(190_000, timestamp="2000-01-01T00:00:00Z")
        self.assertEqual(self.hook("UserPromptSubmit", prompt="third"), {})
        self.usage(125_000)
        self.context(self.hook("UserPromptSubmit", prompt="fourth"), "UserPromptSubmit")

    def test_compact_session_start_relists_handoffs(self) -> None:
        self.handoff("open-task", "Open task")
        self.assertIn("Open task", self.context(self.hook("SessionStart", source="compact"), "SessionStart"))


class LeafAndEventTests(HookTestCase):
    """Subagent budgets on their own transcripts, spawn logging and handoff read/write events."""

    def subagent(self, agent: str) -> Path:
        return self.transcript.with_suffix("") / "subagents" / f"agent-{agent}.jsonl"

    def test_leaf_mode_sends_budget_once_on_own_transcript(self) -> None:
        self.usage(190_000)
        self.usage(85_000, path=self.subagent("a1"))
        self.usage(30_000, path=self.subagent("a2"))
        self.handoff("open-task", "Open task")
        text = self.context(
            self.hook("PostToolUse", agent_id="a1", agent_type="scout", tool_name="Read"), "PostToolUse"
        )
        self.assertEqual(text, policy.BUDGET_MESSAGE.format(tokens=85, budget=60))
        self.assertNotIn("Open task", text)
        self.assertEqual(self.hook("PostToolUse", agent_id="a1", tool_name="Read"), {})
        self.assertEqual(self.hook("PostToolUse", agent_id="a2", tool_name="Read"), {})
        for event in ("SessionStart", "Stop", "PreCompact", "PostCompact"):
            self.assertEqual(self.hook(event, agent_id="a1", trigger="auto"), {})
        self.assertEqual(self.hook("UserPromptSubmit", prompt="go")["decision"], "block")
        budget = [entry for entry in self.logged() if entry["event"] == "subagent_budget"]
        self.assertEqual([entry["session"] for entry in budget], ["a1"])

    def test_agent_type_selects_the_role_budget(self) -> None:
        self.usage(190_000)
        self.usage(65_000, path=self.subagent("r1"))
        text = self.context(
            self.hook("PostToolUse", agent_id="r1", agent_type="runner", tool_name="Read"), "PostToolUse"
        )
        self.assertEqual(text, policy.BUDGET_MESSAGE.format(tokens=65, budget=60))
        self.usage(65_000, path=self.subagent("v1"))
        self.assertEqual(self.hook("PostToolUse", agent_id="v1", agent_type="investigator", tool_name="Read"), {})

    def test_explicit_agent_transcript_path_wins(self) -> None:
        explicit = self.home / "elsewhere" / "agent.jsonl"
        self.usage(85_000, path=explicit)
        text = self.context(
            self.hook("PostToolUse", agent_id="a9", agent_transcript_path=str(explicit), tool_name="Read"),
            "PostToolUse",
        )
        self.assertEqual(text, policy.BUDGET_MESSAGE.format(tokens=85, budget=80))

    def test_agent_type_without_agent_id_is_a_main_session(self) -> None:
        self.usage(120_000)
        self.context(self.hook("UserPromptSubmit", agent_type="reviewer", prompt="go"), "UserPromptSubmit")

    def test_agent_tool_logs_subagent_spawn(self) -> None:
        self.hook(
            "PostToolUse",
            tool_name="Agent",
            tool_input={"subagent_type": "reviewer", "model": "sonnet", "prompt": "review"},
        )
        self.hook("PostToolUse", tool_name="Agent", tool_input={"subagent_type": "scout", "prompt": "find"})
        self.hook("PostToolUse", tool_name="Agent", tool_input={"subagent_type": "context-guard:scout", "prompt": "x"})
        spawns = [entry["detail"] for entry in self.logged() if entry["event"] == "subagent_spawn"]
        scout = {"role": "scout", "tier": "cheap", "model": "claude-haiku-5-5", "effort": "low"}
        self.assertEqual(
            spawns, [{"role": "reviewer", "tier": "standard", "model": "sonnet", "effort": "high"}, scout, scout]
        )

    def test_plugin_agent_type_selects_the_role_budget(self) -> None:
        self.usage(190_000)
        self.usage(65_000, path=self.subagent("p1"))
        text = self.context(
            self.hook("PostToolUse", agent_id="p1", agent_type="context-guard:runner", tool_name="Read"), "PostToolUse"
        )
        self.assertEqual(text, policy.BUDGET_MESSAGE.format(tokens=65, budget=60))

    def test_handoff_reads_and_writes_are_logged(self) -> None:
        path = self.store / "task-0123456789abcdef.md"
        self.hook("PostToolUse", tool_name="Write", tool_input={"file_path": str(path), "content": "x"})
        self.hook("PostToolUse", tool_name="Read", tool_input={"file_path": str(path)})
        self.hook("PostToolUse", tool_name="Read", tool_input={"file_path": str(self.project / "a.md")})
        logged = [(entry["event"], entry["detail"]) for entry in self.logged()]
        self.assertEqual(logged, [("handoff_written", {"path": str(path)}), ("handoff_read", {"path": str(path)})])

    def test_bash_edit_and_multiedit_writes_credit_the_turn_so_stop_does_not_refresh(self) -> None:
        self.store.mkdir(parents=True)
        path = self.store / "existing-task-0123456789abcdef.md"
        writes = [
            ("Bash", {"command": f"cat > {path} <<'EOF'\n# Task\nIt's saved\nEOF"}),
            ("Bash", {"command": f"printf '# Task\\n' | tee {path}"}),
            ("Edit", {"file_path": str(path), "old_string": "a", "new_string": "b"}),
            ("MultiEdit", {"file_path": str(path), "edits": []}),
        ]
        for index, (tool, data) in enumerate(writes):
            with self.subTest(tool=tool, index=index):
                self.usage(130_000 + index * 70_000)
                self.hook("UserPromptSubmit", prompt=f"please write a handoff {index}")
                self.hook("PostToolUse", tool_name=tool, tool_input=data)
                self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": f"sed -n 1,5p {path}"})
        self.assertEqual(
            [entry["event"] for entry in self.logged() if entry["event"].startswith("handoff_")],
            ["handoff_requested", "handoff_written"] * 4 + ["handoff_read"],
        )

    def test_a_note_turn_without_a_write_refreshes_naming_the_unmodified_file(self) -> None:
        self.usage(130_000)
        self.context(self.hook("UserPromptSubmit", prompt="Ship it"), "UserPromptSubmit")
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "echo handoff updated"})
        reason = self.hook("Stop", stop_hook_active=False)["reason"]
        self.assertIn(str(self.store), reason)
        self.assertEqual(
            [entry["detail"] for entry in self.logged() if entry["event"] == "handoff_requested"],
            [{"source": "unwritten"}],
        )


class EntryPointTests(harness.HookBoundaryChecks, HookTestCase):
    """The Claude hook process entry passes on missing arguments, errors and overruns."""

    hook_module, hook_event, pass_response = hooks, "UserPromptSubmit", {}


if __name__ == "__main__":
    unittest.main()
