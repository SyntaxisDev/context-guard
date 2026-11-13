"""Copilot plugin package: manifest, hook registrations and rendered agents validate through core.package."""

import contextlib
import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path

import cli
from adapters.copilot import hooks, mapping, plugin
from core import kit, package, render

ROOT = Path(__file__).resolve().parents[3]


class PluginSpecTests(unittest.TestCase):
    """The package spec renders a valid plugin directory."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = hooks.load_config(Path(temporary.name), environ={})
        self.spec = plugin.plugin_spec(self.config)
        self.rendered = package.plan(ROOT, mapping.RUNTIME, self.spec)

    def test_manifest_uses_the_github_plugin_location_and_project_version(self) -> None:
        self.assertEqual(self.spec.manifest_file, ".github/plugin/plugin.json")
        manifest = json.loads(self.rendered[self.spec.manifest_file])
        self.assertEqual((manifest["name"], manifest["version"]), (plugin.NAME, kit.source_version()))
        self.assertEqual(manifest["agents"], ["./agents"])
        self.assertIn("skills/handoff/SKILL.md", self.rendered)

    def test_agents_carry_the_tier_model(self) -> None:
        for agent in render.agents(mapping.runtime_mapping(self.config)):
            text = self.rendered[f"agents/{agent.name}.agent.md"]
            self.assertIn(f'model: "{self.config["tiers"][agent.tier]["model"]}"', text)

    def test_hooks_are_flat_bash_handlers_that_pass_the_native_plugin_root(self) -> None:
        data = json.loads(self.rendered[plugin.HOOKS_FILE])
        self.assertEqual(data["version"], 1)
        self.assertEqual(sorted(data["hooks"]), sorted(mapping.HOOK_EVENTS))
        for event, [handler] in data["hooks"].items():
            with self.subTest(event=event):
                self.assertEqual(handler["env"], {plugin.ROOT_VARIABLE: "${PLUGIN_ROOT}"})
                argv = shlex.split(handler["bash"])
                self.assertEqual(argv[0], "sh")
                self.assertEqual(argv[1], f"{plugin.ROOT_TOKEN}/{package.LAUNCHER}")
                self.assertEqual(argv[2:], [mapping.HOOK_SCRIPT, event])
                self.assertNotIn("command", handler)

    def test_package_vendors_the_hook_script_the_commands_run(self) -> None:
        self.assertIn(mapping.HOOK_SCRIPT, self.rendered)
        self.assertIn("adapters/copilot/agents.py", self.rendered)
        self.assertIn(package.LAUNCHER, self.rendered)

    def test_public_cli_packages_plugin_and_local_marketplace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            output = Path(temporary)
            self.assertEqual(
                cli.main(["package", "--runtime", "copilot", "--out", str(output), "--marketplace", "test"]), 0
            )
            listing = json.loads((output / "copilot" / plugin.MARKETPLACE_FILE).read_text())
            self.assertEqual(listing["plugins"][0]["source"], "./plugins/context-guard")
            self.assertTrue((output / "copilot/plugins/context-guard" / plugin.MANIFEST_FILE).is_file())


if __name__ == "__main__":
    unittest.main()
