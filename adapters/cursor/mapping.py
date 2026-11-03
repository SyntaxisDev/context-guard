"""Cursor-native rendering of the shared content and the install plan for ~/.cursor."""

import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import NamedTuple

from core import deadline, hookfile, install, render
from core.install_spec import InstallSpec, JsonMerge, frontmatter_signatures
from core.observation import PROMPT, SESSION, STOP, TOOL


class HookEvent(NamedTuple):
    """How a native Cursor hook event is registered and observed.

    Attributes:
        event: The core event the native hook maps to.
        options: Extra hooks.json keys for its registration, as (key, value) pairs.
        spawn_only: Whether the hook only observes spawns (a preToolUse on Task).
    """

    event: str
    options: tuple[tuple[str, object], ...] = ()
    spawn_only: bool = False


RUNTIME = "cursor"
CURSOR_DIR = ".cursor"
HOOKS_FILE = f"{CURSOR_DIR}/hooks.json"
HOOKS_LABEL = "hooks.json"
AGENTS_DIR = f"{CURSOR_DIR}/agents"
SKILLS_DIR = f"{CURSOR_DIR}/skills"
POLICY_FILE = f"{CURSOR_DIR}/rules/context-guard.mdc"
HOOK_SCRIPT = "adapters/cursor/hooks.py"
SESSION_MARKER = "context-guard hooks: active."
PLUGIN_HOOKS = "cursor-hooks"
HOOK_EVENTS = {
    "sessionStart": HookEvent(SESSION),
    "beforeSubmitPrompt": HookEvent(PROMPT),
    "preToolUse": HookEvent(TOOL, (("matcher", "Task"),), spawn_only=True),
    "postToolUse": HookEvent(TOOL),
    "stop": HookEvent(STOP, (("loop_limit", 1),)),
}
OVERRIDE_SYNTAX = (
    "the Task tool `model` argument set to the tier's slug from this table; pass it on every Task, not only to "
    "override, because plugin agents inherit the parent's model and the Cursor CLI ignores `~/.cursor/agents`. Use "
    "the role's default tier slug unless you are stepping up. The user authorises this; never omit it"
)
TRANSCRIPTS = "Cursor keeps chat transcripts as JSONL files under `~/.cursor/projects/<workspace>/agent-transcripts/`."
POLICY_FRONTMATTER = (
    "---\ndescription: context-guard delegation, context and handoff policy\nalwaysApply: true\n---\n\n"
)


def describe_tier(profile: dict[str, str]) -> str:
    """Describe a tier for the policy's tier table.

    Args:
        profile: Tier profile with the `call` slug.

    Returns:
        Markdown naming the slug.
    """
    return f"`{profile['call']}`"


def render_agent(agent: render.Agent, profile: dict[str, str]) -> render.RenderedFile:
    """Render a role as a Cursor agent file.

    Cursor can only restrict a role by `readonly`, which also removes the shell. The model is
    the tier's plain `call` slug: Cursor reads no option list there and silently falls back to
    the parent's model for an unknown id. It has no per-agent context size, so variants differ
    only in name.

    Args:
        agent: The role variant to render.
        profile: Tier profile whose `call` slug the agent runs on.

    Returns:
        The agent markdown file under the agents directory.
    """
    readonly = "false" if agent.role.capabilities["shell"] else "true"
    lines = [
        "---",
        f"name: {agent.name}",
        f"description: {json.dumps(agent.role.description)}",
        f"model: {profile['call']}",
        f"readonly: {readonly}",
        "---",
        "",
    ]
    return render.RenderedFile(f"{AGENTS_DIR}/{agent.name}.md", "\n".join(lines) + "\n" + agent.body.strip() + "\n")


def enforced_limits(capabilities: dict[str, bool]) -> frozenset[str]:
    """Name the capability limits Cursor enforces for a role.

    `readonly` blocks edits and the shell; everything else a shell role could do anyway.

    Args:
        capabilities: The role's capability flags.

    Returns:
        `shell` and `write` for a role without shell, else nothing.
    """
    return frozenset() if capabilities["shell"] else frozenset({"shell", "write"})


def wrap_policy(text: str) -> str:
    """Wrap the rendered policy as an always-applied Cursor rule.

    Args:
        text: Rendered policy markdown.

    Returns:
        The rule file text with its frontmatter.
    """
    return POLICY_FRONTMATTER + text.strip() + "\n"


def runtime_mapping(config: dict) -> render.RuntimeMapping:
    """Describe Cursor's rendering of the shared content.

    Args:
        config: Loaded Cursor adapter configuration.

    Returns:
        The runtime mapping the renderer uses for agents, skills and the policy rule.
    """
    return render.RuntimeMapping(
        runtime=RUNTIME,
        runtime_name="Cursor",
        handoff_dir=config["paths"]["handoff_dir"],
        tiers=config["tiers"],
        override_syntax=OVERRIDE_SYNTAX,
        transcripts=TRANSCRIPTS,
        channel="rule_file",
        parent_resume="native",
        describe_tier=describe_tier,
        render_agent=render_agent,
        skills_dir=SKILLS_DIR,
        policy_file=POLICY_FILE,
        wrap_policy=wrap_policy,
        enforced_limits=enforced_limits,
    )


def unmerge_hooks(doc: dict, script: str) -> dict:
    """Remove managed entries and emptied events; keep every other event, entry and key.

    An emptied `hooks` object goes too, and a `version` left alone means nothing else
    remains, so a hooks.json the install created can be deleted on uninstall. A malformed
    `hooks` section raises ValueError from `hookfile.event_lists`.

    Args:
        doc: Parsed hooks.json; it is not modified.
        script: Path of the managed hook script that identifies our entries.

    Returns:
        A copy without our entries, or `{}` when nothing but `version` remains.
    """
    result = json.loads(json.dumps(doc))
    hooks = hookfile.event_lists(result, HOOKS_LABEL)
    for event in list(hooks):
        hooks[event] = [entry for entry in hooks[event] if not hookfile.runs_script(entry, script)]
        if not hooks[event]:
            del hooks[event]
    if "hooks" in result and not hooks:
        del result["hooks"]
    return {} if set(result) == {"version"} else result


def merge_hooks(doc: dict, registrations: dict[str, dict], script: str) -> dict:
    """Register our hook entries in hooks.json.

    A file already holding exactly our entries is returned as is, so user hooks added after
    ours are not reordered. A malformed `hooks` section raises ValueError from
    `hookfile.event_lists`.

    Args:
        doc: Parsed hooks.json; it is not modified.
        registrations: Our entry per native event.
        script: Path of the managed hook script that identifies our entries.

    Returns:
        The document with a `version` and our entries appended after the user's.
    """
    owned = {
        event: [entry for entry in entries if hookfile.runs_script(entry, script)]
        for event, entries in hookfile.event_lists(doc, HOOKS_LABEL).items()
    }
    expected = {native: [entry] for native, entry in registrations.items()}
    if "version" in doc and {event: found for event, found in owned.items() if found} == expected:
        return doc
    result = unmerge_hooks(doc, script)
    result.setdefault("version", 1)
    hooks = result.setdefault("hooks", {})
    for native, entry in registrations.items():
        hooks.setdefault(native, []).append(entry)
    return result


def registrations(command: Callable[[str], str]) -> dict[str, dict]:
    """Build the hooks.json entry of every registered native event.

    Args:
        command: Builds an event's hook command: the installed kit's or the plugin launcher's.

    Returns:
        Entry per native event: command, timeout and the event's options.
    """
    return {
        native: {
            "command": command(native),
            "timeout": deadline.HOOK_TIMEOUT_SECONDS,
            **dict(spec.options),
        }
        for native, spec in HOOK_EVENTS.items()
    }


def install_spec(home: Path, kit: Path, python: Path, config: dict) -> InstallSpec:
    """Plan the Cursor install: rendered files under ~/.cursor and the hooks.json merge.

    Args:
        home: Home directory to install into.
        kit: Installed kit root.
        python: Interpreter that runs the hook script.
        config: Loaded Cursor adapter configuration.

    Returns:
        The install spec for the shared installer.
    """
    mapping = runtime_mapping(config)
    files = install.unclaimed(home, RUNTIME, {home / item.path: item.text for item in render.rendered_files(mapping)})
    script = str(kit / HOOK_SCRIPT)
    hooks = JsonMerge(
        home / HOOKS_FILE,
        partial(
            merge_hooks,
            registrations=registrations(partial(hookfile.hook_command, python, kit / HOOK_SCRIPT)),
            script=script,
        ),
        partial(unmerge_hooks, script=script),
    )
    return InstallSpec(
        RUNTIME,
        files=files,
        json_merges=[hooks],
        kit_signatures=frontmatter_signatures(home / AGENTS_DIR, render.agents(mapping)),
    )
