"""Claude Code rendering and install spec: agents, skills, the policy rule and hook registration."""
import copy
import json
import shlex
from collections.abc import Mapping
from pathlib import Path

from core import deadline, render
from core.install_spec import InstallSpec, JsonMerge

RUNTIME = "claude"
CLAUDE_DIR = ".claude"
POLICY_FILE = f"{CLAUDE_DIR}/rules/context-guard.md"
SKILLS_DIR = f"{CLAUDE_DIR}/skills"
AGENTS_DIR = f"{CLAUDE_DIR}/agents"
SETTINGS_FILE = f"{CLAUDE_DIR}/settings.json"
INSTRUCTIONS_FILE = f"{CLAUDE_DIR}/CLAUDE.md"
HOOK_SCRIPT = Path("adapters", RUNTIME, "hooks.py")
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "PostCompact")
MATCHED_EVENTS = {"PostToolUse": "*"}
SCOUT_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
NETWORK_TOOLS = ("WebFetch", "WebSearch")
PROFILE_KEYS = ("model", "effort", "call")
TRANSCRIPTS = ("Claude Code keeps chat transcripts as JSONL files under "
               "`~/.claude/projects/<project-slug>/<session-id>.jsonl`.")


def valid_tiers(tiers: object) -> dict:
    if not isinstance(tiers, Mapping) or not all(
            isinstance(profile, Mapping) and all(isinstance(profile.get(key), str) and profile[key].strip()
                                                 for key in PROFILE_KEYS) for profile in tiers.values()):
        raise render.RenderError(f"{RUNTIME}: every tier needs {', '.join(PROFILE_KEYS)} strings")
    return {tier: dict(profile) for tier, profile in tiers.items()}


def describe_tier(profile: Mapping[str, str]) -> str:
    return f"`{profile['model']}`, effort {profile['effort']} (Agent `model: \"{profile['call']}\"`)"


def override_syntax(tiers: Mapping[str, Mapping[str, str]]) -> str:
    aliases = ", ".join(f"`{profile['call']}` for {tier}" for tier, profile in tiers.items())
    return (f"the Agent tool's `model` parameter ({aliases}). The user authorises setting `model` on every "
            "spawn according to this tier policy, even though the tool description reserves it for explicit "
            "requests; omit it to run the agent's default tier. Effort comes from the agent definition and "
            "cannot be set per spawn")


def capability_lines(capabilities: Mapping[str, bool]) -> list[str]:
    if not capabilities["shell"]:
        return [f"tools: {', '.join(SCOUT_TOOLS)}"]
    denied = [*(() if capabilities["write"] else EDIT_TOOLS), *(() if capabilities["network"] else NETWORK_TOOLS)]
    return [f"disallowedTools: {', '.join(denied)}"] if denied else []


def enforced_limits(capabilities: Mapping[str, bool]) -> frozenset[str]:
    """The `tools` allowlist enforces every limit; with Bash, removed tools can be replaced by shell
    commands, and no agent setting removes MCP tools, so a shell role's limits are advisory."""
    return frozenset() if capabilities["shell"] else frozenset(render.CAPABILITY_LABELS)


def render_agent(agent: render.Agent, profile: Mapping[str, str]) -> render.RenderedFile:
    lines = ["---", f"name: {agent.name}", f"description: {json.dumps(agent.role.description)}",
             f"model: {json.dumps(profile['model'])}", f"effort: {profile['effort']}",
             *capability_lines(agent.role.capabilities), "---"]
    body = f"{agent.body.strip()}\n\nPermissions: {render.can_do(agent.role)}."
    return render.RenderedFile(f"{AGENTS_DIR}/{agent.name}.md", "\n".join(lines) + "\n\n" + body + "\n")


def runtime_mapping(config: Mapping) -> render.RuntimeMapping:
    tiers = valid_tiers(config["tiers"])
    return render.RuntimeMapping(
        runtime=RUNTIME, runtime_name="Claude Code",
        handoff_dir=config["paths"]["handoff_dir"], tiers=tiers,
        override_syntax=override_syntax(tiers), transcripts=TRANSCRIPTS, channel="rule_file",
        describe_tier=describe_tier, render_agent=render_agent, skills_dir=SKILLS_DIR, policy_file=POLICY_FILE,
        enforced_limits=enforced_limits)


def hook_command(python: Path, kit: Path, event: str) -> str:
    return " ".join(shlex.quote(part) for part in (str(python), str(kit / HOOK_SCRIPT), event))


def registrations(python: Path, kit: Path) -> dict[str, dict]:
    groups = {}
    for event in HOOK_EVENTS:
        hook = {"type": "command", "command": hook_command(python, kit, event),
                "timeout": deadline.HOOK_TIMEOUT_SECONDS}
        groups[event] = {**({"matcher": MATCHED_EVENTS[event]} if event in MATCHED_EVENTS else {}), "hooks": [hook]}
    return groups


def owned_hook(hook: object, markers: tuple[str, ...]) -> bool:
    command = hook.get("command") if isinstance(hook, dict) else None
    return isinstance(command, str) and any(marker in command for marker in markers)


def without_owned(settings: dict, markers: tuple[str, ...]) -> dict:
    """Drop hook entries running our scripts; emptied groups and events go too, all else stays."""
    result = copy.deepcopy(settings)
    hooks = result.get("hooks")
    if hooks is None:
        return result
    if not isinstance(hooks, dict):
        raise ValueError(f"{SETTINGS_FILE}: hooks must be an object")
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                remaining = [hook for hook in group["hooks"] if not owned_hook(hook, markers)]
                if not remaining and group["hooks"]:
                    continue
                group = {**group, "hooks": remaining}
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del result["hooks"]
    return result


def owned_groups(settings: dict, markers: tuple[str, ...]) -> dict[str, list]:
    hooks = settings.get("hooks")
    found = {}
    for event, groups in (hooks.items() if isinstance(hooks, dict) else ()):
        owned = [group for group in (groups if isinstance(groups, list) else [])
                 if isinstance(group, dict) and isinstance(group.get("hooks"), list)
                 and any(owned_hook(hook, markers) for hook in group["hooks"])]
        if owned:
            found[event] = owned
    return found


def settings_merge(home: Path, kit: Path, python: Path) -> JsonMerge:
    markers = (str(kit / HOOK_SCRIPT),)
    groups = registrations(python, kit)

    def merge(settings: dict) -> dict:
        """A file already holding exactly our groups is returned as is, so later user hooks keep their place."""
        if owned_groups(settings, markers) == {event: [group] for event, group in groups.items()}:
            return settings
        result = without_owned(settings, markers)
        hooks = result.setdefault("hooks", {})
        for event, group in groups.items():
            hooks.setdefault(event, []).append(copy.deepcopy(group))
        return result

    return JsonMerge(home / SETTINGS_FILE, merge, lambda settings: without_owned(settings, markers))


def install_spec(home: Path, kit: Path, python: Path, config: dict) -> InstallSpec:
    files = {home / item.path: item.text for item in render.rendered_files(runtime_mapping(config))}
    return InstallSpec(runtime=RUNTIME, files=files, json_merges=[settings_merge(home, kit, python)])
