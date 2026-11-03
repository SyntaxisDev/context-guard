"""Render the shipped content for one runtime from mapping data the adapter supplies."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
RUNTIME_SLOTS = frozenset(
    {
        "runtime_name",
        "handoff_dir",
        "role_table",
        "tier_table",
        "override_syntax",
        "transcripts",
        "handoff_rules",
        "subagent_delivery",
    }
)
CAPABILITY_LABELS = {
    "shell": "shell",
    "write": "edit files",
    "network": "network",
    "mcp": "MCP tools",
    "git_push": "git push / PRs",
}
CHANNELS = ("rule_file", "session_context")
ParentResume = Literal["native", "adapter", "none"]
PARENT_RESUMES = frozenset({"native", "adapter", "none"})


class RenderError(ValueError):
    """Content or mapping data that cannot be rendered completely."""


@dataclass(frozen=True)
class Role:
    """One delegation role declared in roles.json.

    Attributes:
        name: Role name, also the name of its body file.
        description: One-line description shown to the orchestrator.
        capabilities: Every declared capability mapped to whether the role has it.
        default_tier: Tier the role runs on unless overridden.
        allowed_tiers: Tiers the role may run on, including the default.
    """

    name: str
    description: str
    capabilities: Mapping[str, bool]
    default_tier: str
    allowed_tiers: tuple[str, ...]


@dataclass(frozen=True)
class Agent:
    """One native agent definition: a role, or a named variant of it.

    Attributes:
        name: Native agent name.
        role: The role the agent implements.
        tier: Tier the agent runs on.
        context: Variant context size label, or None for the runtime default.
        body: Rendered role body.
    """

    name: str
    role: Role
    tier: str
    context: str | None
    body: str


@dataclass(frozen=True)
class RenderedFile:
    """One rendered native file.

    Attributes:
        path: Path relative to the user home.
        text: Complete file contents.
    """

    path: str
    text: str


def no_enforced_limits(_capabilities: Mapping[str, bool]) -> frozenset[str]:
    """Report that the runtime enforces none of a role's denied capabilities.

    Args:
        _capabilities: The role's capabilities; unused.

    Returns:
        An empty set.
    """
    return frozenset()


@dataclass(frozen=True)
class RuntimeMapping:
    """Runtime slots and native rendering hooks; every path is relative to the user home.

    Attributes:
        runtime: Runtime id used in errors and manifests.
        runtime_name: Display name filled into the content.
        handoff_dir: Handoff directory shown in the content.
        tiers: Each tier name mapped to its native model profile.
        override_syntax: How the runtime overrides a tier for one spawn.
        transcripts: Where the runtime keeps past chats, as shown in the content.
        channel: How the policy reaches the model, one of CHANNELS.
        parent_resume: How completed background work resumes an idle parent.
        describe_tier: Render a native profile for the tier table.
        render_agent: Render one agent with its tier's native profile.
        skills_dir: Directory the skills are rendered into.
        policy_file: Policy file path; required by the "rule_file" channel.
        wrap_policy: Wrap the rendered policy in the runtime's rule format.
        enforced_limits: The denied capabilities the runtime enforces for a role's capabilities.
    """

    runtime: str
    runtime_name: str
    handoff_dir: str
    tiers: Mapping[str, Mapping[str, str]]
    override_syntax: str
    transcripts: str
    channel: str
    parent_resume: ParentResume
    describe_tier: Callable[[Mapping[str, str]], str]
    render_agent: Callable[[Agent, Mapping[str, str]], RenderedFile]
    skills_dir: str
    policy_file: str | None = None
    wrap_policy: Callable[[str], str] = str
    enforced_limits: Callable[[Mapping[str, bool]], frozenset[str]] = no_enforced_limits


def render(template: str, values: Mapping[str, str]) -> str:
    """Substitute every `{{token}}`; inserted values are not rendered again.

    Args:
        template: Text holding `{{token}}` placeholders.
        values: Token names mapped to their text.

    Returns:
        The template with every token replaced.

    Raises:
        RenderError: A token in the template has no value.
    """
    missing = sorted({match[1] for match in TOKEN.finditer(template)} - values.keys())
    if missing:
        msg = "unfilled tokens: " + ", ".join(missing)
        raise RenderError(msg)
    return TOKEN.sub(lambda match: values[match[1]], template)


def read_roles_file(content: Path) -> dict:
    """Read `roles/roles.json` from a content directory.

    Args:
        content: The content directory.

    Returns:
        The parsed file, which holds a `roles` object.

    Raises:
        RenderError: The file is unreadable, not JSON, or lacks a `roles` object.
    """
    try:
        data = json.loads((content / "roles" / "roles.json").read_text())
    except (OSError, ValueError) as error:
        msg = f"unreadable roles.json: {error}"
        raise RenderError(msg) from error
    if not isinstance(data, dict) or not isinstance(data.get("roles"), dict):
        msg = "roles.json must contain a roles object"
        raise RenderError(msg)
    return data


def parse_role(name: str, spec: dict, capabilities: list[str], tiers: list[str]) -> Role:
    """Validate one roles.json entry.

    Args:
        name: The role name.
        spec: The role's roles.json object.
        capabilities: Every capability a role must declare.
        tiers: Every known tier.

    Returns:
        The validated role.

    Raises:
        RenderError: Capabilities are not exactly `capabilities` as booleans, the tiers are
            invalid, or the description is missing.
    """
    caps = spec.get("capabilities")
    if (
        not isinstance(caps, dict)
        or sorted(caps) != sorted(capabilities)
        or not all(type(value) is bool for value in caps.values())
    ):
        msg = f"role {name}: capabilities must declare {capabilities} as booleans"
        raise RenderError(msg)
    allowed = tuple(spec.get("allowed_tiers") or ())
    if not allowed or not set(allowed) <= set(tiers) or spec.get("default_tier") not in allowed:
        msg = f"role {name}: default_tier must be one of allowed_tiers within {tiers}"
        raise RenderError(msg)
    if not isinstance(spec.get("description"), str) or not spec["description"]:
        msg = f"role {name}: description is required"
        raise RenderError(msg)
    return Role(name, spec["description"], dict(caps), spec["default_tier"], allowed)


def load_roles(content: Path = CONTENT_DIR) -> dict[str, Role]:
    """Load and validate every role.

    Args:
        content: The content directory.

    Returns:
        Role names mapped to roles, in file order.
    """
    data = read_roles_file(content)
    capabilities, tiers = data.get("capabilities") or [], data.get("tiers") or []
    return {name: parse_role(name, spec, capabilities, tiers) for name, spec in data["roles"].items()}


def load_tiers(content: Path = CONTENT_DIR) -> tuple[str, ...]:
    """Load the tier names declared in roles.json.

    Args:
        content: The content directory.

    Returns:
        The tiers in declared order; empty when none are declared.
    """
    return tuple(read_roles_file(content).get("tiers") or ())


def can_do(role: Role) -> str:
    """Describe what a role may do for the role table.

    Args:
        role: The role.

    Returns:
        A short phrase listing granted capabilities.
    """
    granted = [label for key, label in CAPABILITY_LABELS.items() if role.capabilities.get(key)]
    if not granted:
        return "read and search only"
    text = "read, " + ", ".join(granted)
    return text if role.capabilities.get("write") else text + "; no edits (evidence only under /tmp)"


def advisory_limits(role: Role, mapping: RuntimeMapping) -> str:
    """List the denied capabilities the runtime cannot enforce for this role; the brief must hold them.

    Args:
        role: The role.
        mapping: The runtime mapping that reports enforced limits.

    Returns:
        Comma-separated capability labels, or "none".
    """
    enforced = mapping.enforced_limits(role.capabilities)
    denied = [
        label for key, label in CAPABILITY_LABELS.items() if not role.capabilities.get(key) and key not in enforced
    ]
    return ", ".join(denied) or "none"


def role_table(roles: Mapping[str, Role], mapping: RuntimeMapping) -> str:
    """Render the Markdown role table for the policy.

    Args:
        roles: The roles to list, in order.
        mapping: The runtime mapping.

    Returns:
        The table without a trailing newline.
    """
    rows = [
        f"| role | can do | limits {mapping.runtime_name} does not enforce | default tier | allowed tiers |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows += [
        f"| `{role.name}` | {can_do(role)} | {advisory_limits(role, mapping)} | {role.default_tier} "
        f"| {', '.join(role.allowed_tiers)} |"
        for role in roles.values()
    ]
    return "\n".join(rows)


def tier_table(tiers: tuple[str, ...], mapping: RuntimeMapping) -> str:
    """Render the Markdown tier table for the policy.

    Args:
        tiers: The tiers to list, in order.
        mapping: The runtime mapping holding each tier's native profile.

    Returns:
        The table without a trailing newline.

    Raises:
        RenderError: A tier has no native profile in the mapping.
    """
    missing = [tier for tier in tiers if tier not in mapping.tiers]
    if missing:
        msg = f"{mapping.runtime}: no native profile for tiers {missing}"
        raise RenderError(msg)
    rows = ["| tier | runs on |", "| --- | --- |"]
    rows += [f"| {tier} | {mapping.describe_tier(mapping.tiers[tier])} |" for tier in tiers]
    return "\n".join(rows)


def subagent_delivery(mapping: RuntimeMapping) -> str:
    """Render completion-delivery policy from the runtime's parent-resume capability.

    Args:
        mapping: The runtime mapping.

    Returns:
        Instructions for spawning and receiving delegated work.

    Raises:
        RenderError: The mapping declares an unknown parent-resume capability.
    """
    if mapping.parent_resume not in PARENT_RESUMES:
        msg = f"{mapping.runtime}: unknown parent resume capability {mapping.parent_resume!r}"
        raise RenderError(msg)
    if mapping.parent_resume == "none":
        return (
            "Spawn independent subagents together. When their results are required to complete the current task, "
            "keep the parent turn active: after doing any useful local work, use one long `wait_agent` or equivalent "
            "event wait that covers the running subagents. Do not end the turn, poll, or issue repeated short waits "
            "while required work is outstanding. Fire-and-forget work may run in the background, but tell the user "
            "explicitly that delivery is delayed until their next turn because an idle parent cannot be resumed. Use "
            "a foreground subagent only for a short lookup (under ~2 minutes) whose answer your very next step needs."
        )
    return (
        "Spawn every subagent in the background where the runtime supports it, launching independent ones together "
        "in one message, since a user message stops the current turn's foreground subagents and a dropped connection "
        "can orphan a long one. Tell the user what is running, end your turn, and pick up each result as it arrives as "
        "a new turn; where the runtime does not start that turn itself, context-guard does with a short "
        "`[context-guard]` message. Do not poll or wait on running subagents. Use a foreground subagent only for a "
        "short lookup (under ~2 minutes) whose answer your very next step needs."
    )


def slot_values(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> dict[str, str]:
    """Compute the value of every runtime slot, including the rendered handoff rules.

    Args:
        mapping: The runtime mapping.
        content: The content directory.

    Returns:
        Slot names (RUNTIME_SLOTS) mapped to their text.
    """
    values = {
        "runtime_name": mapping.runtime_name,
        "handoff_dir": mapping.handoff_dir,
        "override_syntax": mapping.override_syntax,
        "transcripts": mapping.transcripts,
        "role_table": role_table(load_roles(content), mapping),
        "tier_table": tier_table(load_tiers(content), mapping),
        "subagent_delivery": subagent_delivery(mapping),
    }
    fragment = read_text(content / "policy" / "handoff.md")
    values["handoff_rules"] = render(fragment, values).strip()
    return values


def read_text(path: Path) -> str:
    """Read one content file.

    Args:
        path: The file.

    Returns:
        The file's text.

    Raises:
        RenderError: The file cannot be read.
    """
    try:
        return path.read_text()
    except OSError as error:
        msg = f"unreadable content file {path}: {error}"
        raise RenderError(msg) from error


def render_policy(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> str:
    """Render the orchestrator policy for one runtime.

    Args:
        mapping: The runtime mapping.
        content: The content directory.

    Returns:
        The policy text, before `wrap_policy`.
    """
    return render(read_text(content / "policy" / "orchestrator.md"), slot_values(mapping, content))


def render_skills(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> dict[str, str]:
    """Render every shipped skill for one runtime.

    Args:
        mapping: The runtime mapping.
        content: The content directory.

    Returns:
        Skill directory names mapped to their rendered SKILL.md text, sorted by name.
    """
    values = slot_values(mapping, content)
    return {
        path.parent.name: render(read_text(path), values) for path in sorted((content / "skills").glob("*/SKILL.md"))
    }


def agents(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> list[Agent]:
    """List role agents and declared variants with rendered bodies.

    Args:
        mapping: The runtime mapping.
        content: The content directory.

    Returns:
        One agent per role on its default tier, then one per variant.

    Raises:
        RenderError: A variant names an unknown role, a tier the role does not allow, or no name.
    """
    roles, values = load_roles(content), slot_values(mapping, content)
    bodies = {name: render(read_text(content / "roles" / f"{name}.md"), values) for name in roles}
    found = [Agent(name, role, role.default_tier, None, bodies[name]) for name, role in roles.items()]
    for variant in read_roles_file(content).get("variants") or []:
        role = roles.get(variant.get("role")) if isinstance(variant, dict) else None
        if role is None or variant.get("tier") not in role.allowed_tiers or not variant.get("name"):
            msg = f"invalid variant {variant!r}"
            raise RenderError(msg)
        found.append(Agent(variant["name"], role, variant["tier"], variant.get("context"), bodies[role.name]))
    return found


def render_agents(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> list[RenderedFile]:
    """Render every agent into the runtime's native agent file.

    Args:
        mapping: The runtime mapping.
        content: The content directory.

    Returns:
        One native file per agent.
    """
    return [mapping.render_agent(agent, mapping.tiers[agent.tier]) for agent in agents(mapping, content)]


def rendered_files(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> list[RenderedFile]:
    """Render every kit-owned native file for one runtime, ready for the installer.

    Args:
        mapping: The runtime mapping.
        content: The content directory.

    Returns:
        The policy file (rule_file channel only), the skills, then the agents.

    Raises:
        RenderError: The channel is unknown, or the rule_file channel has no policy_file.
    """
    if mapping.channel not in CHANNELS:
        msg = f"{mapping.runtime}: unknown channel {mapping.channel!r}"
        raise RenderError(msg)
    files = []
    if mapping.channel == "rule_file":
        if not mapping.policy_file:
            msg = f"{mapping.runtime}: rule_file channel needs policy_file"
            raise RenderError(msg)
        files.append(RenderedFile(mapping.policy_file, mapping.wrap_policy(render_policy(mapping, content))))
    files += [
        RenderedFile(f"{mapping.skills_dir}/{name}/SKILL.md", text)
        for name, text in render_skills(mapping, content).items()
    ]
    return files + render_agents(mapping, content)
