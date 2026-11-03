"""Genie plugin: the package spec for core.package.

Genie plugins ship skills and hooks but neither agents nor always-on instructions, so the policy
reaches the model only through SessionStart context. The package renders two policies:
`hooks.PLUGIN_POLICY_FILE` for a Genie home where adapters/genie/agents.py installed the role
agents, and `hooks.PLUGIN_ROLELESS_POLICY_FILE`, which spawns Genie's default agent with the
packaged role brief and tells the model how the user installs the named agents.
adapters/genie/hooks.py picks one at SessionStart.
"""

import dataclasses
from collections.abc import Mapping

from adapters.genie import hooks, mapping
from core import kit, render
from core.package import LAUNCHER, PLUGINS_DIR, PluginSpec

NAME = hooks.PLUGIN_NAME
DISPLAY_NAME = "context-guard"
DESCRIPTION = "Context warnings, task handoffs and delegation support for Genie"
DEVELOPER = "SyntaxDev"
CATEGORY = "Productivity"
MANIFEST_FILE = ".codex-plugin/plugin.json"
MARKETPLACE_FILE = ".agents/plugins/marketplace.json"
HOOKS_FILE = "hooks/hooks.json"
SKILLS_DIR = "skills"
ROLES_DIR = "roles"
ROOT_TOKEN = hooks.PLUGIN_ROOT_TOKEN
POLICY_FILE = hooks.PLUGIN_POLICY_FILE
ROLELESS_POLICY_FILE = hooks.PLUGIN_ROLELESS_POLICY_FILE
UNSHIPPED_SKILLS = {"install-context-guard"}
AGENTS_COMMAND = f'sh "{ROOT_TOKEN}/{LAUNCHER}" adapters/genie/agents.py install'
ROLELESS_LIMITS = {"shell": "Do not run shell commands.", **mapping.ADVISORY_LIMITS}
ROLELESS_OVERRIDE = (
    "the `spawn_agent` `model` and `reasoning_effort` arguments. The named role agents are not "
    "installed, so pass both on every spawn (the role's default tier unless you step up), omit "
    '`agent_type` so Genie\'s default agent runs, pass `fork_turns: "none"`, and start the message '
    f"with the role's brief from `{ROOT_TOKEN}/{ROLES_DIR}/<role>.md`, then your task brief"
)
ROLELESS_NOTE = f"""
## Named subagents

Named context-guard subagents are not installed in this Genie home. If the user wants them,
the user runs this once, then starts a new session:

```sh
{AGENTS_COMMAND}
```
"""


def hook_command(event: str) -> str:
    """Build the plugin's hook command for an event.

    Args:
        event: Native Genie hook event.

    Returns:
        A command that runs the hook script through the packaged launcher under the plugin root.
    """
    return f'sh "{ROOT_TOKEN}/{LAUNCHER}" {mapping.HOOK_SCRIPT} {event}'


def hook_groups(config: Mapping) -> dict[str, list[dict]]:
    """Build the plugin's hooks.json `hooks` section.

    Args:
        config: Loaded Genie adapter configuration.

    Returns:
        A one-group list per native event.
    """
    groups = mapping.hook_groups(hook_command, config["hook_output"]["session_context_limit"])
    return {event: [group] for event, group in groups.items()}


def roleless_policy(runtime: render.RuntimeMapping) -> str:
    """Render the policy for a Genie home without the named role agents.

    Without a role file nothing is enforced, so every denied capability is listed as advisory.

    Args:
        runtime: The Genie runtime mapping.

    Returns:
        The policy markdown with the install note; it names the plugin root as `ROOT_TOKEN`.
    """
    roleless = dataclasses.replace(
        runtime, override_syntax=ROLELESS_OVERRIDE, enforced_limits=render.no_enforced_limits
    )
    return render.render_policy(roleless) + ROLELESS_NOTE


def native_files(config: Mapping) -> dict[str, str]:
    """Render the plugin's own files: both policies, the role briefs and the shipped skills.

    Args:
        config: Loaded Genie adapter configuration.

    Returns:
        Text per path relative to the plugin root.
    """
    runtime = mapping.runtime_mapping(config)
    files = {POLICY_FILE: render.render_policy(runtime), ROLELESS_POLICY_FILE: roleless_policy(runtime)}
    files |= {
        f"{ROLES_DIR}/{agent.name}.md": mapping.developer_instructions(agent, ROLELESS_LIMITS) + "\n"
        for agent in render.agents(runtime)
    }
    files |= {
        f"{SKILLS_DIR}/{name}/SKILL.md": text
        for name, text in render.render_skills(runtime).items()
        if name not in UNSHIPPED_SKILLS
    }
    return files


def plugin_spec(config: Mapping) -> PluginSpec:
    """Describe the Genie plugin package.

    Args:
        config: Loaded Genie adapter configuration.

    Returns:
        The manifest, hook file, root placeholder and native files for `core.package`.
    """
    manifest = {
        "name": NAME,
        "version": kit.source_version(),
        "description": DESCRIPTION,
        "skills": f"./{SKILLS_DIR}/",
        "hooks": f"./{HOOKS_FILE}",
        "author": {"name": DEVELOPER},
        "interface": {
            "displayName": DISPLAY_NAME,
            "shortDescription": DESCRIPTION,
            "longDescription": DESCRIPTION,
            "developerName": DEVELOPER,
            "category": CATEGORY,
            "capabilities": ["Interactive", "Write"],
            "defaultPrompt": ["Show context status and active handoffs."],
        },
    }
    hook_file = {"hooks": hook_groups(config)}
    return PluginSpec(MANIFEST_FILE, manifest, HOOKS_FILE, hook_file, ROOT_TOKEN, native_files(config))


def marketplace(name: str, spec: PluginSpec) -> tuple[str, dict]:
    """Build a marketplace listing that offers the packaged plugin.

    Args:
        name: Marketplace name, also its display name.
        spec: The packaged plugin's spec.

    Returns:
        The listing's path relative to the package root and its JSON document.
    """
    entry = {
        "name": spec.name,
        "source": {"source": "local", "path": f"./{PLUGINS_DIR}/{spec.name}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": CATEGORY,
    }
    return MARKETPLACE_FILE, {"name": name, "interface": {"displayName": name}, "plugins": [entry]}
