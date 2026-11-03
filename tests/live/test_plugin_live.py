"""scripts/plugin_live.sh installs the plugins into the real home; these tests keep it runnable.

Every subcommand runs against stub runtimes on PATH that report the plugin as installed, with a
temporary HOME and TMPDIR and stdout not a terminal (plain output). Dry runs install nothing, call
no model and write nothing; plain `check` is read-only and only writes its run log to TMPDIR.
The real script changes the real home and is only run by hand.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import harness

SCRIPT = harness.ROOT / "scripts" / "plugin_live.sh"
CLAUDE_STUB = """#!/bin/sh
case "$*" in
    "plugin marketplace list --json") printf '[{"name": "context-guard", "path": "%s"}]\\n' "$STUB_DIST/claude" ;;
    "plugin list --json") printf '[{"id": "context-guard@context-guard", "version": "0.1.0", "scope": "user"}]\\n' ;;
    *) exit 1 ;;
esac
"""
CODEX_STUB = """#!/bin/sh
case "$*" in
    "plugin marketplace list") printf 'MARKETPLACE    ROOT\\ncontext-guard  /elsewhere\\n' ;;
    "plugin list --marketplace context-guard")
        printf 'PLUGIN  STATUS  VERSION\\ncontext-guard@context-guard  installed, enabled  0.1.0\\n' ;;
    *) exit 1 ;;
esac
"""
GENIE_STUB = """#!/bin/sh
case "$*" in
    "plugin marketplace list") printf 'MARKETPLACE    ROOT\ncontext-guard  /elsewhere\n' ;;
    "plugin list")
        printf 'PLUGIN  STATUS  VERSION\ncontext-guard@context-guard  installed, enabled  0.1.0\n' ;;
    *) exit 1 ;;
esac
"""


class PluginLiveTests(unittest.TestCase):
    """Each subcommand prints readable steps with stub runtimes and leaves the home untouched."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ("bin", "home", "tmp"):
            (self.root / name).mkdir()
        stubs = {
            "claude": CLAUDE_STUB,
            "codex": CODEX_STUB,
            "genie": GENIE_STUB,
            "cursor-agent": "#!/bin/sh\nexit 1\n",
        }
        for runtime, text in stubs.items():
            stub = self.root / "bin" / runtime
            stub.write_text(text)
            stub.chmod(0o755)

    def script(self, *args: str, dry_run: bool = True) -> str:
        """Run the script and check the shared output contract.

        Returns:
            The script's stdout.
        """
        env = harness.hook_env(
            self.root / "home",
            {
                "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
                "SMOKE_PYTHON": sys.executable,
                "TMPDIR": str(self.root / "tmp"),
                "STUB_DIST": str(self.root / "home" / ".context-guard" / "plugins"),
            },
        )
        for name in ("CODEX_HOME", "GENIE_HOME", "PLUGIN_DIST", "VERBOSE", "NO_COLOR"):
            env.pop(name, None)
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), *args, *(["--dry-run"] if dry_run else [])],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn(str(self.root / "home"), result.stdout)
        self.assertEqual(list((self.root / "home").iterdir()), [])
        if dry_run:
            self.assertEqual(list((self.root / "tmp").iterdir()), [])
        return result.stdout

    def assert_prints(self, stdout: str, *expected: str) -> None:
        for text in expected:
            self.assertIn(text, stdout)

    def test_install_dry_run_lists_each_step_with_its_commands(self) -> None:
        stdout = self.script("install")
        self.assert_prints(
            stdout,
            "$ ",
            "package --runtime all --out ~/.context-guard/plugins --marketplace context-guard",
            "[skip] Kit",
            "$ claude plugin uninstall context-guard@context-guard",
            "$ claude plugin install context-guard@context-guard --scope user",
            "$ codex plugin marketplace remove context-guard",
            "$ codex plugin marketplace add ~/.context-guard/plugins/codex",
            "$ codex plugin remove context-guard@context-guard",
            "$ codex plugin add context-guard@context-guard",
            "hooks/context-guard-hook adapters/codex/agents.py install",
            "$ genie plugin marketplace remove context-guard",
            "$ genie plugin marketplace add ~/.context-guard/plugins/genie",
            "$ genie plugin remove context-guard@context-guard",
            "$ genie plugin add context-guard@context-guard",
            "hooks/context-guard-hook adapters/genie/agents.py install",
            "$ cp -R ~/.context-guard/plugins/cursor/plugins/context-guard ~/.cursor/plugins/local/context-guard",
            "context-guard/hooks/context-guard-hook adapters/cursor/hooks_install.py install",
            "Next steps",
            "/hooks",
        )
        for tool in ("Package", "Claude", "Codex", "Genie", "Cursor"):
            self.assertRegex(stdout, rf"\[dry\] +{tool} ")
        self.assertNotIn("claude plugin marketplace add", stdout)
        self.assertNotIn("Full log", stdout)

    def test_check_shows_a_status_table_without_commands_and_logs_them(self) -> None:
        stdout = self.script("check", dry_run=False)
        rows = {line.split()[0]: line for line in stdout.splitlines() if line.startswith("  ")}
        self.assertIn("[ok] 0.1.0, user scope", rows["Claude"])
        self.assertIn("agents 0/5", rows["Codex"])
        self.assertIn("agents 0/5", rows["Genie"])
        self.assertIn("[FAIL] not installed", rows["Cursor"])
        self.assertIn("hooks not registered", rows["Cursor"])
        self.assertNotIn("$ ", stdout)
        self.assertIn("install the plugins", stdout)
        (log,) = (self.root / "tmp").iterdir()
        self.assertIn(f"Full log: {log}", stdout)
        self.assertIn("claude plugin list --json", log.read_text())

    def test_check_run_dry_run_smokes_each_tool_in_the_real_home(self) -> None:
        stdout = self.script("check", "--run")
        self.assert_prints(
            stdout,
            "CONTEXT_GUARD_THRESHOLDS_NOTE_CAP=1000",
            "claude -p --model haiku --add-dir ~/.context-guard/handoffs",
            "codex exec resume --last",
            "genie exec resume --last",
            "cursor_interactive.exp",
            "-- cursor-agent --trust --workspace $TMPDIR/",
            "'Use your file-reading tool once to read",
            "wake_check.py claude --log $TMPDIR/",
            "claude -p --input-format stream-json",
            "--waker-log ~/.context-guard/state/codex/waker.log -- codex app-server",
            "--enable multi_agent_v2",
            "-- cursor-agent -p --output-format stream-json --force",
            "unsupported: Genie has no public idle-thread queue command",
            "wake_check.py clean-handoffs --store ~/.context-guard/handoffs --workspace $TMPDIR/",
            "dry run: nothing was changed and no model was called",
        )
        for tool in ("Claude", "Codex", "Genie", "Cursor", "Claude wake", "Codex wake", "Cursor wake"):
            self.assertRegex(stdout, rf"\[dry\] +{tool} ")
        self.assertNotIn("--setting-sources", stdout)
        self.assertNotIn("CONTEXT_GUARD_HOME", stdout)
        self.assertNotIn("genie exec resume --last --json --skip-git-repo-check --sandbox", stdout)

    def test_uninstall_removes_plugins_marketplaces_and_agents(self) -> None:
        self.assert_prints(
            self.script("uninstall"),
            "$ claude plugin uninstall context-guard@context-guard",
            "$ claude plugin marketplace remove context-guard",
            "adapters/codex/agents.py uninstall --home ~",
            "$ codex plugin remove context-guard@context-guard",
            "$ codex plugin marketplace remove context-guard",
            "adapters/genie/agents.py uninstall --home ~",
            "$ genie plugin remove context-guard@context-guard",
            "$ genie plugin marketplace remove context-guard",
            "adapters/cursor/hooks_install.py uninstall --home ~",
            "$ rm -rf ~/.cursor/plugins/local/context-guard",
            "cli.py install --runtime all",
        )

    def test_checklist_numbers_the_steps_of_every_tool(self) -> None:
        stdout = self.script("checklist")
        for heading in ("Cursor IDE", "Claude Code", "Codex", "Genie", "Cross-runtime"):
            self.assertRegex(stdout, rf"(?m)^{heading}.*\n  1\. ")
        self.assert_prints(stdout, "CoWork", "/hooks")

    def test_only_selects_one_runtime_and_rejects_bad_arguments(self) -> None:
        stdout = self.script("install", "--only", "codex")
        self.assert_prints(stdout, "codex plugin add")
        self.assertNotIn("claude plugin", stdout)
        self.assertNotIn("genie plugin", stdout)
        self.assertNotIn("cp -R", stdout)
        for args in (["install", "--only", "vim"], []):
            result = subprocess.run(["/bin/bash", str(SCRIPT), *args], capture_output=True, check=False)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
