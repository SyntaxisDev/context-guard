"""Temporary native driver setup and shared scenario support."""

import importlib
import tempfile
import unittest
from pathlib import Path

from core.features import Adapter

NOTE_TOKENS = 160_000
BLOCK_TOKENS = 300_000


class ScenarioCase(unittest.TestCase):
    """Isolate each scenario in a temporary runtime home and project."""

    adapter: Adapter

    def setUp(self) -> None:
        """Create an isolated home, workspace and native driver for one scenario."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.project = self.home / "project"
        (self.project / ".git").mkdir(parents=True)
        module = importlib.import_module(f"adapters.{self.adapter.name}.tests.driver")
        self.driver = module.Driver(self.home, self.project, "conformance-session")

    def names(self) -> list[str]:
        """Read ordered event names from the native hook log.

        Returns:
            Observed event names, including repeats.
        """
        return [event["event"] for event in self.driver.events()]

    def warning(self) -> object:
        """Require a delivered advisory and its event at the note threshold.

        Returns:
            The decoded model-visible advisory outcome.
        """
        self.driver.tokens(min(NOTE_TOKENS, int(self.driver.window * 0.6)))
        self.driver.fire("prompt", prompt="Implement the requested feature")
        outcome = self.driver.deliver_prompt()
        self.assertIn("Context check", outcome.text)
        self.assertFalse(outcome.blocked)
        self.assertIn("note_delivered", self.names())
        return outcome
