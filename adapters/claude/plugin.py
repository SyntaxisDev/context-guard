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
from core import kit, render
from core.package import LAUNCHER, PLUGINS_DIR, PluginSpec

NAME = hooks.PLUGIN_NAME
DESCRIPTION = "Context warnings, task handoffs and delegation support for Claude Code and CoWork"
MANIFEST_FILE = ".claude-plugin/plugin.json"
MARKETPLACE_FILE = ".claude-plugin/marketplace.json"
HOOKS_FILE = "hooks/hooks.json"
ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"  # noqa: S105
POLICY_FILE = hooks.PLUGIN_POLICY_FILE
UNSHIPPED_SKILLS = {"install-context-guard"}


def hook_command(event: str) -> str:
    """Build the plugin's hook command for an event.

    Args:
        event: Native Claude Code hook event.

    Returns:
        A command that runs the hook script through the packaged launcher under the plugin root.
    """
    return f'sh "{ROOT_TOKEN}/{LAUNCHER}" {mapping.HOOK_SCRIPT.as_posix()} {event}'


def hook_groups() -> dict[str, list[dict]]:
    """Build the plugin's hooks.json `hooks` section.

    Returns:
        A one-group list per native event.
    """
    return {event: [group] for event, group in mapping.hook_groups(hook_command).items()}


def plugin_policy(runtime: render.RuntimeMapping) -> str:
    """Render the policy with every agent name in backticks namespaced as `context-guard:<role>`.

    Args:
        runtime: The Claude Code runtime mapping.

    Returns:
        The packaged policy markdown.
    """
    names = sorted((agent.name for agent in render.agents(runtime)), key=len, reverse=True)
    pattern = re.compile(r"`(" + "|".join(map(re.escape, names)) + r")`")
    return pattern.sub(lambda match: f"`{NAME}:{match[1]}`", render.render_policy(runtime))


def native_files(config: Mapping) -> dict[str, str]:
    """Render the plugin's own files: the policy, shipped skills and agents.

    Args:
        config: Loaded Claude Code adapter configuration.

    Returns:
        Text per path relative to the plugin root.
    """
    runtime = mapping.runtime_mapping(config)
    files = {POLICY_FILE: plugin_policy(runtime)}
    files |= {
        f"skills/{name}/SKILL.md": text
        for name, text in render.render_skills(runtime).items()
        if name not in UNSHIPPED_SKILLS
    }
    files |= {f"agents/{Path(item.path).name}": item.text for item in render.render_agents(runtime)}
    return files


def plugin_spec(config: Mapping) -> PluginSpec:
    """Describe the Claude Code plugin package.

    Args:
        config: Loaded Claude Code adapter configuration.

    Returns:
        The manifest, hook file, root placeholder and native files for `core.package`.
    """
    manifest = {"name": NAME, "version": kit.source_version(), "description": DESCRIPTION}
    hook_file = {"description": "context-guard context warnings, handoff listing and policy", "hooks": hook_groups()}
    return PluginSpec(MANIFEST_FILE, manifest, HOOKS_FILE, hook_file, ROOT_TOKEN, native_files(config))


def marketplace(name: str, spec: PluginSpec) -> tuple[str, dict]:
    """Build a marketplace listing that offers the packaged plugin.

    Args:
        name: Marketplace name, also used as its owner.
        spec: The packaged plugin's spec.

    Returns:
        The listing's path relative to the package root and its JSON document.
    """
    entry = {
        "name": spec.name,
        "source": f"./{PLUGINS_DIR}/{spec.name}",
        "version": spec.manifest["version"],
        "description": spec.manifest["description"],
    }
    return MARKETPLACE_FILE, {
        "name": name,
        "owner": {"name": name},
        "metadata": {"description": DESCRIPTION},
        "plugins": [entry],
    }
