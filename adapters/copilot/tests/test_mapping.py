"""Copilot role rendering and the reversible settings.json agent merge."""

import json
import tempfile
import unittest
from pathlib import Path

from adapters.copilot import hooks, mapping
from core import install, render


class RoleTests(unittest.TestCase):
    """Agent files and per-agent settings follow the configured tiers."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.config = hooks.load_config(self.home, environ={})
        self.runtime = mapping.runtime_mapping(self.config)

    def test_idle_parents_resume_natively_and_no_limit_is_claimed_enforced(self) -> None:
        self.assertEqual(self.runtime.parent_resume, "native")
        for role in render.load_roles().values():
            self.assertEqual(self.runtime.enforced_limits(role.capabilities), frozenset())

    def test_only_a_role_without_shell_or_write_gets_a_tools_allowlist(self) -> None:
        files = {Path(item.path).name: item.text for item in render.render_agents(self.runtime)}
        self.assertIn('tools: ["view", "glob", "grep"]', files["scout.agent.md"])
        self.assertNotIn("\ntools:", files["implementer.agent.md"])
        self.assertIn("Permissions:", files["scout.agent.md"])

    def test_subagent_settings_key_namespaced_plugin_roles_by_effective_name(self) -> None:
        entries = mapping.subagent_settings(self.config, f"{mapping.PLUGIN_NAME}:")
        cheap = self.config["tiers"]["cheap"]
        self.assertEqual(
            entries["context-guard:scout"],
            {"model": cheap["model"], "effortLevel": cheap["effort"], "contextTier": "default"},
        )
        self.assertNotIn("scout", entries)


class SettingsMergeTests(unittest.TestCase):
    """The installer merges and reverses the owned agent entries without touching user keys."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.config = hooks.load_config(self.home, environ={})
        self.settings = self.home / ".copilot" / "settings.json"
        self.spec = mapping.install_spec(self.home, self.home / "kit", Path("/py"), self.config, environ={})

    def test_merge_writes_agent_entries_only_and_uninstall_restores_user_bytes(self) -> None:
        original = '{\n  "theme": "dark",\n  "subagents": {"maxConcurrency": 2}\n}\n'
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(original)
        planned = install.plan(self.spec, {})
        self.assertEqual(self.settings.read_text(), original)
        change = next(item for item in planned.changes if item.path == self.settings)
        merged = json.loads(change.text)
        self.assertEqual(merged["theme"], "dark")
        self.assertEqual(merged["subagents"]["maxConcurrency"], 2)
        names = sorted(agent.name for agent in render.agents(mapping.runtime_mapping(self.config)))
        self.assertEqual(sorted(merged["subagents"]["agents"]), names)
        self.assertEqual(sorted(merged), ["subagents", "theme"])
        self.settings.write_text(change.text)
        removal = install.plan_uninstall(self.spec, planned.manifest)
        restored = next(item for item in removal.changes if item.path == self.settings)
        self.assertEqual(restored.text, original)

    def test_user_entry_is_kept_and_unmerge_removes_only_untouched_owned_entries(self) -> None:
        entries = mapping.subagent_settings(self.config)
        user = {"model": "claude-opus-5.5"}
        data = {"subagents": {"agents": {"scout": user}}}
        merged = mapping.merge_subagents(data, entries)
        self.assertEqual(merged["subagents"]["agents"]["scout"], user)
        restored = mapping.unmerge_subagents(merged, entries)
        self.assertEqual(restored, {"subagents": {"agents": {"scout": user}}})
        self.assertEqual(mapping.unmerge_subagents(mapping.merge_subagents({}, entries), entries), {})

    def test_non_object_subagents_is_left_alone(self) -> None:
        entries = mapping.subagent_settings(self.config)
        self.assertEqual(mapping.merge_subagents({"subagents": []}, entries), {"subagents": []})
        self.assertEqual(mapping.unmerge_subagents({"subagents": []}, entries), {"subagents": []})


if __name__ == "__main__":
    unittest.main()
