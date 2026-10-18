"""Hook deadlines and bounded child process groups."""
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from core import deadline, processes


class AlarmTests(unittest.TestCase):
    def test_deadline_restores_handler_and_elapsed_existing_timer(self):
        previous = signal.getsignal(signal.SIGALRM)
        handler = lambda *_args: None
        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, 1)
        try:
            with self.assertRaises(deadline.DeadlineExceeded):
                with deadline.HookDeadline(time.monotonic() + 0.05):
                    time.sleep(1)
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertIs(signal.getsignal(signal.SIGALRM), handler)
            self.assertGreater(remaining, 0.8)
            self.assertLess(remaining, 0.97)
            self.assertEqual(interval, 0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_prior_periodic_alarm_and_nested_earlier_deadline(self):
        previous = signal.getsignal(signal.SIGALRM)
        calls = []
        signal.signal(signal.SIGALRM, lambda *_args: calls.append(time.monotonic()))
        signal.setitimer(signal.ITIMER_REAL, 0.02, 0.02)
        try:
            with deadline.HookDeadline(time.monotonic() + 0.2):
                time.sleep(0.055)
            self.assertGreaterEqual(len(calls), 2)
            self.assertAlmostEqual(signal.getitimer(signal.ITIMER_REAL)[1], 0.02)
            signal.setitimer(signal.ITIMER_REAL, 0)
            started = time.monotonic()
            with self.assertRaises(deadline.DeadlineExceeded):
                with deadline.HookDeadline(started + 0.05):
                    with deadline.HookDeadline(started + 1):
                        time.sleep(1)
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_deadline_cancellation_kills_pipe_holding_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'survived'
            child = 'import time; from pathlib import Path; time.sleep(.4); Path(' + repr(str(marker)) + ').touch()'
            parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(10)'
            started = time.monotonic()
            with self.assertRaises(deadline.DeadlineExceeded):
                with deadline.HookDeadline(started + 0.15):
                    processes.run([sys.executable, '-c', parent], cwd=tmp, env=dict(os.environ),
                                  timeout=10, capture_output=True, cleanup_timeout=0.1)
            self.assertLess(time.monotonic() - started, 0.5)
            time.sleep(0.45)
            self.assertFalse(marker.exists())

    def test_detached_pipe_holder_cannot_extend_cleanup_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'pid'
            child = 'import os,time; from pathlib import Path; Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid())); time.sleep(10)'
            parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + '],start_new_session=True); time.sleep(10)'
            try:
                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    processes.run([sys.executable, '-c', parent], cwd=tmp, env=dict(os.environ),
                                  timeout=0.2, capture_output=True, cleanup_timeout=0.1)
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                if pidfile.exists():
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(int(pidfile.read_text()), signal.SIGKILL)

    def test_alarm_during_spawn_is_delivered_after_child_is_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'survived'
            code = 'import time; from pathlib import Path; time.sleep(.3); Path(' + repr(str(marker)) + ').touch()'
            original = subprocess.Popen
            def spawn(*args, **kwargs):
                process = original(*args, **kwargs)
                time.sleep(0.1)  # The alarm fires before Popen returns its owned child.
                return process
            with patch.object(subprocess, 'Popen', side_effect=spawn), self.assertRaises(deadline.DeadlineExceeded):
                with deadline.HookDeadline(time.monotonic() + 0.05):
                    processes.run([sys.executable, '-c', code], cwd=tmp, env=dict(os.environ), timeout=10,
                                  capture_output=True, cleanup_timeout=0.1)
            time.sleep(0.35)
            self.assertFalse(marker.exists())

    def test_cancellation_at_successful_cleanup_entry_still_kills_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'survived'
            child = 'import time; from pathlib import Path; time.sleep(.3); Path(' + repr(str(marker)) + ').touch()'
            parent = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",' + repr(child) + '])'
            original = processes.stop_group
            calls = []
            def cleanup(process, cleanup_timeout):
                calls.append(process.pid)
                if len(calls) == 1:
                    raise deadline.DeadlineExceeded()
                original(process, cleanup_timeout)
            with patch.object(processes, 'stop_group', side_effect=cleanup), self.assertRaises(deadline.DeadlineExceeded):
                processes.run([sys.executable, '-c', parent], cwd=tmp, env=dict(os.environ), timeout=10,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cleanup_timeout=0.1)
            self.assertEqual(len(calls), 2)
            time.sleep(0.35)
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
