"""Genie hook payload contracts, rollout telemetry, leaf mode and logged events."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from adapters.genie import hooks, rollout
from core import policy, state
from core.runner.provider import INTERNAL_ENV
from tests import harness

RECORDED = {
    "SessionStart": "SessionStart-startup",
    "UserPromptSubmit": "UserPromptSubmit-root",
    "PostToolUse": "PostToolUse-exec",
    "Stop": "Stop-root",
}
WINDOW = 258400
SESSION = "01a112b2-session-one"
AGENT = "01a13900-51f5-7700-b8a2-7a471a202ade"


class HookCase(harness.RuntimeHookCase):
    """A Genie home with a session rollout and a helper that runs hooks on recorded payloads."""

    runtime = "genie"

    def setUp(self) -> None:
        super().setUp()
        self.sessions = self.home / "sessions"
        self.sessions.mkdir()
        self.rollout = self.sessions / f"rollout-2026-10-18T09-00-00-{SESSION}.jsonl"
        self.write_rollout(self.rollout, 1000)

    def write_rollout(self, path: Path, tokens: int, *extra: object) -> None:
        harness.write_rollout(path, tokens, WINDOW, *extra)

    def event(self, name: str, **fields: object) -> dict:
        recorded = harness.payload("genie", RECORDED[name], self.home, self.project)
        payload = {key: value for key, value in recorded.items() if key not in ("agent_id", "agent_type")}
        payload.update(
            {
                "session_id": SESSION,
                "cwd": str(self.project),
                "transcript_path": str(self.rollout),
                "model": "gpt-6.1-sol",
                "hook_event_name": name,
                **fields,
            }
        )
        return hooks.run(name, payload, self.home)

    def context(self, response: dict, event: str) -> str:
        self.assertEqual(set(response), {"hookSpecificOutput"})
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], event)
        return response["hookSpecificOutput"]["additionalContext"]


class SessionStartTests(HookCase):
    """SessionStart injects the policy and ranked open handoffs."""

    def test_startup_injects_policy_and_ranked_handoffs(self) -> None:
        self.store.mkdir(parents=True)
        (self.store / "invoice-rounding-0123456789abcdef.md").write_text(
            f"# Invoice rounding\nStatus: open · Workspace: {self.project}\n"
        )
        (self.store / "finished-fedcba9876543210.md").write_text("# Finished\nStatus: done\n")
        text = self.context(self.event("SessionStart", source="startup"), "SessionStart")
        self.assertIn('`fork_turns: "none"`', text)
        self.assertIn("Invoice rounding", text)
        self.assertIn("(likely match)", text)
        self.assertNotIn("Finished", text)
        self.assertLess(len(text) / 4, 6000)

    def test_compact_source_reinjects_and_resets_fired_tiers(self) -> None:
        self.write_rollout(self.rollout, 160000)
        self.assertIn(
            "Context check", self.context(self.event("UserPromptSubmit", prompt="fix it"), "UserPromptSubmit")
        )
        self.assertEqual(self.event("UserPromptSubmit", prompt="more"), {})
        text = self.context(self.event("SessionStart", source="compact"), "SessionStart")
        self.assertIn('`fork_turns: "none"`', text)
        self.assertIn("Context check", self.context(self.event("UserPromptSubmit", prompt="again"), "UserPromptSubmit"))


class PromptAndToolTests(HookCase):
    """Prompt and tool hooks note or block by rollout usage."""

    def test_note_then_silence_until_next_tier(self) -> None:
        self.write_rollout(self.rollout, 160000)
        text = self.context(self.event("UserPromptSubmit", prompt="Fix invoice rounding"), "UserPromptSubmit")
        self.assertIn("160k", text)
        self.assertIn("258k", text)
        self.assertEqual(self.event("UserPromptSubmit", prompt="next"), {})
        self.assertEqual(self.event("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}), {})
        self.write_rollout(self.rollout, 200000)
        self.assertIn("200k", self.context(self.event("PostToolUse", tool_name="Bash"), "PostToolUse"))
        self.assertEqual([r["tier"] for r in self.logged() if r["event"] == "note"], ["note", "firm"])
        self.assertEqual([r["tier"] for r in self.logged() if r["event"] == "note_delivered"], ["note", "firm"])

    def test_block_response_repeats_the_reason_as_system_message(self) -> None:
        self.write_rollout(self.rollout, 250000)
        blocked = self.event("UserPromptSubmit", prompt="implement feature")
        self.assertEqual(set(blocked), {"decision", "reason", "systemMessage"})
        self.assertEqual(blocked["decision"], "block")
        self.assertEqual(blocked["reason"], blocked["systemMessage"])
        self.assertIn("250k", blocked["reason"])

    def test_tool_notes_never_block(self) -> None:
        self.write_rollout(self.rollout, 250000)
        response = self.event("PostToolUse", tool_name="Bash", tool_input={"command": "ls"})
        self.assertNotIn("decision", response)
        self.context(response, "PostToolUse")


class StopTests(HookCase):
    """Stop requests a handoff refresh at most once per turn."""

    def test_refresh_continues_once_per_turn(self) -> None:
        self.write_rollout(self.rollout, 160000)
        result = self.event("Stop", stop_hook_active=False)
        self.assertEqual(set(result), {"decision", "reason"})
        self.assertTrue(result["reason"].startswith("[context-guard]"))
        self.assertEqual(self.event("Stop", stop_hook_active=True), {})
        self.assertEqual(self.event("Stop", stop_hook_active=False), {})

    def test_a_note_turn_refreshes_only_when_the_handoff_was_not_written(self) -> None:
        self.write_rollout(self.rollout, 160000)
        self.context(self.event("UserPromptSubmit", prompt="task"), "UserPromptSubmit")
        self.store.mkdir(parents=True)
        handoff = self.store / "existing-task-0123456789abcdef.md"
        self.event("PostToolUse", tool_name="exec_command", tool_input={"cmd": f"cat > {handoff} <<'EOF'\n# T\nEOF"})
        self.assertEqual(self.event("Stop", stop_hook_active=False), {})
        self.write_rollout(self.rollout, 200000)
        self.context(self.event("UserPromptSubmit", prompt="next"), "UserPromptSubmit")
        reason = self.event("Stop", stop_hook_active=False)["reason"]
        self.assertIn(str(handoff), reason)
        self.assertEqual(
            [r["detail"] for r in self.logged() if r["event"] == "handoff_requested"], [{"source": "unwritten"}]
        )


class TelemetryTests(HookCase):
    """Missing, stale or invalid rollout telemetry and invalid payloads pass."""

    def test_missing_telemetry_passes(self) -> None:
        self.rollout.unlink()
        self.assertEqual(self.event("UserPromptSubmit", prompt="implement feature"), {})
        self.assertEqual(self.event("PostToolUse", tool_name="Bash"), {})
        self.assertEqual(self.event("Stop", stop_hook_active=False), {})
        self.assertEqual(self.event("UserPromptSubmit", prompt="x", transcript_path=None), {})

    def test_compaction_after_latest_count_disables_it(self) -> None:
        self.write_rollout(self.rollout, 250000, {"type": "compacted", "payload": {}})
        self.assertEqual(self.event("UserPromptSubmit", prompt="implement feature"), {})
        markers = {
            "context_compacted": {"type": "event_msg", "payload": {"type": "context_compacted"}},
            "compaction item": {
                "type": "response_item",
                "payload": {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"},
            },
        }
        for label, marker in markers.items():
            with self.subTest(label):
                self.write_rollout(self.rollout, 250000, marker)
                self.assertIsNone(rollout.latest_usage(str(self.rollout), 1 << 20))
                self.write_rollout(self.rollout, 250000, marker, harness.token_count(20000))
                self.assertEqual(rollout.latest_usage(str(self.rollout), 1 << 20), rollout.Usage(20000, WINDOW))

    def test_latest_usage_ignores_partial_lines_oversized_integers_and_cumulative_totals(self) -> None:
        self.addCleanup(sys.set_int_max_str_digits, sys.get_int_max_str_digits())
        sys.set_int_max_str_digits(4300)
        self.write_rollout(self.rollout, 40000, '{"type":', "\xff", '{"x": ' + "9" * 5000 + "}")
        self.assertEqual(rollout.latest_usage(str(self.rollout), 1 << 20), rollout.Usage(40000, WINDOW))
        self.write_rollout(self.rollout, 40000, harness.token_count(30000, None))
        self.assertEqual(rollout.latest_usage(str(self.rollout), 1 << 20), rollout.Usage(30000, None))

    def test_invalid_latest_usage_disables_stale_count(self) -> None:
        for info in (
            [],
            {},
            {"last_token_usage": []},
            {"last_token_usage": {"total_tokens": True}},
            {"last_token_usage": {"total_tokens": -1}},
            {"last_token_usage": {"input_tokens": 20, "output_tokens": "x"}},
        ):
            with self.subTest(info=info):
                latest = {"type": "event_msg", "payload": {"type": "token_count", "info": info}}
                self.write_rollout(self.rollout, 250000, latest)
                self.assertIsNone(rollout.latest_usage(str(self.rollout), 1 << 20))
        partial = {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 20, "output_tokens": 3}}},
        }
        self.write_rollout(self.rollout, 250000, partial)
        self.assertEqual(rollout.latest_usage(str(self.rollout), 1 << 20), rollout.Usage(23, None))
        for path, tail in ((None, 1024), ("bad\0path", 1024), (str(self.rollout), 0), (str(self.rollout), True)):
            self.assertIsNone(rollout.latest_usage(path, tail))

    def test_rollout_tail_size_follows_the_environment(self) -> None:
        self.write_rollout(self.rollout, 250000)
        self.assertEqual(
            hooks.load_config(self.home, {"CONTEXT_GUARD_ROLLOUT_TAIL_BYTES": "0"})["rollout"], {"tail_bytes": 0}
        )
        with mock.patch.dict(os.environ, {"CONTEXT_GUARD_ROLLOUT_TAIL_BYTES": "0"}):
            self.assertEqual(self.event("UserPromptSubmit", prompt="implement feature"), {})
        self.assertEqual(self.event("UserPromptSubmit", prompt="implement feature")["decision"], "block")

    def test_invalid_payloads_pass(self) -> None:
        self.write_rollout(self.rollout, 250000)
        for name, payload in (
            ("UserPromptSubmit", None),
            ("UserPromptSubmit", []),
            ("Unknown", {}),
            ("UserPromptSubmit", {"session_id": SESSION, "prompt": "a\0b"}),
            ("UserPromptSubmit", {"session_id": SESSION, "hook_event_name": "Stop"}),
            ("UserPromptSubmit", {"prompt": "no session"}),
            ("PostToolUse", {"session_id": SESSION, "agent_id": "../escape"}),
        ):
            with self.subTest(name=name, payload=payload):
                self.assertEqual(hooks.run(name, payload, self.home, {}), {})


class LeafTests(HookCase):
    """Subagent threads get their role budget instead of the orchestrator tiers."""

    def setUp(self) -> None:
        super().setUp()
        self.write_rollout(self.rollout, 250000)
        self.agent_rollout = self.sessions / f"rollout-2026-10-18T09-01-24-{AGENT}.jsonl"

    def test_budget_note_once_from_the_subagent_rollout(self) -> None:
        self.write_rollout(self.agent_rollout, 50000)
        self.assertEqual(self.event("PostToolUse", agent_id=AGENT, agent_type="scout", tool_name="Bash"), {})
        self.write_rollout(self.agent_rollout, 90000)
        text = self.context(
            self.event("PostToolUse", agent_id=AGENT, agent_type="scout", tool_name="Bash"), "PostToolUse"
        )
        self.assertEqual(text, policy.BUDGET_MESSAGE.format(tokens=90, budget=60))
        self.assertEqual(self.event("PostToolUse", agent_id=AGENT, tool_name="Bash"), {})
        self.assertTrue((self.state_dir / f"{state.session_key('genie', AGENT)}.json").exists())
        self.assertEqual(
            [record["session"] for record in self.logged() if record["event"] == "subagent_budget"], [AGENT]
        )

    def test_agent_type_selects_the_role_budget(self) -> None:
        self.write_rollout(self.agent_rollout, 100000)
        self.assertEqual(self.event("PostToolUse", agent_id=AGENT, agent_type="reviewer", tool_name="Bash"), {})
        self.write_rollout(self.agent_rollout, 125000)
        self.context(self.event("PostToolUse", agent_id=AGENT, agent_type="reviewer", tool_name="Bash"), "PostToolUse")
        self.assertEqual(
            [r["detail"] for r in self.logged() if r["event"] == "subagent_budget"],
            [{"role": "reviewer", "budget": 120_000}],
        )

    def test_parent_tokens_and_state_stay_with_the_parent(self) -> None:
        self.assertEqual(self.event("UserPromptSubmit", agent_id=AGENT, prompt="go"), {})
        self.assertEqual(self.event("PostToolUse", agent_id=AGENT, tool_name="Bash"), {})
        self.assertEqual(self.event("UserPromptSubmit", prompt="parent prompt")["decision"], "block")

    def test_subagent_rollout_is_found_in_the_next_day_directory(self) -> None:
        parent = self.sessions / "2026" / "10" / "18" / self.rollout.name
        child = self.sessions / "2026" / "10" / "19" / f"rollout-2026-10-19T00-01-24-{AGENT}.jsonl"
        for path, tokens in ((parent, 250000), (child, 90000)):
            path.parent.mkdir(parents=True, exist_ok=True)
            self.write_rollout(path, tokens)
        response = self.event("PostToolUse", agent_id=AGENT, tool_name="Bash", transcript_path=str(parent))
        self.assertEqual(self.context(response, "PostToolUse"), policy.BUDGET_MESSAGE.format(tokens=90, budget=80))


class EventLogTests(HookCase):
    """Spawn and handoff read/write events are logged."""

    def test_spawn_records_role_tier_model_and_effort(self) -> None:
        self.event(
            "PostToolUse",
            tool_name="spawn_agent",
            tool_input={
                "task_name": "find",
                "agent_type": "reviewer",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "fork_turns": "none",
            },
        )
        self.event("PostToolUse", tool_name="collaborationspawn_agent", tool_input={"model": "gpt-5.6-luna"})
        self.event("PostToolUse", tool_name="spawn_agent", tool_input={"agent_type": "scout", "fork_turns": "none"})
        spawns = [record["detail"] for record in self.logged() if record["event"] == "subagent_spawn"]
        self.assertEqual(
            spawns,
            [
                {"role": "reviewer", "tier": "deep", "model": "gpt-5.6-sol", "effort": "high"},
                {"role": None, "tier": "cheap", "model": "gpt-5.6-luna", "effort": None},
                {"role": "scout", "tier": "cheap", "model": "gpt-5.6-luna", "effort": "low"},
            ],
        )

    def test_relative_shell_writes_resolve_against_the_workdir(self) -> None:
        self.store.mkdir(parents=True)
        self.event(
            "PostToolUse",
            tool_name="exec_command",
            tool_input={"cmd": "printf x > task-0123456789abcdef.md", "workdir": str(self.store)},
        )
        self.assertEqual(
            [(r["event"], r["detail"]["path"]) for r in self.logged()],
            [("handoff_written", str(self.store / "task-0123456789abcdef.md"))],
        )

    def test_handoff_write_and_read_detection(self) -> None:
        self.store.mkdir(parents=True)
        handoff = self.store / "invoice-rounding-0123456789abcdef.md"
        patch = f"*** Begin Patch\n*** Add File: {handoff}\n+# Invoice rounding\n*** End Patch\n"
        self.event("PostToolUse", tool_name="apply_patch", tool_input={"command": patch})
        self.event("PostToolUse", tool_name="Bash", tool_input={"command": f"sed -n '1,40p' '{handoff}'"})
        self.event("PostToolUse", tool_name="Bash", tool_input={"command": "cat ~/.context-guard/handoffs/x.md"})
        self.event("PostToolUse", tool_name="Bash", tool_input={"command": f"cat README.md {self.project}/x.md"})
        found = [
            (record["event"], record["detail"]["path"])
            for record in self.logged()
            if record["event"].startswith("handoff_")
        ]
        self.assertEqual(
            found,
            [
                ("handoff_written", str(handoff)),
                ("handoff_read", str(handoff)),
                ("handoff_read", str(self.store / "x.md")),
            ],
        )
        session = json.loads((self.state_dir / f"{state.session_key('genie', SESSION)}.json").read_text())
        self.assertEqual(session["handoff_path"], str(handoff))

    def test_exec_and_shell_tool_names_detect_handoff_access(self) -> None:
        self.store.mkdir(parents=True)
        handoff = self.store / "parser-0123456789abcdef.md"
        patch = json.dumps(f"*** Begin Patch\n*** Add File: {handoff}\n+# Parser\n*** End Patch\n")
        self.event("PostToolUse", tool_name="exec", tool_input=f"text(await tools.apply_patch({patch}));\n")
        read = json.dumps(f"sed -n '1,40p' {handoff}")
        self.event(
            "PostToolUse",
            tool_name="exec",
            tool_input={"input": f'text(await tools.exec_command({{cmd:{read},"max_output_tokens":3000}}));'},
        )
        self.event("PostToolUse", tool_name="exec_command", tool_input={"cmd": f"cat {handoff}"})
        self.event("PostToolUse", tool_name="shell", tool_input={"command": ["cat", str(handoff)]})
        self.event("PostToolUse", tool_name="exec", tool_input='text(await tools.exec_command({cmd:"ls"}));')
        found = [record["event"] for record in self.logged() if record["event"].startswith("handoff_")]
        self.assertEqual(found, ["handoff_written", "handoff_read", "handoff_read", "handoff_read"])


class EntryPointTests(harness.HookBoundaryChecks, HookCase):
    """The Genie hook process entry passes on missing arguments, errors, overruns and internal runs."""

    hook_module, hook_event, pass_response = hooks, "UserPromptSubmit", {}

    def test_configuration_errors_and_internal_runs_pass(self) -> None:
        with mock.patch.object(hooks.config_mod, "load_config", side_effect=ValueError("bad config")):
            self.assertEqual(hooks.run("UserPromptSubmit", {"session_id": "s"}, self.home), {})
        payload = {"session_id": SESSION, "cwd": str(self.project), "source": "startup"}
        self.assertEqual(hooks.run("SessionStart", payload, self.home, {INTERNAL_ENV: "1"}), {})
        self.assertIn("fork_turns", self.context(self.event("SessionStart", source="startup"), "SessionStart"))


if __name__ == "__main__":
    unittest.main()
