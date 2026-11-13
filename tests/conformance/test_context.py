"""Shared context contracts, composed only by the scenario suite."""

from tests.conformance.base import BLOCK_TOKENS, NOTE_TOKENS
from tests.handback_cases import REPORTS, USER_PROMPTS


class ContextContracts:
    """Runtime-neutral context assertions using the isolated scenario driver."""

    def test_session_ranked_handoffs(self) -> None:
        handoff = self.driver.handoff()
        other = handoff.with_name("other-conformance-1111111111111111.md")
        other.write_text(f"# Other conformance\nStatus: open\nWorkspace: {self.home / 'other'}\n")
        finished = handoff.with_name("finished-conformance-2222222222222222.md")
        finished.write_text(f"# Finished conformance\nStatus: done\nWorkspace: {self.project}\n")
        outcome = self.driver.fire("start")
        self.assertIn(handoff.stem, outcome.text)
        self.assertIn("likely match", outcome.text)
        self.assertLess(outcome.text.index(handoff.name), outcome.text.index(other.name))
        self.assertLess(len(outcome.text), 24_000)
        self.assertNotIn("Finished conformance", outcome.text)

    def test_session_policy(self) -> None:
        self.assertIn("delegat", self.driver.policy_text().lower())

    def test_session_resume(self) -> None:
        handoff = self.driver.handoff()
        self.assertIn(handoff.stem, self.driver.fire("resume").text)

    def test_prompt_warning_once(self) -> None:
        self.warning()
        self.driver.fire("prompt", prompt="Continue implementation")
        self.assertNotIn("Context check", self.driver.deliver_prompt().text)
        self.assertEqual(self.names().count("note_delivered"), 1)

    def test_prompt_warning_tier_escalation(self) -> None:
        self.warning()
        self.driver.tokens(min(200_000, int(self.driver.window * 0.75)) + 1_000)
        self.driver.fire("prompt", prompt="Continue implementation")
        outcome = self.driver.deliver_prompt()
        self.assertIn("Context check", outcome.text)
        self.assertFalse(outcome.blocked)
        tiers = [event["tier"] for event in self.driver.events() if event["event"] == "note_delivered"]
        self.assertEqual(tiers, ["note", "firm"])

    def test_prompt_warning_below_threshold(self) -> None:
        self.driver.tokens(1_000)
        self.assertFalse(self.driver.fire("prompt").blocked)
        self.assertNotIn("Context check", self.driver.deliver_prompt().text)
        self.assertNotIn("note", self.names())

    def test_prompt_block_resend(self) -> None:
        self.driver.tokens(BLOCK_TOKENS)
        first = self.driver.fire("prompt", prompt="Implement a feature")
        self.assertTrue(first.blocked)
        self.assertTrue(first.user_text or first.text, "Prompt refusal must carry a visible explanation")
        self.assertIn("block", self.names())
        second = self.driver.fire("prompt", prompt="Implement a feature")
        self.assertFalse(second.blocked)
        self.assertIn("resend", self.names())

    def test_tool_warning_once(self) -> None:
        self.driver.tokens(min(NOTE_TOKENS, int(self.driver.window * 0.6)))
        outcome = self.driver.fire("tool")
        self.assertIn("Context check", outcome.text)
        self.assertFalse(outcome.blocked)
        self.assertFalse(self.driver.fire("tool").text)
        self.assertEqual(self.names().count("note_delivered"), 1)

    def test_tool_warning_never_blocks(self) -> None:
        self.driver.tokens(BLOCK_TOKENS)
        outcome = self.driver.fire("tool")
        self.assertTrue(outcome.text)
        self.assertFalse(outcome.blocked)
        self.assertIn("note_delivered", self.names())

    def test_compaction_rearms(self) -> None:
        self.warning()
        self.driver.fire("compact")
        self.warning()
        self.assertEqual(self.names().count("note_delivered"), 2)

    def test_compaction_context(self) -> None:
        path = self.driver.handoff()
        self.assertIn(path.stem, self.driver.fire("compact").text)

    def test_handback_delivery_above_block(self) -> None:
        for index, prompt in enumerate(REPORTS):
            with self.subTest(prompt=prompt):
                home = self.home / f"report-{index}"
                home.mkdir()
                driver = type(self.driver)(home, self.project, f"report-{index}")
                driver.tokens(BLOCK_TOKENS)
                report = driver.fire("prompt", prompt=prompt)
                self.assertFalse(report.blocked)
                if driver.deferred_prompt:
                    report = driver.fire("tool")
                self.assertTrue(report.text)
                self.assertTrue(driver.fire("prompt", prompt="Continue implementing the feature.").blocked)

    def test_quoted_handback_still_blocks(self) -> None:
        for index, prompt in enumerate(USER_PROMPTS):
            with self.subTest(prompt=prompt):
                home = self.home / f"user-{index}"
                home.mkdir()
                driver = type(self.driver)(home, self.project, f"user-{index}")
                driver.tokens(BLOCK_TOKENS)
                self.assertTrue(driver.fire("prompt", prompt=prompt).blocked)
