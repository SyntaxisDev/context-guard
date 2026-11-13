"""Shared boundaries contracts, composed only by the scenario suite."""

import json

from tests import harness


class BoundaryContracts:
    """Runtime-neutral boundaries assertions using the isolated scenario driver."""

    def test_boundary_malformed(self) -> None:
        outcome = self.driver.malformed()
        self.assertFalse(outcome.blocked)
        self.assertFalse(outcome.text)

    def test_boundary_missing_telemetry(self) -> None:
        outcome = self.driver.fire("prompt")
        self.assertFalse(outcome.blocked)
        self.assertFalse(outcome.text)

    def test_boundary_corrupt_telemetry(self) -> None:
        self.driver.corrupt_telemetry()
        outcome = self.driver.fire("prompt")
        self.assertFalse(outcome.blocked)
        self.assertFalse(outcome.text)

    def test_boundary_exception(self) -> None:
        self.assertTrue(self.driver.exception_passes())

    def test_boundary_deadline(self) -> None:
        self.assertTrue(self.driver.deadline_passes())

    def test_event_prompt(self) -> None:
        self.driver.tokens(1_000)
        self.driver.fire("prompt")
        event = next(event for event in self.driver.events() if event["event"] == "prompt")
        self.assertEqual(event["runtime"], self.adapter.name)
        self.assertEqual(event["session"], "conformance-session")
        self.assertEqual(event["tokens"], 1_000)

    def test_event_spawn(self) -> None:
        role = "reviewer"
        roles = json.loads((harness.ROOT / "content/roles/roles.json").read_text())["roles"]
        tiers = json.loads((harness.ROOT / "adapters" / self.adapter.name / "config.json").read_text())["tiers"]
        native = self.driver.payload("spawn", role=role)
        request = native.get("tool_input", native.get("toolArgs", {}))
        requested_model = request.get("model") if isinstance(request, dict) else None
        self.driver.fire("spawn", role="reviewer")
        event = next(event for event in self.driver.events() if event["event"] == "subagent_spawn")
        self.assertEqual(event["runtime"], self.adapter.name)
        self.assertEqual(event["session"], "conformance-session")
        detail = event["detail"]
        self.assertEqual(detail["role"], role)
        tier = detail["tier"]
        self.assertIsNotNone(tier)
        self.assertIn(tier, tiers)
        if not requested_model:
            self.assertEqual(tier, roles[role]["default_tier"])
        profile = tiers[tier]
        self.assertEqual(detail["model"], profile.get("model", profile.get("call")))
        if "effort" in profile:
            self.assertIsNotNone(detail["effort"])
            self.assertIn(
                detail["effort"],
                (profile["effort"], tiers[roles[role]["default_tier"]]["effort"]),
            )
