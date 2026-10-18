"""Claude Code plugin: the package spec for core.package.

Plugins cannot ship rules files, so the policy is rendered into the package at
`hooks.PLUGIN_POLICY_FILE`, where adapters/claude/hooks.py finds it and puts it ahead of the
handoff listing in the session's additional context. Plugin agents are namespaced
`context-guard:<role>`, so the packaged policy names them that way.
"""
import re
from collections.abc import Mapping
from pathlib import Path

from adapters.claude import hooks, mapping
from core import deadline, kit, render
from core.package import LAUNCHER, PLUGINS_DIR, PluginSpec

NAME = hooks.PLUGIN_NAME
DESCRIPTION = "Context warnings, task handoffs and delegation support for Claude Code and CoWork"
MANIFEST_FILE = ".claude-plugin/plugin.json"
MARKETPLACE_FILE = ".claude-plugin/marketplace.json"
HOOKS_FILE = "hooks/hooks.json"
ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"
POLICY_FILE = hooks.PLUGIN_POLICY_FILE
UNSHIPPED_SKILLS = {"install-context-guard"}


def hook_command(event: str) -> str:
    return f'sh "{ROOT_TOKEN}/{LAUNCHER}" {mapping.HOOK_SCRIPT.as_posix()} {event}'


def hook_groups() -> dict[str, list]:
    groups = {}
    for event in mapping.HOOK_EVENTS:
        hook = {"type": "command", "command": hook_command(event), "timeout": deadline.HOOK_TIMEOUT_SECONDS}
        matcher = mapping.MATCHED_EVENTS.get(event)
        groups[event] = [{**({"matcher": matcher} if matcher else {}), "hooks": [hook]}]
    return groups


def plugin_policy(runtime: render.RuntimeMapping) -> str:
    names = sorted((agent.name for agent in render.agents(runtime)), key=len, reverse=True)
    pattern = re.compile(r"`(" + "|".join(map(re.escape, names)) + r")`")
    return pattern.sub(lambda match: f"`{NAME}:{match[1]}`", render.render_policy(runtime))


def native_files(config: Mapping) -> dict[str, str]:
    runtime = mapping.runtime_mapping(config)
    files = {POLICY_FILE: plugin_policy(runtime)}
    files |= {f"skills/{name}/SKILL.md": text for name, text in render.render_skills(runtime).items()
              if name not in UNSHIPPED_SKILLS}
    files |= {f"agents/{Path(item.path).name}": item.text for item in render.render_agents(runtime)}
    return files


def plugin_spec(config: Mapping) -> PluginSpec:
    manifest = {"name": NAME, "version": kit.source_version(), "description": DESCRIPTION}
    hook_file = {"description": "context-guard context warnings, handoff listing and policy", "hooks": hook_groups()}
    return PluginSpec(MANIFEST_FILE, manifest, HOOKS_FILE, hook_file, ROOT_TOKEN, native_files(config))


def marketplace(name: str, spec: PluginSpec) -> tuple[str, dict]:
    entry = {"name": spec.name, "source": f"./{PLUGINS_DIR}/{spec.name}",
             "version": spec.manifest["version"], "description": spec.manifest["description"]}
    return MARKETPLACE_FILE, {"name": name, "owner": {"name": name},
                              "metadata": {"description": DESCRIPTION}, "plugins": [entry]}
