"""Real installer ownership and explicit plugin preference helper contracts."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.copilot import agents, hooks, mapping, tools
from adapters.copilot.tests import test_hooks
from core import install


class ProfileTests(unittest.TestCase):
    """Regenerated specs preserve original settings and edited preferences."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.config = hooks.load_config(self.home, environ={})
        self.settings = self.home / ".copilot/settings.json"
        self.settings.parent.mkdir()
        self.manifest = install.manifest_path(self.home, "copilot")

    def spec(self) -> install.InstallSpec:
        """Regenerate a script spec from persisted ownership.

        Returns:
            The Copilot script installation spec.
        """
        return mapping.install_spec(self.home, self.home / "kit", Path("/py"), self.config, environ={})

    def apply(self, *, uninstall: bool = False) -> None:
        """Apply a regenerated installation or removal plan.

        Args:
            uninstall: Whether to reverse the existing installation.
        """
        planner = install.plan_uninstall if uninstall else install.plan
        result = planner(self.spec(), install.read_manifest(self.manifest))
        self.assertEqual(result.conflicts, [])
        install.apply(result, self.manifest, self.home / "backups")

    def test_reinstall_and_uninstall_restore_preexisting_entries_and_empty_containers(self) -> None:
        matching = mapping.subagent_settings(self.config)["scout"]
        for initial in (
            {"subagents": {"agents": {"scout": matching}}},
            {"subagents": {"agents": {"scout": {"model": "custom"}}}},
            {"subagents": {"agents": {}}},
            {"subagents": {}},
            {},
        ):
            with self.subTest(initial=initial):
                original = json.dumps(initial, indent=4) + "\n\n"
                self.settings.write_text(original)
                self.apply()
                self.apply()
                self.apply(uninstall=True)
                self.assertEqual(self.settings.read_text(), original)

    def test_uninstall_preserves_user_edits_and_unrelated_keys(self) -> None:
        self.settings.write_text('{"theme": "dark"}\n')
        self.apply()
        data = json.loads(self.settings.read_text())
        data["subagents"]["agents"]["scout"]["model"] = "custom"
        data["new"] = True
        self.settings.write_text(json.dumps(data))
        self.apply()
        self.apply(uninstall=True)
        self.assertEqual(
            json.loads(self.settings.read_text()),
            {
                "theme": "dark",
                "new": True,
                "subagents": {"agents": {"scout": data["subagents"]["agents"]["scout"]}},
            },
        )

    def test_helper_dry_run_and_real_round_trip_touch_only_namespaced_preferences(self) -> None:
        original = '{ "theme": "dark", "subagents": {"agents": {}} }\n'
        self.settings.write_text(original)
        for command in ("install", "uninstall"):
            self.assertEqual(agents.main([command, "--home", str(self.home), "--dry-run"], {}, io.StringIO()), 0)
            self.assertEqual(self.settings.read_text(), original)
        self.assertFalse((self.home / ".context-guard").exists())
        for command in ("install", "install", "uninstall"):
            self.assertEqual(agents.main([command, "--home", str(self.home)], {}, io.StringIO()), 0)
            if command == "install":
                data = json.loads(self.settings.read_text())
                self.assertEqual(set(data), {"theme", "subagents"})
                self.assertTrue(all(name.startswith("context-guard:") for name in data["subagents"]["agents"]))
                self.assertFalse((self.settings.parent / "agents").exists())
                self.assertFalse((self.settings.parent / "hooks").exists())
        self.assertEqual(self.settings.read_text(), original)

    def test_namespace_normalization_and_policy_are_specific_to_our_plugin(self) -> None:
        self.assertEqual(tools.role_name("context-guard:reviewer120k"), "reviewer120k")
        self.assertEqual(tools.role_name("other:reviewer120k"), "other:reviewer120k")
        with patch.dict("os.environ", {"CONTEXT_GUARD_PLUGIN_ROOT": "/package"}):
            policy = hooks.session_policy(self.config)
            self.assertIn("`context-guard:reviewer`", policy)
            self.assertNotIn("`reviewer`", policy)
        with patch.dict("os.environ", {}, clear=True):
            self.assertIn("`reviewer`", hooks.session_policy(self.config))

    def test_helper_profile_preserves_matching_entry_empty_containers_and_edits(self) -> None:
        profile = self.home / "profile"
        profile.mkdir()
        settings = profile / "settings.json"
        environment = {"COPILOT_HOME": str(profile)}
        matching = mapping.subagent_settings(self.config, "context-guard:")["context-guard:scout"]
        for initial in (
            {"subagents": {"agents": {"context-guard:scout": matching}}},
            {"subagents": {"agents": {"context-guard:scout": {"model": "custom"}}}},
            {"subagents": {"agents": {}}},
            {"subagents": {}},
        ):
            original = json.dumps(initial, indent=4) + "\n\n"
            settings.write_text(original)
            for command in ("install", "install", "uninstall"):
                self.assertEqual(agents.main([command, "--home", str(self.home)], environment, io.StringIO()), 0)
            self.assertEqual(settings.read_text(), original)
        settings.write_text("{}")
        self.assertEqual(agents.main(["install", "--home", str(self.home)], environment, io.StringIO()), 0)
        data = json.loads(settings.read_text())
        data["subagents"]["agents"]["context-guard:scout"]["model"] = "custom"
        settings.write_text(json.dumps(data))
        self.assertEqual(agents.main(["uninstall", "--home", str(self.home)], environment, io.StringIO()), 0)
        self.assertEqual(
            json.loads(settings.read_text()),
            {
                "subagents": {"agents": {"context-guard:scout": data["subagents"]["agents"]["context-guard:scout"]}},
            },
        )


class PluginLeafTests(test_hooks.HookTests):
    """Recorded hook inputs retain the plugin leaf role's own budget."""

    def test_plugin_reviewer120k_uses_leaf_budget(self) -> None:
        start = self.payload(
            "subagentStart-general-purpose",
            agentId="child",
            parentAgentId="root",
            agentType="context-guard:reviewer120k",
        )
        self.fire("subagentStart", start)
        self.usage(100000, "child")
        tool = self.payload("postToolUse-bash-subagent")
        tool["sessionId"] = "child"
        response = self.fire("postToolUse", tool)
        self.assertIn("budget", response["additionalContext"].lower())
