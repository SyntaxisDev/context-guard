"""Claude Code rendering and install spec: agents, skills, the policy rule and hook registration."""

import json
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from core import deadline, hookfile, render
from core.install_spec import InstallSpec, frontmatter_signatures

RUNTIME = "claude"
CLAUDE_DIR = ".claude"
POLICY_FILE = f"{CLAUDE_DIR}/rules/context-guard.md"
SKILLS_DIR = f"{CLAUDE_DIR}/skills"
AGENTS_DIR = f"{CLAUDE_DIR}/agents"
SETTINGS_FILE = f"{CLAUDE_DIR}/settings.json"
HOOK_SCRIPT = Path("adapters", RUNTIME, "hooks.py")
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "PostCompact", "SubagentStop")
MATCHED_EVENTS = {"PostToolUse": "*"}
SCOUT_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
NETWORK_TOOLS = ("WebFetch", "WebSearch")
PROFILE_KEYS = ("model", "effort", "call")
TRANSCRIPTS = (
    "Claude Code keeps chat transcripts as JSONL files under `~/.claude/projects/<project-slug>/<session-id>.jsonl`."
)


def valid_tiers(tiers: object) -> dict[str, dict[str, str]]:
    """Validate the configured tiers before rendering.

    Args:
        tiers: The configuration's `tiers` section.

    Returns:
        A copy of the tiers as plain dicts.

    Raises:
        RenderError: A tier lacks a non-blank `model`, `effort` or `call` string.
    """
    if not isinstance(tiers, Mapping) or not all(
        isinstance(profile, Mapping)
        and all(isinstance(profile.get(key), str) and profile[key].strip() for key in PROFILE_KEYS)
        for profile in tiers.values()
    ):
        msg = f"{RUNTIME}: every tier needs {', '.join(PROFILE_KEYS)} strings"
        raise render.RenderError(msg)
    return {tier: dict(profile) for tier, profile in tiers.items()}


def describe_tier(profile: Mapping[str, str]) -> str:
    """Describe a tier for the policy's tier table.

    Args:
        profile: Tier profile with `model`, `effort` and the Agent `call` alias.

    Returns:
        Markdown naming the model, its effort and the alias that selects it.
    """
    return f'`{profile["model"]}`, effort {profile["effort"]} (Agent `model: "{profile["call"]}"`)'


def override_syntax(tiers: Mapping[str, Mapping[str, str]]) -> str:
    """Describe how a spawn overrides its tier, for the policy.

    Args:
        tiers: Validated tier profiles.

    Returns:
        The policy text naming the Agent `model` alias of every tier.
    """
    aliases = ", ".join(f"`{profile['call']}` for {tier}" for tier, profile in tiers.items())
    return (
        f"the Agent tool's `model` parameter ({aliases}). The user authorises setting `model` on every "
        "spawn according to this tier policy, even though the tool description reserves it for explicit "
        "requests; omit it to run the agent's default tier. Effort comes from the agent definition and "
        "cannot be set per spawn"
    )


def capability_lines(capabilities: Mapping[str, bool]) -> list[str]:
    """Render a role's capabilities as agent frontmatter lines.

    Args:
        capabilities: The role's capability flags.

    Returns:
        A read-only `tools` allowlist for a role without shell, else `disallowedTools` for the
        missing write and network capabilities, or no lines.
    """
    if not capabilities["shell"]:
        return [f"tools: {', '.join(SCOUT_TOOLS)}"]
    denied = [*(() if capabilities["write"] else EDIT_TOOLS), *(() if capabilities["network"] else NETWORK_TOOLS)]
    return [f"disallowedTools: {', '.join(denied)}"] if denied else []


def enforced_limits(capabilities: Mapping[str, bool]) -> frozenset[str]:
    """Name the capability limits Claude Code enforces for a role.

    The `tools` allowlist enforces every limit; with Bash, removed tools can be replaced by shell
    commands, and no agent setting removes MCP tools, so a shell role's limits are advisory.

    Args:
        capabilities: The role's capability flags.

    Returns:
        Every capability for a role without shell, else nothing.
    """
    return frozenset() if capabilities["shell"] else frozenset(render.CAPABILITY_LABELS)


def render_agent(agent: render.Agent, profile: Mapping[str, str]) -> render.RenderedFile:
    """Render a role as a Claude Code agent file.

    Args:
        agent: The role variant to render.
        profile: Tier profile with the `model` and `effort` the agent runs on.

    Returns:
        The agent markdown file under the agents directory, ending with its permissions.
    """
    lines = [
        "---",
        f"name: {agent.name}",
        f"description: {json.dumps(agent.role.description)}",
        f"model: {json.dumps(profile['model'])}",
        f"effort: {profile['effort']}",
        *capability_lines(agent.role.capabilities),
        "---",
    ]
    body = f"{agent.body.strip()}\n\nPermissions: {render.can_do(agent.role)}."
    return render.RenderedFile(f"{AGENTS_DIR}/{agent.name}.md", "\n".join(lines) + "\n\n" + body + "\n")


def runtime_mapping(config: Mapping) -> render.RuntimeMapping:
    """Describe Claude Code's rendering of the shared content.

    Invalid tiers raise RenderError from `valid_tiers`.

    Args:
        config: Loaded Claude Code adapter configuration.

    Returns:
        The runtime mapping the renderer uses for agents, skills and the policy rule.
    """
    tiers = valid_tiers(config["tiers"])
    return render.RuntimeMapping(
        runtime=RUNTIME,
        runtime_name="Claude Code",
        handoff_dir=config["paths"]["handoff_dir"],
        tiers=tiers,
        override_syntax=override_syntax(tiers),
        transcripts=TRANSCRIPTS,
        channel="rule_file",
        describe_tier=describe_tier,
        render_agent=render_agent,
        skills_dir=SKILLS_DIR,
        policy_file=POLICY_FILE,
        enforced_limits=enforced_limits,
    )


def hook_groups(command: Callable[[str], str]) -> dict[str, dict]:
    """Build one handler group per registered event.

    Args:
        command: Builds an event's hook command: the installed kit's or the plugin launcher's.

    Returns:
        Group per native event, with a `*` matcher for PostToolUse.
    """
    groups = {}
    for event in HOOK_EVENTS:
        hook = {"type": "command", "command": command(event), "timeout": deadline.HOOK_TIMEOUT_SECONDS}
        groups[event] = {**({"matcher": MATCHED_EVENTS[event]} if event in MATCHED_EVENTS else {}), "hooks": [hook]}
    return groups


def install_spec(home: Path, kit: Path, python: Path, config: dict) -> InstallSpec:
    """Plan the Claude Code install: rendered files under ~/.claude and the settings.json hook merge.

    Args:
        home: Home directory to install into.
        kit: Installed kit root.
        python: Interpreter that runs the hook script.
        config: Loaded Claude Code adapter configuration.

    Returns:
        The install spec for the shared installer.
    """
    mapping = runtime_mapping(config)
    files = {home / item.path: item.text for item in render.rendered_files(mapping)}
    script = kit / HOOK_SCRIPT
    settings = hookfile.group_merge(
        home / SETTINGS_FILE, hook_groups(partial(hookfile.hook_command, python, script)), script
    )
    return InstallSpec(
        runtime=RUNTIME,
        files=files,
        json_merges=[settings],
        kit_signatures=frontmatter_signatures(home / AGENTS_DIR, render.agents(mapping)),
    )
