import copy
import tempfile
import unittest
from pathlib import Path

from adapters.copilot import events, hooks
from tests import harness


class EventTests(unittest.TestCase):
    """Native event selection separates root, child and compaction records."""

    def test_root_and_child_usage_are_selected_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            root = copy.deepcopy(harness.read_fixture("copilot", "events-usage-record")["record"])
            root["data"]["usage"].update(inputTokens=100, interactionType="conversation-agent")
            harness.append_transcript(path, root)
            child = copy.deepcopy(root)
            child["agentId"] = "child"
            child["data"]["usage"].update(inputTokens=200, interactionType="conversation-subagent")
            harness.append_transcript(path, child)
            self.assertEqual(events.latest_usage(path, 10000).tokens, 100)
            self.assertEqual(events.latest_usage(path, 10000, "child").tokens, 200)
            self.assertIsNone(events.latest_usage(path, 10000, "another-child"))

    def test_successful_applied_compaction_invalidates_stale_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            usage = copy.deepcopy(harness.read_fixture("copilot", "events-usage-record")["record"])
            usage["data"]["usage"]["interactionType"] = "conversation-agent"
            harness.append_transcript(path, usage)
            complete = copy.deepcopy(harness.read_fixture("copilot", "events-compaction-complete")["record"])
            complete["id"] = "compaction-1"
            complete["data"]["success"] = False  # Never-produced failure variant.
            harness.append_transcript(path, complete)
            self.assertIsNotNone(events.latest_usage(path, 10000))
            self.assertIsNone(events.latest_compaction(path, 10000))
            complete["data"]["success"] = True
            harness.append_transcript(path, complete)
            self.assertIsNone(events.latest_usage(path, 10000))
            self.assertEqual(events.latest_compaction(path, 10000), "compaction-1")

    def test_corrupt_latest_usage_is_isolated_to_its_exact_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            root = copy.deepcopy(harness.read_fixture("copilot", "events-usage-record")["record"])
            root["data"]["usage"]["inputTokens"] = 270000
            child = copy.deepcopy(root)
            child["agentId"] = "child"
            child["data"]["usage"].update(inputTokens=60000, interactionType="conversation-subagent")
            for record in (root, child):
                harness.append_transcript(path, record)
            corrupt_child = copy.deepcopy(child)
            corrupt_child["data"]["usage"]["inputTokens"] = "changed"
            harness.append_transcript(path, corrupt_child)
            self.assertIsNone(events.latest_usage(path, 10000, "child"))
            self.assertEqual(events.latest_usage(path, 10000).tokens, 270000)
            harness.append_transcript(path, child)
            root["data"]["usage"]["inputTokens"] = "changed"
            harness.append_transcript(path, root)
            self.assertIsNone(events.latest_usage(path, 10000))
            self.assertEqual(events.latest_usage(path, 10000, "child").tokens, 60000)
            foreign = copy.deepcopy(child)
            foreign["agentId"] = "another-child"
            foreign["data"]["usage"]["inputTokens"] = 270000
            harness.append_transcript(path, foreign)
            self.assertEqual(events.latest_usage(path, 10000, "child").tokens, 60000)
            self.assertIsNone(events.latest_usage(path, 10000))

    def test_missing_corrupt_and_foreign_interaction_usage_fails_open(self) -> None:
        self.assertIsNone(events.latest_usage(None, 1000))
        self.assertIsNone(events.usage_from({"usage": {"inputTokens": True}}))
        self.assertIsNone(events.usage_from({"usage": {"inputTokens": 100}}))
        self.assertIsNone(events.usage_from({"usage": {"inputTokens": 100, "interactionType": "summarizer"}}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(b"not json\n[]\n")
            self.assertIsNone(events.latest_usage(path, 1000))
            self.assertIsNone(events.latest_compaction(path, 1000))


class LatestUsagePromptTests(harness.RuntimeHookCase):
    """Changed latest telemetry cannot make a prompt block on stale high usage."""

    runtime = "copilot"

    def test_malformed_latest_record_passes_prompt_after_older_high_usage(self) -> None:
        config = hooks.load_config(self.home, {})
        config["kit_check"]["enabled"] = False
        path = self.home / ".copilot/session-state/root/events.jsonl"
        payload = harness.payload(
            "copilot",
            "userPromptSubmitted-root",
            self.home,
            self.project,
            sessionId="root",
            transcriptPath=str(path),
        )
        high = copy.deepcopy(harness.read_fixture("copilot", "events-usage-record")["record"])
        high["data"]["usage"]["inputTokens"] = 270000
        malformed = [None, {}, {"usage": []}]
        for field, value in (
            ("inputTokens", None),
            ("inputTokens", True),
            ("inputTokens", "270000"),
            ("interactionType", None),
            ("interactionType", []),
            ("interactionType", "summarizer"),
        ):
            data = copy.deepcopy(high["data"])
            data["usage"][field] = value
            malformed.append(data)
        missing_type = copy.deepcopy(high["data"])
        del missing_type["usage"]["interactionType"]
        malformed.append(missing_type)
        for data in malformed:
            with self.subTest(data=data):
                path.unlink(missing_ok=True)
                harness.append_transcript(path, high)
                changed = copy.deepcopy(high)
                changed["data"] = data
                harness.append_transcript(path, changed)
                response = hooks.handle("userPromptSubmitted", payload, config, self.home, {})
                self.assertEqual(response, {})
