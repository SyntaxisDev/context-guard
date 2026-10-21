"""scripts/sandbox_smoke.sh is the live smoke; these tests keep it runnable.

The dry run always runs: stub runtimes on PATH, a temporary HOME and TMPDIR, no install and no
model call. The real run is skipped unless CONTEXT_GUARD_LIVE=1; it uses your real Claude Code,
Codex and Cursor logins with hooks installed into a temporary sandbox, and spends a few cheap
model calls (see the script header).
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import harness

SCRIPT = harness.ROOT / "scripts" / "sandbox_smoke.sh"
LIVE = os.environ.get("CONTEXT_GUARD_LIVE") == "1"
LIVE_SECONDS = 1800


class DryRunTests(unittest.TestCase):
    """The dry run prints every runtime's commands with stub runtimes and leaves nothing behind."""

    def dry_run(self, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        for name in ("bin", "home", "tmp"):
            (root / name).mkdir()
        for runtime in ("claude", "codex", "cursor-agent"):
            stub = root / "bin" / runtime
            stub.write_text("#!/bin/sh\nexit 1\n")
            stub.chmod(0o755)
        env = harness.hook_env(
            root / "home",
            {"PATH": f"{root / 'bin'}:/usr/bin:/bin", "SMOKE_PYTHON": sys.executable, "TMPDIR": str(root / "tmp")},
        )
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--dry-run", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return result, root

    def test_dry_run_prints_each_runtime_command_and_leaves_nothing_behind(self) -> None:
        result, root = self.dry_run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for expected in (
            "install --runtime claude --home",
            "--setting-sources project",
            "--plugin-dir",
            "codex exec resume --last",
            "CODEX_HOME=",
            "cursor_interactive.exp",
            "-- cursor-agent --trust --workspace",
            "--set thresholds.note_cap=1000",
            "claude -p 'Use your file-reading tool once to read",
            "--home $TMPDIR/",
            "dry run: nothing was changed and no model was called",
        ):
            self.assertIn(expected, result.stdout)
        for tool in ("Setup", "Claude", "Claude plugin", "Codex", "Cursor"):
            self.assertRegex(result.stdout, rf"\[dry\] +{tool} ")
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn(str(root), result.stdout)
        self.assertEqual(list((root / "tmp").iterdir()), [])
        self.assertEqual(list((root / "home").iterdir()), [])

    def test_only_selects_one_runtime_and_rejects_unknown_ones(self) -> None:
        result, _ = self.dry_run("--only", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("claude -p", result.stdout)
        self.assertNotIn("cursor-agent", result.stdout)
        self.assertEqual(self.dry_run("--only", "vim")[0].returncode, 2)


@unittest.skipUnless(LIVE, "set CONTEXT_GUARD_LIVE=1 to run scripts/sandbox_smoke.sh (spends model calls)")
class LiveSmokeTests(unittest.TestCase):
    """The real sandbox smoke passes against the logged-in runtimes."""

    def test_sandbox_smoke_passes(self) -> None:
        result = subprocess.run(["/bin/bash", str(SCRIPT)], timeout=LIVE_SECONDS, check=False)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
