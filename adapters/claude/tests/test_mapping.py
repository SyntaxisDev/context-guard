"""Claude Code rendering and install spec against temporary homes."""

import json
import tempfile
import unittest
from pathlib import Path

from adapters.claude import hooks, mapping
from core import install, render
from core.install_spec import InstallSpec


def frontmatter(text: str) -> dict[str, str]:
    """Parse the flat YAML frontmatter of a rendered agent file.

    Args:
        text: The rendered file, starting with a `---` fence.

    Returns:
        Each frontmatter key mapped to its raw value text.
    """
    head = text.split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.strip().splitlines())


class MappingTestCase(unittest.TestCase):
    """A temporary home with the Claude adapter config and its install spec."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.kit = self.home / ".context-guard" / "kit"
        self.python = self.kit / ".venv" / "bin" / "python"
        self.config = hooks.load_config(self.home, environ={})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def spec(self) -> InstallSpec:
        return mapping.install_spec(self.home, self.kit, self.python, self.config)


class RenderedFileTests(MappingTestCase):
    """Rendered agents, policy rule and skills for the Claude home."""

    def test_agent_frontmatter_maps_tiers_and_capabilities(self) -> None:
        agents_dir = self.home / ".claude" / "agents"
        agents = {path.stem: frontmatter(text) for path, text in self.spec().files.items() if path.parent == agents_dir}
        self.assertEqual(set(agents), set(render.load_roles()))
        self.assertEqual(
            agents["scout"],
            {
                "name": "scout",
                "description": agents["scout"]["description"],
                "model": '"claude-haiku-5-5"',
                "effort": "low",
                "tools": "Read, Glob, Grep",
            },
        )
        for role in ("investigator", "reviewer"):
            self.assertEqual(agents[role]["disallowedTools"], "Edit, Write, NotebookEdit")
        self.assertEqual((agents["reviewer"]["model"], agents["reviewer"]["effort"]), ('"opus"', "high"))
        self.assertEqual((agents["investigator"]["model"], agents["investigator"]["effort"]), ('"sonnet"', "medium"))
        self.assertEqual(agents["implementer"]["disallowedTools"], "WebFetch, WebSearch")
        self.assertNotIn("disallowedTools", agents["runner"])
        self.assertNotIn("tools", agents["runner"])
        self.assertTrue(all(json.loads(meta["description"]) for meta in agents.values()))

    def test_policy_rule_and_skills_have_no_unfilled_tokens(self) -> None:
        files = self.spec().files
        policy = files[self.home / ".claude" / "rules" / "context-guard.md"]
        self.assertFalse(policy.startswith("---"))
        for heading in ("## Delegate heavy work", "## Watch your own context", "## Handoffs"):
            self.assertIn(heading, policy)
        self.assertIn(mapping.override_syntax(self.config["tiers"]), policy)
        self.assertIn("`haiku` for cheap", policy)
        self.assertIn("| `scout` | read and search only | none | cheap | cheap |", policy)
        self.assertIn(
            "| `reviewer` | read, shell, network; no edits (evidence only under /tmp) "
            "| edit files, MCP tools, git push / PRs |",
            policy,
        )
        for name in ("handoff", "install-context-guard"):
            self.assertIn(self.home / ".claude" / "skills" / name / "SKILL.md", files)
        for path, text in files.items():
            with self.subTest(path=path):
                self.assertTrue(path.is_relative_to(self.home / ".claude"))
                self.assertNotRegex(text, r"\{\{")

    def test_incomplete_tier_profile_is_rejected(self) -> None:
        del self.config["tiers"]["deep"]["call"]
        with self.assertRaises(render.RenderError):
            self.spec()


class SettingsMergeTests(MappingTestCase):
    """The settings.json hook merge keeps user settings and hooks and reverses exactly."""

    def settings(self) -> dict:
        return {
            "model": "opus",
            "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {
                "PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "prettier"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
                "Notification": [{"hooks": [{"type": "command", "command": "say hi"}]}],
            },
        }

    def test_merge_registers_every_event_and_keeps_user_hooks(self) -> None:
        original = self.settings()
        merge = self.spec().json_merges[0]
        self.assertEqual(merge.path, self.home / ".claude" / "settings.json")
        merged = merge.merge(original)
        self.assertEqual(original, self.settings())
        self.assertEqual((merged["model"], merged["permissions"]), ("opus", {"allow": ["Bash(ls)"]}))
        self.assertEqual(merged["hooks"]["Notification"], original["hooks"]["Notification"])
        self.assertEqual(merged["hooks"]["PostToolUse"][0], original["hooks"]["PostToolUse"][0])
        self.assertEqual(merged["hooks"]["Stop"][0]["hooks"], [{"type": "command", "command": "notify"}])
        script = self.kit / "adapters" / "claude" / "hooks.py"
        for event in mapping.HOOK_EVENTS:
            ours = [
                hook for group in merged["hooks"][event] for hook in group["hooks"] if str(script) in hook["command"]
            ]
            self.assertEqual(ours, [{"type": "command", "command": f"{self.python} {script} {event}", "timeout": 5}])
        self.assertEqual(merged["hooks"]["PostToolUse"][-1]["matcher"], "*")
        self.assertEqual(merge.merge(merged), merged)
        self.assertEqual(merge.unmerge(merged), self.settings())

    def test_a_command_that_only_mentions_the_hook_script_is_not_ours(self) -> None:
        merge = self.spec().json_merges[0]
        script = self.kit / mapping.HOOK_SCRIPT
        user = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"{self.python} {script}.bak Stop"}]}]}}
        self.assertEqual(merge.unmerge(merge.merge(user)), user)

    def test_an_event_that_is_not_a_list_is_a_conflict_for_install_and_doctor(self) -> None:
        spec = self.spec()
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        original = json.dumps({"hooks": {"Stop": {}}})
        settings.write_text(original)
        plan = install.plan(spec, {})
        self.assertEqual([conflict.path for conflict in plan.conflicts], [settings])
        self.assertNotIn(settings, [change.path for change in plan.changes])
        install.apply(plan, install.manifest_path(self.home, mapping.RUNTIME), self.home / "backups")
        self.assertEqual(settings.read_text(), original)
        manifest = install.read_manifest(install.manifest_path(self.home, mapping.RUNTIME))
        problems = install.doctor(spec, manifest, self.kit, self.python)
        self.assertEqual(
            [problem for problem in problems if "settings.json" in problem],
            ["settings.json: hooks must map event names to lists"],
        )


OLD_RUNNER = """---
name: runner
description: "Mechanical execution."
model: "haiku"
---

You run commands and report results. You don't change code to make things pass.
"""
OWN_REVIEWER = "---\nname: reviewer\nmodel: opus\n---\n\nReview like I do.\n"


class StaleKitOutputTests(MappingTestCase):
    """Install replaces outdated kit agents and treats unknown agents as conflicts."""

    def test_old_kit_agents_are_replaced_and_unknown_agents_stay_conflicts(self) -> None:
        agents = self.home / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "runner.md").write_text(OLD_RUNNER)
        (agents / "reviewer.md").write_text(OWN_REVIEWER)
        plan = install.plan(self.spec(), {})
        self.assertEqual([c.path for c in plan.changes if c.reason == install.STALE_KIT], [agents / "runner.md"])
        self.assertEqual([c.path for c in plan.conflicts], [agents / "reviewer.md"])

    def test_an_installed_agent_the_user_edited_stays_a_conflict(self) -> None:
        spec = self.spec()
        manifest_path = install.manifest_path(self.home, mapping.RUNTIME)
        install.apply(install.plan(spec, {}), manifest_path, self.home / "backups")
        scout = self.home / ".claude" / "agents" / "scout.md"
        scout.write_text(scout.read_text().replace("\nmodel: ", "\nmodel: opus # ", 1))
        plan = install.plan(spec, install.read_manifest(manifest_path))
        self.assertNotIn(scout, [change.path for change in plan.changes])
        self.assertEqual([conflict.path for conflict in plan.conflicts], [scout])


if __name__ == "__main__":
    unittest.main()
