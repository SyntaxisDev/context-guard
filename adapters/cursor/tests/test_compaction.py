"""Native captured compaction lifecycle, delivery and transactional failure contracts."""

import time
import unittest
from unittest.mock import patch

from adapters.cursor import hooks
from adapters.cursor.tests.driver import Driver
from adapters.cursor.tests.test_hooks import CursorHookCase
from core import deadline, events, state
from tests import harness


class CompactionTests(CursorHookCase):
    """Replay genuine lifecycle payloads, varying only distinct failure conditions."""

    def fire(self, fixture: str, **fields: object) -> dict:
        """Invoke the recorded hook using one isolated session.

        Args:
            fixture: Source-backed fixture identity.
            **fields: Negative-case field overrides.

        Returns:
            Native hook response.
        """
        payload = harness.payload(
            "cursor", fixture, self.home, self.project, conversation_id="c1", session_id="c1", **fields
        )
        return hooks.run(payload["hook_event_name"], payload, self.home, {})

    def complete(self) -> None:
        """Replay initiation and its matching successful stop."""
        self.fire("preCompact-root")
        self.fire("stop-compaction-completed")

    def test_confirmed_recovery_delivers_policy_handoffs_and_rearms_once(self) -> None:
        path = harness.seed_handoff(self.home, self.project)
        self.tokens("c1", 120000)
        self.assertIn("Context check", self.fire("postToolUse-recovery-read")["additional_context"])
        self.complete()
        result = self.fire("postToolUse-recovery-read")["additional_context"]
        self.assertIn(path.stem, result)
        self.assertIn("Orchestrator mode", result)
        self.assertIn("Recovery context", result)
        self.assertIn("Context check", self.fire("postToolUse-recovery-read")["additional_context"])
        self.complete()
        self.assertEqual(self.fire("postToolUse-recovery-read"), {})

    def test_session_start_delivers_ready_context_once(self) -> None:
        self.complete()
        self.assertIn("Recovery context", self.hook("sessionStart")["additional_context"])
        self.assertNotIn("Recovery context", self.hook("sessionStart")["additional_context"])
        self.assertEqual(self.fire("postToolUse-recovery-read"), {})

    def test_ignored_overlapping_initiation_cannot_be_replayed_as_new(self) -> None:
        self.fire("preCompact-root")
        self.fire("preCompact-root", generation_id="overlap")
        self.fire("stop-compaction-completed", generation_id="overlap")
        self.assertEqual(self.fire("postToolUse-recovery-read"), {})
        self.fire("stop-compaction-completed")
        self.assertIn("Recovery context", self.fire("postToolUse-recovery-read")["additional_context"])
        self.fire("preCompact-root", generation_id="overlap")
        self.fire("stop-compaction-completed", generation_id="overlap")
        self.assertEqual(self.fire("postToolUse-recovery-read"), {})

    def test_initiation_and_low_usage_do_not_rearm(self) -> None:
        self.tokens("c1", 120000)
        self.fire("postToolUse-recovery-read")
        self.fire("preCompact-root")
        self.tokens("c1", 1000)
        self.assertEqual(self.fire("postToolUse-recovery-read"), {})
        self.tokens("c1", 120000)
        self.assertEqual(self.fire("postToolUse-recovery-read"), {})

    def test_mismatched_generation_or_session_cannot_confirm(self) -> None:
        self.fire("preCompact-root")
        self.fire("stop-compaction-completed", generation_id="different")
        payload = harness.payload(
            "cursor", "stop-compaction-completed", self.home, self.project, conversation_id="other", session_id="other"
        )
        hooks.run("stop", payload, self.home, {})
        self.assertEqual(self.fire("postToolUse-recovery-read"), {})
        self.fire("stop-compaction-completed")
        self.assertIn("Recovery context", self.fire("postToolUse-recovery-read")["additional_context"])

    def test_failed_cancelled_missing_status_and_loop_do_not_confirm(self) -> None:
        for status, loop in (("cancelled", 0), ("error", 0), (None, 0), ("completed", 1)):
            with self.subTest(status=status, loop=loop):
                self.fire("preCompact-root", generation_id=str((status, loop)))
                self.fire(
                    "stop-compaction-completed", generation_id=str((status, loop)), status=status, loop_count=loop
                )
                self.fire("stop-compaction-completed", generation_id=str((status, loop)))
                self.assertEqual(self.fire("postToolUse-recovery-read"), {})

    def test_prompt_and_spawn_cannot_consume_ready_context(self) -> None:
        self.complete()
        self.assertEqual(self.hook("beforeSubmitPrompt"), {"continue": True})
        self.assertEqual(self.hook("preToolUse", tool_name="Task", tool_input={}), {})
        self.assertIn("Recovery context", self.fire("postToolUse-recovery-read")["additional_context"])

    def test_response_failure_and_deadline_preserve_retry(self) -> None:
        for failure in (ValueError("undelivered"), deadline.DeadlineExceeded()):
            with self.subTest(failure=type(failure).__name__):
                self.complete()
                with patch.object(hooks, "respond", side_effect=failure):
                    result = deadline.run_within(lambda: self.fire("postToolUse-recovery-read"), {})
                self.assertEqual(result, {})
                self.assertIn("Recovery context", self.fire("postToolUse-recovery-read")["additional_context"])
                # A fresh observed generation is needed after successful delivery.
                self.fire("preCompact-root", generation_id="retry-generation")
                self.fire("stop-compaction-completed", generation_id="retry-generation")

    def test_real_deadline_before_delivery_rolls_back_for_retry(self) -> None:
        self.complete()
        original = hooks.respond

        def stalled(decision: object, event: str) -> dict:
            """Delay native response construction until the deadline interrupts it.

            Args:
                decision: Policy decision awaiting native rendering.
                event: Native event's semantic identity.

            Returns:
                The native response if the deadline does not interrupt.
            """
            self.assertFalse(deadline.active().committed)
            time.sleep(2)
            return original(decision, event)

        with patch.object(hooks, "respond", side_effect=stalled):
            result = deadline.run_within(lambda: self.fire("postToolUse-recovery-read"), {}, seconds=0.6)
        self.assertEqual(result, {})
        self.assertIn("Recovery context", self.fire("postToolUse-recovery-read")["additional_context"])

    def test_installed_commands_replay_actual_compaction_recipe(self) -> None:
        driver = Driver(self.home, self.project, "installed-recovery")
        result = driver.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preCompact", driver.registered_commands())
        path = driver.handoff()
        driver.tokens(120000)
        driver.fire("prompt")
        driver.deliver_prompt()
        recovered = driver.fire("compact")
        self.assertIn(path.stem, recovered.text)
        self.assertIn("Orchestrator mode", recovered.text)
        self.assertIn("Context check", driver.fire("tool").text)
        self.assertEqual(driver.fire("tool").text, "")

    def test_empty_rendered_response_does_not_consume_recovery(self) -> None:
        self.complete()
        with patch.object(hooks, "respond", return_value={}):
            self.assertEqual(self.fire("postToolUse-recovery-read"), {})
        self.assertIn("Recovery context", self.fire("postToolUse-recovery-read")["additional_context"])

    def test_contended_diagnostic_log_cannot_prevent_native_recovery_response(self) -> None:
        self.complete()
        self.tokens("c1", 120000)
        payload = harness.payload(
            "cursor", "postToolUse-recovery-read", self.home, self.project, conversation_id="c1", session_id="c1"
        )
        with state.file_lock(self.state_dir / f"{events.EVENTS_FILE}.lock"):
            started = time.monotonic()
            run = harness.run_hook(harness.source_hook("cursor", "postToolUse"), payload, self.home, self.project)
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        self.assertIn("Recovery context", run.response()["additional_context"])
        self.assertNotIn("Recovery context", self.fire("postToolUse-recovery-read").get("additional_context", ""))


if __name__ == "__main__":
    unittest.main()
