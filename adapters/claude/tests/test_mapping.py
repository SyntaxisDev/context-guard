"""Claude Code rendering and install spec against temporary homes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from adapters.claude import hooks, mapping  # noqa: E402
from core import render  # noqa: E402

def frontmatter(text: str) -> dict[str, str]:
    head = text.split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.strip().splitlines())


class MappingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.kit = self.home / ".context-guard" / "kit"
        self.python = self.kit / ".venv" / "bin" / "python"
        self.config = hooks.load_config(self.home, environ={})

    def tearDown(self):
        self.tmp.cleanup()

    def spec(self):
        return mapping.install_spec(self.home, self.kit, self.python, self.config)


class RenderedFileTests(MappingTestCase):
    def test_agent_frontmatter_maps_tiers_and_capabilities(self):
        agents_dir = self.home / ".claude" / "agents"
        agents = {path.stem: frontmatter(text) for path, text in self.spec().files.items()
                  if path.parent == agents_dir}
        self.assertEqual(set(agents), set(render.load_roles()))
        self.assertEqual(agents["scout"], {"name": "scout", "description": agents["scout"]["description"],
                                           "model": '"claude-haiku-5-5"', "effort": "low",
                                           "tools": "Read, Glob, Grep"})
        for role in ("investigator", "reviewer"):
            self.assertEqual(agents[role]["disallowedTools"], "Edit, Write, NotebookEdit")
        self.assertEqual((agents["reviewer"]["model"], agents["reviewer"]["effort"]), ('"opus"', "high"))
        self.assertEqual((agents["investigator"]["model"], agents["investigator"]["effort"]), ('"sonnet"', "medium"))
        self.assertEqual(agents["implementer"]["disallowedTools"], "WebFetch, WebSearch")
        self.assertNotIn("disallowedTools", agents["runner"])
        self.assertNotIn("tools", agents["runner"])
        self.assertTrue(all(json.loads(meta["description"]) for meta in agents.values()))

    def test_policy_rule_and_skills_have_no_unfilled_tokens(self):
        files = self.spec().files
        policy = files[self.home / ".claude" / "rules" / "context-guard.md"]
        self.assertFalse(policy.startswith("---"))
        for heading in ("## Delegate heavy work", "## Watch your own context", "## Handoffs"):
            self.assertIn(heading, policy)
        self.assertIn(mapping.override_syntax(self.config["tiers"]), policy)
        self.assertIn("`haiku` for cheap", policy)
        self.assertIn("| `scout` | read and search only | none | cheap | cheap |", policy)
        self.assertIn("| `reviewer` | read, shell, network; no edits (evidence only under /tmp) "
                      "| edit files, MCP tools, git push / PRs |", policy)
        for name in ("handoff", "install-context-guard"):
            self.assertIn(self.home / ".claude" / "skills" / name / "SKILL.md", files)
        for path, text in files.items():
            with self.subTest(path=path):
                self.assertTrue(path.is_relative_to(self.home / ".claude"))
                self.assertNotRegex(text, r"\{\{")

    def test_incomplete_tier_profile_is_rejected(self):
        del self.config["tiers"]["deep"]["call"]
        with self.assertRaises(render.RenderError):
            self.spec()


class SettingsMergeTests(MappingTestCase):
    def settings(self) -> dict:
        return {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}, "hooks": {
            "PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "prettier"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
            "Notification": [{"hooks": [{"type": "command", "command": "say hi"}]}]}}

    def test_merge_registers_every_event_and_keeps_user_hooks(self):
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
            ours = [hook for group in merged["hooks"][event] for hook in group["hooks"]
                    if str(script) in hook["command"]]
            self.assertEqual(ours, [{"type": "command", "command": f"{self.python} {script} {event}", "timeout": 5}])
        self.assertEqual(merged["hooks"]["PostToolUse"][-1]["matcher"], "*")
        self.assertEqual(merge.merge(merged), merged)

    def test_merge_keeps_user_hooks_that_follow_ours_in_place(self):
        merge = self.spec().json_merges[0]
        merged = merge.merge({})
        merged["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "notify"}]})
        merged["hooks"] = dict(reversed(merged["hooks"].items()))
        self.assertEqual(json.dumps(merge.merge(merged)), json.dumps(merged))

    def test_unmerge_removes_only_context_guard_entries(self):
        merge = self.spec().json_merges[0]
        self.assertEqual(merge.unmerge(merge.merge(self.settings())), self.settings())
        self.assertEqual(merge.unmerge({}), {})
        self.assertEqual(merge.unmerge(merge.merge({"theme": "dark"})), {"theme": "dark"})
        with self.assertRaises(ValueError):
            merge.merge({"hooks": []})


if __name__ == "__main__":
    unittest.main()
