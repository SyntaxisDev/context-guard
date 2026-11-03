"""Cursor plugin package: rendered layout, no hook registrations, and the hooks install step."""

import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cli
from adapters.cursor import hooks, hooks_install, mapping, plugin
from core import install, kit, package, render
from core.install_spec import InstallSpec
from tests import harness

FENCED_COMMAND = re.compile(r"```sh\n(.+)\n```")
USER_HOOKS = '{\n  "version": 1,\n  "hooks": {"stop": [{"command": "say done"}]}\n}\n'


def snapshot(directory: Path) -> dict[str, bytes | None]:
    """Record every path under a directory.

    Args:
        directory: The directory to record.

    Returns:
        Each relative path mapped to its bytes, or None for a directory.
    """
    return {
        str(path.relative_to(directory)): None if path.is_dir() else path.read_bytes()
        for path in sorted(directory.rglob("*"))
    }


class PluginTestCase(unittest.TestCase):
    """A temporary output directory and home for building and launching the Cursor plugin package."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.out = self.base / "out"
        self.home = self.base / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, *extra: str) -> Path:
        out = io.StringIO()
        code = cli.main(["package", "--runtime", "cursor", "--out", str(self.out), *extra], out=out)
        self.assertEqual(code, cli.EXIT_OK, out.getvalue())
        [manifest] = self.out.rglob(plugin.MANIFEST_FILE)
        return manifest.parent.parent

    def installed(self) -> InstallSpec:
        config = hooks.load(self.home, environ={})
        return mapping.install_spec(self.home, self.base / "kit", self.base / "python", config)


class LayoutTests(PluginTestCase):
    """The packaged plugin's manifests and shipped content; it registers no hooks."""

    def test_manifest_and_marketplace_name_the_packaged_plugin(self) -> None:
        root = self.build("--marketplace", "team-tools")
        manifest = json.loads((root / plugin.MANIFEST_FILE).read_text())
        self.assertEqual((manifest["name"], manifest["version"]), (plugin.NAME, kit.source_version()))
        self.assertTrue(manifest["description"])
        [listing_file] = self.out.rglob(plugin.MARKETPLACE_FILE)
        listing = json.loads(listing_file.read_text())
        self.assertEqual((listing["name"], listing["owner"]["name"]), ("team-tools", "team-tools"))
        [entry] = listing["plugins"]
        self.assertEqual(entry["name"], plugin.NAME)
        self.assertEqual((listing_file.parent.parent / entry["source"]).resolve(), root)
        self.assertEqual(entry["version"], manifest["version"])

    def test_plugin_ships_no_hook_registrations_but_keeps_the_launcher_and_hook_code(self) -> None:
        root = self.build()
        self.assertNotIn("hooks", json.loads((root / plugin.MANIFEST_FILE).read_text()))
        self.assertEqual(list(root.rglob("hooks.json")), [])
        for relative in (package.LAUNCHER, mapping.HOOK_SCRIPT, "adapters/cursor/hooks_install.py"):
            with self.subTest(path=relative):
                self.assertTrue((root / relative).is_file())

    def test_policy_rule_is_the_installed_rule_followed_by_the_hooks_install_command(self) -> None:
        root = self.build()
        rule = (root / "rules" / "context-guard.mdc").read_text()
        self.assertIn("\nalwaysApply: true\n", rule.split("---\n")[1])
        installed = self.installed().files[self.home / mapping.POLICY_FILE]
        self.assertEqual(rule, installed + plugin.HOOKS_NOTE)
        self.assertIn(f"`{mapping.SESSION_MARKER}`", rule)
        self.assertIn(f"`~/.context-guard/state/offers/{mapping.PLUGIN_HOOKS}`", rule)
        [command] = FENCED_COMMAND.findall(rule)
        argv = shlex.split(command.replace("<plugin root>", str(root)))
        self.assertEqual(argv[:2], ["sh", str(root / package.LAUNCHER)])
        self.assertTrue((root / argv[2]).is_file(), argv[2])
        self.assertEqual(argv[3:], ["install"])

    def test_agents_keep_plain_role_names_and_the_install_skill_is_not_shipped(self) -> None:
        root = self.build()
        roles = sorted(render.load_roles())
        self.assertEqual(sorted(p.stem for p in (root / "agents").iterdir()), roles)
        for role in roles:
            with self.subTest(role=role):
                self.assertIn(f"\nname: {role}\n", (root / "agents" / f"{role}.md").read_text())
        self.assertEqual([p.name for p in (root / "skills").iterdir()], ["handoff"])
        self.assertTrue((root / "skills" / "handoff" / "SKILL.md").is_file())
        shipped = [p.relative_to(root).parts for p in root.rglob("*")]
        self.assertFalse([parts for parts in shipped if {"tests", "__pycache__", ".venv"} & set(parts)])
        self.assertFalse((root / "adapters" / "claude").exists())


class HooksInstallTests(PluginTestCase):
    """adapters/cursor/hooks_install.py merges our entries into hooks.json and reverses exactly."""

    def setUp(self) -> None:
        super().setUp()
        self.hooks_json = self.home / mapping.HOOKS_FILE
        self.hooks_json.parent.mkdir(parents=True)
        self.hooks_json.write_text(USER_HOOKS)

    def run_install(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        return hooks_install.main([*argv, "--home", str(self.home)], {}, out), out.getvalue()

    def test_install_keeps_user_hooks_and_registers_the_installer_events_through_the_stable_copy(self) -> None:
        self.assertEqual(self.run_install("install")[0], hooks_install.EXIT_OK)
        registered = json.loads(self.hooks_json.read_text())["hooks"]
        self.assertEqual(registered["stop"][0], {"command": "say done"})
        launcher = hooks_install.hooks_dir(self.home) / package.LAUNCHER
        self.assertEqual(launcher.read_text(), package.LAUNCHER_TEXT)
        expected = self.installed().json_merges[0].merge({})["hooks"]
        for event, [entry] in expected.items():
            with self.subTest(event=event):
                ours = registered[event][-1]
                self.assertEqual({**ours, "command": ""}, {**entry, "command": ""})
                self.assertEqual(shlex.split(ours["command"]), ["sh", str(launcher), mapping.HOOK_SCRIPT, event])
        self.assertTrue((hooks_install.hooks_dir(self.home) / mapping.HOOK_SCRIPT).is_file())
        manifest = install.read_manifest(install.manifest_path(self.home, hooks_install.MANIFEST_RUNTIME))
        self.assertIn(str(launcher), manifest["files"])
        self.assertIn(str(self.hooks_json), manifest["merges"])

    def test_rerun_is_up_to_date_and_uninstall_restores_hooks_json_byte_for_byte(self) -> None:
        self.run_install("install")
        after_install = snapshot(self.home)
        code, text = self.run_install("install")
        self.assertEqual((code, text.splitlines()[0]), (hooks_install.EXIT_OK, "[cursor-hooks] up to date"))
        self.assertEqual(snapshot(self.home), after_install)
        (hooks_install.hooks_dir(self.home) / "core" / "__pycache__").mkdir()
        self.assertEqual(self.run_install("uninstall")[0], hooks_install.EXIT_OK)
        self.assertEqual(self.hooks_json.read_text(), USER_HOOKS)
        self.assertFalse(hooks_install.hooks_dir(self.home).exists())
        self.assertFalse(install.manifest_path(self.home, hooks_install.MANIFEST_RUNTIME).exists())

    def test_uninstall_deletes_a_hooks_json_the_install_created(self) -> None:
        self.hooks_json.unlink()
        before = snapshot(self.home)
        self.run_install("install")
        self.assertTrue(self.hooks_json.is_file())
        self.run_install("uninstall")
        backups = install.backup_dir(self.home).parent.relative_to(self.home)
        marker = install.offer_marker(self.home, mapping.PLUGIN_HOOKS).relative_to(self.home)
        kept = {Path(path) for path in snapshot(self.home).keys() - before.keys()}
        expected = (*backups.parents, *marker.parents, marker)
        self.assertTrue(all(path in expected or backups in (path, *path.parents) for path in kept), kept)
        self.assertFalse(self.hooks_json.exists())

    def test_install_writes_the_role_agents_on_tier_models_and_uninstall_removes_exactly_those(self) -> None:
        agents = self.home / mapping.AGENTS_DIR
        agents.mkdir(parents=True)
        (agents / "mine.md").write_text("---\nname: mine\n---\n")
        self.run_install("install")
        config = hooks.load(self.home, {})
        expected = {
            self.home / item.path: item.text
            for item in render.rendered_files(mapping.runtime_mapping(config))
            if item.path.startswith(f"{mapping.AGENTS_DIR}/")
        }
        self.assertTrue(expected)
        for path, text in expected.items():
            self.assertEqual(path.read_text(), text, path)
            self.assertRegex(path.read_text(), r"\nmodel: [a-z0-9][a-z0-9.\-]*\n")
        manifest = install.read_manifest(install.manifest_path(self.home, hooks_install.MANIFEST_RUNTIME))
        self.assertLessEqual({str(path) for path in expected}, set(manifest["files"]))
        self.run_install("uninstall")
        self.assertEqual(sorted(path.name for path in agents.iterdir()), ["mine.md"])

    def script_manifest(self) -> Path:
        return install.manifest_path(self.home, mapping.RUNTIME)

    def install_script(self) -> None:
        plan = install.plan(self.installed(), install.read_manifest(self.script_manifest()))
        install.apply(plan, self.script_manifest(), install.backup_dir(self.home))

    def uninstall_script(self) -> None:
        plan = install.plan_uninstall(self.installed(), install.read_manifest(self.script_manifest()))
        install.apply(plan, self.script_manifest(), install.backup_dir(self.home))

    def role_files(self) -> list[Path]:
        config = hooks.load(self.home, {})
        return [
            self.home / item.path
            for item in render.rendered_files(mapping.runtime_mapping(config))
            if item.path.startswith(f"{mapping.AGENTS_DIR}/")
        ]

    def test_the_script_install_and_the_hooks_install_never_own_the_same_role_file(self) -> None:
        for name, first, second, remove_second in (
            (
                "script then hooks",
                self.install_script,
                lambda: self.run_install("install"),
                lambda: self.run_install("uninstall"),
            ),
            ("hooks then script", lambda: self.run_install("install"), self.install_script, self.uninstall_script),
        ):
            with self.subTest(name):
                shutil.rmtree(self.home / ".context-guard", ignore_errors=True)
                shutil.rmtree(self.home / mapping.AGENTS_DIR, ignore_errors=True)
                first()
                second()
                self.assertFalse(
                    set(install.read_manifest(self.script_manifest())["files"])
                    & set(
                        install.read_manifest(install.manifest_path(self.home, hooks_install.MANIFEST_RUNTIME))["files"]
                    )
                )
                remove_second()
                self.assertTrue(all(path.is_file() for path in self.role_files()))
                if name == "script then hooks":
                    manifest = install.read_manifest(self.script_manifest())
                    self.assertEqual(install.file_problems(self.installed(), manifest), [])
                else:
                    spec = hooks_install.hooks_spec(self.home, hooks_install.KIT, hooks.load(self.home, {}))
                    manifest = install.read_manifest(install.manifest_path(self.home, hooks_install.MANIFEST_RUNTIME))
                    self.assertEqual(install.file_problems(spec, manifest), [])

    def test_install_records_the_offer_and_uninstall_keeps_it(self) -> None:
        marker = install.offer_marker(self.home, mapping.PLUGIN_HOOKS)
        self.run_install("install")
        self.assertTrue(marker.is_file())
        self.run_install("uninstall")
        self.assertTrue(marker.is_file())

    def test_dry_run_writes_nothing(self) -> None:
        before = snapshot(self.home)
        code, text = self.run_install("install", "--dry-run")
        self.assertEqual(code, hooks_install.EXIT_OK)
        self.assertIn("would write", text)
        self.assertIn("Dry run: nothing was written.", text)
        self.assertEqual(snapshot(self.home), before)

    def test_edited_copy_is_kept_and_reported(self) -> None:
        self.run_install("install")
        edited = hooks_install.hooks_dir(self.home) / mapping.HOOK_SCRIPT
        edited.write_text(edited.read_text() + "# mine\n")
        code, text = self.run_install("uninstall")
        self.assertEqual(code, hooks_install.EXIT_ISSUES)
        self.assertIn(f"conflict: {edited}", text)
        self.assertTrue(edited.read_text().endswith("# mine\n"))
        self.assertEqual(self.hooks_json.read_text(), USER_HOOKS)


class PackagedHookTests(PluginTestCase):
    """The rule's command, run from a packaged plugin, registers hooks that outlive the plugin directory."""

    def run_shell(self, command: str, stdin: str, project: Path) -> subprocess.CompletedProcess:
        bin_dir = self.base / "bin"
        if not bin_dir.exists():
            bin_dir.mkdir()
            (bin_dir / "python3").symlink_to(sys.executable)
            (bin_dir / "sh").symlink_to("/bin/sh")
        return subprocess.run(
            ["/bin/sh", "-c", command],
            input=stdin,
            capture_output=True,
            text=True,
            env={
                "HOME": str(self.home),
                "PATH": str(bin_dir),
                "CONTEXT_GUARD_SEARCH_PATH": "",
                "CONTEXT_GUARD_HOME": str(self.home),
            },
            cwd=project,
            timeout=harness.HOOK_SECONDS * 4,
            check=False,
        )

    def test_session_start_lists_the_workspace_handoff_after_the_plugin_is_gone(self) -> None:
        root = self.build()
        project = self.base / "project"
        (project / ".git").mkdir(parents=True)
        [command] = FENCED_COMMAND.findall((root / "rules" / "context-guard.mdc").read_text())
        command = command.replace("<plugin root>", str(root)) + f" --home {shlex.quote(str(self.home))}"
        result = self.run_shell(command, "", project)
        self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout)
        shutil.rmtree(root)
        handoffs = self.home / ".context-guard" / "handoffs"
        handoffs.mkdir(parents=True)
        (handoffs / "task-0123456789abcdef.md").write_text(f"# Packaged task\nStatus: open\nWorkspace: {project}\n")
        [entry] = json.loads((self.home / mapping.HOOKS_FILE).read_text())["hooks"]["sessionStart"]
        payload = json.dumps(harness.payload("cursor", "sessionStart-root", self.home, project))
        result = self.run_shell(entry["command"], payload, project)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        [line] = result.stdout.splitlines()
        self.assertIn("Packaged task", json.loads(line)["additional_context"])
        self.assertTrue((self.home / ".context-guard" / "state" / "cursor").is_dir())


if __name__ == "__main__":
    unittest.main()
