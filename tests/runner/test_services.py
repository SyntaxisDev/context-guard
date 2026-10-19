"""Service contracts with temporary runtime state and injected model callbacks."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core.config import DEFAULTS_FILE, ConfigLoader, ConfigSources
from core.runner import calibration, intent_labels, prompt_services
from core.state import write_json

ROOT = Path(__file__).resolve().parents[2]


class ServicesTests(unittest.TestCase):
    """Classifier labels and cache, and calibration runs, proposals and scheduling, over temporary state."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.state = self.home / "state"
        self.loader = ConfigLoader(ConfigSources("test", DEFAULTS_FILE, None, self.home / "config.json"), {})
        self.loader.update_user_config(
            {
                "paths": {"state_dir": str(self.state)},
                "classifier": {"enabled": True, "model": "fake"},
                "calibration": {
                    "enabled": True,
                    "model": "fake",
                    "min_mechanical": 5,
                    "sample_rejected": 1000,
                    "first_run_prompts": 10,
                },
            }
        )
        self.cache = intent_labels.LabelCache(self.state / "classifier_cache.json", 1000)

    def records(self) -> list[dict[str, str]]:
        return (
            [{"prompt": f"commit and push batch {i}", "label": "MECHANICAL"} for i in range(10)]
            + [{"prompt": f"bump the version to 1.{i}", "label": "MECHANICAL"} for i in range(30)]
            + [{"prompt": f"design a pricing model for region {i}", "label": "NEW_TASK"} for i in range(30)]
        )

    def log(self, records: list[dict]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "decisions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))

    def test_classifier_parsing_cache_and_prefilter_semantics(self) -> None:
        self.assertEqual(intent_labels.normalise("  COMMIT  and push! "), "commit and push")
        ask = mock.Mock(return_value="Earlier CONTINUE. Final: MECHANICAL")
        conf = self.loader.load()["classifier"]
        found = prompt_services.decide("commit and push", conf, self.cache, ask, ask=True)
        self.assertEqual(found["label"], "MECHANICAL")
        self.assertTrue(found["asked"])
        repeated = prompt_services.decide(" COMMIT and push! ", conf, self.cache, ask, ask=True)
        self.assertTrue(repeated["cached"])
        self.assertEqual(ask.call_count, 1)
        for prompt in ("design a feature", "commit\nand push"):
            self.assertFalse(prompt_services.decide(prompt, conf, self.cache, ask, ask=True)["asked"])
        skipped = prompt_services.decide("commit", conf, self.cache, ask, ask=False)
        self.assertEqual((skipped["prefilter"], skipped["asked"]), (True, False))

    def test_invalid_model_output_not_cached(self) -> None:
        self.assertEqual(
            intent_labels.classify("commit", self.loader.load()["classifier"], self.cache, lambda *_args: "unknown")[0],
            None,
        )
        self.assertFalse(self.cache.path.exists())
        write_json(self.cache.path, {"commit": "bad label"})
        self.assertFalse(self.cache.load())

    def test_cache_merges_concurrent_process_writes(self) -> None:
        script = (
            "import sys; from pathlib import Path; from core.runner import intent_labels; "
            'intent_labels.LabelCache(Path(sys.argv[1]), 1000).remember({sys.argv[2]: "MECHANICAL"})'
        )
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        children = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.cache.path), f"commit {i}"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for i in range(12)
        ]
        for child in children:
            _output, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, error.decode())
        self.assertEqual(len(self.cache.load()), 12)
        small = intent_labels.LabelCache(self.cache.path, 2)
        small.remember({"latest": "CONTINUE"})
        self.assertEqual(len(small.load()), 2)
        self.assertIn("latest", small.load())

    def test_calibration_dry_run_uses_existing_labels_and_never_writes_or_calls(self) -> None:
        self.log(self.records())
        self.loader.update_user_config({"calibration": {"mode": "auto"}})
        ask = mock.Mock(side_effect=AssertionError("offline dry run called model"))
        service = calibration.Calibration(self.loader, self.state, ask, self.cache)
        before = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(service.run(dry_run=True), 0)
        result = json.loads(output.getvalue())
        self.assertIn("bump", result["changes"]["keywords"])
        after = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        ask.assert_not_called()

    def test_calibration_judges_length_by_the_recorded_prompt_length(self) -> None:
        limit = self.loader.load()["classifier"]["max_chars"] + 1
        long = [f"commit and push batch {i} " + "with a detailed message about the refactor " * 8 for i in range(20)]
        self.log([{"prompt": text[:limit], "chars": len(text), "label": "MECHANICAL"} for text in long])
        service = calibration.Calibration(self.loader, self.state, mock.Mock(return_value=None), self.cache)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            service.run(dry_run=True)
        self.assertNotIn("max_chars", json.loads(output.getvalue())["changes"])

    def test_automatic_training_updates_only_explicit_runtime_config(self) -> None:
        self.log(self.records())
        self.loader.update_user_config({"calibration": {"mode": "auto"}})
        other = self.home / "other-runtime.json"
        other.write_text('{"classifier":{"keywords":["other"]}}')
        service = calibration.Calibration(self.loader, self.state, mock.Mock(return_value=None), self.cache)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.run(), 0)
        self.assertIn("bump", self.loader.load()["classifier"]["keywords"])
        self.assertEqual(json.loads(other.read_text()), {"classifier": {"keywords": ["other"]}})
        self.assertFalse(service.is_due())
        self.assertIn("applied", service.session_note())
        self.assertEqual(service.session_note(), "")
        self.assertTrue(service.awaiting_reply())
        self.assertIn("calibration.mode=propose", service.command("keep"))
        self.assertEqual(self.loader.load()["calibration"]["mode"], "propose")
        self.assertIn("bump", self.loader.load()["classifier"]["keywords"])
        self.assertFalse(service.awaiting_reply())

    def test_replies_are_awaited_only_for_an_open_proposal_with_calibration_on(self) -> None:
        service = calibration.Calibration(self.loader, self.state, mock.Mock(return_value=None), self.cache)
        self.assertFalse(service.awaiting_reply())
        write_json(service.proposal_path(), {"status": "pending", "changes": {"max_chars": 250}, "summary": "s"})
        self.assertTrue(service.awaiting_reply())
        self.loader.update_user_config({"calibration": {"enabled": False}})
        self.assertFalse(service.awaiting_reply())
        self.loader.update_user_config({"calibration": {"enabled": True}})
        self.assertIn("Applied the calibration proposal", service.command("apply"))
        self.assertEqual(self.loader.load()["classifier"]["max_chars"], 250)
        self.assertFalse(service.awaiting_reply())

    def test_scheduling_preserves_explicit_child_environment_and_cooldown(self) -> None:
        self.log(self.records())
        service = calibration.Calibration(self.loader, self.state, mock.Mock(return_value=None), self.cache)
        child_env = {"CONTEXT_GUARD_CHILD": "1", "PROVIDER_CONFIG": str(self.loader.user_config_path())}
        with mock.patch.object(calibration.subprocess, "Popen") as launch:
            service.maybe_start(["provider-calibrate", "run"], child_env)
            self.assertEqual(launch.call_args.kwargs["env"], child_env)
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            write_json(service.calib_dir() / "last_run.json", {"ts": time.time()})
            service.maybe_start(["provider-calibrate", "run"], child_env)
            self.assertEqual(launch.call_count, 1)
        write_json(service.calib_dir() / "last_run.json", {"ts": "invalid"})
        self.assertTrue(service.is_due())


if __name__ == "__main__":
    unittest.main()
