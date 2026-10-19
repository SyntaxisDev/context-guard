"""Rendering parity: shipped content differs across runtimes only in declared slots."""

import shutil
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from core import render
from core.render import Agent, RenderedFile, RenderError, RuntimeMapping


def fake_mapping(name: str, channel: str = "rule_file") -> RuntimeMapping:
    def describe(profile: Mapping[str, str]) -> str:
        return f"`{profile['model']}`"

    def agent_file(agent: Agent, profile: Mapping[str, str]) -> RenderedFile:
        header = f"name: {agent.name}\nmodel: {profile['model']}\nshell: {agent.role.capabilities['shell']}\n"
        return RenderedFile(f".{name}/agents/{agent.name}.md", header + "\n" + agent.body)

    return RuntimeMapping(
        runtime=name,
        runtime_name=f"Runtime-{name}",
        handoff_dir=f"/stores/{name}/handoffs",
        tiers={tier: {"model": f"{name}-{tier}-model"} for tier in ("cheap", "standard", "deep")},
        override_syntax=f"the `{name}` spawn argument",
        transcripts=f"{name} keeps transcripts somewhere.",
        channel=channel,
        describe_tier=describe,
        render_agent=agent_file,
        skills_dir=f".{name}/skills",
        policy_file=f".{name}/rules/context-guard.md" if channel == "rule_file" else None,
        wrap_policy=lambda text: "---\nalwaysApply: true\n---\n" + text if name == "alpha" else text,
    )


MAPPINGS = (fake_mapping("alpha"), fake_mapping("beta"), fake_mapping("gamma", "session_context"))


class ParityTests(unittest.TestCase):
    """Rendered content differs across runtimes only in declared slots and channel."""

    def masked(self, text: str, mapping: RuntimeMapping) -> str:
        values = render.slot_values(mapping)
        for slot in sorted(render.RUNTIME_SLOTS, key=lambda slot: -len(values[slot])):
            text = text.replace(values[slot], "<" + slot + ">")
        for profile in mapping.tiers.values():
            text = text.replace(profile["model"], "<model>")
        return text

    def test_policy_skills_and_role_bodies_differ_only_in_slots(self) -> None:
        outputs = [
            (
                render.render_policy(mapping),
                render.render_skills(mapping),
                [agent.body for agent in render.agents(mapping)],
            )
            for mapping in MAPPINGS
        ]
        masked = [
            (
                self.masked(policy, mapping),
                {name: self.masked(text, mapping) for name, text in skills.items()},
                [self.masked(body, mapping) for body in bodies],
            )
            for (policy, skills, bodies), mapping in zip(outputs, MAPPINGS, strict=True)
        ]
        self.assertEqual(masked[0], masked[1])
        self.assertEqual(masked[0], masked[2])
        self.assertNotEqual(outputs[0][0], outputs[1][0])
        self.assertEqual(set(outputs[0][1]), {"handoff", "install-context-guard"})

    def test_content_uses_only_declared_slots_and_renders_without_other_runtimes(self) -> None:
        for path in render.CONTENT_DIR.rglob("*.md"):
            with self.subTest(path=str(path.relative_to(render.CONTENT_DIR))):
                tokens = {match[1] for match in render.TOKEN.finditer(path.read_text())}
                self.assertLessEqual(tokens, render.RUNTIME_SLOTS)
        for mapping in MAPPINGS:
            others = [other.runtime for other in MAPPINGS if other is not mapping]
            for file in render.rendered_files(mapping):
                with self.subTest(runtime=mapping.runtime, path=file.path):
                    self.assertNotIn("{{", file.text)
                    for other in others:
                        self.assertNotIn(other, file.text.lower())

    def test_policy_has_subagent_line_role_and_tier_tables(self) -> None:
        policy = render.render_policy(MAPPINGS[0])
        for heading in ("## Delegate heavy work", "## Watch your own context", "## Handoffs"):
            self.assertIn(heading, policy)
        self.assertIn(MAPPINGS[0].override_syntax, policy)
        rows = {
            line.split("|")[1].strip(" `"): [cell.strip() for cell in line.strip("|\n").split("|")]
            for line in policy.splitlines()
            if line.startswith("| `")
        }
        for name, role in render.load_roles().items():
            with self.subTest(role=name):
                self.assertEqual(rows[name][0].strip("`"), name)
                self.assertEqual(rows[name][-2:], [role.default_tier, ", ".join(role.allowed_tiers)])
        self.assertIn("| deep | `alpha-deep-model` |", policy)
        self.assertIn("the `alpha` spawn argument", policy)

    def test_role_table_lists_only_limits_the_runtime_does_not_enforce(self) -> None:
        enforcing = replace(
            MAPPINGS[0], enforced_limits=lambda caps: frozenset() if caps["shell"] else frozenset({"shell", "write"})
        )
        policy = render.render_policy(enforcing)
        self.assertIn("| `scout` | read and search only | network, MCP tools, git push / PRs | cheap | cheap |", policy)
        self.assertIn(
            "| `reviewer` | read, shell, network; no edits (evidence only under /tmp) "
            "| edit files, MCP tools, git push / PRs |",
            policy,
        )

    def test_channel_controls_policy_file(self) -> None:
        alpha = {file.path for file in render.rendered_files(MAPPINGS[0])}
        gamma = {file.path for file in render.rendered_files(MAPPINGS[2])}
        self.assertIn(".alpha/rules/context-guard.md", alpha)
        self.assertIn(".alpha/skills/handoff/SKILL.md", alpha)
        self.assertIn(".alpha/agents/runner.md", alpha)
        self.assertFalse(any("rules" in path for path in gamma))


class FailureTests(unittest.TestCase):
    """Unfilled tokens, missing tier profiles and invalid roles raise RenderError."""

    def test_unfilled_token_raises(self) -> None:
        with self.assertRaisesRegex(RenderError, "unfilled tokens: missing"):
            render.render("Hello {{ runtime_name }} and {{missing}}", {"runtime_name": "x"})
        self.assertEqual(render.render("{{a}}", {"a": "{{b}}"}), "{{b}}")

    def test_missing_tier_profile_raises(self) -> None:
        mapping = fake_mapping("alpha")
        broken = RuntimeMapping(**{**mapping.__dict__, "tiers": {"cheap": {"model": "m"}}})
        with self.assertRaisesRegex(RenderError, "no native profile"):
            render.render_policy(broken)

    def test_invalid_roles_and_variants_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content"
            shutil.copytree(render.CONTENT_DIR, content)
            roles = content / "roles" / "roles.json"
            original = roles.read_text()
            roles.write_text(original.replace('"default_tier": "deep"', '"default_tier": "cheap"'))
            with self.assertRaisesRegex(RenderError, "reviewer"):
                render.load_roles(content)
            roles.write_text(
                original.replace(
                    '"variants": []', '"variants": [{"name": "scout-deep", "role": "scout", "tier": "deep"}]'
                )
            )
            with self.assertRaisesRegex(RenderError, "invalid variant"):
                render.agents(fake_mapping("alpha"), content)
            roles.write_text(
                original.replace(
                    '"variants": []',
                    '"variants": [{"name": "implementer-long", "role": "implementer", '
                    '"tier": "deep", "context": "1m"}]',
                )
            )
            variant = [
                agent for agent in render.agents(fake_mapping("alpha"), content) if agent.name == "implementer-long"
            ]
            self.assertEqual(
                [(agent.role.name, agent.tier, agent.context) for agent in variant], [("implementer", "deep", "1m")]
            )


if __name__ == "__main__":
    unittest.main()
