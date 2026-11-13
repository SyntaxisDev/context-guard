"""Measured Claude wake requires idle, native notification and actual root delivery."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.claude.tests.native_lifecycle import observed_wake
from scripts.claude_lifecycle import framed_text, native_event
from scripts.record_payloads import main
from tests import harness


class NativeWakeTests(unittest.TestCase):
    """Reject independent removals, identity substitutions and chronology mutations."""

    def fixture(self) -> dict:
        """Read the measured scrubbed native lifecycle recording.

        Returns:
            Captured native stream and original source provenance.
        """
        return harness.read_fixture("claude", "lifecycle/idle-parent-wake")

    def assert_rejected(self, fixture: dict) -> None:
        """Require invalid evidence to establish neither wake nor delivery.

        Args:
            fixture: Mutated native capture.
        """
        evidence = observed_wake(fixture)
        self.assertFalse(evidence["parent_started"])
        self.assertFalse(evidence["result_delivered"])

    def test_measured_wake_retains_idle_despite_first_reply_quoting_token(self) -> None:
        fixture = self.fixture()
        evidence = observed_wake(fixture)
        self.assertTrue(evidence["parent_started"])
        self.assertTrue(evidence["result_delivered"])
        self.assertEqual(list(evidence["sequence"].values()), [3, 7, 9, 11, 14, 15])
        token = fixture["marker"]
        self.assertIn(token, fixture["wire"][7]["result"])
        self.assertNotEqual(token, fixture["wire"][7]["result"])
        self.assertEqual(token, fixture["wire"][15]["result"])
        self.assertIn("offline replay", evidence["observation"])
        self.assertNotIn("parent_started", fixture)
        self.assertEqual(fixture["provenance"]["version"], "2.1.311")

    def test_removing_each_native_boundary_or_actual_output_fails(self) -> None:
        for index in (3, 7, 9, 11, 14, 15):
            with self.subTest(index=index):
                fixture = self.fixture()
                del fixture["wire"][index]
                self.assert_rejected(fixture)

    def test_waiting_for_child_before_first_root_result_fails(self) -> None:
        fixture = self.fixture()
        child = fixture["wire"].pop(9)
        fixture["wire"].insert(7, child)
        self.assert_rejected(fixture)

    def test_intervening_human_submission_fails_through_final_result(self) -> None:
        for index in (8, 12, 15):
            with self.subTest(index=index):
                fixture = self.fixture()
                fixture["wire"].insert(index, {"type": "user", "session_id": fixture["root_session"]})
                self.assert_rejected(fixture)

    def test_task_tool_agent_run_and_session_mismatches_fail(self) -> None:
        for index, keys in (
            (3, ("session_id", "task_id", "tool_use_id", "run_id")),
            (7, ("session_id",)),
            (9, ("session_id", "agent_id", "parent_tool_use_id")),
            (11, ("session_id", "task_id", "tool_use_id", "run_id")),
            (14, ("session_id", "parent_tool_use_id")),
            (15, ("session_id",)),
        ):
            for key in keys:
                with self.subTest(index=index, key=key):
                    fixture = self.fixture()
                    fixture["wire"][index][key] = "different-native-identity"
                    self.assert_rejected(fixture)

    def test_erasing_actual_child_notification_or_root_text_fails(self) -> None:
        for index, field in ((9, "message"), (11, "summary"), (14, "message"), (15, "result")):
            with self.subTest(index=index):
                fixture = self.fixture()
                fixture["wire"][index][field] = {} if field == "message" else ""
                self.assert_rejected(fixture)

    def test_missing_provenance_and_forged_success_flags_fail(self) -> None:
        fixture = self.fixture()
        fixture["provenance"]["artifacts"] = []
        fixture.update(parent_started=True, result_delivered=True)
        self.assert_rejected(fixture)

    def test_redaction_keeps_token_framing_and_scrubs_unrelated_text(self) -> None:
        token = self.fixture()["marker"]
        self.assertEqual(framed_text(f"private prompt {token} private output"), f"<scrubbed>{token}<scrubbed>")
        self.assertEqual(framed_text(token), token)
        event = native_event(
            {
                "type": "user",
                "session_id": "personal-session",
                "message": {"role": "user", "content": "private human submission"},
            },
            lambda _: "fake",
        )
        self.assertEqual(event["message"]["content"], "<scrubbed>")
        self.assertEqual(event["session_id"], "fake")

    def test_root_results_cannot_belong_to_child(self) -> None:
        for index in (7, 15):
            for key in ("parent_tool_use_id", "agent_id"):
                with self.subTest(index=index, key=key):
                    fixture = self.fixture()
                    fixture["wire"][index][key] = "child-identity"
                    self.assert_rejected(fixture)

    def test_notification_must_match_actual_child_output_not_just_known_token(self) -> None:
        fixture = self.fixture()
        fixture["wire"][11]["summary"] = fixture["marker"]
        self.assert_rejected(fixture)

    def test_reply_cannot_belong_to_child_or_reuse_an_earlier_message(self) -> None:
        fixture = self.fixture()
        fixture["wire"][14]["agent_id"] = fixture["wire"][3]["task_id"]
        self.assert_rejected(fixture)
        for index in (6, 9):
            with self.subTest(reused_message=index):
                fixture = self.fixture()
                fixture["wire"][14]["message"]["id"] = fixture["wire"][index]["message"]["id"]
                self.assert_rejected(fixture)

    def test_explicit_recorder_route_is_read_only_and_writes_only_lifecycle(self) -> None:
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            capture = base / "capture"
            capture.mkdir()
            rows = fixture["wire"]
            for row in rows:
                if row.get("subtype") == "init":
                    row["claude_code_version"] = fixture["provenance"]["version"]
            sources = {
                capture / "wire.jsonl": "\n".join(json.dumps(row) for row in rows),
                capture / "request.json": json.dumps({"prompt_template": "redacted replay request"}),
                capture / "verdict.txt": "original FAIL replay evidence",
                base / "claude-existing-auth.py": "# replay source; never execute",
                base / "verify-claude.py": "# replay validator; never execute",
            }
            for path, text in sources.items():
                path.write_text(text)
            argv = [
                "--runtime",
                "claude",
                "--home",
                str(base / "absent-home"),
                "--claude-lifecycle-root",
                str(capture),
                "--out",
                str(base / "out"),
            ]
            with (
                patch("scripts.record_payloads.record_claude", side_effect=AssertionError("home read")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main([*argv, "--dry-run"]), 0)
                self.assertFalse((base / "out").exists())
                self.assertEqual(main(argv), 0)
            outputs = list((base / "out").rglob("*.json"))
            self.assertEqual(len(outputs), 1)
            recorded = json.loads(outputs[0].read_text())
            self.assertTrue(observed_wake(recorded)["result_delivered"])
            wire = next(item for item in recorded["provenance"]["artifacts"] if item["name"] == "wire")
            self.assertEqual(wire["sha256"], hashlib.sha256(sources[capture / "wire.jsonl"].encode()).hexdigest())
            for path, text in sources.items():
                self.assertEqual(path.read_text(), text)
