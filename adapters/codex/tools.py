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

from core import handoffs
from core.observation import OTHER, SPAWN, WRITE, ToolUse

SPAWN_TOOLS = {"spawn_agent", "Agent"}
PATCH_TOOL = "apply_patch"
SHELL_TOOLS = {"Bash", "exec_command", "shell", "shell_command", "local_shell"}
CODE_MODE_TOOL = "exec"
COMMAND_KEYS = ("command", "cmd", "input", "code")
PATCH_TARGET = re.compile(r"^\*\*\* (?:(?:Add|Update) File|Move to): (.+?)\s*$", re.MULTILINE)
JS_STRING = r'("(?:[^"\\]|\\.)*")'
JS_COMMAND = re.compile(r'\bcmd"?\s*:\s*' + JS_STRING)
JS_PATCH = re.compile(r"\btools\.apply_patch\(\s*" + JS_STRING)


def text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def command_text(tool_input: object) -> str:
    if isinstance(tool_input, str):
        return tool_input
    values = [tool_input.get(key) for key in COMMAND_KEYS] if isinstance(tool_input, Mapping) else []
    command = next((value for value in values if value), None)
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return shlex.join(command)
    return command if isinstance(command, str) else ""


def js_strings(pattern: re.Pattern, source: str) -> list[str]:
    decoded = []
    for literal in pattern.findall(source):
        try:
            decoded.append(json.loads(literal))
        except ValueError:
            continue
    return decoded


def infer_tier(model: str | None, effort: str | None, tiers: Mapping) -> str | None:
    if model is None:
        return None
    profiles = [(tier, profile) for tier, profile in tiers.items() if isinstance(profile, Mapping)]
    exact = [tier for tier, profile in profiles if profile.get("model") == model and profile.get("effort") == effort]
    by_model = [tier for tier, profile in profiles if profile.get("model") == model]
    return exact[0] if exact else (by_model[0] if len(by_model) == 1 else None)


def spawn_detail(tool_input: Mapping, tiers: Mapping) -> dict:
    model, effort = text(tool_input.get("model")), text(tool_input.get("reasoning_effort"))
    return {"role": text(tool_input.get("agent_type")), "tier": infer_tier(model, effort, tiers),
            "model": model, "effort": effort}


def code_mode_access(source: str, cwd: str, home: Path, store: Path) -> tuple[str, str] | None:
    targets = [target for patch in js_strings(JS_PATCH, source) for target in PATCH_TARGET.findall(patch)]
    written = handoffs.store_path(targets, cwd, home, store)
    if written is not None:
        return WRITE, written
    accesses = (handoffs.shell_access(command, cwd, home, store) for command in js_strings(JS_COMMAND, source))
    return next((access for access in accesses if access is not None), None)


def classify(payload: Mapping, cwd: str, home: Path, store: Path, tiers: Mapping) -> ToolUse | None:
    name, tool_input = text(payload.get("tool_name")), payload.get("tool_input")
    if name is None:
        return None
    if name.rsplit(".", 1)[-1] in SPAWN_TOOLS and isinstance(tool_input, Mapping):
        return ToolUse(name, SPAWN, detail=spawn_detail(tool_input, tiers))
    if name == PATCH_TOOL:
        targets = PATCH_TARGET.findall(command_text(tool_input))
        return ToolUse(name, WRITE, handoffs.store_path(targets, cwd, home, store))
    access = None
    if name in SHELL_TOOLS:
        access = handoffs.shell_access(command_text(tool_input), cwd, home, store)
    elif name == CODE_MODE_TOOL:
        access = code_mode_access(command_text(tool_input), cwd, home, store)
    return ToolUse(name, access[0], access[1]) if access is not None else ToolUse(name, OTHER)
