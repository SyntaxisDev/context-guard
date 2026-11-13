"""Recorder selection of a genuine terminal compaction stop."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.record_payloads import Scrubber, project_fixture, record_cursor_capture


class CursorCaptureTests(unittest.TestCase):
    """Only the first matching successful terminal stop confirms compaction."""

    def test_first_matching_cancelled_stop_rejects_later_success(self) -> None:
        for status, loop in (("aborted", 0), ("completed", 1), ("completed", 2)):
            with self.subTest(status=status, loop=loop), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                pre = {"hook_event_name": "preCompact", "conversation_id": "c", "session_id": "s", "generation_id": "g"}
                stop = {**pre, "hook_event_name": "stop", "status": status, "loop_count": loop}
                success = {**stop, "status": "completed", "loop_count": 0}
                read = {**pre, "hook_event_name": "postToolUse", "tool_name": "Read"}
                rows = [
                    {"event": event, "payload": payload}
                    for event, payload in (
                        ("preCompact", pre),
                        ("stop", {**success, "generation_id": "other"}),
                        ("stop", stop),
                        ("stop", success),
                        ("postToolUse", read),
                    )
                ]
                path = root / "capture.jsonl"
                path.write_text("\n".join(map(json.dumps, rows)) + "\n")
                with self.assertRaises(ValueError):
                    record_cursor_capture(path, Scrubber(root))

    def test_missing_loop_count_accepts_first_matching_completed_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pre = {"hook_event_name": "preCompact", "conversation_id": "c", "session_id": "s", "generation_id": "g"}
            rows = [
                {"event": "preCompact", "payload": pre},
                {"event": "stop", "payload": {**pre, "hook_event_name": "stop", "status": "completed"}},
                {"event": "postToolUse", "payload": {**pre, "hook_event_name": "postToolUse", "tool_name": "Read"}},
            ]
            path = root / "capture.jsonl"
            path.write_text("\n".join(map(json.dumps, rows)) + "\n")
            self.assertIn("stop-compaction-completed", record_cursor_capture(path, Scrubber(root)))

    def test_projection_keeps_native_marker_and_excludes_investigation_files(self) -> None:
        fixture = {
            "experiment": {"token": "NATIVE_CLAUDE_WAKE_Q8J2", "prompt_template": "<scrubbed>"},
            "provenance": {
                "version": "1",
                "artifacts": [
                    {"name": name, "bytes": 1, "sha256": "a" * 64}
                    for name in ("wire", "request", "probe", "verifier", "original_smoke_verdict")
                ],
            },
            "wire": [{"type": "assistant", "result": "NATIVE_CLAUDE_WAKE_Q8J2"}],
        }
        name, projected = project_fixture("claude", "lifecycle/idle-parent-wake", fixture)
        self.assertEqual(name, "lifecycle/idle-parent-wake")
        self.assertEqual(projected["marker"], "NATIVE_CLAUDE_WAKE_Q8J2")
        self.assertNotIn("experiment", projected)
        self.assertEqual([item["name"] for item in projected["provenance"]["artifacts"]], ["wire", "request"])
        self.assertEqual(projected["wire"], fixture["wire"])

    def test_projection_renames_plugin_fixture_and_native_role(self) -> None:
        name, fixture = project_fixture(
            "copilot",
            "subagentStart-spike-plugin-plugin-child",
            {"payload": {"agentType": "spike-plugin:plugin-child"}},
        )
        self.assertEqual(name, "subagentStart-plugin-child")
        self.assertEqual(fixture["payload"]["agentType"], "plugin-child")
