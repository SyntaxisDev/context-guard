"""GitHub Copilot CLI plugin: the package spec for core.package.

The manifest lives at `.github/plugin/plugin.json` (one of the locations Copilot reads) and names the
`agents` and `skills` directories; the hook file is the default `hooks/hooks.json`. Copilot hook
handlers take an `env` map whose values expand `${PLUGIN_ROOT}` (the bundled browser-use plugin passes
the root that way), so commands reach the package through `$CONTEXT_GUARD_PLUGIN_ROOT`.
Plugin agents are namespaced `context-guard:<role>`, and their per-agent model settings are keyed by
that name in the user's settings.json, which a package cannot ship (`mapping.subagent_settings`).
The explicit `adapters/copilot/agents.py` helper installs and removes those preferences reversibly.
"""

from collections.abc import Mapping
from pathlib import Path

from adapters.copilot import mapping
from core import deadline, kit, render
from core.package import LAUNCHER, PLUGINS_DIR, PluginSpec

NAME = mapping.PLUGIN_NAME
DESCRIPTION = "Context warnings, task handoffs and delegation support for GitHub Copilot CLI"
MANIFEST_FILE = ".github/plugin/plugin.json"
MARKETPLACE_FILE = ".github/plugin/marketplace.json"
HOOKS_FILE = "hooks/hooks.json"
ROOT_VARIABLE = "CONTEXT_GUARD_PLUGIN_ROOT"
ROOT_TOKEN = f"${ROOT_VARIABLE}"
NATIVE_ROOT = "${PLUGIN_ROOT}"
AGENTS_DIR = "agents"
SKILLS_DIR = "skills"
UNSHIPPED_SKILLS = {"install-context-guard"}


def hook_command(event: str) -> str:
    """Build the plugin's hook command for an event.

    Args:
        event: Native Copilot hook event.

    Returns:
        A command that runs the hook script through the packaged launcher under the plugin root.
    """
    return f'sh "{ROOT_TOKEN}/{LAUNCHER}" {mapping.HOOK_SCRIPT} {event}'


def hook_entries() -> dict[str, list[dict]]:
    """Build the plugin's hook registrations: one flat bash handler per event.

    Returns:
        Handlers per native event, each passing the plugin root through its `env`.
    """
    return {
        event: [
            {
                "type": "command",
                "bash": hook_command(event),
                "env": {ROOT_VARIABLE: NATIVE_ROOT},
                "timeoutSec": deadline.HOOK_TIMEOUT_SECONDS,
            }
        ]
        for event in mapping.HOOK_EVENTS
    }


def native_files(config: Mapping) -> dict[str, str]:
    """Render the plugin's own files: the shipped skills and the role agents.

    Args:
        config: Loaded Copilot adapter configuration.

    Returns:
        Text per path relative to the plugin root.
    """
    runtime = mapping.runtime_mapping(config)
    files = {
        f"{SKILLS_DIR}/{name}/SKILL.md": text
        for name, text in render.render_skills(runtime).items()
        if name not in UNSHIPPED_SKILLS
    }
    files |= {f"{AGENTS_DIR}/{Path(item.path).name}": item.text for item in render.render_agents(runtime)}
    return files


def plugin_spec(config: Mapping) -> PluginSpec:
    """Describe the Copilot plugin package.

    Args:
        config: Loaded Copilot adapter configuration.

    Returns:
        The manifest, version 1 hook file, root variable and native files for `core.package`.
    """
    manifest = {
        "name": NAME,
        "version": kit.source_version(),
        "description": DESCRIPTION,
        "agents": [f"./{AGENTS_DIR}"],
        "skills": SKILLS_DIR,
    }
    hook_file = {"version": mapping.HOOKS_VERSION, "hooks": hook_entries()}
    return PluginSpec(MANIFEST_FILE, manifest, HOOKS_FILE, hook_file, ROOT_TOKEN, native_files(config))


def marketplace(name: str, spec: PluginSpec) -> tuple[str, dict]:
    """Describe a local marketplace using Copilot's native GitHub manifest location.

    The pinned native loader recognizes this path and the name/owner/plugins fields.

    Args:
        name: Marketplace name and owner.
        spec: Plugin package offered by the marketplace.

    Returns:
        The relative marketplace file path and its local plugin catalogue.
    """
    return MARKETPLACE_FILE, {
        "name": name,
        "owner": {"name": name},
        "plugins": [
            {
                "name": spec.name,
                "source": f"./{PLUGINS_DIR}/{spec.name}",
                "version": spec.manifest["version"],
                "description": spec.manifest["description"],
            }
        ],
    }
