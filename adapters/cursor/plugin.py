"""Cursor plugin: the package spec for core.package.

A Cursor plugin ships rules, agents and skills in its default component directories, so the
policy, agents and skills are the installer's files under `rules/`, `agents/` and `skills/`.
Cursor does not namespace plugin agents, so their names stay the plain role names.

The plugin registers no hooks: the Cursor app runs plugin hooks only with its Third-Party Imports
setting on, which also imports Claude Code and Codex configuration. The user registers them once in
`~/.cursor/hooks.json` with adapters/cursor/hooks_install.py, run through the packaged launcher; the
policy rule ends with that command, so a hook can never fire twice when the setting is on.
"""

from collections.abc import Mapping
from pathlib import Path

from adapters.cursor import mapping
from core import install, kit, render
from core.package import LAUNCHER, PLUGINS_DIR, PluginSpec

NAME = "context-guard"
DESCRIPTION = "Context warnings, task handoffs and delegation support for Cursor"
MANIFEST_FILE = ".cursor-plugin/plugin.json"
MARKETPLACE_FILE = ".cursor-plugin/marketplace.json"
ROOT_TOKEN = "${CURSOR_PLUGIN_ROOT}"  # noqa: S105
INSTALL_PREFIX = f"{mapping.CURSOR_DIR}/"
POLICY_FILE = mapping.POLICY_FILE.removeprefix(INSTALL_PREFIX)
UNSHIPPED_SKILLS = {"install-context-guard"}
HOOKS_COMMAND = f'sh "<plugin root>/{LAUNCHER}" adapters/cursor/hooks_install.py install'
HOOKS_OFFER_MARKER = install.offer_marker(Path("~"), mapping.PLUGIN_HOOKS)
HOOKS_NOTE = f"""
## Hooks

Context warnings, the handoff listing and subagent tracking come from context-guard hooks, which
this plugin does not register by itself. Registered hooks start every chat with a session context
(separate from this rule) whose first line is `{mapping.SESSION_MARKER}` Subagents skip this
section. If this chat has no such line, check whether `{HOOKS_OFFER_MARKER}` exists
(`$CONTEXT_GUARD_HOME` replaces `~` when set). If it exists, say nothing about hooks. Otherwise,
offer once in your reply to register them, then create that file with a file-writing tool so the
offer is never repeated. When the user agrees or asks, run this command (or give it to them), where
`<plugin root>` is the directory holding this rule's `rules/` directory, then ask them to reload the
Cursor window and to turn Settings > Agents > Third-Party Imports off unless they use it otherwise
(the hooks do not need it, and it imports duplicate Claude Code and Codex configuration). Rerun the
command after a plugin update; `uninstall` in place of `install` removes the hooks.

```sh
{HOOKS_COMMAND}
```
"""


def shipped(relative: str) -> bool:
    """Tell whether an installer file belongs in the plugin.

    Args:
        relative: Path of the rendered file relative to the plugin root.

    Returns:
        False for the skills a plugin does not ship, else True.
    """
    parts = relative.split("/")
    return not (parts[0] == "skills" and parts[1] in UNSHIPPED_SKILLS)


def native_files(config: Mapping) -> dict[str, str]:
    """Render the plugin's own files: the policy rule with the hooks note, shipped skills and agents.

    Args:
        config: Loaded Cursor adapter configuration.

    Returns:
        Text per path relative to the plugin root, laid out as under the installer's `.cursor/`.
    """
    rendered = {
        item.path.removeprefix(INSTALL_PREFIX): item.text
        for item in render.rendered_files(mapping.runtime_mapping(dict(config)))
    }
    files = {relative: text for relative, text in rendered.items() if shipped(relative)}
    files[POLICY_FILE] += HOOKS_NOTE
    return files


def plugin_spec(config: Mapping) -> PluginSpec:
    """Describe the Cursor plugin package.

    Args:
        config: Loaded Cursor adapter configuration.

    Returns:
        The manifest, root placeholder and native files for `core.package`, without a hooks file.
    """
    manifest = {
        "name": NAME,
        "version": kit.source_version(),
        "description": DESCRIPTION,
    }
    return PluginSpec(MANIFEST_FILE, manifest, None, None, ROOT_TOKEN, native_files(config))


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
