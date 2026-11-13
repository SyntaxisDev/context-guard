import copy
from unittest.mock import patch

from adapters.copilot import hooks
from core import deadline, state
from tests import harness


class CompactionTransactionTests(harness.RuntimeHookCase):
    """Native completed compactions remain retryable until context is committed for delivery."""

    runtime = "copilot"

    def setUp(self) -> None:
        super().setUp()
        self.config = hooks.load_config(self.home, {})
        self.config["kit_check"]["enabled"] = False
        self.transcript = self.home / ".copilot/session-state/root/events.jsonl"
        self.payload = harness.payload(
            "copilot",
            "userPromptSubmitted-root",
            self.home,
            self.project,
            sessionId="root",
            cwd=str(self.project),
            transcriptPath=str(self.transcript),
        )

    def complete(self, identity: str) -> None:
        """Append a recorded successful compaction with a distinct native event identity.

        Args:
            identity: Stable compaction event id.
        """
        record = copy.deepcopy(harness.read_fixture("copilot", "events-compaction-complete")["record"])
        record["id"] = identity
        harness.append_transcript(self.transcript, record)

    def fire(self) -> dict:
        """Run a root prompt through the real hook deadline boundary.

        Returns:
            Native hook response or fail-open fallback on deadline interruption.
        """
        return deadline.run_within(
            lambda: hooks.handle("userPromptSubmitted", self.payload, self.config, self.home, {}), {}
        )

    def test_interrupted_first_evaluation_does_not_consume_completed_compaction(self) -> None:
        self.complete("compaction-1")
        with patch.object(hooks, "evaluate", side_effect=deadline.DeadlineExceeded):
            self.assertEqual(self.fire(), {})
        with patch.object(hooks, "session_policy", return_value="recovered-policy"):
            self.assertIn("recovered-policy", self.fire()["additionalContext"])
            self.assertEqual(self.fire(), {})

    def test_interrupted_response_evaluation_keeps_compaction_retryable(self) -> None:
        self.complete("compaction-1")
        original = hooks.policy.decide
        calls = 0

        def interrupted(*args: object) -> object:
            """Interrupt after compaction policy is evaluated, before response policy.

            Args:
                *args: Arguments forwarded to the policy decision.

            Returns:
                The compaction policy decision.

            Raises:
                DeadlineExceeded: At the second evaluation before compaction consumption.
            """
            nonlocal calls
            calls += 1
            if calls == 2:
                raise deadline.DeadlineExceeded
            return original(*args)

        with patch.object(hooks.policy, "decide", side_effect=interrupted):
            self.assertEqual(self.fire(), {})
        with patch.object(hooks, "session_policy", return_value="retry-policy"):
            self.assertIn("retry-policy", self.fire()["additionalContext"])
            self.assertEqual(self.fire(), {})

    def test_failed_deadline_reserve_does_not_consume_compaction_or_pending_context(self) -> None:
        self.complete("compaction-1")
        with (
            patch.object(hooks, "session_policy", return_value="new-policy"),
            patch.object(deadline, "commit", side_effect=deadline.DeadlineExceeded),
        ):
            self.assertEqual(self.fire(), {})
        with patch.object(hooks, "session_policy", return_value="retry-policy"):
            self.assertIn("retry-policy", self.fire()["additionalContext"])
            self.assertEqual(self.fire(), {})

    def test_new_compaction_replaces_old_blocked_context_without_replaying_it(self) -> None:
        self.complete("compaction-1")
        usage = copy.deepcopy(harness.read_fixture("copilot", "events-usage-record")["record"])
        usage["data"]["usage"]["inputTokens"] = 270000
        harness.append_transcript(self.transcript, usage)
        with patch.object(hooks, "session_policy", return_value="obsolete-policy"):
            self.assertEqual(self.fire()["decision"], "block")
        self.complete("compaction-2")
        with patch.object(hooks, "session_policy", return_value="current-policy"):
            response = self.fire()
            self.assertIn("current-policy", response["additionalContext"])
            self.assertNotIn("obsolete-policy", response["additionalContext"])
            self.assertEqual(self.fire(), {})

    def test_session_start_compaction_drops_obsolete_pending_context(self) -> None:
        with state.session_state(self.state_dir, "copilot", "root") as saved:
            saved["adapter"] = {"recovery_context": "obsolete-policy"}
        self.complete("compaction-1")
        start = harness.payload(
            "copilot",
            "sessionStart-new",
            self.home,
            self.project,
            sessionId="root",
            cwd=str(self.project),
            transcriptPath=str(self.transcript),
        )
        with patch.object(hooks, "session_policy", return_value="current-policy"):
            response = hooks.handle("sessionStart", start, self.config, self.home, {})
        self.assertIn("current-policy", response["additionalContext"])
        with state.session_state(self.state_dir, "copilot", "root") as saved:
            self.assertEqual(saved["adapter"]["compaction"], "compaction-1")
            self.assertNotIn("recovery_context", saved["adapter"])
        self.assertNotIn("obsolete-policy", str(self.fire()))
