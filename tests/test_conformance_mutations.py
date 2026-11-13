"""Meaningful delivery regressions fail shared scenarios through real hook processes."""

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.codex.tests.driver import Driver
from adapters.registry import discover
from core.kit import source_files
from tests import harness
from tests.conformance import test_cases


class ConformanceMutationTests(unittest.TestCase):
    """Temporarily break native delivery while retaining the delivery event."""

    def test_missing_native_spawn_role_fails_event_contract(self) -> None:
        case = type("Mutation", (test_cases.ConformanceCase,), {"adapter": discover()["codex"]})
        original_payload = Driver.payload

        def without_role(driver: Driver, action: str, **overrides: object) -> dict:
            """Omit the native role from spawn tool arguments.

            Args:
                driver: Real native scenario driver.
                action: Semantic action.
                **overrides: Requested payload values.

            Returns:
                The mutated native payload.
            """
            if action == "spawn":
                overrides["role"] = None
            return original_payload(driver, action, **overrides)

        baseline = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([case("test_event_spawn")]))
        self.assertTrue(baseline.wasSuccessful(), baseline.failures + baseline.errors)
        with mock.patch.object(Driver, "payload", without_role):
            broken = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([case("test_event_spawn")]))
        self.assertEqual(len(broken.failures), 1, broken.errors)
        self.assertIn("reviewer", broken.failures[0][1])

    def test_empty_native_note_is_detected_then_restored(self) -> None:
        for runtime in ("codex", "copilot"):
            with self.subTest(runtime=runtime):
                self.check_delivery_mutation(runtime)

    def check_delivery_mutation(self, runtime: str) -> None:
        """Remove delivery in a temporary kit and require unchanged scenarios to catch it.

        Args:
            runtime: Adapter whose real process response is mutated.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in source_files(harness.ROOT):
                destination = root / source.relative_to(harness.ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            hook = root / "adapters" / runtime / "hooks.py"
            original = hook.read_text()
            fragment = '"additionalContext": message'
            self.assertEqual(original.count(fragment), 1)
            command = [str(harness.source_hook(runtime, "unused")[0]), str(hook)]
            with mock.patch.object(harness, "source_hook", side_effect=lambda _runtime, event: [*command, event]):
                baseline = self.run_warning(runtime)
                self.assertTrue(baseline.wasSuccessful(), baseline.failures + baseline.errors)
                try:
                    hook.write_text(original.replace(fragment, '"additionalContext": ""'))
                    broken = self.run_warning(runtime)
                    self.assertEqual(len(broken.failures), 1, broken.errors)
                    self.assertIn("Context check", broken.failures[0][1])
                finally:
                    hook.write_text(original)
                restored = self.run_warning(runtime)
                self.assertTrue(restored.wasSuccessful(), restored.failures + restored.errors)

    def run_warning(self, runtime: str) -> unittest.TestResult:
        """Run the unchanged once-only delivery scenario against the selected hook.

        Args:
            runtime: Adapter exercised by the shared scenario.

        Returns:
            The independent shared scenario result.
        """
        case = type("Mutation", (test_cases.ConformanceCase,), {"adapter": discover()[runtime]})
        return unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([case("test_prompt_warning_once")]))
