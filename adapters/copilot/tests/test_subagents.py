import copy
from unittest.mock import patch

from adapters.copilot import hooks, subagents
from core import state
from tests import harness


class SilenceTests(harness.RuntimeHookCase):
    """Recorded lifecycle identities drive notices under a controlled activity clock."""

    runtime = "copilot"

    def setUp(self) -> None:
        super().setUp()
        self.config = hooks.load_config(self.home, {})
        self.config["kit_check"]["enabled"] = False
        self.transcript = self.home / ".copilot/session-state/root/events.jsonl"

    def fire(self, fixture: str, now: float, **extra: object) -> dict:
        """Run one recorded hook with a controlled native activity clock.

        Args:
            fixture: Recorded payload name.
            now: Activity clock value in epoch seconds.
            **extra: Explicit native identity or prompt overrides.

        Returns:
            Native hook response.
        """
        recorded = harness.read_fixture("copilot", fixture)
        payload = harness.payload(
            "copilot",
            fixture,
            self.home,
            self.project,
            sessionId="root",
            cwd=str(self.project),
            transcriptPath=str(self.transcript),
        )
        payload.update(extra)
        with patch.object(subagents, "clock", return_value=now):
            return hooks.handle(recorded["event"], payload, self.config, self.home, {})

    def start(self, now: float = 100) -> None:
        """Record a genuine native general-purpose child lifecycle start.

        Args:
            now: Controlled spawn clock.
        """
        self.fire("subagentStart-general-purpose", now, agentId="child", parentAgentId="root")

    def child_tool(self, now: float) -> dict:
        """Run a child's recorded tool with identity recovered from its start.

        Args:
            now: Controlled child activity clock.

        Returns:
            Native tool response.
        """
        return self.fire("postToolUse-bash-subagent", now, sessionId="child")

    def silent_events(self) -> list[dict]:
        """Read delivered silence events.

        Returns:
            Policy subagent_silent records.
        """
        return [entry for entry in self.logged() if entry["event"] == "subagent_silent"]

    def test_parent_prompt_and_tool_do_not_refresh_child_activity(self) -> None:
        self.start()
        self.assertEqual(self.fire("userPromptSubmitted-root", 500), {})
        self.assertEqual(self.fire("postToolUse-view-root", 600), {})
        response = self.fire("postToolUse-view-root", 700)
        self.assertIn(
            "Subagent `general-purpose` (agent child) has been silent for 10 minutes", response["additionalContext"]
        )
        self.assertIn(str(self.transcript), response["additionalContext"])
        self.assertEqual(self.fire("userPromptSubmitted-root", 800), {})
        self.assertEqual(len(self.silent_events()), 1)

    def test_child_activity_starts_a_new_silence_period_after_each_notice(self) -> None:
        self.start()
        self.assertIn("silent", self.fire("userPromptSubmitted-root", 700)["additionalContext"])
        self.assertEqual(self.child_tool(750), {})
        self.assertEqual(self.fire("userPromptSubmitted-root", 1000), {})
        self.assertIn("silent", self.fire("postToolUse-view-root", 1350)["additionalContext"])
        self.assertEqual(len(self.silent_events()), 2)

    def test_child_prompt_refreshes_activity_and_other_root_is_isolated(self) -> None:
        self.start()
        self.fire("userPromptSubmitted-subagent", 650, sessionId="child", agentId="child", parentAgentId="root")
        self.assertEqual(self.fire("postToolUse-view-root", 700), {})
        self.assertEqual(self.fire("postToolUse-view-root", 1300, sessionId="other-root"), {})
        self.assertIn("silent", self.fire("userPromptSubmitted-root", 1300)["additionalContext"])

    def test_completed_child_is_removed_and_later_tool_does_not_resurrect_it(self) -> None:
        self.start()
        self.fire("subagentStop-general-purpose", 200, agentId="child", parentAgentId="root")
        self.child_tool(700)
        self.assertEqual(self.fire("userPromptSubmitted-root", 1500), {})
        self.assertEqual(self.silent_events(), [])

    def test_prompt_block_retains_notice_until_the_resend_delivers_it(self) -> None:
        self.start()
        usage = copy.deepcopy(harness.read_fixture("copilot", "events-usage-record")["record"])
        usage["data"]["usage"]["inputTokens"] = 270000
        harness.append_transcript(self.transcript, usage)
        self.assertEqual(self.fire("userPromptSubmitted-root", 700)["decision"], "block")
        self.assertEqual(self.silent_events(), [])
        self.assertIn("silent", self.fire("userPromptSubmitted-root", 701)["additionalContext"])
        self.assertEqual(len(self.silent_events()), 1)
        self.assertEqual(self.fire("userPromptSubmitted-root", 800), {})

    def test_corrupt_tracker_entries_are_ignored(self) -> None:
        with state.session_state(self.state_dir, "copilot", "root") as current:
            current["adapter"] = {
                subagents.TRACKED: {
                    "bad-count": {"active": True},
                    "bad-record": [],
                    "missing-time": {},
                }
            }  # Never-produced corrupt state variants.
        self.assertEqual(self.fire("userPromptSubmitted-root", 1000), {})
        self.assertEqual(self.silent_events(), [])

    def test_parent_agent_stop_does_not_consume_a_notice(self) -> None:
        self.start()
        self.assertEqual(self.fire("agentStop-root", 700), {})
        self.assertEqual(self.silent_events(), [])
        self.assertIn("silent", self.fire("postToolUse-view-root", 701)["additionalContext"])
