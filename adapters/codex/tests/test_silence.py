"""Observable silence delivery with recorded child hooks and temporary homes."""

import os
from unittest import mock

from adapters.codex import hooks, subagents
from adapters.codex.tests.driver import Driver
from core import deadline
from tests import harness


class SilenceTests(harness.RuntimeHookCase):
    """Exercise delivery, real activity, completion and refusal without native calls."""

    runtime = "codex"

    def setUp(self) -> None:
        super().setUp()
        self.driver = Driver(self.home, self.project, "silence-parent")
        self.driver.tokens(1000)
        self.driver.tokens(1000, leaf=True)

    def fire(self, action: str, now: float, **fields: object) -> dict:
        """Run the real hook with an explicit activity clock and recorded payload.

        Args:
            action: Recorded native action.
            now: Controlled activity time.
            **fields: Native field overrides.

        Returns:
            Native hook response.
        """
        data = self.driver.payload(action, **fields)
        with mock.patch.object(subagents, "clock", return_value=now):
            return hooks.run(data["hook_event_name"], data, self.home, {})

    def text(self, response: dict) -> str:
        """Read model-facing native context.

        Args:
            response: Hook response.

        Returns:
            Additional context or an empty string.
        """
        return response.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_actual_activity_rearms_once_and_child_keeps_its_budget(self) -> None:
        self.fire("leaf", 100)
        self.assertIn("silent for 10 minutes", self.text(self.fire("tool", 701)))
        self.assertNotIn("has been silent", self.text(self.fire("start", 702)))
        self.driver.tokens(125000, leaf=True)
        child = self.text(self.fire("leaf", 800, role="reviewer"))
        self.assertIn("budget", child)
        self.assertNotIn("silent", child)
        self.assertNotIn("silent", self.text(self.fire("tool", 1399)))
        self.assertIn("silent for 10 minutes", self.text(self.fire("prompt", 1401)))
        self.assertEqual(sum(e["event"] == "subagent_silent" for e in self.logged()), 2)

    def test_refused_prompt_does_not_consume_notice(self) -> None:
        self.fire("leaf", 100)
        self.driver.tokens(250000)
        self.assertEqual(self.fire("prompt", 701)["decision"], "block")
        self.assertFalse(any(e["event"] == "subagent_silent" for e in self.logged()))
        self.assertIn("silent", self.text(self.fire("tool", 702)))

    def test_completed_child_is_retired(self) -> None:
        self.fire("leaf", 100)
        with mock.patch("adapters.codex.hooks.waker.start"):
            self.fire("leaf_stop", 200)
        self.assertNotIn("silent", self.text(self.fire("tool", 900)))

    def test_rollout_activity_rearms_without_child_tool(self) -> None:
        self.fire("leaf", 100)
        self.assertIn("has been silent", self.text(self.fire("tool", 701)))
        self.driver.tokens(2000, leaf=True)
        child = harness.codex_child_rollout(self.driver.transcript, self.driver.agent)
        os.utime(child, (800, 800))
        self.assertNotIn("has been silent", self.text(self.fire("tool", 1399)))
        self.assertIn("silent for 10 minutes", self.text(self.fire("start", 1401)))

    def test_user_threshold_controls_delivery(self) -> None:
        config = hooks.load_config(self.home, {})
        config["subagents"]["silence_minutes"] = 3
        with mock.patch.object(hooks, "load_config", return_value=config):
            self.fire("leaf", 100)
            self.assertNotIn("has been silent", self.text(self.fire("tool", 279)))
            self.assertIn("silent for 3 minutes", self.text(self.fire("prompt", 281)))

    def test_missing_child_rollout_still_tracks_observed_hooks(self) -> None:
        harness.codex_child_rollout(self.driver.transcript, self.driver.agent).unlink()
        self.fire("leaf", 100)
        self.assertIn("has been silent", self.text(self.fire("tool", 701)))

    def test_late_commit_cannot_log_or_consume_notice(self) -> None:
        self.fire("leaf", 100)
        original = len([event for event in self.logged() if event["event"] == "subagent_silent"])
        with mock.patch.object(deadline, "commit", side_effect=deadline.DeadlineExceeded):
            deadline.run_within(lambda: self.fire("tool", 701), {})
        self.assertEqual(len([event for event in self.logged() if event["event"] == "subagent_silent"]), original)
        self.assertIn("has been silent", self.text(self.fire("tool", 702)))

    def test_stop_response_cannot_consume_silence_notice(self) -> None:
        self.fire("leaf", 100)
        self.assertNotIn("has been silent", self.text(self.fire("stop", 701)))
        self.assertIn("has been silent", self.text(self.fire("tool", 702)))

    def test_tracking_preserves_distinct_children(self) -> None:
        self.fire("leaf", 100)
        self.fire("leaf", 101, agent_id="other-child")
        note = self.text(self.fire("tool", 702))
        self.assertIn(f"agent {self.driver.agent}", note)
        self.assertIn("agent other-child", note)
        self.assertEqual(sum(e["event"] == "subagent_silent" for e in self.logged()), 2)
