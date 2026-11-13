"""Session state schema and the bounded event log."""

import concurrent.futures
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from core import events, state
from core.observation import Observation


class StateTests(unittest.TestCase):
    """Session state round-trips known fields, fails open and prunes only stale files."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "state" / "test"

    def test_state_round_trips_and_drops_damaged_or_unknown_fields(self) -> None:
        with state.session_state(self.directory, "test", "s1") as current:
            current.update(tier="note", turn=2, handoff_at=-5, unknown="x", budget_sent="yes")
        with state.session_state(self.directory, "test", "s1") as current:
            self.assertEqual(current, {"tier": "note", "turn": 2})
        with state.session_state(self.directory, "test", "s2") as other:
            self.assertEqual(other, {})
        with state.session_state(self.directory, "other-runtime", "s1") as other:
            self.assertEqual(other, {})

    def test_malformed_state_file_fails_open(self) -> None:
        self.directory.mkdir(parents=True)
        (self.directory / (state.session_key("test", "s1") + ".json")).write_text("{")
        with state.session_state(self.directory, "test", "s1") as current:
            self.assertEqual(current, {})

    def test_prune_removes_only_stale_session_files(self) -> None:
        self.directory.mkdir(parents=True)
        stale_key, fresh_key = state.session_key("test", "old"), state.session_key("test", "new")
        kept = (events.EVENTS_FILE, "install-manifest.json", "classifier_cache.json", "old.json")
        names = (f"{stale_key}.json", f"{stale_key}.lock", f"{fresh_key}.json", *kept)
        for name in names:
            (self.directory / name).write_text("{}")
        for name in names:
            if name != f"{fresh_key}.json":
                os.utime(self.directory / name, (0, 0))
        state.prune(self.directory, 30)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), sorted((f"{fresh_key}.json", *kept)))

    def test_prune_keeps_a_live_sessions_old_lock(self) -> None:
        with state.session_state(self.directory, "test", "live") as current:
            current["turn"] = 1
        lock = self.directory / (state.session_key("test", "live") + ".lock")
        os.utime(lock, (0, 0))
        state.prune(self.directory, 30)
        self.assertTrue(lock.exists())


class EventLogTests(unittest.TestCase):
    """The event log keeps the newest lines under concurrency and fails open."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state" / events.EVENTS_FILE
        self.obs = Observation("test", "s1", "prompt", tokens=1200, window=200_000)

    def test_concurrent_appends_are_bounded_to_newest_lines(self) -> None:
        def write(number: int) -> None:
            events.append(self.path, [events.record("prompt", self.obs, detail={"n": number})], 20, slack=0)

        for number in range(-20, 0):
            write(number)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(100)))
        rows = events.read(self.path)
        self.assertEqual(len(rows), 20)
        self.assertEqual(len({row["detail"]["n"] for row in rows}), 20)
        write(1000)
        self.assertEqual(events.read(self.path)[-1]["detail"]["n"], 1000)

    def test_slack_defers_trimming_and_unwritable_log_fails_open(self) -> None:
        events.append(self.path, [events.record("prompt", self.obs)] * 15, 10, slack=10)
        self.assertEqual(len(self.path.read_text().splitlines()), 15)
        events.append(self.path, [events.record("prompt", self.obs)] * 6, 10, slack=10)
        self.assertEqual(len(self.path.read_text().splitlines()), 10)
        blocked = Path(self.path.parent / "file")
        blocked.write_text("")
        events.append(blocked / events.EVENTS_FILE, [events.record("prompt", self.obs)], 10)
        self.path.write_text("not json\n" + json.dumps({"event": "prompt"}) + "\n")
        self.assertEqual(events.read(self.path), [{"event": "prompt"}])

    def test_contended_logging_drops_diagnostics_without_waiting(self) -> None:
        with state.file_lock(self.path.with_name(self.path.name + ".lock")):
            started = time.monotonic()
            events.append(self.path, [events.record("prompt", self.obs)], 10)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(events.read(self.path), [])
        events.append(self.path, [events.record("prompt", self.obs)], 10)
        self.assertEqual(len(events.read(self.path)), 1)


if __name__ == "__main__":
    unittest.main()
