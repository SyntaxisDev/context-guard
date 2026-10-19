"""Fail-closed mechanical execution contracts of the optional runner."""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import handoffs, processes
from core.runner import mechanical
from core.state import read_json, write_json


class Commands(unittest.TestCase):
    """Plan validation, git scoping, process isolation and the job lifecycle of the mechanical runner."""

    def test_full_plan_rejected_before_any_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(processes, "run") as run:
            job = {"workspace": tmp, "prompt": "commit and push"}
            for bad in [
                ["sh", "-c", "true"],
                ["git", "-c", "alias.x=!true", "x"],
                ["git", "add", "../escape"],
                ["git", "push", "--force"],
                ["python3", "-c", "print(1)"],
                ["git", "commit", "--no-verify"],
            ]:
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    mechanical.execute_plan({"commands": [["git", "status"], bad]}, job, {}, Path(tmp) / "log")
            run.assert_not_called()

    def test_git_authorization_and_path_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "outside").symlink_to("/tmp")
            self.assertFalse(mechanical.git_allowed(["commit", "-m", "change"], "check status", workspace))
            self.assertFalse(mechanical.git_allowed(["add", "outside/a"], "stage files", workspace))
            self.assertTrue(mechanical.git_allowed(["commit", "-m", "change"], "commit files", workspace))
            self.assertTrue(mechanical.git_allowed(["push"], "can you commit and push", workspace))
            for task in ("check status; do not commit", "explain why commit failed", "can you explain commit"):
                self.assertFalse(mechanical.git_allowed(["commit", "-m", "change"], task, workspace))
            for task in (
                "explain why push failed",
                "explain commit and push behavior",
                "check whether commit and then push is supported",
                '"commit and push" is an example',
                'commit with message "and push"',
            ):
                self.assertFalse(mechanical.git_allowed(["push"], task, workspace))
            self.assertTrue(mechanical.git_allowed(["push"], "please can you commit and then push", workspace))
            with self.assertRaises(ValueError):
                mechanical.validate_command(["git", "push"], "explain why push failed", workspace, [["git", "push"]])

    def test_git_add_never_reaches_outside_the_workspace_through_pathspec_magic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            workspace = repo / "sub"
            workspace.mkdir()
            (repo / "outside.py").write_text("")
            (workspace / "inside.py").write_text("")
            job = {"workspace": str(workspace), "prompt": "stage files"}
            for magic in (":/", ":(top)outside.py", ":!inside.py"):
                with self.subTest(magic=magic), self.assertRaises(ValueError):
                    mechanical.execute_plan({"commands": [["git", "add", "--", magic]]}, job, {}, repo / "log")
            result = mechanical.execute_plan({"commands": [["git", "add", "*.py"]]}, job, {}, repo / "log")
            self.assertEqual(result["status"], "failed")
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(staged, "")

    def test_configured_command_must_name_a_program_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, "--version"]
            with self.assertRaisesRegex(ValueError, "program on PATH"):
                mechanical.validate_command(command, "run it", Path(tmp), [command])

    def test_exec_cwd_shell_environment_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(processes, "run") as run:
            run.return_value.returncode = 0
            result = mechanical.execute_plan(
                {"commands": [["git", "status", "--short"]]},
                {"workspace": tmp, "prompt": "status"},
                {},
                Path(tmp) / "log",
            )
            self.assertEqual(result["status"], "done")
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertEqual(run.call_args.kwargs["cwd"], Path(tmp).resolve())
            self.assertEqual(run.call_args.kwargs["env"]["CONTEXT_GUARD_INTERNAL"], "1")
            run.side_effect = subprocess.TimeoutExpired("git", 1)
            result = mechanical.execute_plan(
                {"commands": [["git", "status"]]}, {"workspace": tmp, "prompt": "status"}, {}, Path(tmp) / "log"
            )
            self.assertEqual(result["status"], "timeout")

    def test_worker_persists_failure_and_one_time_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = mechanical.Runner(Path(tmp), {"enabled": True}, Path(tmp) / "handoffs")
            path = runner.directory / ("a" * 32 + ".json")
            write_json(path, {"status": "scheduled", "workspace": tmp, "prompt": "status", "created_at": 1})
            self.assertEqual(runner.work(path, lambda *_args: {"commands": [["sh", "-c", "true"]]}, "fake"), 1)
            self.assertIn("failed", runner.reports(tmp))
            self.assertIn("failed", runner.reports(tmp))
            pending = Path(tmp) / "handoffs/pending" / ("native-" + path.name)
            pending.unlink()
            self.assertEqual(runner.reports(tmp), "")
            self.assertTrue(path.with_suffix(".lock").exists())

    def test_git_environment_redirects_removed_auth_retained(self) -> None:
        with patch.dict(
            os.environ,
            GIT_DIR="/outside",
            GIT_WORK_TREE="/outside",
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="alias.x",
            GIT_INDEX_FILE="/outside/index",
            SSH_AUTH_SOCK="/auth",
            API_KEY_FIXTURE="kept",
        ):
            env = mechanical.execution_environment()
        self.assertFalse(
            any(
                key in env
                for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0")
            )
        )
        self.assertEqual(env["SSH_AUTH_SOCK"], "/auth")
        self.assertEqual(env["API_KEY_FIXTURE"], "kept")
        self.assertEqual(env["CONTEXT_GUARD_INTERNAL"], "1")
        self.assertEqual(env["GIT_LITERAL_PATHSPECS"], "1")

    def test_timeout_kills_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child-survived"
            child = "import time; from pathlib import Path; time.sleep(1); Path(" + repr(str(marker)) + ").touch()"
            parent = (
                'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + "]); time.sleep(5)"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                processes.run(
                    [sys.executable, "-c", parent], cwd=tmp, env=dict(os.environ), timeout=0.2, capture_output=True
                )
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_normal_exit_cleans_background_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child-survived"
            child = "import time; from pathlib import Path; time.sleep(0.5); Path(" + repr(str(marker)) + ").touch()"
            parent = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",' + repr(child) + "])"
            with (Path(tmp) / "log").open("w") as output:
                result = processes.run(
                    [sys.executable, "-c", parent],
                    cwd=tmp,
                    env=dict(os.environ),
                    timeout=0.1,
                    stdout=output,
                    stderr=output,
                )
            self.assertEqual(result.returncode, 0)
            time.sleep(0.6)
            self.assertFalse(marker.exists())

    def test_final_publish_is_serialized_with_consuming_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = mechanical.Runner(Path(tmp), {}, Path(tmp) / "handoffs")
            path = runner.directory / ("d" * 32 + ".json")
            write_json(path, {"status": "scheduled", "workspace": tmp, "prompt": "status", "created_at": 1})
            publishing, release, reading, done = (threading.Event() for _ in range(4))
            original = handoffs.publish_pending
            calls, errors = [], []

            def publish(*args: object) -> Path:
                calls.append(threading.current_thread().name)
                if threading.current_thread().name == "native-worker":
                    publishing.set()
                    if not release.wait(3):
                        msg = "test publisher barrier timed out"
                        raise RuntimeError(msg)
                return original(*args)

            def work() -> None:
                try:
                    runner.work(path, lambda *_args: {"commands": []}, "fake")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            def consume() -> None:
                try:
                    reading.set()
                    runner.reports(tmp)
                    for sidecar in (Path(tmp) / "handoffs/pending").glob("*.json"):
                        sidecar.unlink()
                    done.set()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            with patch.object(handoffs, "publish_pending", side_effect=publish):
                worker = threading.Thread(target=work, name="native-worker")
                reader = threading.Thread(target=consume, name="native-reader")
                worker.start()
                self.assertTrue(publishing.wait(2))
                reader.start()
                self.assertTrue(reading.wait(2))
                blocked = not done.wait(0.1)
                release.set()
                worker.join(3)
                reader.join(3)
                self.assertTrue(blocked)
                self.assertFalse(worker.is_alive() or reader.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(calls, ["native-worker"])
            self.assertEqual(runner.reports(tmp), "")

    def staged_job(self, tmp: str, confirm_seconds: float) -> tuple[mechanical.Runner, Path]:
        self.addCleanup(mechanical.STAGED_JOBS.clear)
        runner = mechanical.Runner(Path(tmp), {"enabled": True}, Path(tmp) / "handoffs", confirm_seconds)
        with patch.object(subprocess, "Popen"):
            path = Path(runner.schedule("status", tmp, ["python"]))
        self.assertEqual(read_json(path)["status"], "staged")
        return runner, path

    def test_unconfirmed_job_is_cancelled_and_never_planned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, path = self.staged_job(tmp, 0.2)
            mechanical.STAGED_JOBS.clear()
            planner = Mock(return_value={"commands": [["git", "status"]]})
            self.assertEqual(runner.work(path, planner, "fake"), 1)
            self.assertEqual(read_json(path)["status"], "cancelled")
            planner.assert_not_called()

    def test_job_runs_only_after_the_hook_confirms_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, path = self.staged_job(tmp, 5)
            mechanical.confirm_staged()
            self.assertEqual((read_json(path)["status"], mechanical.STAGED_JOBS), ("scheduled", []))
            planner = Mock(return_value={"commands": []})
            runner.work(path, planner, "fake")
            planner.assert_called_once()
            self.assertEqual(read_json(path)["status"], "failed")

    def test_spawn_failure_does_not_block_and_cooldown(self) -> None:
        self.addCleanup(mechanical.STAGED_JOBS.clear)
        with tempfile.TemporaryDirectory() as tmp:
            runner = mechanical.Runner(Path(tmp), {"enabled": True, "cooldown_seconds": 60})
            with patch.object(subprocess, "Popen", side_effect=OSError("unavailable")):
                self.assertEqual(runner.schedule("status", tmp, ["missing"]), "")
            with patch.object(subprocess, "Popen") as process:
                self.assertTrue(runner.schedule("status", tmp, ["python"]))
                self.assertEqual(runner.schedule("status", tmp, ["python"]), "")
                self.assertEqual(process.call_count, 1)


if __name__ == "__main__":
    unittest.main()
