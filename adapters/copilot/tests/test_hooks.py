import copy
from pathlib import Path

from adapters.copilot import hooks, mapping
from core import config as config_mod
from tests import harness


class HookTests(harness.RuntimeHookCase):
    """Recorded native tools and identities exercise the shared policy in temporary homes."""

    runtime = "copilot"

    def setUp(self) -> None:
        super().setUp()
        self.config = hooks.load_config(self.home, {})
        self.config["kit_check"]["enabled"] = False
        self.transcript = self.home / ".copilot/session-state/root/events.jsonl"

    def payload(self, fixture: str, **extra: object) -> dict:
        """Materialize a recorded payload in the temporary test home.

        Args:
            fixture: Recorded payload name.
            **extra: Explicit semantic test modifications.

        Returns:
            Materialized native hook input.
        """
        return harness.payload(
            "copilot",
            fixture,
            self.home,
            self.project,
            sessionId="root",
            cwd=str(self.project),
            transcriptPath=str(self.transcript),
            **extra,
        )

    def fire(self, event: str, payload: dict) -> dict:
        """Run a hook against this test's temporary home.

        Args:
            event: Native hook event from argv.
            payload: Materialized native payload.

        Returns:
            Native response.
        """
        return hooks.handle(event, payload, self.config, self.home, {})

    def usage(self, count: int, child: str | None = None) -> None:
        """Append native usage with explicit root or child identity.

        Args:
            count: Input tokens including cache.
            child: Native child identity, None for root usage.
        """
        record = copy.deepcopy(harness.read_fixture("copilot", "events-usage-record")["record"])
        record["data"]["usage"].update(
            inputTokens=count, interactionType="conversation-subagent" if child else "conversation-agent"
        )
        if child:
            record["agentId"] = child
        harness.append_transcript(self.transcript, record)

    def logged(self) -> list[dict]:
        """Read policy event records.

        Returns:
            Events recorded by hooks in the temporary home.
        """
        return harness.events(self.home, "copilot")

    def test_native_child_registration_protects_parent_policy_and_uses_child_budget(self) -> None:
        start = self.payload("subagentStart-general-purpose", agentId="child", parentAgentId="root", agentType="scout")
        self.fire("subagentStart", start)
        self.usage(270000)
        tool = self.payload("postToolUse-bash-subagent")
        tool["sessionId"] = "child"
        self.assertEqual(self.fire("postToolUse", tool), {})
        self.usage(60000, "child")
        self.assertIn("budget", self.fire("postToolUse", tool)["additionalContext"].lower())
        self.assertEqual(self.fire("postToolUse", tool), {})
        self.assertIn("decision", self.fire("userPromptSubmitted", self.payload("userPromptSubmitted-root")))

    def test_child_session_start_does_not_receive_root_policy(self) -> None:
        payload = self.payload("sessionStart-new", agentId="child", parentAgentId="root", agentType="scout")
        self.assertEqual(self.fire("sessionStart", payload), {})

    def test_spawn_details_and_successful_shell_write_credit(self) -> None:
        self.usage(170000)
        self.fire("userPromptSubmitted", self.payload("userPromptSubmitted-root"))
        path = config_mod.handoff_dir(self.config, self.home) / "task.md"
        payload = self.payload("postToolUse-bash-subagent")
        payload["toolArgs"] = {"command": f"printf x > '{path}'"}
        self.fire("postToolUse", payload)
        self.assertEqual(self.fire("agentStop", self.payload("agentStop-root")), {})
        self.fire("postToolUse", self.payload("postToolUse-task-root"))
        logged = self.logged()
        self.assertIn("handoff_written", {entry["event"] for entry in logged})
        detail = next(entry["detail"] for entry in logged if entry["event"] == "subagent_spawn")
        self.assertEqual(detail["role"], "plugin-child")
        self.assertEqual(detail["model"], "gpt-6-luna")

    def test_recorded_patch_add_and_update_credit_handoff_writes(self) -> None:
        self.usage(170000)
        for fixture in ("postToolUse-apply_patch-add", "postToolUse-apply_patch-update"):
            with self.subTest(fixture=fixture):
                self.fire("userPromptSubmitted", self.payload("userPromptSubmitted-root"))
                payload = self.payload(fixture)
                path = config_mod.handoff_dir(self.config, self.home) / "task.md"
                payload["toolArgs"] = payload["toolArgs"].replace("<scrubbed>", str(path), 1)
                self.fire("postToolUse", payload)
                self.assertEqual(self.fire("agentStop", self.payload("agentStop-root")), {})
        self.assertEqual(sum(entry["event"] == "handoff_written" for entry in self.logged()), 2)

    def test_failed_patch_and_patch_outside_store_never_credit_handoff(self) -> None:
        payload = self.payload("postToolUse-apply_patch-add")
        self.fire("postToolUse", payload)
        path = config_mod.handoff_dir(self.config, self.home) / "task.md"
        payload["toolArgs"] = payload["toolArgs"].replace("<scrubbed>", str(path), 1)
        payload["toolResult"]["resultType"] = "failure"  # Never-produced failure variant.
        self.fire("postToolUse", payload)
        self.assertNotIn("handoff_written", {entry["event"] for entry in self.logged()})

    def test_failed_shell_call_never_credits_a_write(self) -> None:
        payload = self.payload("postToolUse-bash-subagent")
        payload["toolArgs"] = {"command": f"printf x > '{config_mod.handoff_dir(self.config, self.home) / 'task.md'}'"}
        payload["toolResult"]["resultType"] = "failure"  # Never-produced result variant.
        self.fire("postToolUse", payload)
        self.assertNotIn("handoff_written", {entry["event"] for entry in self.logged()})

    def test_completed_compaction_redelivers_policy_once_and_rearms_warning(self) -> None:
        prompt = self.payload("userPromptSubmitted-root")
        self.usage(170000)
        self.fire("userPromptSubmitted", prompt)
        complete = copy.deepcopy(harness.read_fixture("copilot", "events-compaction-complete")["record"])
        complete["id"] = "compaction-1"
        harness.append_transcript(self.transcript, complete)
        text = self.fire("userPromptSubmitted", prompt)["additionalContext"]
        self.assertIn("context-guard", text)
        self.assertEqual(self.fire("userPromptSubmitted", prompt), {})
        self.usage(170000)
        self.assertIn("Context check", self.fire("userPromptSubmitted", prompt)["additionalContext"])
        self.assertEqual(self.fire("userPromptSubmitted", prompt), {})

    def test_compaction_context_survives_prompt_refusal_until_resend(self) -> None:
        complete = copy.deepcopy(harness.read_fixture("copilot", "events-compaction-complete")["record"])
        complete["id"] = "compaction-1"
        harness.append_transcript(self.transcript, complete)
        self.usage(270000)
        prompt = self.payload("userPromptSubmitted-root")
        self.assertEqual(self.fire("userPromptSubmitted", prompt)["decision"], "block")
        self.assertIn("context-guard", self.fire("userPromptSubmitted", prompt)["additionalContext"])
        self.assertEqual(self.fire("userPromptSubmitted", prompt), {})

    def test_read_credit_and_role_budget_respect_native_agent_type(self) -> None:
        payload = self.payload(
            "postToolUse-view-subagent", agentId="child", parentAgentId="root", agentType="investigator"
        )
        self.usage(60000, "child")
        self.assertEqual(self.fire("postToolUse", payload), {})
        self.usage(120000, "child")
        self.assertIn("budget", self.fire("postToolUse", payload)["additionalContext"].lower())
        root = self.payload("postToolUse-view-root")
        root["toolArgs"]["path"] = str(config_mod.handoff_dir(self.config, self.home) / "task.md")
        self.fire("postToolUse", root)
        self.assertIn("handoff_read", {entry["event"] for entry in self.logged()})
        shell = self.payload("postToolUse-bash-subagent")
        shell["toolArgs"] = {"command": f"cat '{root['toolArgs']['path']}'"}
        self.fire("postToolUse", shell)
        self.assertEqual(sum(entry["event"] == "handoff_read" for entry in self.logged()), 2)

    def test_stop_loop_guard_rejects_non_boolean_variants(self) -> None:
        self.usage(170000)
        payload = self.payload("agentStop-root")
        for key in hooks.STOP_ACTIVE_KEYS:
            for value in (True, 1, "false"):
                self.assertEqual(hooks.run("agentStop", {**payload, key: value}, self.home, {}), {})
        self.assertIn("reason", hooks.run("agentStop", {**payload, "stop_hook_active": False}, self.home, {}))

    def test_relative_runtime_home_is_explicitly_invalid(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            mapping.copilot_home(Path("/temporary"), {"COPILOT_HOME": "relative"})
