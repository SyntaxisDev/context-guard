"""GitHub Copilot CLI rendering and install spec: skills, agents and the kit-owned hook file."""

import json
import os
from collections.abc import Mapping
from pathlib import Path

from core import install, render
from core.deadline import HOOK_TIMEOUT_SECONDS
from core.hookfile import hook_command
from core.install_spec import InstallSpec, JsonMerge

RUNTIME = "copilot"
RUNTIME_NAME = "GitHub Copilot CLI"
COPILOT_DIR = ".copilot"
COPILOT_HOME_ENV = "COPILOT_HOME"
SKILLS_DIR = ".copilot/skills"
AGENTS_DIR = ".copilot/agents"
HOOK_SCRIPT = "adapters/copilot/hooks.py"
HOOK_FILE = "hooks/context-guard.json"
HOOKS_VERSION = 1
SESSION_START = "sessionStart"
PROMPT_SUBMITTED = "userPromptSubmitted"
AGENT_STOP = "agentStop"
POST_TOOL = "postToolUse"
SUBAGENT_START = "subagentStart"
SUBAGENT_STOP = "subagentStop"
HOOK_EVENTS = (SESSION_START, PROMPT_SUBMITTED, AGENT_STOP, POST_TOOL, SUBAGENT_START, SUBAGENT_STOP)
SETTINGS_FILE = "settings.json"
PLUGIN_NAME = "context-guard"
READ_ONLY_TOOLS = ("view", "glob", "grep")
OVERRIDE_SYNTAX = "the `task` tool's `agent_type` and `model` arguments"
TRANSCRIPTS = (
    "Copilot keeps each session's event log at `$COPILOT_HOME/session-state/<session-id>/events.jsonl` "
    "(default `~/.copilot/session-state/`)."
)


def describe_tier(profile: Mapping[str, str]) -> str:
    """Describe a tier for the policy's tier table.

    Args:
        profile: Tier profile with `model` and `effort`.

    Returns:
        Markdown naming the model and its reasoning effort.
    """
    return f"`{profile['model']}` with `{profile['effort']}` reasoning"


def enforced_limits(_capabilities: Mapping[str, bool]) -> frozenset[str]:
    """Report the denied capabilities Copilot enforces for a role: none.

    The agent file's `tools` allowlist is parsed natively, but no run showed it removing a tool from a
    child, and shell, MCP and network access can replace removed tools, so every limit stays advisory.

    Args:
        _capabilities: The role's capability flags; unused.

    Returns:
        An empty set.
    """
    return frozenset()


def capability_lines(capabilities: Mapping[str, bool]) -> list[str]:
    """Render a role's capabilities as agent front matter lines.

    Args:
        capabilities: The role's capability flags.

    Returns:
        A read-only `tools` allowlist for a role with neither shell nor write access, else no lines.
    """
    if capabilities["shell"] or capabilities["write"]:
        return []
    return ["tools: " + json.dumps(list(READ_ONLY_TOOLS))]


def render_agent(agent: render.Agent, profile: Mapping[str, str]) -> render.RenderedFile:
    """Render a role as a Copilot custom agent file.

    Args:
        agent: The role variant to render.
        profile: Tier profile with the `model` the agent runs on.

    Returns:
        The agent markdown file under the agents directory; the model is pinned in its front matter.
    """
    lines = [
        "---",
        f"name: {agent.name}",
        f"description: {json.dumps(agent.role.description)}",
        f"model: {json.dumps(profile['model'])}",
        *capability_lines(agent.role.capabilities),
        "---",
    ]
    body = f"{agent.body.strip()}\n\nPermissions: {render.can_do(agent.role)}."
    return render.RenderedFile(f"{AGENTS_DIR}/{agent.name}.agent.md", "\n".join(lines) + "\n\n" + body + "\n")


def runtime_mapping(config: dict) -> render.RuntimeMapping:
    """Describe Copilot's rendering of the shared content.

    Args:
        config: Loaded Copilot adapter configuration.

    Returns:
        The runtime mapping; the policy reaches Copilot as session context, not as a file. Completed
        background subagents wake an idle parent natively (an idle root starts a turn without a user message).
    """
    return render.RuntimeMapping(
        runtime=RUNTIME,
        runtime_name=RUNTIME_NAME,
        handoff_dir=config["paths"]["handoff_dir"],
        tiers=config["tiers"],
        override_syntax=OVERRIDE_SYNTAX,
        transcripts=TRANSCRIPTS,
        channel="session_context",
        parent_resume="native",
        describe_tier=describe_tier,
        render_agent=render_agent,
        skills_dir=SKILLS_DIR,
        enforced_limits=enforced_limits,
    )


def policy_text(config: dict) -> str:
    """Render the Copilot policy that sessionStart injects.

    Args:
        config: Loaded Copilot adapter configuration.

    Returns:
        The rendered policy markdown.
    """
    return render.render_policy(runtime_mapping(config))


def copilot_home(home: Path, environ: Mapping[str, str]) -> Path:
    """Locate the Copilot home that holds hooks, skills, agents and session state.

    Args:
        home: The user home.
        environ: Environment that may set COPILOT_HOME.

    Returns:
        `$COPILOT_HOME` when set, otherwise `<home>/.copilot`.

    Raises:
        ValueError: COPILOT_HOME is set to a relative path.
    """
    configured = environ.get(COPILOT_HOME_ENV) or ""
    if configured and not Path(configured).is_absolute():
        msg = "COPILOT_HOME must be an absolute path"
        raise ValueError(msg)
    return (Path(configured) if configured else home / COPILOT_DIR).resolve()


def hook_file_text(python: Path, script: Path) -> str:
    """Render the kit-owned hook file: one flat command handler per event.

    Args:
        python: Interpreter that runs the hook script.
        script: The hook script.

    Returns:
        The JSON text of a version 1 hook file.
    """
    hooks = {
        event: [{"type": "command", "bash": hook_command(python, script, event), "timeoutSec": HOOK_TIMEOUT_SECONDS}]
        for event in HOOK_EVENTS
    }
    return json.dumps({"version": HOOKS_VERSION, "hooks": hooks}, indent=2) + "\n"


def agent_signatures(agents_dir: Path, agents: list[render.Agent]) -> dict[Path, tuple[str, ...]]:
    """Identify kit agent files that no manifest records.

    Args:
        agents_dir: Directory the agent files are installed in.
        agents: The agents rendered into that directory.

    Returns:
        The lines that mark each `<name>.agent.md` file as the kit's.
    """
    return {
        agents_dir / f"{agent.name}.agent.md": (
            "---",
            f"name: {agent.name}",
            agent.body.strip().splitlines()[0].strip(),
        )
        for agent in agents
    }


def subagent_settings(config: Mapping, prefix: str = "") -> dict[str, dict[str, str]]:
    """Build the per-agent `subagents.agents` entries that pin each role's model and effort.

    Args:
        config: Loaded Copilot adapter configuration.
        prefix: Namespace in front of every agent name, e.g. `context-guard:` for plugin agents,
            which Copilot keys by their namespaced name.

    Returns:
        Each effective agent name mapped to its `model`, `effortLevel` and `contextTier`.
    """
    entries = {}
    for agent in render.agents(runtime_mapping(config)):
        profile = config["tiers"][agent.tier]
        entries[f"{prefix}{agent.name}"] = {
            "model": profile["model"],
            "effortLevel": profile["effort"],
            "contextTier": agent.context or "default",
        }
    return entries


def merge_subagents(
    data: dict, entries: Mapping[str, dict[str, str]], installed: Mapping[str, dict[str, str]] | None = None
) -> dict:
    """Add each owned agent entry that the user has not set, creating `subagents.agents` when absent.

    Args:
        data: Parsed settings.json; it is changed in place.
        entries: The kit's entries from `subagent_settings`.
        installed: Last recorded values for entries the kit owns.

    Returns:
        `data`; a `subagents` or `agents` that is not an object is left alone.
    """
    subagents = data.setdefault("subagents", {})
    agents = subagents.setdefault("agents", {}) if isinstance(subagents, dict) else None
    if isinstance(agents, dict):
        for name, entry in entries.items():
            owned = installed is not None and name in installed
            if (owned and agents.get(name) == installed[name]) or (not owned and name not in agents):
                agents[name] = dict(entry)
    return data


def unmerge_subagents(data: dict, entries: Mapping[str, dict[str, str]], original: dict | None = None) -> dict:
    """Remove the kit's entries that still equal what it wrote, and the containers that empties.

    Args:
        data: Parsed settings.json; it is changed in place.
        entries: The kit's entries from `subagent_settings`.
        original: Settings before the first merge, preserving existing names and containers.

    Returns:
        `data`; an entry the user edited is kept.
    """
    subagents = data.get("subagents")
    agents = subagents.get("agents") if isinstance(subagents, dict) else None
    if not isinstance(agents, dict):
        return data
    prior = (original or {}).get("subagents")
    prior_agents = prior.get("agents") if isinstance(prior, dict) else None
    for name, entry in entries.items():
        if agents.get(name) == entry and (not isinstance(prior_agents, dict) or name not in prior_agents):
            del agents[name]
    if not agents and not isinstance(prior_agents, dict):
        del subagents["agents"]
    if not subagents and not isinstance(prior, dict):
        del data["subagents"]
    return data


def agent_entries(data: dict) -> dict | None:
    """Find the agent preferences in a settings object.

    Args:
        data: Parsed settings.

    Returns:
        The preferences object, or None when absent or malformed.
    """
    subagents = data.get("subagents")
    agents = subagents.get("agents") if isinstance(subagents, dict) else None
    return agents if isinstance(agents, dict) else None


def recorded_preferences(
    current: dict, merged: dict, original: dict, entries: Mapping[str, dict[str, str]], installed: dict
) -> dict:
    """Update recorded ownership only for preferences written by this merge.

    Args:
        current: Settings before merging.
        merged: Settings after merging.
        original: Settings before the first installation.
        entries: Current desired kit preferences.
        installed: Last installed values.

    Returns:
        The updated installed values.
    """
    prior, before, after = agent_entries(original), agent_entries(current), agent_entries(merged)
    result = dict(installed)
    if after is not None:
        for name, entry in entries.items():
            if prior is not None and name in prior:
                continue
            owned = name in installed
            if (owned and before is not None and before.get(name) == installed[name]) or (
                not owned and (before is None or name not in before)
            ):
                result[name] = dict(entry)
    return result


def edited_preferences(current: dict, installed: Mapping[str, dict[str, str]]) -> list[str]:
    """Identify edits to preferences previously installed by the kit.

    Args:
        current: Settings on disk.
        installed: Last installed values.

    Returns:
        One note per edited entry.
    """
    agents = agent_entries(current)
    return [
        f"subagents.agents.{name} edited since install; kept"
        for name, value in installed.items()
        if agents is not None and name in agents and agents[name] != value
    ]


def subagent_merge(path: Path, entries: Mapping[str, dict[str, str]], manifest: dict | None = None) -> JsonMerge:
    """Build the reversible settings.json merge for the owned `subagents.agents` entries.

    Args:
        path: The settings.json file.
        entries: The kit's entries from `subagent_settings`.
        manifest: Persisted install ownership, when regenerating the spec.

    Returns:
        The merge; every other key keeps its value.
    """
    recorded = (manifest or {}).get("merges", {}).get(str(path), {})
    original = install.original_object(path, recorded.get("original"))
    if original is None and recorded.get("created"):
        original = {}
    installed = dict(recorded.get("installed", {}))

    def merge(data: dict) -> dict:
        """Capture the first settings object and merge the role preferences.

        Args:
            data: Current settings object.

        Returns:
            Settings with missing preferences filled.
        """
        nonlocal original
        if original is None:
            original = json.loads(json.dumps(data))
        return merge_subagents(data, entries, installed)

    def unmerge(data: dict) -> dict:
        """Remove only preferences absent before installation.

        Args:
            data: Current settings object.

        Returns:
            Settings with unchanged owned preferences removed.
        """
        baseline = original if original is not None else data
        return unmerge_subagents(data, installed, baseline)

    return JsonMerge(
        path,
        merge,
        unmerge,
        owned=lambda current, merged: recorded_preferences(current, merged, original or {}, entries, installed),
        notes=lambda current: edited_preferences(current, installed),
    )


def install_spec(
    home: Path, kit: Path, python: Path, config: dict, environ: Mapping[str, str] | None = None
) -> InstallSpec:
    """Plan the Copilot install: rendered skills and agents, the kit-owned hook file and the agent settings.

    The hook file is a whole file the kit owns. The settings merge adds only the kit's agent entries and
    never writes trust, hook or permission settings.

    Args:
        home: Home directory to install into.
        kit: Installed kit root.
        python: Interpreter that runs the hook script.
        config: Loaded Copilot adapter configuration.
        environ: Environment that may set COPILOT_HOME; None reads the process environment.

    Returns:
        The install spec for the shared installer.
    """
    mapping = runtime_mapping(config)
    root = copilot_home(home, os.environ if environ is None else environ)
    files = {
        root / Path(rendered.path).relative_to(COPILOT_DIR): rendered.text
        for rendered in render.rendered_files(mapping)
    }
    files[root / HOOK_FILE] = hook_file_text(python, kit / HOOK_SCRIPT)
    manifest = install.read_manifest(install.manifest_path(home, install_identity(home, RUNTIME, environ)))
    return InstallSpec(
        runtime=RUNTIME,
        files=files,
        json_merges=[subagent_merge(root / SETTINGS_FILE, subagent_settings(config), manifest)],
        kit_signatures=agent_signatures(root / "agents", render.agents(mapping)),
    )


def install_identity(home: Path, name: str, environ: Mapping[str, str] | None = None) -> str:
    """Identify installation state for the selected Copilot home.

    Args:
        home: User home directory.
        name: Base runtime, helper manifest or offer name.
        environ: Environment that may set COPILOT_HOME; None reads the process environment.

    Returns:
        The base name for the default home, otherwise a profile-specific name.
    """
    environment = os.environ if environ is None else environ
    return install.profile_name(name, copilot_home(home, environment), home / COPILOT_DIR)
