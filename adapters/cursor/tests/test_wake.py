"""Independent captured Cursor wake rejects missing evidence and false correlations."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.cursor.tests.native_lifecycle import observed_wake
from scripts.cursor_lifecycle import framed_text, native_event
from scripts.record_payloads import main
from tests import harness


class NativeWakeTests(unittest.TestCase):
    """Replay recorder evidence and mutate independently necessary boundaries."""

    def fixture(self) -> dict:
        """Read the source-backed offline capture.

        Returns:
            A fresh fixture for each mutation.
        """
        return harness.read_fixture("cursor", "lifecycle/idle-parent-wake")

    def assert_rejected(self, fixture: dict) -> None:
        """Require both delivery claims to fail closed.

        Args:
            fixture: Mutated captured evidence.
        """
        result = observed_wake(fixture)
        self.assertFalse(result["parent_started"])
        self.assertFalse(result["result_delivered"])

    def test_measured_actual_read_after_idle_proves_wake_but_delay_control_failed(self) -> None:
        fixture = self.fixture()
        result = observed_wake(fixture)
        self.assertTrue(result["result_delivered"])
        self.assertTrue(result["parent_started"])
        self.assertEqual(result["strict_delay_control"], "FAILED")
        self.assertEqual(
            result["sequence"],
            {"idle_ns": 1794569746774592000, "child_read_ns": 1794569749782532000, "receipt_ns": 1794569785939178000},
        )
        self.assertNotIn("parent_started", fixture)
        self.assertIn("offline replay", result["observation"])

    def test_removing_idle_read_or_final_receipt_fails(self) -> None:
        for index in (4, 7, 10):
            with self.subTest(index=index):
                fixture = self.fixture()
                del fixture["hooks"][index]
                self.assert_rejected(fixture)

    def test_removing_actual_child_text_notification_or_root_reply_fails(self) -> None:
        for index in (2, 3, 9, 10):
            with self.subTest(index=index):
                fixture = self.fixture()
                del fixture["native"][index]
                self.assert_rejected(fixture)

    def test_human_keys_fail_in_either_chronology_through_final_receipt(self) -> None:
        for stream in ("terminal", "ordered"):
            for ns in (1794569747000000000, 1794569785900000000):
                with self.subTest(stream=stream, ns=ns):
                    fixture = self.fixture()
                    fixture[stream].append({"ns": ns, "kind": "input"})
                    fixture[stream].sort(key=lambda r: r["ns"])
                    self.assert_rejected(fixture)

    def test_shell_notification_cannot_substitute_for_matching_task(self) -> None:
        fixture = self.fixture()
        fixture["native"][9]["notification"] = fixture["native"][7]["notification"]
        self.assert_rejected(fixture)

    def test_native_identities_and_ancestry_must_match(self) -> None:
        for index, key in (
            (2, "session_id"),
            (3, "session_id"),
            (6, "session_id"),
            (9, "session_id"),
            (10, "session_id"),
            (10, "agent_id"),
            (10, "parent_tool_call_id"),
        ):
            with self.subTest(index=index, key=key):
                fixture = self.fixture()
                fixture["native"][index][key] = "wrong-identity"
                self.assert_rejected(fixture)
        for key in ("kind", "status", "task_id", "detail"):
            with self.subTest(notification=key):
                fixture = self.fixture()
                fixture["native"][9]["notification"][key] = "wrong"
                self.assert_rejected(fixture)

    def test_reused_message_or_generation_identity_fails(self) -> None:
        for earlier in (3, 6, 8):
            with self.subTest(earlier=earlier):
                fixture = self.fixture()
                fixture["native"][10]["message_id"] = fixture["native"][earlier]["message_id"]
                self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["hooks"][-1]["generation_id"] = fixture["hooks"][4]["generation_id"]
        self.assert_rejected(fixture)

    def test_misordered_child_result_notification_or_root_receipt_fails(self) -> None:
        fixture = self.fixture()
        fixture["hooks"][7]["ns"] = fixture["hooks"][4]["ns"] - 1
        self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["native"][9], fixture["native"][10] = fixture["native"][10], fixture["native"][9]
        self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["hooks"][-1]["ns"] = fixture["hooks"][4]["ns"] - 1
        self.assert_rejected(fixture)

    def test_erasing_actual_text_and_misordering_native_child_output_fails(self) -> None:
        for index in (2, 3, 10):
            with self.subTest(index=index):
                fixture = self.fixture()
                fixture["native"][index]["content"] = []
                self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["native"][2], fixture["native"][3] = fixture["native"][3], fixture["native"][2]
        self.assert_rejected(fixture)

    def test_hook_task_session_generation_and_agent_mismatches_fail(self) -> None:
        for index, key in (
            (3, "session_id"),
            (3, "tool_use_id"),
            (4, "generation_id"),
            (7, "session_id"),
            (10, "session_id"),
            (10, "parent_tool_call_id"),
        ):
            with self.subTest(index=index, key=key):
                fixture = self.fixture()
                fixture["hooks"][index][key] = "mismatched"
                self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["hooks"][3]["output"]["agentId"] = "mismatched"
        self.assert_rejected(fixture)

    def test_duplicate_child_calls_and_uncorrelated_timed_hook_fail(self) -> None:
        fixture = self.fixture()
        sleep = fixture["native"][0]["content"][0]["toolCallId"]
        for index, part in ((0, 1), (2, 0)):
            fixture["native"][index]["content"][part]["toolCallId"] = sleep
        self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["hooks"][7]["tool_use_id"] = "uncorrelated"
        for row in fixture["ordered"]:
            if row.get("ns") == fixture["hooks"][7]["ns"]:
                row["tool_use_id"] = "uncorrelated"
        self.assert_rejected(fixture)

    def test_prior_root_knowledge_and_parent_reads_fail(self) -> None:
        fixture = self.fixture()
        fixture["native"][8]["content"][0]["text"] = fixture["native"][10]["content"][0]["text"]
        self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["hooks"][1]["prompt"] = fixture["token"]
        self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["native"][4]["content"][0]["operation"] = "Read"
        self.assert_rejected(fixture)

    def test_root_launch_role_ancestry_and_missing_transcripts_fail(self) -> None:
        for index in (3, 7):
            with self.subTest(hook=index):
                fixture = self.fixture()
                fixture["hooks"][index]["parent_tool_call_id"] = "unrelated-parent"
                self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture["native"][5]["role"] = "assistant"
        self.assert_rejected(fixture)
        fixture = self.fixture()
        fixture.update(transcripts=[], transcript_sessions=[])
        self.assert_rejected(fixture)

    def test_recorder_retains_bad_ancestry_for_verifier_rejection(self) -> None:
        for row_index, field in ((10, "agent_id"), (0, "parent_tool_call_id")):
            with self.subTest(row=row_index, field=field):
                fixture = self.fixture()
                source = self.native_source(fixture["native"][row_index])
                source[field] = "unrelated-ancestor"
                projected = native_event(source, lambda value: value)
                self.assertEqual(projected[field], "unrelated-ancestor")
                fixture["native"][row_index] = projected
                self.assert_rejected(fixture)

    def test_capture_only_route_rejects_other_runtime_or_compaction_source(self) -> None:
        for arguments in (("--runtime", "all"), ("--runtime", "cursor", "--cursor-capture", "unused")):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main([*arguments, "--cursor-lifecycle-root", "unused"])
                self.assertEqual(error.exception.code, 2)

    def test_forged_flags_and_missing_source_hashes_do_not_prove_wake(self) -> None:
        fixture = self.fixture()
        fixture["provenance"]["artifacts"] = []
        fixture.update(parent_started=True, result_delivered=True)
        self.assert_rejected(fixture)

    def test_scrubber_preserves_only_token_framing(self) -> None:
        token = self.fixture()["token"]
        self.assertEqual(framed_text(f"private {token} /private/path"), f"<scrubbed>{token}<scrubbed>")
        event = native_event(
            {
                "session_id": "secret",
                "blob_id": "secret",
                "role": "assistant",
                "content": json.dumps(
                    [{"type": "reasoning", "text": "secret"}, {"type": "text", "text": f"private {token}"}]
                ),
            },
            lambda _: "redacted",
        )
        self.assertNotIn("secret", json.dumps(event))
        self.assertEqual(event["content"], [{"type": "text", "text": f"<scrubbed>{token}"}])

    def test_explicit_capture_route_never_reads_home_and_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch("scripts.record_payloads.record_cursor", side_effect=AssertionError("home access")),
                patch(
                    "scripts.record_payloads.record_cursor_lifecycle",
                    return_value={"lifecycle/idle-parent-wake": self.fixture()},
                ) as record,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                argv = [
                    "--runtime",
                    "cursor",
                    "--home",
                    str(root / "absent"),
                    "--out",
                    str(root / "out"),
                    "--cursor-lifecycle-root",
                    str(root / "capture"),
                ]
                self.assertEqual(main([*argv, "--dry-run"]), 0)
                self.assertFalse((root / "out").exists())
                self.assertEqual(main(argv), 0)
                self.assertEqual(record.call_count, 2)
            recorded = json.loads((root / "out/cursor/lifecycle/idle-parent-wake.json").read_text())
            self.assertTrue(observed_wake(recorded)["result_delivered"])

    def test_real_recorder_hashes_original_capture_bytes_and_preserves_sources(self) -> None:
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture"
            capture.mkdir()
            sources = self.capture_sources(fixture)
            for name, content in sources.items():
                (capture / name).write_text(content)
            argv = [
                "--runtime",
                "cursor",
                "--home",
                str(root / "absent"),
                "--cursor-lifecycle-root",
                str(capture),
                "--out",
                str(root / "out"),
            ]
            with (
                patch("scripts.record_payloads.record_cursor", side_effect=AssertionError("home access")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main([*argv, "--dry-run"]), 0)
                self.assertFalse((root / "out").exists())
                self.assertEqual(main(argv), 0)
            recorded = json.loads((root / "out/cursor/lifecycle/idle-parent-wake.json").read_text())
            self.assertTrue(observed_wake(recorded)["result_delivered"])
            for name, content in sources.items():
                self.assertEqual((capture / name).read_text(), content)
            for item in recorded["provenance"]["artifacts"]:
                if item["name"] in sources:
                    self.assertEqual(item["sha256"], hashlib.sha256(sources[item["name"]].encode()).hexdigest())

    def native_source(self, row: dict) -> dict:
        """Reconstitute one recorded native message without inventing a new payload.

        Args:
            row: Recorded projected native message.

        Returns:
            Capture-shaped source with recorded identities and text.
        """
        parts = row.get("content", [])
        for part in parts:
            if part.get("type") == "tool-call":
                part["args"] = {"command": part.get("command", ""), "toolName": part.get("operation", "")}
                if "run_in_background" in part:
                    part["args"]["arguments"] = {"run_in_background": part["run_in_background"]}
            if "agent_id" in part:
                part["result"] = "Agent ID: " + part["agent_id"]
        content = json.dumps(parts)
        if row["role"] == "user":
            notice = row["notification"]
            content = (
                "<system_notification><task>\n"
                + "\n".join(f"{k}: {v}" for k, v in notice.items())
                + "\n</task></system_notification>"
            )
        return {
            "session_id": row["session_id"],
            "blob_id": row["message_id"],
            "role": row["role"],
            "content": content,
            **{k: row[k] for k in ("parent_tool_call_id", "agent_id") if k in row},
        }

    def capture_sources(self, fixture: dict) -> dict[str, str]:
        """Reconstitute captured shapes from recorded evidence for route verification.

        Args:
            fixture: Recorded evidence, with already redacted text and identities.

        Returns:
            Temporary source contents; these are never shipped as fixtures.
        """
        native = [self.native_source(row) for row in fixture["native"]]
        hooks = []
        for row in fixture["hooks"]:
            payload = {k: v for k, v in row.items() if k not in {"ns", "event", "output", "command"}}
            payload["tool_input"] = {"command": row["command"]}
            if "run_in_background" in row:
                payload["tool_input"]["run_in_background"] = row["run_in_background"]
            if "output" in row:
                output = row["output"]
                if "text" in output:
                    output["output"] = output.pop("text")
                payload["tool_output"] = json.dumps(output)
            hooks.append({"ns": row["ns"], "event": row["event"], "payload": payload})
        sources = {
            "hooks.jsonl": "\n".join(map(json.dumps, hooks)),
            "native-allowlisted-events.json": json.dumps(native),
            "terminal-chronology.jsonl": "\n".join(
                json.dumps({"ns": r["ns"], r["kind"]: "redacted"}) for r in fixture["terminal"]
            ),
            "ordered-events.json": json.dumps(fixture["ordered"]),
            "native-source-hashes.json": json.dumps(
                {r["source_id"]: r["sha256"] for r in fixture["provenance"]["original_database_hashes"]}
            ),
            "source-hashes.json": "{}",
            "transcript-source-hashes.json": "{}",
            "report.md": "strict delay FAILED",
        }
        for session, transcript in zip(fixture["transcript_sessions"], fixture["transcripts"], strict=True):
            sources[f"native-{session}.jsonl"] = "\n".join(
                json.dumps(
                    {
                        "role": r["role"],
                        "type": r["type"],
                        "status": r["status"],
                        "message": {"content": [{"type": "text", "text": r["text"]}]},
                    }
                )
                for r in transcript
            )
        return sources

    def test_recorded_artifacts_distinguish_projected_bytes_from_original_database_hashes(self) -> None:
        fixture = self.fixture()
        provenance = fixture["provenance"]
        self.assertIn("not a snapshot", provenance["database_hash_scope"])
        self.assertEqual(len(provenance["original_database_hashes"]), 4)
        native = next(a for a in provenance["artifacts"] if a["name"] == "native-allowlisted-events.json")
        self.assertNotIn(native["sha256"], [a["sha256"] for a in provenance["original_database_hashes"]])
        self.assertEqual(len(native["sha256"]), hashlib.sha256().digest_size * 2)
