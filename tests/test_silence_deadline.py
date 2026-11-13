"""Silence scanning shares the policy deadline and cannot consume an undelivered notice."""

import importlib
import subprocess
import sys
import time
from functools import partial
from unittest import mock

from core import config, deadline, state
from tests import harness


class SilenceDeadlineTests(harness.RuntimeHookCase):
    """Exercise the Codex/Genie transaction with stalled scans and real lock contention."""

    runtime = "codex"

    def test_slow_scan_times_out_before_commit_and_retries(self) -> None:
        for runtime in ("codex", "genie"):
            with self.subTest(runtime=runtime):
                hooks = importlib.import_module(f"adapters.{runtime}.hooks")
                subagents = importlib.import_module(f"adapters.{runtime}.subagents")
                driver_type = importlib.import_module(f"adapters.{runtime}.tests.driver").Driver
                driver = driver_type(self.home, self.project, f"scan-{runtime}")
                driver.tokens(1000)
                driver.tokens(1000, leaf=True)
                child, root = driver.payload("leaf"), driver.payload("tool")
                with mock.patch.object(subagents, "clock", return_value=100):
                    hooks.run(child["hook_event_name"], child, self.home, {})

                def stalled_scan(record: dict, now: float) -> None:
                    """Stall actual scan work while checking the transaction has not committed."""
                    del record, now
                    self.assertFalse(deadline.active().committed)
                    time.sleep(5)

                started = time.monotonic()
                with (
                    mock.patch.object(subagents, "clock", return_value=701),
                    mock.patch.object(subagents, "refresh", side_effect=stalled_scan),
                ):
                    response = deadline.run_within(
                        partial(hooks.run, root["hook_event_name"], root, self.home, {}), {}, seconds=0.6
                    )
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(response, {})
                driver.now = 702
                self.assertIn("has been silent", driver.fire("tool").text)
                self.assertEqual(sum(e["event"] == "subagent_silent" for e in driver.events()), 1)

    def test_contended_state_lock_times_out_and_keeps_notice_pending(self) -> None:
        for runtime in ("codex", "genie"):
            with self.subTest(runtime=runtime):
                hooks = importlib.import_module(f"adapters.{runtime}.hooks")
                subagents = importlib.import_module(f"adapters.{runtime}.subagents")
                driver_type = importlib.import_module(f"adapters.{runtime}.tests.driver").Driver
                driver = driver_type(self.home, self.project, f"lock-{runtime}")
                driver.tokens(1000)
                driver.tokens(1000, leaf=True)
                child, root = driver.payload("leaf"), driver.payload("tool")
                with mock.patch.object(subagents, "clock", return_value=100):
                    hooks.run(child["hook_event_name"], child, self.home, {})
                directory = config.state_dir(hooks.load_config(self.home, {}), self.home, runtime)
                lock = directory / f"{state.session_key(runtime, driver.session)}.lock"
                script = (
                    "import fcntl,sys,time; f=open(sys.argv[1],'a'); "
                    "fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(10)"
                )
                with subprocess.Popen(
                    [sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True
                ) as holder:
                    try:
                        self.assertEqual(holder.stdout.readline().strip(), "locked")
                        started = time.monotonic()
                        with mock.patch.object(subagents, "clock", return_value=701):
                            response = deadline.run_within(
                                partial(hooks.run, root["hook_event_name"], root, self.home, {}), {}, seconds=0.6
                            )
                        self.assertLess(time.monotonic() - started, 1.5)
                        self.assertEqual(response, {})
                    finally:
                        holder.terminate()
                        holder.wait(timeout=2)
                driver.now = 702
                self.assertIn("has been silent", driver.fire("tool").text)
                self.assertEqual(sum(e["event"] == "subagent_silent" for e in driver.events()), 1)
