"""Codex install spec: agent TOML, config.toml registrations and hooks.json merge."""

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from adapters.codex import hooks, mapping
from core import config as config_mod

USER_CONFIG = """model = "gpt-6.1-sol"
approval_policy = "on-request"

[agents]
max_threads = 4

[mcp_servers.docs]
command = "docs-mcp"
"""


class SpecCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.kit = self.home / ".context-guard" / "kit"
        self.python = self.kit / ".venv" / "bin" / "python"
        self.config = config_mod.load_config(
            config_mod.sources_for("codex", self.home, hooks.ADAPTER_CONFIG)
        )
        self.spec = mapping.install_spec(self.home, self.kit, self.python, self.config)
        self.codex = self.home / ".codex"



class FileTests(SpecCase):
    def test_agents_are_valid_toml_with_tier_models_and_capabilities(self):
        agents = {
            path.stem: tomllib.loads(text)
            for path, text in self.spec.files.items()
            if path.parent == self.codex / "agents"
        }
        self.assertEqual(
            set(agents), {"scout", "investigator", "reviewer", "implementer", "runner"}
        )
        self.assertEqual(
            (agents["scout"]["model"], agents["scout"]["model_reasoning_effort"]),
            ("gpt-6-luna", "low"),
        )
        self.assertEqual(agents["reviewer"]["model_reasoning_effort"], "high")
        self.assertEqual(agents["scout"]["features"], {"shell_tool": False})
        self.assertTrue(
            all("features" not in agents[name] for name in agents if name != "scout")
        )
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
        self.assertTrue(
            all(
                agent["name"] == name and agent["description"]
                for name, agent in agents.items()
            )
        )
        self.assertTrue(all("sandbox_mode" not in agent for agent in agents.values()))

    def test_skills_render_and_nothing_targets_another_runtime(self):
        skills = {
            path.parent.name
            for path in self.spec.files
            if path.parent.parent == self.codex / "skills"
        }
        self.assertEqual(skills, {"handoff", "install-context-guard"})
        self.assertTrue(all("{{" not in text for text in self.spec.files.values()))
        paths = [
            *self.spec.files,
            *(merge.path for merge in self.spec.json_merges),
            *(edit.path for edit in self.spec.text_edits),
        ]
        self.assertTrue(all(path.is_relative_to(self.codex) for path in paths))


class ConfigTomlTests(SpecCase):
    def setUp(self):
        super().setUp()
        self.registrations = self.spec.text_edits[0]

    def test_apply_registers_roles_and_preserves_user_settings(self):
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

    def test_file_without_final_newline_keeps_none_and_reverts_exactly(self):
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

    def test_apply_keeps_user_edits_made_after_install(self):
        applied = (
            self.registrations.apply(USER_CONFIG)
            + '\n[profiles.fast]\nmodel = "gpt-6-luna"\n'
        )
        reapplied = self.registrations.apply(applied)
        self.assertEqual(reapplied, applied)
        self.assertEqual(
            tomllib.loads(self.registrations.revert(reapplied))["profiles"]["fast"][
                "model"
            ],
            "gpt-6-luna",
        )

    def test_conflicts_raise_without_a_result(self):
        with self.assertRaisesRegex(ValueError, r"\[agents.scout\]"):
            self.registrations.apply(
                USER_CONFIG + '\n[agents.scout]\ndescription = "mine"\n'
            )
        with self.assertRaisesRegex(ValueError, "not valid TOML"):
            self.registrations.apply("model = \n")
        edited = self.registrations.apply(USER_CONFIG).replace(
            'description = "Mechanical', 'description = "Mine'
        )
        with self.assertRaisesRegex(
            ValueError, r"(?s)was edited.*\+description = \"Mine"
        ):
            self.registrations.revert(edited)

    def test_tables_codex_writes_inside_the_section_move_after_it(self):
        trust = (
            '\n[projects."/work"]\ntrust_level = "trusted"\n\n[hooks.state]\n\n'
            '[hooks.state."/x/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:ab"\n'
        )
        installed = self.registrations.apply(USER_CONFIG)
        rewritten = installed.replace(
            mapping.SECTION_END, trust.lstrip("\n") + mapping.SECTION_END
        )
        reapplied = self.registrations.apply(rewritten)
        self.assertEqual(tomllib.loads(reapplied), tomllib.loads(rewritten))
        self.assertTrue(reapplied.startswith(installed))
        self.assertEqual(self.registrations.apply(reapplied), reapplied)
        reverted = self.registrations.revert(rewritten)
        self.assertNotIn("context-guard", reverted)
        self.assertEqual(tomllib.loads(reverted), tomllib.loads(USER_CONFIG + trust))


class HooksJsonTests(SpecCase):
    def setUp(self):
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

    def test_merge_preserves_unrelated_hooks(self):
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
        self.assertEqual(
            session["additionalContextLimit"], self.config["additional_context_limit"]
        )
        self.assertEqual(session["timeout"], 5)
        self.assertNotIn(
            "additionalContextLimit", merged["hooks"]["Stop"][-1]["hooks"][0]
        )
        self.assertEqual(self.merge.merge(merged), merged)
        self.assertEqual(self.existing["hooks"]["Stop"], [{"hooks": [self.user_hook]}])

    def test_merge_keeps_user_hooks_that_follow_ours_in_place(self):
        merged = self.merge.merge({})
        merged["hooks"]["Stop"].append({"hooks": [self.user_hook]})
        merged["hooks"] = dict(reversed(merged["hooks"].items()))
        self.assertEqual(json.dumps(self.merge.merge(merged)), json.dumps(merged))

    def test_unmerge_removes_only_ours(self):
        merged = self.merge.merge({"hooks": {"Stop": [{"hooks": [self.user_hook]}]}})
        self.assertEqual(
            self.merge.unmerge(merged),
            {"hooks": {"Stop": [{"hooks": [self.user_hook]}]}},
        )
        self.assertEqual(self.merge.unmerge(self.merge.merge({})), {})

    def test_malformed_hooks_are_a_conflict(self):
        with self.assertRaises(ValueError):
            self.merge.merge({"hooks": []})
        with self.assertRaises(ValueError):
            self.merge.merge({"hooks": {"Stop": {"not": "a list"}}})


if __name__ == "__main__":
    unittest.main()
