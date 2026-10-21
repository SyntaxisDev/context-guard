"""Every recorded payload, run through adapters/<rt>/hooks.py as a process, yields a valid native response.

Three states per payload: no telemetry (fail open), telemetry above the block tier with one
open handoff (the decision the payload must produce), and every top-level string replaced by a
number (fail open). A payload that never ends passes within the hook deadline. Any run must exit 0
with exactly one JSON line on stdout and nothing on stderr: the hook boundary prints a stderr line
only when it swallowed an unexpected exception.
"""

import re
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adapters.claude import mapping as claude_mapping
from adapters.codex import mapping as codex_mapping
from adapters.cursor import mapping as cursor_mapping
from core import deadline
from tests import harness

PROMPT_EVENTS = ("beforeSubmitPrompt", "UserPromptSubmit")
STOP_EVENTS = ("stop", "Stop")
HUNG_EVENTS = {"cursor": "beforeSubmitPrompt", "codex": "UserPromptSubmit", "claude": "UserPromptSubmit"}
ROOT_EXPECTED = {
    "sessionStart": "context",
    "SessionStart": "context",
    "beforeSubmitPrompt": "block",
    "UserPromptSubmit": "block",
    "postToolUse": "note",
    "PostToolUse": "note",
    "preToolUse": "pass",
    "stop": "refresh",
    "Stop": "refresh",
    "PreCompact": "pass",
    "PostCompact": "pass",
}
ROOT_TOKENS, LEAF_TOKENS, WINDOW = 195_000, 130_000, 200_000
CODEX_TOKENS, CODEX_WINDOW = 250_000, 258_400
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


MAPPINGS = {"cursor": cursor_mapping, "codex": codex_mapping, "claude": claude_mapping}


def registered_events(runtime: str) -> set[str]:
    return set(MAPPINGS[runtime].HOOK_EVENTS)


def expected(rec: harness.Recorded, state: str = "armed") -> str:
    """The decision class a recorded payload must produce in a state.

    Without telemetry only Codex SessionStart (the Codex policy channel) and Cursor sessionStart
    (the hooks-active marker) speak. A recorded event the runtime does not register passes.

    Returns:
        pass, block, note, budget, refresh or context.
    """
    if state != "armed":
        bare_context = {("codex", "SessionStart"), ("cursor", "sessionStart")}
        return "context" if state == "bare" and (rec.runtime, rec.event) in bare_context else "pass"
    if rec.name == "stop-aborted" or rec.event not in registered_events(rec.runtime):
        return "pass"
    if rec.subagent:
        return "pass" if rec.event == "preToolUse" else "budget"
    return ROOT_EXPECTED[rec.event]


def classify(event: str, response: dict) -> str:
    if response in ({}, {"continue": True}):
        return "pass"
    if response.get("continue") is False or (response.get("decision") == "block" and event in PROMPT_EVENTS):
        return "block"
    specific = response.get("hookSpecificOutput") or {}
    text = (
        response.get("additional_context")
        or specific.get("additionalContext")
        or response.get("followup_message")
        or response.get("reason")
        or ""
    )
    if "subagent budget" in text:
        return "budget"
    if event in STOP_EVENTS and text.startswith("[context-guard]"):
        return "refresh"
    return "note" if text.startswith("Context check") else "context"


def arm_cursor(payload: dict, home: Path, *, subagent: bool) -> None:
    harness.write_cursor_chat(home, payload["conversation_id"], LEAF_TOKENS if subagent else ROOT_TOKENS, WINDOW)


def arm_codex(payload: dict, _home: Path, *, subagent: bool) -> None:
    parent = Path(payload["transcript_path"])
    harness.write_rollout(parent, CODEX_TOKENS, CODEX_WINDOW)
    if subagent:
        child = payload.get("agent_transcript_path") or harness.codex_child_rollout(parent, payload["agent_id"])
        harness.write_rollout(Path(child), LEAF_TOKENS, CODEX_WINDOW)


def arm_claude(payload: dict, _home: Path, *, subagent: bool) -> None:
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
            ARM[rec.runtime](data, home, subagent=rec.subagent)
            harness.seed_handoff(home, project)
        elif state == "corrupt":
            data = corrupt(data)
        return rec, state, harness.run_hook(harness.source_hook(rec.runtime, rec.event), data, home, project)


def start_hung(runtime: str, home: Path) -> subprocess.Popen:
    """A hook whose payload starts but never ends: stdin stays open until the test closes it.

    Returns:
        The running hook process.
    """
    process = subprocess.Popen(
        harness.source_hook(runtime, HUNG_EVENTS[runtime]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=home,
        env=harness.hook_env(home),
    )
    process.stdin.write('{"session_id": "')
    process.stdin.flush()
    return process


class RecordedPayloadContractTests(unittest.TestCase):
    """Every recorded payload, bare, armed and corrupted, answers one valid native response."""

    runs: tuple = ()

    @classmethod
    def setUpClass(cls) -> None:
        cases = [
            (rec, state)
            for runtime in harness.RUNTIMES
            for rec in harness.recorded(runtime)
            for state in ("bare", "armed", "corrupt")
        ]
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

    def test_every_registered_event_has_a_recorded_payload(self) -> None:
        for runtime in harness.RUNTIMES:
            with self.subTest(runtime=runtime):
                self.assertLessEqual(registered_events(runtime), {rec.event for rec in harness.recorded(runtime)})

    def test_recorded_payloads_produce_native_responses(self) -> None:
        self.assertGreater(len(self.runs), 90)
        for rec, state, run in self.runs:
            with self.subTest(runtime=rec.runtime, fixture=rec.name, state=state):
                response = self.assert_native(rec, run)
                if state != "armed":
                    self.assertEqual(classify(rec.event, response), expected(rec, state))

    def test_recorded_payloads_reach_their_decision_above_the_block_tier(self) -> None:
        for rec, state, run in self.runs:
            if state == "armed":
                with self.subTest(runtime=rec.runtime, fixture=rec.name):
                    self.assertEqual(classify(rec.event, self.assert_native(rec, run)), expected(rec), run.stdout)

    def test_unparseable_stdin_passes_for_every_registered_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for runtime in harness.RUNTIMES:
                for event in sorted(registered_events(runtime)):
                    with self.subTest(runtime=runtime, event=event):
                        run = harness.run_hook(harness.source_hook(runtime, event), "{not json", home)
                        rec = harness.Recorded(runtime, "garbage", event, {}, "")
                        self.assertEqual(classify(event, self.assert_native(rec, run)), "pass")

    def test_a_payload_that_never_ends_passes_within_the_hook_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            processes = {runtime: start_hung(runtime, Path(tmp)) for runtime in harness.RUNTIMES}
            for runtime, process in processes.items():
                with self.subTest(runtime=runtime), process:
                    returncode = process.wait(timeout=harness.HOOK_SECONDS)
                    self.assertLess(time.monotonic() - started, deadline.HOOK_TIMEOUT_SECONDS)
                    process.stdin.close()
                    run = harness.HookRun(returncode, process.stdout.read(), process.stderr.read())
                    rec = harness.Recorded(runtime, "hung", HUNG_EVENTS[runtime], {}, "")
                    self.assertEqual(classify(rec.event, self.assert_native(rec, run)), "pass")


class FixtureHygieneTests(unittest.TestCase):
    """Recorded fixtures match the runtime input schemas and hold nothing personal."""

    def test_codex_payloads_match_the_binary_input_schemas(self) -> None:
        for rec in harness.recorded("codex"):
            with self.subTest(fixture=rec.name):
                schema = harness.codex_schema(rec.event, "input")
                self.assertEqual(harness.schema_problems(rec.payload, schema, schema), [])

    def test_fixtures_hold_no_prompts_paths_or_identities(self) -> None:
        for path in sorted(harness.FIXTURES.rglob("*.json")):
            text = path.read_text()
            with self.subTest(fixture=str(path.relative_to(harness.FIXTURES))):
                self.assertNotRegex(text, r"/Users/|/home/\w")
                self.assertEqual(set(EMAIL.findall(text)) - {"user@example.com"}, set())
        prompts = {rec.payload.get("prompt") for rt in harness.RUNTIMES for rec in harness.recorded(rt)}
        self.assertLessEqual(prompts, {None, "recorded prompt (scrubbed)"})


if __name__ == "__main__":
    unittest.main()
