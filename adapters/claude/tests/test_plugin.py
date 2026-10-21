"""Claude plugin package: rendered layout, hook commands, the launcher and the package command."""

import contextlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cli
from adapters.claude import hooks, mapping, plugin
from core import kit, package, render

ROOT = Path(__file__).resolve().parents[3]


def run_cli(*argv: str) -> tuple[int, str]:
    """Run the context-guard CLI in process.

    Args:
        *argv: Command-line arguments after the program name.

    Returns:
        The exit code and everything the CLI wrote to its output stream.
    """
    out = io.StringIO()
    return cli.main(list(argv), out=out), out.getvalue()


class PluginTestCase(unittest.TestCase):
    """A temporary output directory and home for building and launching the Claude plugin package."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.out = self.base / "out"
        self.claude_out = self.out / "claude"
        self.home = self.base / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, *extra: str) -> Path:
        code, text = run_cli("package", "--runtime", "claude", "--out", str(self.out), *extra)
        self.assertEqual(code, cli.EXIT_OK, text)
        return self.claude_out / package.PLUGINS_DIR / plugin.NAME

    def hook_commands(self, root: Path) -> list[tuple[str, str]]:
        data = json.loads((root / plugin.HOOKS_FILE).read_text())
        return [
            (event, hook["command"])
            for event, groups in data["hooks"].items()
            for group in groups
            for hook in group["hooks"]
        ]

    def launch(self, root: Path, args: list[str], payload: dict, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", str(root / package.LAUNCHER), *args],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=self.base,
            timeout=30,
            check=False,
        )


class LayoutTests(PluginTestCase):
    """The packaged plugin's manifests, shipped content and hook commands."""

    def test_manifests_are_valid_json_with_the_project_version(self) -> None:
        root = self.build("--marketplace", "team-tools")
        manifest = json.loads((root / plugin.MANIFEST_FILE).read_text())
        self.assertEqual((manifest["name"], manifest["version"]), (plugin.NAME, kit.source_version()))
        listing = json.loads((self.claude_out / plugin.MARKETPLACE_FILE).read_text())
        self.assertEqual(listing["name"], "team-tools")
        [entry] = listing["plugins"]
        self.assertEqual((self.claude_out / entry["source"]).resolve(), root)
        self.assertEqual(entry["version"], manifest["version"])

    def test_package_ships_agents_skills_policy_and_no_tests_or_caches(self) -> None:
        root = self.build()
        self.assertEqual(
            sorted(p.name for p in (root / "agents").iterdir()),
            sorted(f"{role}.md" for role in render.load_roles()),
        )
        self.assertEqual([p.name for p in (root / "skills").iterdir()], ["handoff"])
        self.assertTrue((root / plugin.POLICY_FILE).read_text().startswith("# Orchestrator mode"))
        shipped = [p.relative_to(root).parts for p in root.rglob("*")]
        self.assertFalse([parts for parts in shipped if {"tests", "__pycache__", ".venv"} & set(parts)])
        self.assertFalse((root / "adapters" / "cursor").exists())
        for name in package.LICENSE_FILES:
            with self.subTest(name=name):
                self.assertEqual((root / name).read_text(), (ROOT / name).read_text())

    def test_package_has_no_top_level_bin_directory(self) -> None:
        """claude.ai rejects uploaded plugins that ship bin/ executables."""
        self.assertFalse((self.build() / "bin").exists())

    def test_policy_names_agents_as_claude_namespaces_plugin_agents(self) -> None:
        policy = (self.build() / plugin.POLICY_FILE).read_text()
        for role in render.load_roles():
            with self.subTest(role=role):
                self.assertIn(f"| `context-guard:{role}` |", policy)
                self.assertNotIn(f"`{role}`", policy)

    def test_every_hook_command_resolves_inside_the_package(self) -> None:
        root = self.build()
        commands = self.hook_commands(root)
        self.assertEqual(sorted(event for event, _ in commands), sorted(mapping.HOOK_EVENTS))
        for event, command in commands:
            with self.subTest(event=event):
                argv = shlex.split(command.replace(plugin.ROOT_TOKEN, str(root)))
                self.assertEqual(argv[0], "sh")
                self.assertEqual(Path(argv[1]), root / package.LAUNCHER)
                self.assertTrue((root / argv[2]).is_file(), argv[2])
                self.assertEqual(argv[3:], [event])

    def test_plugin_registers_the_same_groups_as_an_install(self) -> None:
        def without_commands(groups: dict[str, list[dict]]) -> dict[str, list[dict]]:
            return {
                event: [
                    {**group, "hooks": [{k: v for k, v in hook.items() if k != "command"} for hook in group["hooks"]]}
                    for group in found
                ]
                for event, found in groups.items()
            }

        packaged = json.loads((self.build() / plugin.HOOKS_FILE).read_text())["hooks"]
        config = hooks.load_config(self.home, environ={})
        spec = mapping.install_spec(self.home, self.base / "kit", self.base / "python", config)
        installed = spec.json_merges[0].merge({})["hooks"]
        self.assertEqual(without_commands(packaged), without_commands(installed))

    def test_rendered_files_have_no_unfilled_tokens(self) -> None:
        root = self.build()
        rendered = [
            root / plugin.MANIFEST_FILE,
            root / plugin.HOOKS_FILE,
            root / plugin.POLICY_FILE,
            root / package.LAUNCHER,
            *(root / "agents").iterdir(),
            *(root / "skills").rglob("*.md"),
        ]
        for path in rendered:
            with self.subTest(path=str(path.relative_to(root))):
                self.assertIsNone(render.TOKEN.search(path.read_text()))

    def test_baked_settings_land_in_the_package_not_the_user_config(self) -> None:
        root = self.build("--set", "paths.state_dir=/persistent/state", "--home", str(self.home))
        vendored = json.loads((root / "adapters" / "claude" / "config.json").read_text())
        self.assertEqual(vendored["paths"]["state_dir"], "/persistent/state")
        self.assertFalse((self.home / ".context-guard").exists())


class WriteTests(PluginTestCase):
    """Packaging replaces only its own output, honours dry runs and rejects hooks outside the package."""

    def test_repackaging_replaces_the_previous_package(self) -> None:
        root = self.build()
        stale = root / "agents" / "old.md"
        stale.write_text("old")
        self.build()
        self.assertFalse(stale.exists())

    def test_foreign_directory_is_never_replaced(self) -> None:
        target = self.claude_out / package.PLUGINS_DIR / plugin.NAME
        target.mkdir(parents=True)
        (target / "notes.txt").write_text("mine")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code, _ = run_cli("package", "--runtime", "claude", "--out", str(self.out))
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("is not a context-guard package", errors.getvalue())
        self.assertEqual((target / "notes.txt").read_text(), "mine")

    def test_dry_run_writes_nothing(self) -> None:
        code, text = run_cli("package", "--runtime", "claude", "--out", str(self.out), "--dry-run")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("would package", text)
        self.assertFalse(self.out.exists())

    def test_hook_reference_outside_the_package_fails_closed(self) -> None:
        spec = plugin.plugin_spec(
            cli.package_config(
                cli.Context(self.home, ROOT, kit.Toolchain(), cli.import_mapping, io.StringIO()),
                "claude",
                {},
            )
        )
        broken = package.PluginSpec(
            spec.manifest_file,
            spec.manifest,
            spec.hooks_file,
            {"hooks": {"Stop": [{"hooks": [{"command": f"{plugin.ROOT_TOKEN}/bin/missing"}]}]}},
            spec.root_token,
            spec.files,
        )
        with self.assertRaises(package.PackageError):
            package.plan(ROOT, "claude", broken)


class LauncherTests(PluginTestCase):
    """The package launcher with an isolated PATH: silent without Python 3.14, policy first with it."""

    def isolated_env(self, bin_dir: Path) -> dict:
        return {
            "HOME": str(self.home),
            "PATH": str(bin_dir),
            "CONTEXT_GUARD_SEARCH_PATH": "",
        }

    def test_without_python_314_the_launcher_prints_nothing_and_exits_zero(self) -> None:
        root = self.build()
        old = self.base / "old-bin"
        old.mkdir()
        fake = old / "python3"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        for bin_dir in (self.base / "empty", old):
            with self.subTest(bin_dir=bin_dir.name):
                result = self.launch(
                    root,
                    ["adapters/claude/hooks.py", "Stop"],
                    {},
                    self.isolated_env(bin_dir),
                )
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_session_start_injects_policy_ahead_of_the_handoff_listing(self) -> None:
        root = self.build()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        payload = {
            "session_id": "s1",
            "cwd": str(self.base),
            "source": "startup",
            "model": "claude-haiku-5-5",
        }
        (self.base / ".git").mkdir()
        handoffs = self.home / ".context-guard" / "handoffs"
        handoffs.mkdir(parents=True)
        (handoffs / "task-0123456789abcdef.md").write_text(f"# Packaged task\nStatus: open\nWorkspace: {self.base}\n")
        result = self.launch(
            root,
            ["adapters/claude/hooks.py", "SessionStart"],
            payload,
            self.isolated_env(bin_dir),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("# Orchestrator mode"))
        self.assertGreater(context.find("Packaged task"), context.find("## Handoffs"))
        self.assertLessEqual(len(context), mapping_limit())
        self.assertTrue((self.home / ".context-guard" / "state" / "claude").is_dir())


def mapping_limit() -> int:
    """Read the Claude adapter's shipped context character limit.

    Returns:
        The `hook_output.context_chars` value from the adapter config.
    """
    return json.loads((ROOT / "adapters" / "claude" / "config.json").read_text())["hook_output"]["context_chars"]


if __name__ == "__main__":
    unittest.main()
