"""The installed kit tells the agent when its source checkout has moved on."""

import concurrent.futures
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from core import config, install, kit, policy
from core.observation import COMPACT, CONTEXT, SESSION, Decision, Observation


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


class RepoCase(unittest.TestCase):
    """A temporary git checkout standing in for the kit source, with one commit."""

    def setUp(self) -> None:
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
    """A kit behind its source checkout gets one session-start notice with an update hint."""

    def test_new_commit_reports_the_kit_as_behind(self) -> None:
        manifest = self.manifest()
        self.commit("two")
        message = kit.staleness(self.root, manifest)
        self.assertIn("behind its source", message)
        self.assertIn(str(self.repo), message)
        self.assertNotIn("\n", message)

    def test_update_hint_names_the_installed_runtimes_and_home(self) -> None:
        home = self.root / "sandbox"
        path = install.manifest_path(home, "cursor")
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        manifest = self.manifest()
        self.commit("two")
        message = kit.staleness(home, manifest)
        self.assertIn(f"install --runtime cursor --home {home}`", message)
        self.assertNotIn("--runtime all", message)

    def test_update_hint_uses_runtime_names_and_deduplicates_profiles(self) -> None:
        home = self.root / "sandbox"
        for name in ("claude", "claude-" + "a" * 32):
            path = install.manifest_path(home, name)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"runtime": "claude"}))
        self.assertEqual(kit.installed_runtimes(home), ["claude"])
        self.assertIn("--runtime claude --home", kit.update_command(home, self.repo))

    def test_unchanged_missing_or_non_git_sources_stay_silent(self) -> None:
        self.assertIsNone(kit.staleness(self.root, self.manifest()))
        self.assertIsNone(kit.staleness(self.root, {}))
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(kit.staleness(self.root, {"source": str(plain), "commit": "a" * 40}))
        self.assertIsNone(kit.staleness(self.root, {"source": str(self.root / "gone"), "commit": "a" * 40}))
        self.assertNotIn("commit", kit.provenance(plain))

    def test_session_start_adds_the_line_but_compaction_does_not(self) -> None:
        home = self.root / "home"
        stored = self.manifest()
        self.commit("two")
        path = install.manifest_path(home, kit.KIT_RUNTIME)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"runtime": "kit", "files": {}, **stored}))
        cfg = config.load_config(config.sources_for("test", home, None), {})
        ctx = policy.Context(cfg, config.handoff_dir(cfg, home), home)

        def decide(event: str) -> Decision:
            return policy.decide(Observation("test", "s", event), {}, ctx)

        session = decide(SESSION)
        self.assertEqual(session.kind, CONTEXT)
        self.assertIn("behind its source", session.message)
        self.assertNotIn("behind its source", decide(COMPACT).message or "")


class KitAutoUpdateTests(RepoCase):
    """A behind kit reinstalls its runtimes in the background once, unless disabled."""

    def setUp(self) -> None:
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

    def session(self, *, auto_update: bool) -> str:
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

    def test_behind_kit_reinstalls_installed_runtimes_in_the_background(self) -> None:
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

    def test_recent_attempt_falls_back_to_the_notice(self) -> None:
        self.commit("two")
        self.session(auto_update=True)
        self.wait_for_calls()
        self.calls.unlink()
        self.assertIn("behind its source", self.session(auto_update=True))
        self.assertFalse(self.calls.exists())

    def test_concurrent_sessions_claim_one_update(self) -> None:
        log = kit.update_log(self.home)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: kit.claim_update(log), range(8)))
        self.assertEqual(claims.count(True), 1)

    def test_current_kit_or_disabled_option_starts_nothing(self) -> None:
        self.assertNotIn("updating", self.session(auto_update=True))
        self.commit("two")
        self.assertIn("behind its source", self.session(auto_update=False))
        self.assertFalse(kit.update_log(self.home).exists())


if __name__ == "__main__":
    unittest.main()
