"""Codex root wake: SubagentStop starts a detached waker that queues one message on an idle root."""

import dataclasses
import io
import json
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from adapters.codex import hooks, waker
from tests import harness

FIXTURE = "SubagentStop-subagent"
WAKE_ON = {harness.WAKE_PARENT_ENV: "1"}


def turn(kind: str) -> dict:
    return {"type": "event_msg", "payload": {"type": kind}}


def completed(agent: str, path: str = "/root/worker") -> dict:
    item = {"type": "SubAgentActivity", "kind": "completed", "agent_thread_id": agent, "agent_path": path}
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}


class FakeTime:
    """A monotonic clock that only `sleep` advances, with hooks run at chosen sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.actions: dict[int, list] = {}

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        for action in self.actions.pop(len(self.sleeps), []):
            action()


class WakeCase(harness.RuntimeHookCase):
    """A root rollout named by the recorded SubagentStop payload, and a job for its subagent."""

    runtime = "codex"

    def setUp(self) -> None:
        super().setUp()
        self.payload = harness.payload("codex", FIXTURE, self.home, self.project)
        self.agent = self.payload["agent_id"]
        self.root = self.payload["session_id"]
        self.rollout = Path(self.payload["transcript_path"])
        self.append(turn("task_started"), turn("task_complete"))
        self.config = hooks.load_config(self.home, {})
        self.time = FakeTime()
        self.run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))

    def append(self, *records: dict) -> None:
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        with self.rollout.open("a") as handle:
            handle.writelines(json.dumps(record) + "\n" for record in records)

    def job(self, agent: str | None = None) -> waker.Job:
        return waker.Job(
            rollout=str(self.rollout),
            root=self.root,
            agent=agent or self.agent,
            codex_home=str(self.home / ".codex"),
            state_dir=str(self.state_dir),
            tail_bytes=self.config["rollout"]["tail_bytes"],
            events=self.config["events"],
        )

    def wake(self, job: waker.Job | None = None, which: str | None = "/bin/codex") -> waker.Outcome:
        return waker.wake(
            job or self.job(), clock=self.time.clock, sleep=self.time.sleep, run=self.run, which=lambda _: which
        )

    def wakes(self) -> list[dict]:
        return [record["detail"] for record in self.logged() if record["event"] == "subagent_wake"]


class SubagentStopHookTests(WakeCase):
    """The hook passes at once and starts the waker detached, only when a wake can apply."""

    def stop(self, environ: dict[str, str], **overrides: object) -> tuple[dict, mock.Mock]:
        with mock.patch.object(subprocess, "Popen") as popen:
            response = hooks.run("SubagentStop", {**self.payload, **overrides}, self.home, environ)
        return response, popen

    def test_stop_passes_and_starts_a_detached_waker_for_the_root(self) -> None:
        started = time.monotonic()
        response, popen = self.stop({})
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(response, {})
        popen.assert_called_once()
        argv, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(argv[:2], [sys.executable, str(Path(waker.__file__).resolve())])
        job = waker.Job.from_json(argv[2])
        self.assertEqual((job.root, job.agent, job.rollout), (self.root, self.agent, str(self.rollout)))
        self.assertEqual(job.codex_home, str(self.home / ".codex"))
        self.assertTrue(options["start_new_session"])
        self.assertTrue(options["close_fds"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"].name, str(self.state_dir / waker.LOG_FILE))
        self.assertIs(options["stderr"], options["stdout"])
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.home / ".codex"))

    def test_codex_home_from_the_hook_environment_reaches_the_waker(self) -> None:
        codex = self.home / "elsewhere"
        _, popen = self.stop({"CODEX_HOME": str(codex)})
        self.assertEqual(waker.Job.from_json(popen.call_args.args[0][2]).codex_home, str(codex))
        self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], str(codex))

    def test_no_waker_when_wake_parent_is_off(self) -> None:
        response, popen = self.stop({harness.WAKE_PARENT_ENV: "0"})
        self.assertEqual(response, {})
        popen.assert_not_called()

    def test_no_waker_for_a_nested_subagent_or_a_missing_rollout(self) -> None:
        for overrides in ({"session_id": "another-root"}, {"transcript_path": str(self.rollout) + ".gone"}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.stop({}, **overrides)[1].call_count, 0)

    def test_native_wake_skips_the_waker_and_logs_it(self) -> None:
        (self.home / ".codex/config.toml").write_text("[features]\nwake_on_agent_messages = true\n")
        response, popen = self.stop({})
        self.assertEqual(response, {})
        popen.assert_not_called()
        self.assertEqual(self.wakes()[0]["outcome"], waker.SKIPPED)
        self.assertIn("wake_on_agent_messages", self.wakes()[0]["reason"])

    def test_a_failing_spawn_passes(self) -> None:
        with mock.patch.object(subprocess, "Popen", side_effect=OSError("no fork")):
            self.assertEqual(hooks.run("SubagentStop", self.payload, self.home, {}), {})


class WakeTests(WakeCase):
    """The waker matches the child's completion record, skips busy or woken roots and queues once."""

    def test_waits_for_this_subagents_completion_then_queues_the_message(self) -> None:
        self.append(completed("someone-else", "/root/other"))
        self.time.actions[2] = [lambda: self.append(completed(self.agent))]
        result = self.wake()
        self.assertEqual((result.outcome, result.name, result.matched), (waker.SENT, "/root/worker", True))
        self.assertEqual(self.time.sleeps, [0.5, 0.5, waker.SETTLE_SECONDS])
        argv, options = self.run.call_args.args[0], self.run.call_args.kwargs
        message = "[context-guard] subagent /root/worker finished; its result is waiting."
        self.assertEqual(argv, ["/bin/codex", "queue", "--thread", self.root, "--message", message])
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.home / ".codex"))
        self.assertEqual(options["timeout"], waker.QUEUE_TIMEOUT_SECONDS)

    def test_without_a_completion_record_it_queues_after_the_poll_limit(self) -> None:
        result = self.wake()
        self.assertEqual((result.outcome, result.name, result.matched), (waker.SENT, self.agent, False))
        self.assertEqual(self.time.sleeps.count(waker.POLL_SECONDS), 30)
        self.run.assert_called_once()

    def test_a_busy_root_is_not_woken(self) -> None:
        self.append(completed(self.agent), turn("task_started"))
        result = self.wake()
        self.assertEqual((result.outcome, result.reason), (waker.SKIPPED, "root is in a turn"))
        self.run.assert_not_called()

    def test_the_last_turn_record_decides_however_far_back_it_is(self) -> None:
        filler = {"type": "response_item", "payload": {"type": "message", "text": "task_complete " * 100}}
        self.append(turn("task_started"), *[filler] * 3000, completed(self.agent))
        result = self.wake(dataclasses.replace(self.job(), tail_bytes=4096))
        self.assertEqual((result.outcome, result.reason), (waker.SKIPPED, "root is in a turn"))
        self.append(turn("task_complete"), *[filler] * 3000, completed(self.agent))
        self.assertEqual(self.wake(dataclasses.replace(self.job(), tail_bytes=4096)).outcome, waker.SENT)

    def test_a_root_that_took_a_turn_during_the_settle_delay_is_not_woken(self) -> None:
        self.append(completed(self.agent))
        self.time.actions[1] = [lambda: self.append(turn("task_started"), turn("task_complete"))]
        result = self.wake()
        self.assertEqual((result.outcome, result.reason), (waker.SKIPPED, "root already took a turn"))
        self.run.assert_not_called()

    def test_sibling_completions_share_one_queued_wake_until_its_turn_starts(self) -> None:
        sibling = "01a16005-0000-7000-8000-000000000002"
        self.append(completed(self.agent))
        self.assertEqual(self.wake().outcome, waker.SENT)
        self.append(completed(sibling, "/root/sibling"))
        result = self.wake(self.job(sibling))
        self.assertEqual((result.outcome, result.reason), (waker.SKIPPED, "a wake is already queued"))
        self.append(turn("task_started"), turn("task_complete"), completed(self.agent))
        self.assertEqual(self.wake().outcome, waker.SENT)
        self.assertEqual(self.run.call_count, 2)

    def test_queue_failures_are_reported(self) -> None:
        self.append(completed(self.agent))
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "no app server\nmore\n")
        self.assertEqual(self.wake().reason, "codex queue exited 1: no app server")
        self.run.side_effect = subprocess.TimeoutExpired("codex", 30)
        self.assertEqual(self.wake().outcome, waker.FAILED)
        self.assertEqual(self.wake(which=None).reason, "codex is not on PATH")

    def test_main_logs_one_line_and_one_event_per_outcome(self) -> None:
        failed = waker.Outcome(waker.FAILED, "codex queue exited 1", self.agent, matched=True)
        stderr = io.StringIO()
        with mock.patch.object(waker, "wake", return_value=failed), redirect_stderr(stderr):
            self.assertEqual(waker.main([self.job().to_json()]), 0)
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        self.assertIn(f"root={self.root} agent={self.agent} failed: codex queue exited 1", stderr.getvalue())
        self.assertEqual(self.wakes(), [{**dataclasses.asdict(failed), "agent": self.agent}])

    def test_an_invalid_job_exits_without_waking(self) -> None:
        with redirect_stderr(io.StringIO()):
            for argv in ([], ["{}"], ['{"rollout": 1}'], ["not json"]):
                with self.subTest(argv=argv):
                    self.assertEqual(waker.main(argv), 2)


class DetachedWakeProcessTests(WakeCase):
    """The registered hook, run as a process, wakes the root through `codex queue` on PATH."""

    def test_the_hook_process_returns_at_once_and_the_waker_queues_through_codex(self) -> None:
        bin_dir, queued = self.home / "bin", self.home / "queued.txt"
        bin_dir.mkdir()
        stub = bin_dir / "codex"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$CODEX_HOME" "$@" > "{queued}"\n')
        stub.chmod(0o755)
        self.append(completed(self.agent))
        env = {**WAKE_ON, "PATH": f"{bin_dir}:/usr/bin:/bin", "CONTEXT_GUARD_HOME": str(self.home)}
        run = harness.run_hook(harness.source_hook("codex", "SubagentStop"), self.payload, self.home, env=env)
        self.assertEqual((run.returncode, run.stderr, run.response()), (0, "", {}))
        deadline = time.monotonic() + 15
        while not self.wakes() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.wakes()[0]["outcome"], waker.SENT, (self.state_dir / waker.LOG_FILE).read_text())
        lines = queued.read_text().splitlines()
        self.assertEqual(lines[:5], [str(self.home / ".codex"), "queue", "--thread", self.root, "--message"])
        self.assertTrue(lines[5].startswith("[context-guard] subagent "))


if __name__ == "__main__":
    unittest.main()
