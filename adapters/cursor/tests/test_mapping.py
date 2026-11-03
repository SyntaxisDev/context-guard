import copy
import json
import re
import shlex
import tempfile
import unittest
from pathlib import Path

from adapters.cursor import hooks, mapping
from core import deadline, install, render

NATIVE_EVENTS = ("sessionStart", "beforeSubmitPrompt", "preToolUse", "postToolUse", "stop")


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a rendered agent file into its flat frontmatter and body.

    Args:
        text: The rendered file, starting with a `---` fence.

    Returns:
        Each frontmatter key mapped to its raw value text, and the body after the closing fence.
    """
    head, _, body = text.partition("\n---\n")
    lines = head.splitlines()
    assert lines[0] == "---", text
    return dict(line.split(": ", 1) for line in lines[1:]), body


class MappingCase(unittest.TestCase):
    """A temporary home with the Cursor adapter config and its install spec."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.kit = self.home / ".context-guard" / "kit dir"
        self.python = self.kit / ".venv" / "bin" / "python"
        self.config = hooks.load(self.home, {})
        self.spec = mapping.install_spec(self.home, self.kit, self.python, self.config)


class RenderedFileTests(MappingCase):
    """Rendered agents, policy rule and skills for the Cursor home."""

    def test_agents_carry_only_cursor_frontmatter_with_tier_model_and_readonly(self) -> None:
        for name, role in render.load_roles().items():
            with self.subTest(role=name):
                fields, body = frontmatter(self.spec.files[self.home / ".cursor" / "agents" / f"{name}.md"])
                self.assertEqual(set(fields), {"name", "description", "model", "readonly"})
                self.assertEqual(fields["name"], name)
                self.assertEqual(json.loads(fields["description"]), role.description)
                self.assertEqual(fields["model"], self.config["tiers"][role.default_tier]["call"])
                self.assertEqual(fields["readonly"], "false" if role.capabilities["shell"] else "true")
                self.assertTrue(body.strip())

    def test_models_are_plain_cursor_slugs_for_every_tier_and_variant(self) -> None:
        slug = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")
        role = render.load_roles()["implementer"]
        for tier, profile in self.config["tiers"].items():
            with self.subTest(tier=tier):
                self.assertRegex(profile["call"], slug)
                agent = render.Agent("implementer-long", role, tier, "1m", "Body")
                rendered = mapping.render_agent(agent, profile)
                self.assertEqual(rendered.path, ".cursor/agents/implementer-long.md")
                self.assertEqual(frontmatter(rendered.text)[0]["model"], profile["call"])
        for path, text in self.spec.files.items():
            if path.parent == self.home / ".cursor" / "agents":
                self.assertRegex(frontmatter(text)[0]["model"], slug)

    def test_policy_rule_is_always_applied_and_fully_rendered(self) -> None:
        policy = self.spec.files[self.home / ".cursor" / "rules" / "context-guard.mdc"]
        fields, body = frontmatter(policy)
        self.assertEqual(fields["alwaysApply"], "true")
        self.assertNotRegex(policy, r"\{\{")
        self.assertIn(mapping.OVERRIDE_SYNTAX, body)
        self.assertIn("pass it on every Task", body)
        for profile in self.config["tiers"].values():
            self.assertIn(f"`{profile['call']}`", body)

    def test_skills_are_rendered_and_every_file_stays_in_the_cursor_home(self) -> None:
        for skill in ("handoff", "install-context-guard"):
            text = self.spec.files[self.home / ".cursor" / "skills" / skill / "SKILL.md"]
            self.assertNotRegex(text, r"\{\{")
            self.assertIn("Cursor", text)
        for path in self.spec.files:
            self.assertTrue(path.is_relative_to(self.home / ".cursor"), path)

    def test_role_table_claims_only_readonly_enforcement(self) -> None:
        policy = self.spec.files[self.home / ".cursor" / "rules" / "context-guard.mdc"]
        self.assertIn("| `scout` | read and search only | network, MCP tools, git push / PRs |", policy)
        self.assertIn(
            "| `reviewer` | read, shell, network; no edits (evidence only under /tmp) "
            "| edit files, MCP tools, git push / PRs |",
            policy,
        )


class HooksMergeTests(MappingCase):
    """The hooks.json merge registers each event once, keeps unrelated hooks and reverses exactly."""

    def setUp(self) -> None:
        super().setUp()
        [self.merge] = self.spec.json_merges
        self.unrelated = {"command": "afplay /System/Library/Sounds/Glass.aiff"}
        self.shell_guard = {"command": "./hooks/shell-guard.sh", "timeout": 3}
        self.doc = {
            "version": 1,
            "extra": {"keep": True},
            "hooks": {"stop": [self.unrelated], "beforeShellExecution": [self.shell_guard]},
        }

    def test_merge_registers_each_event_once_and_keeps_unrelated_hooks(self) -> None:
        original = copy.deepcopy(self.doc)
        merged = self.merge.merge(self.doc)
        self.assertEqual(self.doc, original)
        self.assertEqual(self.merge.path, self.home / ".cursor" / "hooks.json")
        script = str(self.kit / "adapters" / "cursor" / "hooks.py")
        for native in NATIVE_EVENTS:
            ours = [entry for entry in merged["hooks"][native] if script in entry["command"]]
            self.assertEqual(len(ours), 1, native)
            self.assertEqual(shlex.split(ours[0]["command"]), [str(self.python), script, native])
            self.assertEqual(ours[0]["timeout"], deadline.HOOK_TIMEOUT_SECONDS)
        self.assertEqual(set(mapping.HOOK_EVENTS), set(NATIVE_EVENTS))
        self.assertEqual(merged["hooks"]["stop"][0], self.unrelated)
        self.assertEqual(merged["hooks"]["beforeShellExecution"], [self.shell_guard])
        self.assertEqual(merged["extra"], {"keep": True})
        self.assertEqual(merged["hooks"]["stop"][1]["loop_limit"], 1)
        self.assertEqual(merged["hooks"]["preToolUse"][0]["matcher"], "Task")
        self.assertNotIn("subagentStart", merged["hooks"])
        self.assertEqual(self.merge.merge(merged), merged)
        self.assertEqual(self.merge.unmerge(merged), self.doc)

    def test_version_is_added_and_a_created_file_unmerges_to_nothing(self) -> None:
        self.assertEqual(self.merge.merge({})["version"], 1)
        self.assertEqual(self.merge.unmerge(self.merge.merge({})), {})
        self.assertEqual(
            self.merge.unmerge({"version": 1, "hooks": {"stop": [self.unrelated]}}),
            {"version": 1, "hooks": {"stop": [self.unrelated]}},
        )
        self.assertEqual(self.merge.unmerge({"version": 1, "extra": 1}), {"version": 1, "extra": 1})


OLD_RUNNER = """---
name: runner
description: Mechanical execution. Use for running tests, lint, dbt parse/compile, long-running commands.
model: claude-haiku-5-5[effort=low]
---

You run commands and report results. You don't change code to make things pass.

- Run what the brief asks, in the given directory, with the given flags (dbt commands need full sandbox permissions).
- Git: only the operations named in the brief. Never force-push, skip hooks, or commit unrelated files.

Return:
- Command(s) and overall pass/fail.
"""
OWN_REVIEWER = "---\nname: reviewer\nmodel: my-model\n---\n\nReview like I do.\n"


class StaleKitOutputTests(MappingCase):
    """Install replaces and backs up outdated kit output and treats unknown content as a conflict."""

    def setUp(self) -> None:
        super().setUp()
        cursor = self.home / ".cursor"
        self.paths = {"runner": cursor / "agents" / "runner.md", "reviewer": cursor / "agents" / "reviewer.md"}
        for key, text in (("runner", OLD_RUNNER), ("reviewer", OWN_REVIEWER)):
            self.paths[key].parent.mkdir(parents=True, exist_ok=True)
            self.paths[key].write_text(text)
        self.manifest_file = install.manifest_path(self.home, mapping.RUNTIME)

    def test_old_kit_output_is_replaced_with_a_diff_and_unknown_content_stays_a_conflict(self) -> None:
        plan = install.plan(self.spec, {})
        stale = {change.path: change for change in plan.changes if change.reason == install.STALE_KIT}
        self.assertEqual(set(stale), {self.paths["runner"]})
        self.assertTrue(all(change.backup for change in stale.values()))
        described = "\n".join(install.describe(plan, dry_run=True))
        self.assertIn("-- Run what the brief asks, in the given directory, with the given flags (dbt", described)
        self.assertEqual([conflict.path for conflict in plan.conflicts], [self.paths["reviewer"]])
        self.assertEqual(self.paths["runner"].read_text(), OLD_RUNNER)

    def test_apply_backs_up_old_kit_output_and_reinstall_is_clean(self) -> None:
        backups = self.home / "backups"
        install.apply(install.plan(self.spec, {}), self.manifest_file, backups)
        self.assertEqual(self.paths["runner"].read_text(), self.spec.files[self.paths["runner"]])
        self.assertEqual(self.paths["reviewer"].read_text(), OWN_REVIEWER)
        saved = {path.read_text() for path in backups.iterdir() if path.name != "paths.jsonl"}
        self.assertEqual(saved, {OLD_RUNNER})
        again = install.plan(self.spec, install.read_manifest(self.manifest_file))
        self.assertEqual([change.path for change in again.changes], [])

    def test_a_role_file_without_the_role_body_is_not_recognised(self) -> None:
        self.paths["runner"].write_text(OLD_RUNNER.replace("You run commands and report results.", "I run things."))
        plan = install.plan(self.spec, {})
        self.assertIn(self.paths["runner"], [conflict.path for conflict in plan.conflicts])


if __name__ == "__main__":
    unittest.main()
