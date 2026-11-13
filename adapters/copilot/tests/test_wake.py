"""Measured native idle-parent wake must survive independent negative controls."""

import copy
import unittest

from adapters.copilot.tests.native_lifecycle import model_chunks, observed_wake
from tests import harness


class NativeWakeTests(unittest.TestCase):
    """Only actual idle, completion, restart and model-visible child output prove wake."""

    def fixture(self) -> dict:
        """Read only the committed scrubbed native lifecycle recording.

        Returns:
            Independent captured native boundary observations.
        """
        return harness.read_fixture("copilot", "lifecycle/idle-parent-wake")

    def assert_not_delivered(self, fixture: dict) -> None:
        """Require mutated evidence to fail the model-visible delivery contract.

        Args:
            fixture: Native recording with one evidence component removed or changed.
        """
        self.assertFalse(observed_wake(fixture)["result_delivered"])

    def test_measured_native_wake_matches_actual_model_chunks(self) -> None:
        fixture = self.fixture()
        evidence = observed_wake(fixture)
        self.assertTrue(evidence["parent_started"])
        self.assertTrue(evidence["result_delivered"])
        self.assertEqual(evidence["idle_at"], "2026-11-08T11:28:14.510Z")
        self.assertEqual(evidence["restarted_at"], "2026-11-08T11:28:18.313Z")
        self.assertIn("offline replay", evidence["observation"])
        messages = model_chunks(fixture["wire"])
        self.assertIn("CHILD_RETURNED_ACP", messages[(evidence["model_message"], None)]["text"])
        self.assertEqual(
            {item["name"] for item in fixture["provenance"]["artifacts"]}, {"journal", "lifecycle", "wire"}
        )
        for artifact in fixture["provenance"]["artifacts"]:
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(artifact["bytes"], 0)

    def test_missing_idle_restart_or_child_completion_cannot_prove_wake(self) -> None:
        for event in ("assistant.idle", "assistant.turn_start", "subagent.completed"):
            with self.subTest(event=event):
                fixture = self.fixture()
                fixture["lifecycle"] = [row for row in fixture["lifecycle"] if row["type"] != event]
                fixture["journal"] = [row for row in fixture["journal"] if row["type"] != event]
                self.assert_not_delivered(fixture)

    def test_missing_child_model_output_or_parent_model_output_fails(self) -> None:
        for child in (True, False):
            with self.subTest(child=child):
                fixture = self.fixture()
                fixture["journal"] = [
                    row
                    for row in fixture["journal"]
                    if not (row["type"] == "assistant.message" and bool(row.get("agentId")) == child)
                ]
                self.assert_not_delivered(fixture)

    def test_wake_event_without_actual_parent_output_chunks_fails(self) -> None:
        fixture = self.fixture()
        fixture["wire"] = [
            row
            for row in fixture["wire"]
            if not (
                row["message"]["params"].get("update", {}).get("sessionUpdate") == "agent_message_chunk"
                and not row["message"]["params"]["update"].get("_meta")
            )
        ]
        self.assert_not_delivered(fixture)

    def test_child_output_cannot_be_mistaken_for_root_model_output(self) -> None:
        fixture = self.fixture()
        for row in fixture["wire"]:
            update = row["message"]["params"].get("update", {})
            if update.get("_meta"):
                del update["_meta"]
        self.assert_not_delivered(fixture)

    def test_injected_human_submission_between_idle_and_result_fails(self) -> None:
        for source in ("wire", "journal"):
            with self.subTest(source=source):
                fixture = self.fixture()
                if source == "wire":
                    prompt = copy.deepcopy(
                        next(row for row in fixture["wire"] if row["message"]["method"] == "session/prompt")
                    )
                    prompt["sequence"] = 155
                    fixture["wire"].append(prompt)
                else:
                    fixture["journal"].append(
                        {
                            "type": "user.message",
                            "timestamp": "2026-11-08T11:28:17.000Z",
                            "data": {"content": "recorded prompt (scrubbed)"},
                        }
                    )
                self.assert_not_delivered(fixture)

    def test_old_prompt_marker_without_child_context_does_not_prove_delivery(self) -> None:
        fixture = self.fixture()
        for row in fixture["journal"]:
            if row["type"] == "assistant.message":
                row["data"]["content"] = row["data"].get("content", "").replace("S_CHILD_4P6C", "")
        self.assert_not_delivered(fixture)
