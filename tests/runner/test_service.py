"""Optional runner contracts: off by default, classifier gating and deadline, plan -> validate -> execute."""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import config, deadline
from core.observation import Decision, Observation
from core.runner import mechanical, service
from core.runner.provider import Invocation, Provider
from core.state import read_json, write_json

WINDOW = 200_000


def fake_cli(directory: Path, body: str) -> Path:
    """Write an executable standing in for a runtime CLI; it records each call next to itself.

    Args:
        directory: Where the executable and its `calls` record are written.
        body: Python source the executable runs after recording the call.

    Returns:
        The path of the executable.
    """
    path = directory / "fake-cli"
    path.write_text(
        f"#!{sys.executable}\nimport pathlib, sys, time\n"
        f"pathlib.Path({str(directory / 'calls')!r}).open('a').write('call\\n')\n{body}\n"
    )
    path.chmod(0o755)
    return path


class ServiceCase(unittest.TestCase):
    """A temporary home and workspace with a fake runtime CLI behind the runner services."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.loader = config.ConfigLoader(config.sources_for("test", self.home, None), {})

    def enable(self, **sections: dict) -> None:
        self.loader.update_user_config({name: {"enabled": True, **values} for name, values in sections.items()})

    def services(self, answer: str = "", body: str | None = None) -> service.Services:
        cli = fake_cli(self.root, body if body is not None else f"print({answer!r})")
        provider = Provider(
            str(cli),
            lambda program, model, prompt, _scratch, _schema: Invocation([program, model], prompt),
            lambda stdout, _scratch: stdout,
        )
        return service.Services(self.loader, self.home, "test", provider, "cheap-model", [sys.executable, "worker"])

    def calls(self) -> int:
        path = self.root / "calls"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def prompt(self, text: str, tokens: int | None) -> Observation:
        return Observation("test", "s1", "prompt", tokens=tokens, window=WINDOW, cwd=str(self.workspace), prompt=text)

    def decisions(self) -> list[dict]:
        path = config.state_dir(self.loader.load(), self.home, "test") / "decisions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]


class GatingTests(ServiceCase):
    """When the classifier, calibration and runner act, and how routing combines with the policy decision."""

    def test_disabled_by_default_builds_nothing_and_starts_no_process(self) -> None:
        build = mock.Mock()
        with mock.patch.object(subprocess, "Popen") as popen:
            routing = service.route(self.prompt("commit and push", 190_000), self.loader.load(), build)
        self.assertEqual(routing, service.Routing())
        build.assert_not_called()
        popen.assert_not_called()

    def test_subagents_and_other_events_never_route(self) -> None:
        self.enable(classifier={}, runner={})
        build = mock.Mock()
        leaf = Observation("test", "a1", "prompt", tokens=190_000, is_subagent=True, prompt="commit")
        tool = Observation("test", "s1", "tool", tokens=190_000)
        for obs in (leaf, tool):
            self.assertEqual(service.route(obs, self.loader.load(), build), service.Routing())
        build.assert_not_called()

    def test_classifier_is_skipped_below_the_note_threshold(self) -> None:
        self.enable(classifier={}, calibration={})
        services = self.services("MECHANICAL")
        self.assertEqual(services.prompt(self.prompt("commit and push", 100_000)).message, "")
        self.assertEqual(self.calls(), 0)
        routing = services.prompt(self.prompt("commit and push", 120_000))
        self.assertEqual((self.calls(), routing.message), (1, service.MECHANICAL_NOTE))
        self.assertEqual([row["asked"] for row in self.decisions()], [False, True])

    def test_classifier_timeout_fails_open_inside_the_hook_deadline(self) -> None:
        self.enable(classifier={"timeout_seconds": 20}, calibration={})
        services = self.services(body="time.sleep(30)")
        started = time.monotonic()
        routing = deadline.run_within(
            lambda: services.prompt(self.prompt("commit and push", 190_000)), "deadline expired", seconds=2
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(routing, service.Routing())
        self.assertEqual(self.calls(), 1)
        self.assertEqual([(row["asked"], row["label"]) for row in self.decisions()], [(True, None)])

    def test_decisions_are_logged_only_with_calibration_and_truncated(self) -> None:
        self.enable(classifier={})
        services = self.services("CONTINUE")
        services.prompt(self.prompt("commit and push", 190_000))
        self.assertFalse(services.calibration.log_path().exists())
        self.enable(calibration={})
        long = "x" * 5000
        self.services("CONTINUE").prompt(self.prompt(long, 190_000))
        limit = self.loader.load()["classifier"]["max_chars"] + 1
        self.assertEqual([(row["prompt"], row["chars"]) for row in self.decisions()], [(long[:limit], 5000)])

    def test_calibration_alone_logs_prompts_without_asking_the_classifier(self) -> None:
        self.enable(calibration={})
        build = mock.Mock(side_effect=lambda: self.services("MECHANICAL"))
        routing = service.route(self.prompt("commit and push", 190_000), self.loader.load(), build)
        self.assertEqual((routing, self.calls()), (service.Routing(), 0))
        self.assertEqual([(row["prefilter"], row["asked"]) for row in self.decisions()], [(True, False)])

    def test_calibration_replies_pass_through_unless_a_proposal_awaits_them(self) -> None:
        self.enable(classifier={})
        services = self.services("CONTINUE")
        self.assertFalse(services.prompt(self.prompt("calibration keep", 190_000)).blocked)
        write_json(services.calibration.proposal_path(), {"status": "pending", "changes": {}, "summary": "s"})
        self.assertFalse(services.prompt(self.prompt("calibration keep", 190_000)).blocked)
        self.enable(calibration={})
        routing = self.services("CONTINUE").prompt(self.prompt("calibration keep", 190_000))
        self.assertTrue(routing.blocked)
        self.assertIn("Discarded the calibration proposal", routing.message)

    def test_mechanical_label_schedules_the_runner_and_blocks_the_prompt(self) -> None:
        self.enable(runner={})
        self.addCleanup(mechanical.STAGED_JOBS.clear)
        launch, workers = subprocess.Popen, []

        def popen(argv: list[str], **kwargs: object) -> subprocess.Popen | mock.Mock:
            if argv[:2] != [sys.executable, "worker"]:
                return launch(argv, **kwargs)
            workers.append(argv)
            return mock.Mock()

        with mock.patch.object(subprocess, "Popen", side_effect=popen):
            routing = self.services("MECHANICAL").prompt(self.prompt("commit and push", 190_000))
        self.assertTrue(routing.blocked)
        self.assertIn("Mechanical runner scheduled", routing.message)
        self.assertEqual([argv[2] for argv in workers], [service.WORKER])
        self.assertEqual(service.combine(Decision("note", "tier note"), routing).kind, "block")

    def test_runner_is_not_staged_when_the_deadline_is_nearly_spent(self) -> None:
        self.enable(runner={})
        services = self.services()
        obs = self.prompt("commit and push", 190_000)
        with mock.patch.object(subprocess, "Popen") as popen:
            routing = deadline.run_within(
                lambda: services.route("MECHANICAL", "commit and push", obs),
                None,
                seconds=service.SCHEDULE_RESERVE_SECONDS / 2,
            )
        self.assertEqual(routing, service.Routing(message=service.MECHANICAL_NOTE))
        popen.assert_not_called()

    def test_staged_jobs_are_released_only_after_a_blocking_response(self) -> None:
        self.enable(runner={})
        self.addCleanup(mechanical.STAGED_JOBS.clear)
        services = self.services()
        with mock.patch.object(subprocess, "Popen"):
            routing = services.route("MECHANICAL", "commit and push", self.prompt("commit and push", 190_000))
        job = Path(routing.message.split("Report: ")[1].split(". ")[0])
        service.confirm_staged({}, {})
        self.assertEqual(read_json(job)["status"], "staged")
        self.assertEqual(mechanical.STAGED_JOBS, [])
        mechanical.STAGED_JOBS.append(job)
        service.confirm_staged({"decision": "block"}, {})
        self.assertEqual(read_json(job)["status"], "scheduled")

    def test_combine_appends_routing_to_the_policy_decision(self) -> None:
        note = service.Routing(message="routed")
        self.assertEqual(service.combine(Decision(), note), Decision("note", "routed"))
        self.assertEqual(service.combine(Decision("block", "full"), note).message, "full\n\nrouted")
        self.assertEqual(service.combine(Decision("pass"), service.Routing()), Decision("pass"))


@unittest.skipUnless(shutil.which("git"), "git is required for the worker contract")
class WorkerTests(ServiceCase):
    """The worker executes only validated, authorised plans and publishes a report."""

    def job(self, prompt: str, key: str) -> Path:
        directory = config.state_dir(self.loader.load(), self.home, "test") / "runner"
        path = directory / (key * 32 + ".json")
        write_json(
            path, {"status": "scheduled", "prompt": prompt, "workspace": str(self.workspace), "created_at": time.time()}
        )
        return path

    def test_worker_executes_a_validated_plan_and_publishes_a_report(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        self.enable(runner={})
        plan = json.dumps({"commands": [["git", "status", "--short"]]})
        job = self.job("check the git status", "a")
        self.assertEqual(self.services(f"Plan: {plan}").work([service.WORKER, str(job)]), 0)
        self.assertEqual(read_json(job)["status"], "done")
        pending = config.handoff_dir(self.loader.load(), self.home) / "pending" / f"native-{job.stem}.json"
        self.assertTrue(pending.exists())

    def test_mutating_git_without_an_affirmative_request_fails_closed(self) -> None:
        self.enable(runner={})
        plan = json.dumps({"commands": [["git", "status"], ["git", "push"]]})
        job = self.job("explain why push failed", "b")
        self.assertEqual(self.services(plan).work([service.WORKER, str(job)]), 1)
        self.assertEqual((read_json(job)["status"], read_json(job)["error"]), ("failed", "git command not authorized"))
        self.assertNotIn('"argv"', job.with_suffix(".log").read_text())


if __name__ == "__main__":
    unittest.main()
