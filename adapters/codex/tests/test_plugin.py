"""Codex plugin package: layout, hook commands, policy choice at SessionStart and the agents command."""

import io
import json
import re
import shlex
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

import cli
from adapters.codex import access, agents, hooks, mapping, plugin
from core import install, kit, package, render
from tests import harness

ROOT = Path(__file__).resolve().parents[3]
FENCED_COMMAND = re.compile(r"```sh\n(.+)\n```")


def run_cli(*argv: str) -> tuple[int, str]:
    """Run the context-guard CLI in process.

    Args:
        *argv: Command-line arguments after the program name.

    Returns:
        The exit code and everything the CLI wrote to its output stream.
    """
    out = io.StringIO()
    return cli.main(list(argv), out=out), out.getvalue()


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
    """A temporary output directory, home and Codex home for building and running the Codex plugin."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.out = self.base / "out"
        self.home = self.base / "home"
        self.codex_home = self.base / "codex-home"
        self.project = self.base / "project"
        for directory in (self.home, self.codex_home, self.project):
            directory.mkdir()

    def build(self, *extra: str) -> Path:
        code, text = run_cli("package", "--runtime", "codex", "--out", str(self.out), *extra)
        self.assertEqual(code, cli.EXIT_OK, text)
        return self.out / "codex" / package.PLUGINS_DIR / plugin.NAME

    def hook_commands(self, root: Path) -> dict[str, str]:
        data = json.loads((root / plugin.HOOKS_FILE).read_text())
        return {
            event: hook["command"]
            for event, groups in data["hooks"].items()
            for group in groups
            for hook in group["hooks"]
        }


class LayoutTests(PluginTestCase):
    """The packaged manifest, marketplace listing, hook commands and rendered content."""

    def test_manifest_names_the_version_and_paths_inside_the_plugin(self) -> None:
        root = self.build()
        manifest = json.loads((root / plugin.MANIFEST_FILE).read_text())
        self.assertEqual((manifest["name"], manifest["version"]), (plugin.NAME, kit.source_version()))
        self.assertEqual(manifest["interface"]["developerName"], "SyntaxDev")
        for key in ("skills", "hooks"):
            with self.subTest(key=key):
                self.assertTrue(manifest[key].startswith("./"))
                target = (root / manifest[key]).resolve()
                self.assertTrue(target.is_relative_to(root) and target.exists(), target)

    def test_only_the_codex_manifest_is_written(self) -> None:
        root = self.build()
        self.assertFalse((root / ".claude-plugin").exists())
        self.assertFalse((root / "plugin.json").exists())

    def test_marketplace_lists_the_plugin_as_a_local_source(self) -> None:
        root = self.build("--marketplace", "team-tools")
        listing = json.loads((self.out / "codex" / plugin.MARKETPLACE_FILE).read_text())
        self.assertEqual((listing["name"], listing["interface"]["displayName"]), ("team-tools", "team-tools"))
        [entry] = listing["plugins"]
        self.assertEqual(entry["name"], plugin.NAME)
        self.assertEqual(entry["source"]["source"], "local")
        self.assertEqual((self.out / "codex" / entry["source"]["path"]).resolve(), root)
        self.assertEqual(entry["policy"], {"installation": "AVAILABLE", "authentication": "ON_INSTALL"})
        self.assertEqual(entry["category"], plugin.CATEGORY)

    def test_every_hook_command_runs_a_packaged_script_through_the_launcher(self) -> None:
        root = self.build()
        commands = self.hook_commands(root)
        self.assertEqual(sorted(commands), sorted(mapping.HOOK_EVENTS))
        for event, command in commands.items():
            with self.subTest(event=event):
                self.assertTrue(command.startswith(f'sh "{plugin.ROOT_TOKEN}/'))
                argv = shlex.split(command.replace(plugin.ROOT_TOKEN, str(root)))
                self.assertEqual(Path(argv[1]), root / package.LAUNCHER)
                self.assertTrue((root / argv[2]).is_file(), argv[2])
                self.assertEqual(argv[3:], [event])

    def test_plugin_registers_the_same_groups_as_an_install(self) -> None:
        def without_commands(groups: dict) -> dict:
            return {
                event: [{"hooks": [{k: v for k, v in h.items() if k != "command"} for h in g["hooks"]]} for g in found]
                for event, found in groups.items()
            }

        packaged = json.loads((self.build() / plugin.HOOKS_FILE).read_text())["hooks"]
        config = hooks.load_config(self.home, environ={})
        spec = mapping.install_spec(self.home, self.base / "kit", self.base / "python", config)
        installed = spec.json_merges[0].merge({})["hooks"]
        self.assertEqual(without_commands(packaged), without_commands(installed))

    def test_package_ships_skills_role_briefs_and_policies_but_no_agents(self) -> None:
        root = self.build()
        self.assertEqual([p.name for p in (root / plugin.SKILLS_DIR).iterdir()], ["handoff"])
        self.assertEqual(
            sorted(p.name for p in (root / plugin.ROLES_DIR).iterdir()),
            sorted(f"{role}.md" for role in render.load_roles()),
        )
        self.assertFalse((root / "agents").exists())
        for name in (plugin.POLICY_FILE, plugin.ROLELESS_POLICY_FILE):
            with self.subTest(policy=name):
                self.assertTrue((root / name).read_text().startswith("# Orchestrator mode (Codex)"))
        self.assertTrue((root / "adapters" / "codex" / "agents.py").is_file())

    def test_rendered_files_have_no_unfilled_tokens(self) -> None:
        root = self.build()
        rendered = [root / plugin.MANIFEST_FILE, root / plugin.HOOKS_FILE, root / plugin.POLICY_FILE]
        rendered += [root / plugin.ROLELESS_POLICY_FILE, *(root / plugin.ROLES_DIR).iterdir()]
        rendered += list((root / plugin.SKILLS_DIR).rglob("*.md"))
        for path in rendered:
            with self.subTest(path=str(path.relative_to(root))):
                self.assertIsNone(render.TOKEN.search(path.read_text()))

    def test_roleless_policy_spawns_the_default_agent_with_the_role_brief(self) -> None:
        root = self.build()
        roleless = (root / plugin.ROLELESS_POLICY_FILE).read_text()
        self.assertIn(f"{plugin.ROOT_TOKEN}/{plugin.ROLES_DIR}/<role>.md", roleless)
        self.assertIn("omit `agent_type`", roleless)
        self.assertIn(plugin.AGENTS_COMMAND, roleless)
        self.assertIn("| `scout` | read and search only | shell, edit files", roleless)
        named = (root / plugin.POLICY_FILE).read_text()
        self.assertIn("as `agent_type`", named)
        self.assertNotIn("agents.py", named)

    def test_role_briefs_hold_every_denied_capability(self) -> None:
        brief = (self.build() / plugin.ROLES_DIR / "scout.md").read_text()
        for limit in plugin.ROLELESS_LIMITS.values():
            with self.subTest(limit=limit):
                self.assertIn(f"- {limit}", brief)


class PackagedSessionCase(PluginTestCase):
    """A built plugin whose packaged SessionStart hook runs with a temporary home and Codex home."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.build()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = {
            "PATH": f"{self.bin}:/bin",
            "CONTEXT_GUARD_SEARCH_PATH": "",
            "CODEX_HOME": str(self.codex_home),
            "PLUGIN_ROOT": str(self.root),
        }

    def session_context(self) -> str:
        command = self.hook_commands(self.root)[mapping.SESSION_START]
        data = harness.payload("codex", "SessionStart-startup", self.home, self.project)
        result = harness.run_hook(command, data, self.home, self.project, self.env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        response = result.response()
        self.assertEqual(harness.response_problems("codex", mapping.SESSION_START, response), [])
        return response["hookSpecificOutput"]["additionalContext"]


class SessionStartTests(PackagedSessionCase):
    """The packaged SessionStart hook picks the policy from the agent TOMLs in the Codex home."""

    def test_without_agent_tomls_the_roleless_policy_names_the_absolute_install_command(self) -> None:
        context = self.session_context()
        self.assertIn(f"`{self.root}/{plugin.ROLES_DIR}/<role>.md`", context)
        self.assertNotIn(plugin.ROOT_TOKEN, context)
        [command] = FENCED_COMMAND.findall(context)
        self.assertEqual(shlex.split(command)[:2], ["sh", f"{self.root}/{package.LAUNCHER}"])

    def test_the_install_command_installs_the_agents_and_switches_the_policy(self) -> None:
        [command] = FENCED_COMMAND.findall(self.session_context())
        result = harness.run_hook(command, "", self.home, self.project, self.env)
        self.assertEqual(result.returncode, agents.EXIT_OK, result.stderr)
        self.assertEqual(
            sorted(p.name for p in (self.codex_home / "agents").iterdir()),
            sorted(f"{role}.toml" for role in render.load_roles()),
        )
        context = self.session_context()
        self.assertIn("as `agent_type`", context)
        self.assertNotIn("Named subagents", context)

    def test_a_missing_role_toml_keeps_the_roleless_policy(self) -> None:
        environ = {"CODEX_HOME": str(self.codex_home)}
        self.assertEqual(agents.main(["install", "--home", str(self.home)], environ, io.StringIO()), agents.EXIT_OK)
        (self.codex_home / "agents" / "scout.toml").unlink()
        self.assertIn("Named subagents", self.session_context())

    def test_without_agents_the_first_main_session_offers_them_once_and_records_the_offer(self) -> None:
        marker = install.offer_marker(
            self.home, mapping.install_identity(self.home, mapping.PLUGIN_AGENTS, {"CODEX_HOME": str(self.codex_home)})
        )
        self.assertIn(hooks.AGENTS_OFFER, self.session_context())
        self.assertTrue(marker.is_file())
        self.assertNotIn(hooks.AGENTS_OFFER, self.session_context())

    def test_an_existing_offer_marker_or_install_manifest_suppresses_the_offer(self) -> None:
        for path in (
            install.offer_marker(
                self.home,
                mapping.install_identity(self.home, mapping.PLUGIN_AGENTS, {"CODEX_HOME": str(self.codex_home)}),
            ),
            install.manifest_path(
                self.home, mapping.install_identity(self.home, mapping.RUNTIME, {"CODEX_HOME": str(self.codex_home)})
            ),
            install.manifest_path(
                self.home,
                mapping.install_identity(self.home, mapping.PLUGIN_AGENTS, {"CODEX_HOME": str(self.codex_home)}),
            ),
        ):
            with self.subTest(path=path.relative_to(self.home)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
                self.assertNotIn(hooks.AGENTS_OFFER, self.session_context())
                path.unlink()

    def test_installed_agents_get_no_offer(self) -> None:
        environ = {"CODEX_HOME": str(self.codex_home)}
        self.assertEqual(agents.main(["install", "--home", str(self.home)], environ, io.StringIO()), agents.EXIT_OK)
        self.assertNotIn(hooks.AGENTS_OFFER, self.session_context())


class HandoffAccessTests(PackagedSessionCase):
    """The packaged SessionStart offers the handoff writable root once; access.py adds and removes it."""

    def setUp(self) -> None:
        super().setUp()
        self.marker = install.offer_marker(
            self.home, mapping.install_identity(self.home, mapping.PLUGIN_ACCESS, {"CODEX_HOME": str(self.codex_home)})
        )
        self.config_toml = self.codex_home / "config.toml"
        self.root_dir = self.home / ".context-guard" / "handoffs"

    def offered(self) -> bool:
        return "adapters/codex/access.py install" in self.session_context()

    def access_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        return access.main([*argv, "--home", str(self.home)], {"CODEX_HOME": str(self.codex_home)}, out), out.getvalue()

    def test_first_main_session_offers_the_absolute_command_once_and_records_the_offer(self) -> None:
        context = self.session_context()
        self.assertIn(f'`sh "{self.root}/{package.LAUNCHER}" adapters/codex/access.py install`', context)
        self.assertTrue(self.marker.is_file())
        self.assertFalse(self.offered())

    def test_install_manifests_marker_disabled_setting_or_present_root_suppress_the_offer(self) -> None:
        cases = {
            "script install": (
                install.manifest_path(
                    self.home,
                    mapping.install_identity(self.home, mapping.RUNTIME, {"CODEX_HOME": str(self.codex_home)}),
                ),
                "{}",
            ),
            "access install": (
                install.manifest_path(
                    self.home,
                    mapping.install_identity(self.home, mapping.PLUGIN_ACCESS, {"CODEX_HOME": str(self.codex_home)}),
                ),
                "{}",
            ),
            "marker": (self.marker, ""),
            "disabled": (self.home / ".context-guard" / "config.json", '{"handoff_access": {"enabled": false}}'),
            "root present": (self.config_toml, mapping.access_block(self.root_dir)),
            "unparsable config.toml": (self.config_toml, "model = \n"),
        }
        for name, (path, text) in cases.items():
            with self.subTest(case=name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                self.assertFalse(self.offered())
                path.unlink()
                self.marker.unlink(missing_ok=True)

    def test_access_command_adds_the_root_and_uninstall_restores_config_toml(self) -> None:
        original = 'model = "mine"'
        self.config_toml.write_text(original)
        code, text = self.access_cli("install", "--dry-run")
        self.assertEqual(code, access.EXIT_OK)
        self.assertIn("would write", text)
        self.assertEqual((self.config_toml.read_text(), self.marker.exists()), (original, False))
        self.assertEqual(self.access_cli("install")[0], access.EXIT_OK)
        self.assertTrue(mapping.has_root(tomllib.loads(self.config_toml.read_text()), self.root_dir))
        self.assertTrue(self.marker.is_file())
        self.assertEqual(self.access_cli("uninstall")[0], access.EXIT_OK)
        self.assertEqual(self.config_toml.read_text(), original)

    def test_foreign_writable_roots_are_reported_and_left_alone(self) -> None:
        original = '[sandbox_workspace_write]\nwritable_roots = ["/srv"]\n'
        self.config_toml.write_text(original)
        code, text = self.access_cli("install")
        self.assertEqual(code, access.EXIT_OK)
        self.assertIn(f"note: {self.config_toml}: ", text)
        self.assertEqual(self.config_toml.read_text(), original)


class AgentsCommandTests(PluginTestCase):
    """adapters/codex/agents.py writes only the role TOMLs, as the installer renders them, and reverses cleanly."""

    def setUp(self) -> None:
        super().setUp()
        self.agents = self.home / mapping.AGENTS_DIR
        self.agents.mkdir(parents=True)
        (self.agents / "mine.toml").write_text('name = "mine"\n')
        (self.home / mapping.CODEX_DIR / "config.toml").write_text('model = "mine"\n')

    def agents_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        return agents.main([*argv, "--home", str(self.home)], {}, out), out.getvalue()

    def test_install_renders_the_installer_agent_files_and_uninstall_restores_the_home(self) -> None:
        before = snapshot(self.home)
        self.assertEqual(self.agents_cli("install")[0], agents.EXIT_OK)
        config = hooks.load_config(self.home, environ={})
        spec = mapping.install_spec(self.home, self.base / "kit", self.base / "python", config)
        expected = {path: text for path, text in spec.files.items() if path.parent == self.agents}
        self.assertEqual(len(expected), len(render.load_roles()))
        for path, text in expected.items():
            with self.subTest(path=path.name):
                self.assertEqual(path.read_text(), text)
        self.assertEqual((self.home / mapping.CODEX_DIR / "config.toml").read_text(), 'model = "mine"\n')
        self.assertEqual(self.agents_cli("uninstall")[0], agents.EXIT_OK)
        marker = install.offer_marker(self.home, mapping.PLUGIN_AGENTS)
        offers = {str(p.relative_to(self.home)): None for p in marker.parents if p.is_relative_to(self.home)}
        offers.pop(".")
        self.assertEqual(snapshot(self.home), {**before, **offers, str(marker.relative_to(self.home)): b""})

    def test_install_records_the_offer_so_it_is_never_made(self) -> None:
        self.assertEqual(self.agents_cli("install")[0], agents.EXIT_OK)
        self.assertTrue(install.offer_marker(self.home, mapping.PLUGIN_AGENTS).is_file())

    def test_dry_run_writes_nothing(self) -> None:
        before = snapshot(self.home)
        code, text = self.agents_cli("install", "--dry-run")
        self.assertEqual(code, agents.EXIT_OK)
        self.assertIn("would write", text)
        self.assertEqual(snapshot(self.home), before)

    def test_user_edited_agent_is_kept_and_reported(self) -> None:
        self.agents_cli("install")
        edited = self.agents / "scout.toml"
        edited.write_text(edited.read_text() + "# mine\n")
        code, text = self.agents_cli("uninstall")
        self.assertEqual(code, agents.EXIT_ISSUES)
        self.assertIn(f"conflict: {edited}", text)
        self.assertTrue(edited.read_text().endswith("# mine\n"))
        self.assertEqual(sorted(p.name for p in self.agents.iterdir()), ["mine.toml", "scout.toml"])

    def test_foreign_file_with_a_role_name_is_not_replaced(self) -> None:
        foreign = self.agents / "scout.toml"
        foreign.write_text('name = "scout"\ndescription = "my own"\n')
        code, text = self.agents_cli("install")
        self.assertEqual(code, agents.EXIT_ISSUES)
        self.assertIn(f"conflict: {foreign}", text)
        self.assertEqual(foreign.read_text(), 'name = "scout"\ndescription = "my own"\n')

    def test_codex_home_environment_selects_the_agents_directory(self) -> None:
        code = agents.main(["install", "--home", str(self.home)], {"CODEX_HOME": str(self.codex_home)}, io.StringIO())
        self.assertEqual(code, agents.EXIT_OK)
        self.assertTrue((self.codex_home / "agents" / "scout.toml").is_file())
        self.assertFalse((self.agents / "scout.toml").exists())


if __name__ == "__main__":
    unittest.main()
