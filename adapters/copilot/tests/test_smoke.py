"""Smoke sandbox isolation checks."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import copilot_smoke


class SmokeIsolationTests(unittest.TestCase):
    """Explicit smoke outputs and subprocess environments stay private."""

    def test_output_rejects_symlink_result_and_resolves_relative_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "relative"
            output.mkdir()
            (output / "result.json").symlink_to(root / "victim")
            with self.assertRaisesRegex(RuntimeError, "result already exists"):
                copilot_smoke.output_directory(output)
            (output / "result.json").unlink()
            relative = Path(os.path.relpath(output, Path.cwd()))
            self.assertEqual(copilot_smoke.output_directory(relative), output.resolve())

    def test_default_output_is_new_and_private(self) -> None:
        output = copilot_smoke.output_directory(None)
        self.addCleanup(output.rmdir)
        self.assertTrue(output.is_absolute())
        self.assertEqual(output.stat().st_mode & 0o077, 0)

    def test_result_write_refuses_symlink_inserted_after_output_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            victim = root / "victim"
            victim.write_text("unchanged")
            directory_fd = copilot_smoke.verified_output_fd(copilot_smoke.output_directory(output))
            try:
                (output / "result.json").symlink_to(victim)
                with self.assertRaises(FileExistsError):
                    copilot_smoke.write_result(directory_fd, {"status": "pass"})
            finally:
                os.close(directory_fd)
            self.assertEqual(victim.read_text(), "unchanged")

    def test_result_write_stays_bound_to_verified_directory_after_path_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, moved, victim = root / "output", root / "moved", root / "victim"
            victim.write_text("unchanged")
            done = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            def replace_output(*_args: object) -> dict:
                output.rename(moved)
                output.mkdir()
                (output / "result.json").symlink_to(victim)
                return {"block": True}

            with (
                mock.patch.object(sys, "argv", ["copilot_smoke.py", "--dry-run", "--output", str(output)]),
                mock.patch.object(copilot_smoke, "invoke", return_value=done),
                mock.patch.object(copilot_smoke, "offline_check", side_effect=replace_output),
            ):
                self.assertEqual(copilot_smoke.main(), 0)
            self.assertEqual(victim.read_text(), "unchanged")
            self.assertEqual(json.loads((moved / "result.json").read_text())["status"], "pass")

    def test_installed_hook_uses_only_supplied_sandbox_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hook = root / "hooks/context-guard.json"
            hook.parent.mkdir()
            hook.write_text(json.dumps({"hooks": {"sessionStart": [{"bash": "true", "timeoutSec": 5}]}}))
            done = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
            with (
                mock.patch.dict(os.environ, {"CONTEXT_GUARD_PATHS_STATE_DIR": "/unsafe"}),
                mock.patch.object(copilot_smoke.subprocess, "run", return_value=done) as run,
            ):
                copilot_smoke.installed_hook(root, "sessionStart", {}, root, {"CONTEXT_GUARD_HOME": str(root)})
            self.assertEqual(run.call_args.kwargs["env"], {"CONTEXT_GUARD_HOME": str(root)})

    def test_main_sanitises_inherited_overrides_before_invoking_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "relative"
            done = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            def offline(_home: Path, _runtime: Path, _work: Path, _handoffs: Path, env: dict) -> dict:
                self.assertNotIn("CONTEXT_GUARD_PATHS_STATE_DIR", env)
                self.assertEqual(env["CONTEXT_GUARD_HOME"], str(output.resolve() / "sandbox/home"))
                self.assertTrue(Path(env["COPILOT_HOME"]).is_absolute())
                return {"block": True}

            with (
                mock.patch.dict(os.environ, {"CONTEXT_GUARD_PATHS_STATE_DIR": "/unsafe"}),
                mock.patch.object(sys, "argv", ["copilot_smoke.py", "--dry-run", "--output", str(output)]),
                mock.patch.object(copilot_smoke, "invoke", return_value=done),
                mock.patch.object(copilot_smoke, "offline_check", side_effect=offline),
            ):
                self.assertEqual(copilot_smoke.main(), 0)


if __name__ == "__main__":
    unittest.main()
