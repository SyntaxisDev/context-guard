"""Classify Codex PostToolUse payloads: spawn_agent spawns and handoff reads/writes.

Codex has no read tool: reads are shell commands (`Bash`, or the `exec_command`/`shell` function
names) and writes are `apply_patch` patches. Code mode runs both through one `exec` tool whose
input is JavaScript calling `tools.exec_command({cmd: "..."})` and `tools.apply_patch("...")`.
"""

import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path

from core import handoffs, spawn
from core.observation import OTHER, SPAWN, WRITE, ToolUse

SPAWN_TOOL = "spawn_agent"
SPAWN_TOOLS = {SPAWN_TOOL, "Agent"}
PATCH_TOOL = "apply_patch"
SHELL_TOOLS = {"Bash", "exec_command", "shell", "shell_command", "local_shell"}
CODE_MODE_TOOL = "exec"
COMMAND_KEYS = ("command", "cmd", "input", "code")
PATCH_TARGET = re.compile(r"^\*\*\* (?:(?:Add|Update) File|Move to): (.+?)\s*$", re.MULTILINE)
JS_STRING = r'("(?:[^"\\]|\\.)*")'
JS_COMMAND = re.compile(r'\bcmd"?\s*:\s*' + JS_STRING)
JS_PATCH = re.compile(r"\btools\.apply_patch\(\s*" + JS_STRING)


def text(value: object) -> str | None:
    """Validate an untrusted JSON text field.

    Args:
        value: Untrusted JSON value.

    Returns:
        The value when it is a non-empty string, else None.
    """
    return value if isinstance(value, str) and value else None


def command_text(tool_input: object) -> str:
    """Extract the command or patch text of a tool input.

    Args:
        tool_input: Untrusted tool input: the text itself, or an object holding it under a
            command key, as a string or an argv list.

    Returns:
        The text, an argv list joined with shell quoting, or an empty string.
    """
    if isinstance(tool_input, str):
        return tool_input
    values = [tool_input.get(key) for key in COMMAND_KEYS] if isinstance(tool_input, Mapping) else []
    command = next((value for value in values if value), None)
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return shlex.join(command)
    return command if isinstance(command, str) else ""


def js_strings(pattern: re.Pattern[str], source: str) -> list[str]:
    """Decode the JavaScript string literals a pattern captures in code-mode source.

    Args:
        pattern: Pattern whose first group captures a double-quoted literal.
        source: Code-mode JavaScript.

    Returns:
        Every literal that decodes as a JSON string; others are skipped.
    """
    decoded = []
    for literal in pattern.findall(source):
        try:
            decoded.append(json.loads(literal))
        except ValueError:
            continue
    return decoded


def infer_tier(model: str | None, effort: str | None, tiers: Mapping[str, object]) -> str | None:
    """Find the tier a spawn's model and effort belong to.

    Args:
        model: Spawned model, or None when unknown.
        effort: Spawned reasoning effort, or None.
        tiers: Tier profiles from the configuration.

    Returns:
        The tier matching model and effort, else the only tier with that model, else None.
    """
    if model is None:
        return None
    profiles = [(tier, profile) for tier, profile in tiers.items() if isinstance(profile, Mapping)]
    exact = [tier for tier, profile in profiles if profile.get("model") == model and profile.get("effort") == effort]
    by_model = [tier for tier, profile in profiles if profile.get("model") == model]
    return exact[0] if exact else (by_model[0] if len(by_model) == 1 else None)


def profile_model(profile: Mapping[str, object]) -> tuple[str | None, str | None]:
    """Read a tier profile's model and effort.

    Args:
        profile: One tier profile from the configuration.

    Returns:
        Model and effort, each None when not text.
    """
    return text(profile.get("model")), text(profile.get("effort"))


def spawn_detail(tool_input: Mapping[str, object], tiers: Mapping[str, object]) -> dict[str, str | None]:
    """Describe a spawn_agent call by role, tier, model and effort.

    Without a model the spawn runs on its role file, which pins the role's default tier.

    Args:
        tool_input: The spawn tool input from the payload.
        tiers: Tier profiles from the configuration.

    Returns:
        The spawn detail logged with the event; the tier is None when it cannot be inferred.
    """
    role = text(tool_input.get("agent_type"))
    model, effort = text(tool_input.get("model")), text(tool_input.get("reasoning_effort"))
    if model is None:
        default = spawn.default_detail(role, tiers, profile_model)
        if effort is None or effort == default["effort"]:
            return default
        model = default["model"]
    return {"role": role, "tier": infer_tier(model, effort, tiers), "model": model, "effort": effort}


def shell_cwd(tool_input: object, cwd: str) -> str:
    """Resolve the directory a shell command runs in.

    Args:
        tool_input: Untrusted tool input; its optional `workdir` is relative to the session cwd.
        cwd: The session's working directory.

    Returns:
        `workdir` joined to the session cwd, else the session cwd.
    """
    workdir = text(tool_input.get("workdir")) if isinstance(tool_input, Mapping) else None
    return str(Path(cwd) / workdir) if workdir else cwd


def code_mode_access(source: str, cwd: str, home: Path, store: Path) -> tuple[str, str] | None:
    """Find the handoff a code-mode script writes or reads.

    Args:
        source: Code-mode JavaScript.
        cwd: The session's working directory.
        home: Home directory that the store resolves against.
        store: Handoff store directory.

    Returns:
        (write, path) for a patched handoff, else the first (read or write, path) a command
        names, else None.
    """
    targets = [target for patch in js_strings(JS_PATCH, source) for target in PATCH_TARGET.findall(patch)]
    written = handoffs.store_path(targets, cwd, home, store)
    if written is not None:
        return WRITE, written
    accesses = (handoffs.shell_access(command, cwd, home, store) for command in js_strings(JS_COMMAND, source))
    return next((access for access in accesses if access is not None), None)


def is_spawn(name: str) -> bool:
    """Tell whether a hook tool name is a spawn.

    Codex names `spawn_agent` from a non-default tool namespace by joining namespace and name
    without a separator (`collaborationspawn_agent`), and the namespace is configurable.

    Args:
        name: The payload's `tool_name`.

    Returns:
        True for `spawn_agent` under any namespace and for `Agent`.
    """
    base = name.rsplit(".", 1)[-1]
    return base in SPAWN_TOOLS or base.endswith(SPAWN_TOOL)


def classify(
    payload: Mapping[str, object], cwd: str, home: Path, store: Path, tiers: Mapping[str, object]
) -> ToolUse | None:
    """Classify the tool of a PostToolUse payload.

    Args:
        payload: Validated hook payload.
        cwd: The session's working directory.
        home: Home directory that the store resolves against.
        store: Handoff store directory.
        tiers: Tier profiles from the configuration.

    Returns:
        A spawn, a handoff read or write, or an unclassified tool use; None without a tool name.
    """
    name, tool_input = text(payload.get("tool_name")), payload.get("tool_input")
    if name is None:
        return None
    if is_spawn(name) and isinstance(tool_input, Mapping):
        return ToolUse(name, SPAWN, detail=spawn_detail(tool_input, tiers))
    if name == PATCH_TOOL:
        targets = PATCH_TARGET.findall(command_text(tool_input))
        return ToolUse(name, WRITE, handoffs.store_path(targets, cwd, home, store))
    access = None
    if name in SHELL_TOOLS:
        access = handoffs.shell_access(command_text(tool_input), shell_cwd(tool_input, cwd), home, store)
    elif name == CODE_MODE_TOOL:
        access = code_mode_access(command_text(tool_input), cwd, home, store)
    return ToolUse(name, access[0], access[1]) if access is not None else ToolUse(name, OTHER)
