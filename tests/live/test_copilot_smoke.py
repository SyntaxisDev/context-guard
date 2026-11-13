"""Offline installed Copilot smoke driver checks."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import copilot_smoke

ROOT = Path(__file__).resolve().parents[2]


class CopilotSmokeTests(unittest.TestCase):
    """The smoke requires explicit native mode and keeps offline work isolated."""

    def test_dry_run_installs_and_invokes_real_hooks_in_temporary_home(self) -> None:
        """Dry-run confirms installed prompt refusal from temporary telemetry."""
        with tempfile.TemporaryDirectory() as output:
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/copilot_smoke.py"), "--dry-run", "--output", output],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=240,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads((Path(output) / "result.json").read_text())
            self.assertEqual(data["status"], "pass")
            self.assertTrue(data["offline"]["block"])

    def test_native_delivery_fails_without_native_hook_records(self) -> None:
        """Model text alone cannot establish installed-hook delivery."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime, home = base / "copilot", base / "home"
            (runtime / "session-state/session").mkdir(parents=True)
            (home / ".context-guard/state/copilot").mkdir(parents=True)
            (runtime / "session-state/session/events.jsonl").write_text(
                '{"type":"assistant.message","content":"Context check"}\n'
            )
            (home / ".context-guard/state/copilot/events.jsonl").write_text("")
            with self.assertRaisesRegex(RuntimeError, "missing native"):
                copilot_smoke.assert_native_delivery(
                    runtime, home, "session", "note_delivered", "userPromptSubmitted", "Context check", "Context check"
                )

    def test_user_prompt_is_not_augmented_with_blind_title(self) -> None:
        """The user prompt contains no assertion-only handoff title."""
        command = copilot_smoke.native_command(
            "copilot", resume=None, prompt="Report visible handoff", output=Path("/tmp")
        )
        self.assertEqual(command[-2:], ["-p", "Report visible handoff"])

    def test_native_account_selection_never_copies_credentials_or_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            source = home / ".copilot"
            source.mkdir()
            account = {"host": "https://github.com", "login": "octo", "token": "DO_NOT_COPY"}
            (source / "config.json").write_text(json.dumps({"lastLoggedInUser": account, "trustedFolders": ["/"]}))
            runtime = home / "sandbox"
            runtime.mkdir()
            with patch.object(Path, "home", return_value=home):
                copilot_smoke.copy_account_selection(runtime)
            actual = json.loads((runtime / "config.json").read_text())
            self.assertEqual(actual, {"lastLoggedInUser": {"host": "https://github.com", "login": "octo"}})

    def test_native_cost_deduplicates_requests_and_ignores_cumulative_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            session = runtime / "session-state/session"
            session.mkdir(parents=True)
            rows = [
                {"id": key, "type": "session.usage_record", "data": {"usage": {"copilotUsage": {"totalNanoAiu": cost}}}}
                for key, cost in (("request1", 100_000_000), ("request2", 50_000_000), ("request1", 100_000_000))
            ]
            (session / "events.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
            (runtime / "usage.json").write_text('{"totalNanoAiu":300000000}')
            self.assertEqual(copilot_smoke.native_usage(runtime), {"requests": 2, "ai_credits": 0.15})


if __name__ == "__main__":
    unittest.main()
