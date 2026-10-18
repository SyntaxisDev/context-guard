"""The installed kit tells the agent when its source checkout has moved on."""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from core import config, install, kit, policy
from core.observation import COMPACT, CONTEXT, SESSION, Observation


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


class RepoCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        self.commit("one")

    def commit(self, name: str) -> None:
        (self.repo / name).write_text(name)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", name)

    def manifest(self, source: Path | None = None) -> dict:
        return {"runtime": kit.KIT_RUNTIME, **kit.provenance(source or self.repo)}


class KitStalenessTests(RepoCase):
    def test_new_commit_reports_the_kit_as_behind(self):
        manifest = self.manifest()
        self.commit("two")
        message = kit.staleness(manifest)
        self.assertIn("behind its source", message)
        self.assertIn(str(self.repo), message)
        self.assertNotIn("\n", message)

    def test_unchanged_missing_or_non_git_sources_stay_silent(self):
        self.assertIsNone(kit.staleness(self.manifest()))
        self.assertIsNone(kit.staleness({}))
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(kit.staleness({"source": str(plain), "commit": "a" * 40}))
        self.assertIsNone(
            kit.staleness({"source": str(self.root / "gone"), "commit": "a" * 40})
        )
        self.assertNotIn("commit", kit.provenance(plain))

    def test_session_start_adds_the_line_but_compaction_does_not(self):
        home = self.root / "home"
        stored = self.manifest()
        self.commit("two")
        path = install.manifest_path(home, kit.KIT_RUNTIME)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"runtime": "kit", "files": {}, **stored}))
        cfg = config.load_config(config.sources_for("test", home, None), {})
        ctx = policy.Context(cfg, config.handoff_dir(cfg, home), home)

        def decide(event):
            return policy.decide(Observation("test", "s", event), {}, ctx)

        session = decide(SESSION)
        self.assertEqual(session.kind, CONTEXT)
        self.assertIn("behind its source", session.message)
        self.assertNotIn("behind its source", decide(COMPACT).message or "")


class KitAutoUpdateTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.calls = self.root / "calls.txt"
        python = kit.venv_python(kit.kit_dir(self.home))
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\necho "$*" > "{self.calls}"\n')
        python.chmod(0o755)
        for runtime, stored in (
            (kit.KIT_RUNTIME, self.manifest()),
            ("cursor", {}),
            ("claude", {}),
        ):
            path = install.manifest_path(self.home, runtime)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"runtime": runtime, "files": {}, **stored}))

    def session(self, auto_update: bool) -> str:
        cfg = config.load_config(config.sources_for("test", self.home, None), {})
        cfg["kit_check"]["auto_update"] = auto_update
        ctx = policy.Context(cfg, config.handoff_dir(cfg, self.home), self.home)
        return policy.decide(Observation("test", "s", SESSION), {}, ctx).message

    def wait_for_calls(self) -> str:
        deadline = time.monotonic() + 10
        while not self.calls.exists() or not self.calls.read_text():
            self.assertLess(time.monotonic(), deadline, "the update process never ran")
            time.sleep(0.05)
        return self.calls.read_text()

    def test_behind_kit_reinstalls_installed_runtimes_in_the_background(self):
        self.commit("two")
        message = self.session(auto_update=True)
        self.assertIn("updating in the background", message)
        self.assertNotIn("behind its source", message)
        self.assertEqual(
            self.wait_for_calls().split(),
            [
                str(self.repo / "cli.py"),
                "install",
                "--runtime",
                "claude",
                "--runtime",
                "cursor",
                "--home",
                str(self.home),
            ],
        )
        self.assertTrue(kit.update_log(self.home).exists())

    def test_recent_attempt_falls_back_to_the_notice(self):
        self.commit("two")
        self.session(auto_update=True)
        self.wait_for_calls()
        self.calls.unlink()
        self.assertIn("behind its source", self.session(auto_update=True))
        self.assertFalse(self.calls.exists())

    def test_current_kit_or_disabled_option_starts_nothing(self):
        self.assertNotIn("updating", self.session(auto_update=True))
        self.commit("two")
        self.assertIn("behind its source", self.session(auto_update=False))
        self.assertFalse(kit.update_log(self.home).exists())


if __name__ == "__main__":
    unittest.main()
