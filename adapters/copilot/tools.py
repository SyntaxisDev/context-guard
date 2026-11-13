"""Classify recorded Copilot tool calls for handoff credit and spawn logging."""

import re
from pathlib import Path

from core import config as config_mod, handoffs, spawn
from core.observation import READ, SPAWN, WRITE, ToolUse

PATCH_TARGET = re.compile(r"^\*\*\* (?:Add|Update) File: (.+?)\s*$", re.MULTILINE)


def text(value: object) -> str | None:
    """Read nonempty protocol text.

    Args:
        value: Untrusted JSON value.

    Returns:
        Nonempty text without NUL, otherwise None.
    """
    return value if isinstance(value, str) and value and "\0" not in value else None


def role_name(value: object) -> str | None:
    """Normalize only the context-guard plugin's own role namespace.

    Args:
        value: Native agent type.

    Returns:
        The shared role name for this plugin, otherwise the native name or None.
    """
    name = text(value)
    return name.removeprefix("context-guard:") if name else None


def profile_model(profile: dict) -> tuple[str | None, str | None]:
    """Read model and effort from a tier profile.

    Args:
        profile: Configured tier.

    Returns:
        Model and effort when provided.
    """
    return text(profile.get("model")), text(profile.get("effort"))


def spawn_detail(data: dict, tiers: dict) -> dict:
    """Describe a recorded task tool invocation.

    Args:
        data: Native tool arguments.
        tiers: Configured tier profiles.

    Returns:
        Role, tier, model and effort, preserving explicit model selection.
    """
    role = role_name(data.get("agent_type"))
    detail = spawn.default_detail(role, tiers, profile_model)
    model = text(data.get("model"))
    if model:
        detail.update(
            model=model, tier=next((key for key, value in tiers.items() if value.get("model") == model), None)
        )
    return detail


def tool_use(payload: dict, config: dict, home: Path) -> ToolUse | None:
    """Classify successful tools with the recorded toolArgs and toolResult shape.

    Args:
        payload: Native postToolUse input.
        config: Loaded configuration.
        home: Home used to resolve handoff paths.

    Returns:
        Spawn, view read, patch write, shell handoff access or an unclassified tool; None for failed calls.
    """
    name = text(payload.get("toolName"))
    result = payload.get("toolResult")
    if not name or not isinstance(result, dict) or result.get("resultType") != "success":
        return None
    data = payload.get("toolArgs")
    if name == "apply_patch" and isinstance(data, str):
        path = handoffs.store_path(
            PATCH_TARGET.findall(data), payload.get("cwd") or "", home, config_mod.handoff_dir(config, home)
        )
        return ToolUse(name, WRITE, path)
    if not isinstance(data, dict):
        return ToolUse(name)
    if name == "task":
        return ToolUse(name, SPAWN, detail=spawn_detail(data, config["tiers"]))
    if name == "view":
        return ToolUse(name, READ, text(data.get("path")))
    command = text(data.get("command")) if name == "bash" else None
    access = command and handoffs.shell_access(
        command, payload.get("cwd") or "", home, config_mod.handoff_dir(config, home)
    )
    return ToolUse(name, *access) if access else ToolUse(name)
