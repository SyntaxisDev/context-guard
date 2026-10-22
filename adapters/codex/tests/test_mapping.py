"""Codex install spec: agent TOML, config.toml registrations and hooks.json merge."""

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from adapters.codex import hooks, mapping
from core import config as config_mod, install, render

USER_CONFIG = """model = "gpt-6.1-sol"
approval_policy = "on-request"

[agents]
max_threads = 4

[mcp_servers.docs]
command = "docs-mcp"
"""


class SpecCase(unittest.TestCase):
    """A temporary home with the Codex adapter config and its install spec."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.kit = self.home / ".context-guard" / "kit"
        self.python = self.kit / ".venv" / "bin" / "python"
        self.config = config_mod.load_config(config_mod.sources_for("codex", self.home, hooks.ADAPTER_CONFIG))
        self.spec = mapping.install_spec(self.home, self.kit, self.python, self.config)
        self.codex = self.home / ".codex"


class FileTests(SpecCase):
    """Rendered agent TOML files and skills, all inside the Codex home."""

    def test_agents_are_valid_toml_with_tier_models_and_capabilities(self) -> None:
        agents = {
            path.stem: tomllib.loads(text)
            for path, text in self.spec.files.items()
            if path.parent == self.codex / "agents"
        }
        self.assertEqual(set(agents), {"scout", "investigator", "reviewer", "implementer", "runner"})
        self.assertEqual(
            (agents["scout"]["model"], agents["scout"]["model_reasoning_effort"]),
            ("gpt-6-luna", "low"),
        )
        self.assertEqual(agents["reviewer"]["model_reasoning_effort"], "high")
        self.assertEqual(agents["scout"]["features"], {"shell_tool": False})
        self.assertTrue(all("features" not in agents[name] for name in agents if name != "scout"))
        self.assertIn(
            "Do not modify project files",
            agents["investigator"]["developer_instructions"],
        )
        self.assertNotIn("Role limits", agents["runner"]["developer_instructions"])
        policy = mapping.policy_text(self.config)
        self.assertIn(
            "| `scout` | read and search only | edit files, network, MCP tools, git push / PRs |",
            policy,
        )
        self.assertTrue(all(agent["name"] == name and agent["description"] for name, agent in agents.items()))
        self.assertTrue(all("sandbox_mode" not in agent for agent in agents.values()))

    def test_skills_render_and_nothing_targets_another_runtime(self) -> None:
        skills = {path.parent.name for path in self.spec.files if path.parent.parent == self.codex / "skills"}
        self.assertEqual(skills, {"handoff", "install-context-guard"})
        self.assertTrue(all("{{" not in text for text in self.spec.files.values()))
        paths = [
            *self.spec.files,
            *(merge.path for merge in self.spec.json_merges),
            *(edit.path for edit in self.spec.text_edits),
        ]
        self.assertTrue(all(path.is_relative_to(self.codex) for path in paths))


class ConfigTomlTests(SpecCase):
    """The config.toml registration edit keeps user settings and reverses exactly."""

    def setUp(self) -> None:
        super().setUp()
        self.registrations = self.spec.text_edits[0]

    def test_apply_registers_roles_and_preserves_user_settings(self) -> None:
        applied = self.registrations.apply(USER_CONFIG)
        parsed = tomllib.loads(applied)
        self.assertTrue(applied.startswith(USER_CONFIG))
        self.assertEqual(parsed["agents"]["max_threads"], 4)
        self.assertEqual(parsed["mcp_servers"]["docs"]["command"], "docs-mcp")
        self.assertEqual(
            parsed["agents"]["scout"]["config_file"],
            str(self.codex / "agents/scout.toml"),
        )
        self.assertEqual(self.registrations.apply(applied), applied)
        self.assertEqual(self.registrations.revert(applied), USER_CONFIG)
        self.assertEqual(self.registrations.revert(USER_CONFIG), USER_CONFIG)
        self.assertEqual(self.registrations.revert(self.registrations.apply("")), "")

    def test_file_without_final_newline_keeps_none_and_reverts_exactly(self) -> None:
        original = USER_CONFIG.rstrip("\n")
        applied = self.registrations.apply(original)
        self.assertTrue(applied.startswith(original + "\n\n"))
        self.assertFalse(applied.endswith("\n"))
        self.assertEqual(
            tomllib.loads(applied)["agents"]["scout"]["config_file"],
            str(self.codex / "agents/scout.toml"),
        )
        self.assertEqual(self.registrations.apply(applied), applied)
        self.assertEqual(self.registrations.revert(applied), original)

    def test_apply_keeps_user_edits_made_after_install(self) -> None:
        applied = self.registrations.apply(USER_CONFIG) + '\n[profiles.fast]\nmodel = "gpt-6-luna"\n'
        reapplied = self.registrations.apply(applied)
        self.assertEqual(reapplied, applied)
        self.assertEqual(
            tomllib.loads(self.registrations.revert(reapplied))["profiles"]["fast"]["model"],
            "gpt-6-luna",
        )

    def test_conflicts_raise_without_a_result(self) -> None:
        with self.assertRaisesRegex(ValueError, r"\[agents.scout\]"):
            self.registrations.apply(USER_CONFIG + '\n[agents.scout]\ndescription = "mine"\n')
        with self.assertRaisesRegex(ValueError, "not valid TOML"):
            self.registrations.apply("model = \n")
        edited = self.registrations.apply(USER_CONFIG).replace('description = "Mechanical', 'description = "Mine')
        with self.assertRaisesRegex(ValueError, r"(?s)was edited.*\+description = \"Mine"):
            self.registrations.revert(edited)

    def test_tables_codex_writes_inside_the_section_move_after_it(self) -> None:
        trust = (
            '\n[projects."/work"]\ntrust_level = "trusted"\n\n[hooks.state]\n\n'
            '[hooks.state."/x/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:ab"\n'
        )
        installed = self.registrations.apply(USER_CONFIG)
        rewritten = installed.replace(mapping.SECTION_END, trust.lstrip("\n") + mapping.SECTION_END)
        reapplied = self.registrations.apply(rewritten)
        self.assertEqual(tomllib.loads(reapplied), tomllib.loads(rewritten))
        self.assertTrue(reapplied.startswith(installed))
        self.assertEqual(self.registrations.apply(reapplied), reapplied)
        reverted = self.registrations.revert(rewritten)
        self.assertNotIn("context-guard", reverted)
        self.assertEqual(tomllib.loads(reverted), tomllib.loads(USER_CONFIG + trust))


class HooksJsonTests(SpecCase):
    """The hooks.json merge keeps user hooks and reverses exactly."""

    def setUp(self) -> None:
        super().setUp()
        self.merge = self.spec.json_merges[0]
        self.user_hook = {"type": "command", "command": "notify-send done"}
        self.existing = {
            "version": 1,
            "hooks": {
                "Stop": [{"hooks": [self.user_hook]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [self.user_hook]}],
            },
        }

    def commands(self, data: dict) -> dict[str, list[str]]:
        return {
            event: [hook["command"] for group in groups for hook in group["hooks"]]
            for event, groups in data.get("hooks", {}).items()
        }

    def test_merge_preserves_unrelated_hooks(self) -> None:
        merged = self.merge.merge(self.existing)
        commands = self.commands(merged)
        self.assertEqual(merged["version"], 1)
        self.assertEqual(commands["PreToolUse"], ["notify-send done"])
        self.assertEqual(commands["Stop"][0], "notify-send done")
        script = str(self.kit / "adapters/codex/hooks.py")
        for event in mapping.HOOK_EVENTS:
            ours = [command for command in commands[event] if script in command]
            self.assertEqual(ours, [f"{self.python} {script} {event}"])
        session = merged["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(session["additionalContextLimit"], self.config["hook_output"]["session_context_limit"])
        self.assertEqual(session["timeout"], 5)
        self.assertNotIn("additionalContextLimit", merged["hooks"]["Stop"][-1]["hooks"][0])
        self.assertEqual(self.merge.merge(merged), merged)
        self.assertEqual(self.existing["hooks"]["Stop"], [{"hooks": [self.user_hook]}])
        self.assertEqual(self.merge.unmerge(merged), self.existing)

    def test_user_group_with_an_empty_handler_list_survives_install_and_uninstall(self) -> None:
        hooks_json = self.codex / "hooks.json"
        hooks_json.parent.mkdir(parents=True)
        original = json.dumps({"hooks": {"Stop": [{"hooks": []}]}}, indent=4) + "\n"
        hooks_json.write_text(original)
        manifest_file = install.manifest_path(self.home, mapping.RUNTIME)
        install.apply(install.plan(self.spec, {}), manifest_file, self.home / "backups")
        installed = json.loads(hooks_json.read_text())
        self.assertEqual(installed["hooks"]["Stop"][0], {"hooks": []})
        manifest = install.read_manifest(manifest_file)
        self.assertEqual(install.doctor(self.spec, manifest, self.kit, self.python), [])
        install.apply(install.plan_uninstall(self.spec, manifest), manifest_file, self.home / "backups")
        self.assertEqual(hooks_json.read_text(), original)

    def test_reinstall_keeps_one_subagent_stop_registration(self) -> None:
        command = f"{self.python} {self.kit / mapping.HOOK_SCRIPT} SubagentStop"
        stale = {"hooks": [{"type": "command", "command": command}]}
        merged = self.merge.merge({"hooks": {"SubagentStop": [stale]}})
        self.assertEqual(self.commands(merged)["SubagentStop"], [command])
        self.assertEqual(set(merged["hooks"]), set(mapping.HOOK_EVENTS))

    def test_a_command_that_only_mentions_the_hook_script_is_not_ours(self) -> None:
        script = self.kit / mapping.HOOK_SCRIPT
        backup = {"type": "command", "command": f"{self.python} {script}.bak Stop"}
        user = {"hooks": {"Stop": [{"hooks": [backup]}]}}
        self.assertEqual(self.merge.unmerge(self.merge.merge(user)), user)


def toml_agent(name: str, description: str, instructions: str) -> str:
    """Render a Codex agent TOML file as an older kit wrote it.

    Args:
        name: The agent name.
        description: The agent description.
        instructions: The developer instructions body.

    Returns:
        The agent file text.
    """
    return (
        f"name = {json.dumps(name)}\ndescription = {json.dumps(description)}\n"
        f'model = "gpt-5"\nmodel_reasoning_effort = "low"\n'
        f"developer_instructions = {json.dumps(instructions)}\n"
    )


class StaleKitOutputTests(SpecCase):
    """Install replaces outdated kit agents and treats unknown agents as conflicts."""

    def test_old_kit_agents_are_replaced_and_unknown_agents_stay_conflicts(self) -> None:
        roles = render.load_roles()
        agents = self.codex / "agents"
        agents.mkdir(parents=True)
        old = toml_agent("runner", roles["runner"].description, "Old instructions.")
        (agents / "runner.toml").write_text(old)
        (agents / "reviewer.toml").write_text(toml_agent("reviewer", "My own reviewer.", "Review like I do."))
        plan = install.plan(self.spec, {})
        stale = [c.path for c in plan.changes if c.reason == install.STALE_KIT]
        self.assertEqual(stale, [agents / "runner.toml"])
        self.assertEqual([c.path for c in plan.conflicts], [agents / "reviewer.toml"])
        self.assertEqual((agents / "runner.toml").read_text(), old)


if __name__ == "__main__":
    unittest.main()
