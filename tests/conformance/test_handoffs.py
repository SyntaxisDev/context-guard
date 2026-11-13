"""Shared handoffs contracts, composed only by the scenario suite."""

from tests.conformance.base import NOTE_TOKENS


class HandoffContracts:
    """Runtime-neutral handoffs assertions using the isolated scenario driver."""

    def test_refresh_once(self) -> None:
        self.driver.tokens(NOTE_TOKENS)
        outcome = self.driver.fire("stop")
        self.assertIn("handoff", outcome.text.lower())
        self.assertIn("handoff_requested", self.names())
        self.assertFalse(self.driver.fire("stop").text)
        self.assertEqual(self.names().count("handoff_requested"), 1)

    def test_refresh_unwritten(self) -> None:
        self.warning()
        self.assertIn("handoff", self.driver.fire("stop").text.lower())
        requests = [event for event in self.driver.events() if event["event"] == "handoff_requested"]
        self.assertEqual(requests[-1]["detail"]["source"], "unwritten")

    def credit(self, action: str) -> None:
        self.warning()
        path = self.driver.handoff()
        self.driver.fire(action, path=str(path))
        self.assertIn("handoff_written", self.names())
        self.assertFalse(self.driver.fire("stop").text)
        self.assertNotIn("handoff_requested", self.names())

    def test_refresh_outside_store_not_credited(self) -> None:
        self.warning()
        self.driver.fire("write", path=str(self.project / "README.md"))
        self.assertNotIn("handoff_written", self.names())
        self.assertIn("handoff", self.driver.fire("stop").text.lower())

    def test_refresh_loop_guard(self) -> None:
        self.driver.tokens(NOTE_TOKENS)
        self.assertFalse(self.driver.fire("stop", stop_hook_active=True, loop_count=1).text)
        self.assertNotIn("handoff_requested", self.names())

    def test_refresh_file_credit(self) -> None:
        self.credit("write")

    def test_refresh_shell_credit(self) -> None:
        self.credit("shell_write")
