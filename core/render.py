"""Render the shipped content for one runtime from mapping data the adapter supplies."""
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
RUNTIME_SLOTS = frozenset({"runtime_name", "handoff_dir", "role_table", "tier_table",
                           "override_syntax", "transcripts", "handoff_rules"})
CAPABILITY_LABELS = {"shell": "shell", "write": "edit files", "network": "network",
                     "mcp": "MCP tools", "git_push": "git push / PRs"}
CHANNELS = ("rule_file", "session_context")


class RenderError(ValueError):
    """Content or mapping data that cannot be rendered completely."""


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    capabilities: Mapping[str, bool]
    default_tier: str
    allowed_tiers: tuple[str, ...]


@dataclass(frozen=True)
class Agent:
    """One native agent definition: a role, or a named variant of it."""
    name: str
    role: Role
    tier: str
    context: str | None
    body: str


@dataclass(frozen=True)
class RenderedFile:
    path: str
    text: str


@dataclass(frozen=True)
class RuntimeMapping:
    """Runtime slots and native rendering hooks; every path is relative to the user home."""
    runtime: str
    runtime_name: str
    handoff_dir: str
    tiers: Mapping[str, Mapping[str, str]]
    override_syntax: str
    transcripts: str
    channel: str
    describe_tier: Callable[[Mapping[str, str]], str]
    render_agent: Callable[[Agent, Mapping[str, str]], RenderedFile]
    skills_dir: str
    policy_file: str | None = None
    wrap_policy: Callable[[str], str] = str
    enforced_limits: Callable[[Mapping[str, bool]], frozenset[str]] = lambda capabilities: frozenset()


def render(template: str, values: Mapping[str, str]) -> str:
    """Substitute every {{token}}; inserted values are not rendered again."""
    missing = sorted({match[1] for match in TOKEN.finditer(template)} - values.keys())
    if missing:
        raise RenderError("unfilled tokens: " + ", ".join(missing))
    return TOKEN.sub(lambda match: values[match[1]], template)


def read_roles_file(content: Path) -> dict:
    try:
        data = json.loads((content / "roles" / "roles.json").read_text())
    except (OSError, ValueError) as error:
        raise RenderError(f"unreadable roles.json: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("roles"), dict):
        raise RenderError("roles.json must contain a roles object")
    return data


def parse_role(name: str, spec: dict, capabilities: list[str], tiers: list[str]) -> Role:
    caps = spec.get("capabilities")
    if not isinstance(caps, dict) or sorted(caps) != sorted(capabilities) \
            or not all(type(value) is bool for value in caps.values()):
        raise RenderError(f"role {name}: capabilities must declare {capabilities} as booleans")
    allowed = tuple(spec.get("allowed_tiers") or ())
    if not allowed or not set(allowed) <= set(tiers) or spec.get("default_tier") not in allowed:
        raise RenderError(f"role {name}: default_tier must be one of allowed_tiers within {tiers}")
    if not isinstance(spec.get("description"), str) or not spec["description"]:
        raise RenderError(f"role {name}: description is required")
    return Role(name, spec["description"], dict(caps), spec["default_tier"], allowed)


def load_roles(content: Path = CONTENT_DIR) -> dict[str, Role]:
    data = read_roles_file(content)
    capabilities, tiers = data.get("capabilities") or [], data.get("tiers") or []
    return {name: parse_role(name, spec, capabilities, tiers) for name, spec in data["roles"].items()}


def load_tiers(content: Path = CONTENT_DIR) -> tuple[str, ...]:
    return tuple(read_roles_file(content).get("tiers") or ())


def can_do(role: Role) -> str:
    granted = [label for key, label in CAPABILITY_LABELS.items() if role.capabilities.get(key)]
    if not granted:
        return "read and search only"
    text = "read, " + ", ".join(granted)
    return text if role.capabilities.get("write") else text + "; no edits (evidence only under /tmp)"


def advisory_limits(role: Role, mapping: RuntimeMapping) -> str:
    """Denied capabilities the runtime cannot enforce for this role; the brief must hold them."""
    enforced = mapping.enforced_limits(role.capabilities)
    denied = [label for key, label in CAPABILITY_LABELS.items()
              if not role.capabilities.get(key) and key not in enforced]
    return ", ".join(denied) or "none"


def role_table(roles: Mapping[str, Role], mapping: RuntimeMapping) -> str:
    rows = [f"| role | can do | limits {mapping.runtime_name} does not enforce | default tier | allowed tiers |",
            "| --- | --- | --- | --- | --- |"]
    rows += [f"| `{role.name}` | {can_do(role)} | {advisory_limits(role, mapping)} | {role.default_tier} "
             f"| {', '.join(role.allowed_tiers)} |" for role in roles.values()]
    return "\n".join(rows)


def tier_table(tiers: tuple[str, ...], mapping: RuntimeMapping) -> str:
    missing = [tier for tier in tiers if tier not in mapping.tiers]
    if missing:
        raise RenderError(f"{mapping.runtime}: no native profile for tiers {missing}")
    rows = ["| tier | runs on |", "| --- | --- |"]
    rows += [f"| {tier} | {mapping.describe_tier(mapping.tiers[tier])} |" for tier in tiers]
    return "\n".join(rows)


def slot_values(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> dict[str, str]:
    values = {"runtime_name": mapping.runtime_name, "handoff_dir": mapping.handoff_dir,
              "override_syntax": mapping.override_syntax, "transcripts": mapping.transcripts,
              "role_table": role_table(load_roles(content), mapping),
              "tier_table": tier_table(load_tiers(content), mapping)}
    fragment = read_text(content / "policy" / "handoff.md")
    values["handoff_rules"] = render(fragment, values).strip()
    return values


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as error:
        raise RenderError(f"unreadable content file {path}: {error}") from error


def render_policy(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> str:
    return render(read_text(content / "policy" / "orchestrator.md"), slot_values(mapping, content))


def render_skills(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> dict[str, str]:
    values = slot_values(mapping, content)
    return {path.parent.name: render(read_text(path), values)
            for path in sorted((content / "skills").glob("*/SKILL.md"))}


def agents(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> list[Agent]:
    """List role agents and declared variants with rendered bodies."""
    roles, values = load_roles(content), slot_values(mapping, content)
    bodies = {name: render(read_text(content / "roles" / f"{name}.md"), values) for name in roles}
    found = [Agent(name, role, role.default_tier, None, bodies[name]) for name, role in roles.items()]
    for variant in read_roles_file(content).get("variants") or []:
        role = roles.get(variant.get("role")) if isinstance(variant, dict) else None
        if role is None or variant.get("tier") not in role.allowed_tiers or not variant.get("name"):
            raise RenderError(f"invalid variant {variant!r}")
        found.append(Agent(variant["name"], role, variant["tier"], variant.get("context"), bodies[role.name]))
    return found


def render_agents(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> list[RenderedFile]:
    return [mapping.render_agent(agent, mapping.tiers[agent.tier]) for agent in agents(mapping, content)]


def rendered_files(mapping: RuntimeMapping, content: Path = CONTENT_DIR) -> list[RenderedFile]:
    """Every kit-owned native file for one runtime, ready for the installer."""
    if mapping.channel not in CHANNELS:
        raise RenderError(f"{mapping.runtime}: unknown channel {mapping.channel!r}")
    files = []
    if mapping.channel == "rule_file":
        if not mapping.policy_file:
            raise RenderError(f"{mapping.runtime}: rule_file channel needs policy_file")
        files.append(RenderedFile(mapping.policy_file, mapping.wrap_policy(render_policy(mapping, content))))
    files += [RenderedFile(f"{mapping.skills_dir}/{name}/SKILL.md", text)
              for name, text in render_skills(mapping, content).items()]
    return files + render_agents(mapping, content)
