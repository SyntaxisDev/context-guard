"""Every recorded payload, run through adapters/<rt>/hooks.py as a process, yields a valid native response.

Three states per payload: no telemetry (fail open), telemetry above the block tier with one
open handoff (the decision the payload must produce), and every top-level string replaced by a
number (fail open). Any run must exit 0 with exactly one JSON line on stdout and nothing on
stderr: the hook boundary prints a stderr line only when it swallowed an unexpected exception.
"""
import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adapters.claude import mapping as claude_mapping
from adapters.codex import mapping as codex_mapping
from tests import harness

PROMPT_EVENTS = ("beforeSubmitPrompt", "UserPromptSubmit")
STOP_EVENTS = ("stop", "Stop")
ROOT_EXPECTED = {"sessionStart": "context", "SessionStart": "context", "beforeSubmitPrompt": "block",
                 "UserPromptSubmit": "block", "postToolUse": "note", "PostToolUse": "note", "preToolUse": "pass",
                 "stop": "refresh", "Stop": "refresh", "SubagentStop": "pass", "PreCompact": "pass",
                 "PostCompact": "pass"}
ROOT_TOKENS, LEAF_TOKENS, WINDOW = 195_000, 130_000, 200_000
CODEX_TOKENS, CODEX_WINDOW = 250_000, 258_400
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def registered_events(runtime: str) -> set[str]:
    if runtime == "cursor":
        return set(json.loads((harness.ROOT / "adapters/cursor/config.json").read_text())["hook_events"])
    return set((codex_mapping if runtime == "codex" else claude_mapping).HOOK_EVENTS)


def expected(rec: harness.Recorded, state: str = "armed") -> str:
    """Without telemetry only Codex SessionStart speaks: it is the Codex policy channel."""
    if state != "armed":
        return "context" if (rec.runtime, rec.event, state) == ("codex", "SessionStart", "bare") else "pass"
    if rec.name == "stop-aborted":
        return "pass"
    if rec.subagent:
        return "pass" if rec.event in ("SubagentStop", "preToolUse") else "budget"
    return ROOT_EXPECTED[rec.event]


def classify(event: str, response: dict) -> str:
    if response in ({}, {"continue": True}):
        return "pass"
    if response.get("continue") is False or (response.get("decision") == "block" and event in PROMPT_EVENTS):
        return "block"
    specific = response.get("hookSpecificOutput") or {}
    text = (response.get("additional_context") or specific.get("additionalContext")
            or response.get("followup_message") or response.get("reason") or "")
    if "subagent budget" in text:
        return "budget"
    if event in STOP_EVENTS and text.startswith("[context-guard]"):
        return "refresh"
    return "note" if text.startswith("Context check") else "context"


def arm_cursor(payload: dict, home: Path, subagent: bool) -> None:
    harness.write_cursor_chat(home, payload["conversation_id"], LEAF_TOKENS if subagent else ROOT_TOKENS, WINDOW)


def arm_codex(payload: dict, home: Path, subagent: bool) -> None:
    parent = Path(payload["transcript_path"])
    harness.write_rollout(parent, CODEX_TOKENS, CODEX_WINDOW)
    if subagent:
        child = payload.get("agent_transcript_path") or harness.codex_child_rollout(parent, payload["agent_id"])
        harness.write_rollout(Path(child), LEAF_TOKENS, CODEX_WINDOW)


def arm_claude(payload: dict, home: Path, subagent: bool) -> None:
    parent = Path(payload["transcript_path"])
    harness.append_transcript(parent, harness.assistant_usage(ROOT_TOKENS))
    if subagent:
        child = payload.get("agent_transcript_path") or harness.claude_child_transcript(parent, payload["agent_id"])
        harness.append_transcript(Path(child), harness.assistant_usage(LEAF_TOKENS))


ARM = {"cursor": arm_cursor, "codex": arm_codex, "claude": arm_claude}


def corrupt(payload: dict) -> dict:
    return {key: 12345 if isinstance(value, str) else value for key, value in payload.items()}


def run_case(rec: harness.Recorded, state: str) -> tuple[harness.Recorded, str, harness.HookRun]:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp).resolve()
        project = home / "dev" / "project"
        (project / ".git").mkdir(parents=True)
        data = harness.materialize(rec.payload, home, project)
        if state == "armed":
            ARM[rec.runtime](data, home, rec.subagent)
            harness.seed_handoff(home, project)
        elif state == "corrupt":
            data = corrupt(data)
        return rec, state, harness.run_hook(harness.source_hook(rec.runtime, rec.event), data, home, project)


class RecordedPayloadContractTests(unittest.TestCase):
    runs: tuple = ()

    @classmethod
    def setUpClass(cls):
        cases = [(rec, state) for runtime in harness.RUNTIMES for rec in harness.recorded(runtime)
                 for state in ("bare", "armed", "corrupt")]
        with ThreadPoolExecutor(max_workers=8) as pool:
            cls.runs = tuple(pool.map(lambda case: run_case(*case), cases))

    def assert_native(self, rec: harness.Recorded, run: harness.HookRun) -> dict:
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(run.stderr, "", "the hook boundary swallowed an unexpected exception")
        self.assertEqual(len(run.stdout.splitlines()), 1, run.stdout)
        response = run.response()
        self.assertEqual(harness.response_problems(rec.runtime, rec.event, response), [])
        return response

    def test_every_registered_event_has_a_recorded_payload(self):
        for runtime in harness.RUNTIMES:
            with self.subTest(runtime=runtime):
                self.assertLessEqual(registered_events(runtime), {rec.event for rec in harness.recorded(runtime)})

    def test_recorded_payloads_produce_native_responses(self):
        self.assertGreater(len(self.runs), 90)
        for rec, state, run in self.runs:
            with self.subTest(runtime=rec.runtime, fixture=rec.name, state=state):
                response = self.assert_native(rec, run)
                if state != "armed":
                    self.assertEqual(classify(rec.event, response), expected(rec, state))

    def test_recorded_payloads_reach_their_decision_above_the_block_tier(self):
        for rec, state, run in self.runs:
            if state == "armed":
                with self.subTest(runtime=rec.runtime, fixture=rec.name):
                    self.assertEqual(classify(rec.event, self.assert_native(rec, run)), expected(rec), run.stdout)

    def test_unparseable_stdin_passes_for_every_registered_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for runtime in harness.RUNTIMES:
                for event in sorted(registered_events(runtime)):
                    with self.subTest(runtime=runtime, event=event):
                        run = harness.run_hook(harness.source_hook(runtime, event), "{not json", home)
                        rec = harness.Recorded(runtime, "garbage", event, {}, "")
                        self.assertEqual(classify(event, self.assert_native(rec, run)), "pass")


class FixtureHygieneTests(unittest.TestCase):
    def test_codex_payloads_match_the_binary_input_schemas(self):
        for rec in harness.recorded("codex"):
            with self.subTest(fixture=rec.name):
                schema = harness.codex_schema(rec.event, "input")
                self.assertEqual(harness.schema_problems(rec.payload, schema, schema), [])

    def test_fixtures_hold_no_prompts_paths_or_identities(self):
        for path in sorted(harness.FIXTURES.rglob("*.json")):
            text = path.read_text()
            with self.subTest(fixture=str(path.relative_to(harness.FIXTURES))):
                self.assertNotRegex(text, r"/Users/|/home/\w")
                self.assertEqual(set(EMAIL.findall(text)) - {"user@example.com"}, set())
        prompts = {rec.payload.get("prompt") for rt in harness.RUNTIMES for rec in harness.recorded(rt)}
        self.assertLessEqual(prompts, {None, "recorded prompt (scrubbed)"})


if __name__ == "__main__":
    unittest.main()
