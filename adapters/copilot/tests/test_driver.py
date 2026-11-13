"""Independent SDK-response and recorded native-driver contracts."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.copilot.tests import responses
from adapters.copilot.tests.driver import Driver
from tests import harness


class NativeResponseTests(unittest.TestCase):
    """Native prompt refusals and stop continuations have distinct delivery semantics."""

    def test_prompt_refusal_reason_never_counts_as_model_delivery(self) -> None:
        raw = {"decision": "block", "reason": "Context check", "additionalContext": "not delivered"}
        self.assertEqual(responses.problems("userPromptSubmitted", raw), [])
        outcome = responses.decode("userPromptSubmitted", {}, raw)
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.text, "")
        self.assertEqual(outcome.channel, "user_warning")
        self.assertEqual(outcome.user_text, "Context check")
        for raw in ({"decision": "block"}, {"decision": "block", "reason": ""}):
            outcome = responses.decode("userPromptSubmitted", {}, raw)
            self.assertEqual(outcome.user_text, "")
            self.assertEqual(outcome.text, "")

    def test_stop_block_is_model_continuation(self) -> None:
        outcome = responses.decode("agentStop", {}, {"decision": "block", "reason": "continue work"})
        self.assertFalse(outcome.blocked)
        self.assertEqual(outcome.text, "continue work")
        self.assertEqual(outcome.channel, "continuation")

    def test_empty_pass_and_event_specific_fields(self) -> None:
        for event in responses.FIELDS:
            self.assertEqual(responses.problems(event, {}), [])
        for raw in ({"decision": "block"}, {"permissionDecision": "deny"}, {"additionalContext": 7}):
            self.assertTrue(responses.problems("sessionStart", raw))
        self.assertTrue(responses.problems("userPromptSubmitted", {"decision": []}))
        self.assertTrue(responses.problems("subagentStop", {"decision": "allow", "reason": "ignored"}))

    def test_transformed_prompt_requires_original_prefix(self) -> None:
        data = {"transformedPrompt": "original"}
        outcome = responses.decode("userPromptTransformed", data, {"modifiedTransformedPrompt": "original note"})
        self.assertEqual(outcome.text, " note")
        with self.assertRaises(AssertionError):
            responses.decode("userPromptTransformed", data, {"modifiedTransformedPrompt": "replacement"})

    def test_hook_environment_drops_inherited_copilot_home(self) -> None:
        with patch.dict("os.environ", {"COPILOT_HOME": "/unrelated/copilot"}):
            self.assertNotIn("COPILOT_HOME", harness.hook_env(Path("/temporary")))


class RecordedDriverTests(unittest.TestCase):
    """Real offline hook subprocesses consume recorded native usage events."""

    def test_prompt_warning_then_refusal_uses_native_window_and_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            project = home / "project"
            (project / ".git").mkdir(parents=True)
            driver = Driver(home, project, "driver-test")
            self.assertNotIn("hook_event_name", driver.payload("prompt"))
            driver.tokens(int(driver.window * 0.6))
            warning = driver.fire("prompt")
            self.assertIn("Context check", warning.text)
            self.assertFalse(warning.blocked)
            driver.tokens(driver.window)
            refusal = driver.fire("prompt", prompt="A different task")
            self.assertTrue(refusal.blocked)
            self.assertEqual(refusal.text, "")
            self.assertIn("Context check", refusal.raw["reason"])
            self.assertIn("note_delivered", {entry["event"] for entry in driver.events()})
