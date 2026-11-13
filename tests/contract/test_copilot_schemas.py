"""Recorded Copilot SDK contracts independently constrain native response validation."""

import json
import unittest

from adapters.copilot.tests import responses
from tests import harness


class CopilotResponseCoverageTests(unittest.TestCase):
    """SDK fields and every captured native output retain independent coverage."""

    def test_sdk_response_families_and_fields_are_all_validated(self) -> None:
        contract = harness.read_fixture("copilot", "output-contract")
        self.assertIn("sha256", contract["source"])
        self.assertEqual(set(responses.FIELDS) - set(contract["outputs"]), {"notification"})
        self.assertLessEqual(set(contract["outputs"]), set(responses.FIELDS))
        for event, native in contract["outputs"].items():
            with self.subTest(event=event):
                self.assertEqual(set(native["fields"]), set(responses.FIELDS[event]))
        notification = harness.read_fixture("copilot", "output-notification-agent-idle")
        self.assertEqual(set(notification["output"]), set(responses.FIELDS["notification"]))

    def test_every_recorded_output_satisfies_independent_native_validation(self) -> None:
        outputs = []
        for path in sorted((harness.FIXTURES / "copilot").glob("output-*.json")):
            recorded = json.loads(path.read_text())
            if "output" not in recorded:
                continue
            outputs.append(recorded["event"])
            with self.subTest(fixture=path.name):
                self.assertTrue(recorded["source"])
                self.assertEqual(responses.problems(recorded["event"], recorded["output"]), [])
        self.assertGreater(len(outputs), 5)
