"""Shared subagents contracts, composed only by the scenario suite."""

from tests.conformance.base import BLOCK_TOKENS


class SubagentContracts:
    """Runtime-neutral subagents assertions using the isolated scenario driver."""

    def budget(self, role: str | None) -> None:
        self.driver.tokens(BLOCK_TOKENS, leaf=True)
        outcome = self.driver.fire("leaf", role=role)
        self.assertIn("budget", outcome.text.lower())
        self.assertFalse(outcome.blocked)
        self.assertIn("subagent_budget", self.names())
        self.assertNotIn("note", self.names())
        self.assertFalse(self.driver.fire("leaf", role=role).text)
        self.assertEqual(self.names().count("subagent_budget"), 1)

    def test_leaf_budget_once(self) -> None:
        self.budget(None)

    def test_role_budget_threshold(self) -> None:
        self.driver.tokens(100_000, leaf=True)
        self.assertFalse(self.driver.fire("leaf", role="reviewer").text)
        self.assertNotIn("subagent_budget", self.names())
        self.driver.tokens(125_000, leaf=True)
        self.assertIn("budget", self.driver.fire("leaf", role="reviewer").text.lower())
        budgets = [event["detail"] for event in self.driver.events() if event["event"] == "subagent_budget"]
        self.assertEqual(budgets, [{"role": "reviewer", "budget": 120_000}])

    def test_role_budget_once(self) -> None:
        self.budget("reviewer")
        budget = next(event for event in self.driver.events() if event["event"] == "subagent_budget")
        self.assertEqual(budget["detail"], {"role": "reviewer", "budget": 120_000})

    def test_silence_delivery_once(self) -> None:
        outcome = self.driver.silence()
        self.assertIn("silent", outcome.text.lower())
        self.assertIn("subagent_silent", self.names())
        later = self.driver.fire("start").text
        for notice in outcome.text.splitlines():
            if "silent" in notice.lower():
                self.assertNotIn(notice, later, "A delivered silence notice must not be repeated")
        self.assertEqual(self.names().count("subagent_silent"), 1)

    def test_background_wake(self) -> None:
        evidence = self.driver.wake()
        self.assertTrue(evidence.get("parent_started"), "No observed parent-start evidence")
        self.assertTrue(evidence.get("result_delivered"), "No observed result-delivery evidence")
