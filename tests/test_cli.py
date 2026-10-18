"""cli.py contracts with a fake runtime mapping and a toolchain pinned to the current interpreter."""

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import cli
from core import events, kit
from tests.test_install import fake_spec, snapshot

USER_FILES = (
    ".context-guard/config.json",
    ".context-guard/config.json.lock",
    ".context-guard/handoffs",
)


def fake_venv(base: Path, venv: Path) -> None:
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    link = venv / "bin" / "python"
    link.unlink(missing_ok=True)
    link.symlink_to(base)


TOOLCHAIN = kit.Toolchain(
    find_python=lambda: Path(sys.executable), create_venv=fake_venv
)


def fake_mapping(runtime: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        install_spec=lambda home, kit_root, python, config: fake_spec(
            home, kit_root, python, config, runtime
        )
    )


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.source = root / "home", root / "source"
        self.home.mkdir()
        for relative, text in (
            ("core/__init__.py", ""),
            ("content/policy.md", "policy"),
            ("adapters/cursor/hooks.py", "hooks"),
            ("cli.py", "cli"),
            ("pyproject.toml", '[project]\nversion = "9.9.9"\n'),
        ):
            (self.source / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.source / relative).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(
        self, *argv: str, load_mapping=fake_mapping, source=None
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(
                [*argv, "--home", str(self.home)],
                toolchain=TOOLCHAIN,
                load_mapping=load_mapping,
                source=source or self.source,
                out=out,
            )
        return code, out.getvalue(), err.getvalue()


class InstallCommandTests(CliCase):
    def test_dry_run_prints_plan_and_writes_nothing(self):
        (self.home / ".cursor-home").mkdir()
        (self.home / ".cursor-home" / "settings.json").write_text('{"theme": "dark"}')
        before = snapshot(self.home)
        code, out, _ = self.run_cli(
            "install",
            "--runtime",
            "cursor",
            "--dry-run",
            "--set",
            "events.max_lines=10",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("[cursor] would write", out)
        self.assertIn("[kit] would create", out)
        self.assertIn("Dry run", out)
        self.assertEqual(snapshot(self.home), before)

    def test_install_then_reinstall_is_up_to_date(self):
        self.assertEqual(self.run_cli("install", "--runtime", "cursor")[0], cli.EXIT_OK)
        self.assertTrue(kit.venv_python(kit.kit_dir(self.home)).exists())
        before = snapshot(self.home)
        code, out, _ = self.run_cli("install", "--runtime", "cursor")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("[kit] up to date", out)
        self.assertIn("[cursor] up to date", out)
        self.assertEqual(snapshot(self.home), before)

    def test_conflict_exit_code_and_force(self):
        self.run_cli("install", "--runtime", "cursor")
        agent = self.home / ".cursor-home" / "agents" / "scout.md"
        agent.write_text("mine\n")
        code, out, _ = self.run_cli("install", "--runtime", "cursor")
        self.assertEqual(code, cli.EXIT_ISSUES)
        self.assertIn("conflict", out)
        self.assertEqual(agent.read_text(), "mine\n")
        self.assertEqual(
            self.run_cli("install", "--runtime", "cursor", "--force")[0], cli.EXIT_OK
        )
        self.assertEqual(agent.read_text(), "scout\n")

    def test_set_writes_user_config_and_rejects_wrong_types(self):
        code, _, _ = self.run_cli(
            "status",
            "--runtime",
            "cursor",
            "--set",
            "events.max_lines=10",
            "--set",
            "custom.flag=true",
        )
        self.assertEqual(code, cli.EXIT_OK)
        written = json.loads((self.home / ".context-guard" / "config.json").read_text())
        self.assertEqual(
            written, {"events": {"max_lines": 10}, "custom": {"flag": True}}
        )
        code, _, err = self.run_cli(
            "status", "--runtime", "cursor", "--set", "events.max_lines=many"
        )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("expects int", err)

    def test_missing_mapping_is_a_clear_error(self):
        def missing(runtime):
            raise ModuleNotFoundError(f"No module named 'adapters.{runtime}.mapping'")

        code, _, err = self.run_cli("install", "--runtime", "all", load_mapping=missing)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("mapping.py is unavailable", err)
        self.assertFalse((self.home / ".context-guard").exists())

    def test_missing_interpreter_writes_nothing(self):
        def no_python():
            raise kit.ToolchainError("context-guard needs Python >=3.14")

        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(
                ["install", "--runtime", "cursor", "--home", str(self.home)],
                toolchain=kit.Toolchain(find_python=no_python),
                load_mapping=fake_mapping,
                source=self.source,
                out=out,
            )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn(">=3.14", err.getvalue())
        self.assertEqual(snapshot(self.home), {})


class LifecycleTests(CliCase):
    def test_uninstall_leaves_home_as_before_except_user_files(self):
        (self.home / ".cursor-home").mkdir()
        (self.home / ".cursor-home" / "settings.json").write_text(
            json.dumps({"theme": "dark"}, indent=2) + "\n"
        )
        before = snapshot(self.home)
        self.run_cli("install", "--runtime", "all", "--set", "events.max_lines=10")
        handoffs = self.home / ".context-guard" / "handoffs"
        handoffs.mkdir()
        (handoffs / "task.md").write_text("notes")
        self.run_cli("uninstall", "--runtime", "cursor")
        self.assertTrue(kit.kit_dir(self.home).exists())
        cache = kit.kit_dir(self.home) / "core" / "__pycache__"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "policy.cpython-314.pyc").write_bytes(b"\0")
        code, _, _ = self.run_cli("uninstall", "--runtime", "all")
        self.assertEqual(code, cli.EXIT_OK)
        ignored = (*USER_FILES, ".context-guard/backups", ".context-guard")
        self.assertEqual(snapshot(self.home, ignored), before)
        self.assertEqual(
            sorted(p.name for p in (self.home / ".context-guard").iterdir()),
            ["backups", "config.json", "config.json.lock", "handoffs"],
        )

    def test_doctor_detects_drift_and_broken_interpreter(self):
        self.run_cli("install", "--runtime", "cursor")
        self.assertEqual(self.run_cli("doctor", "--runtime", "cursor")[0], cli.EXIT_OK)
        (self.home / ".cursor-home" / "rules" / "policy.md").write_text("changed\n")
        python = kit.venv_python(kit.kit_dir(self.home))
        python.unlink()
        python.write_text("#!/bin/sh\nexit 1\n")
        python.chmod(0o755)
        code, out, _ = self.run_cli("doctor", "--runtime", "cursor")
        self.assertEqual(code, cli.EXIT_ISSUES)
        self.assertIn("policy.md differs", out)
        self.assertIn("does not run", out)
        self.run_cli("install", "--runtime", "cursor", "--force")
        self.assertEqual(self.run_cli("doctor", "--runtime", "cursor")[0], cli.EXIT_OK)

    def test_doctor_reports_kit_drift(self):
        self.run_cli("install", "--runtime", "cursor")
        (self.source / "content" / "policy.md").write_text("new policy")
        code, out, _ = self.run_cli("doctor", "--runtime", "cursor")
        self.assertEqual(code, cli.EXIT_ISSUES)
        self.assertIn("[kit]", out)
        self.assertIn("policy.md differs", out)

    def test_status_reports_version_manifest_and_events(self):
        self.assertIn(
            "[kit] not installed", self.run_cli("status", "--runtime", "cursor")[1]
        )
        self.run_cli("install", "--runtime", "cursor")
        state = self.home / ".context-guard" / "state" / "cursor"
        (state / events.EVENTS_FILE).write_text(
            json.dumps({"ts": 0, "event": "note"}) + "\n"
        )
        code, out, _ = self.run_cli("status", "--runtime", "all")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("version 9.9.9", out)
        self.assertIn("2 files, 1 merges, 1 edits; 1 events (last: note", out)
        self.assertIn("[codex] not installed", out)

    def test_repeated_runtime_selects_each_once(self):
        code, out, _ = self.run_cli(
            "status", "--runtime", "codex", "--runtime", "cursor", "--runtime", "codex"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out.count("[codex] not installed"), 1)
        self.assertIn("[cursor] not installed", out)
        self.assertNotIn("[claude]", out)


if __name__ == "__main__":
    unittest.main()
