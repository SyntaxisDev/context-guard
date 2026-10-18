"""Cursor-native rendering of the shared content and the install plan for ~/.cursor."""
import json
import re
import shlex
from functools import partial
from pathlib import Path

from core import render
from core.install_spec import InstallSpec, JsonMerge

RUNTIME = "cursor"
CURSOR_DIR = ".cursor"
HOOKS_FILE = f"{CURSOR_DIR}/hooks.json"
AGENTS_DIR = f"{CURSOR_DIR}/agents"
SKILLS_DIR = f"{CURSOR_DIR}/skills"
POLICY_FILE = f"{CURSOR_DIR}/rules/context-guard.mdc"
HOOK_SCRIPT = "adapters/cursor/hooks.py"
OVERRIDE_SYNTAX = ("the Task tool `model` argument set to the tier's slug from this table; omit it to run the "
                   "role's default tier")
TRANSCRIPTS = "Cursor keeps chat transcripts as JSONL files under `~/.cursor/projects/<workspace>/agent-transcripts/`."
POLICY_FRONTMATTER = ("---\ndescription: context-guard delegation, context and handoff policy\n"
                      "alwaysApply: true\n---\n\n")
CONTEXT_OPTION = re.compile(r"context=[^,\]]*")


def describe_tier(profile: dict) -> str:
    return f"`{profile['call']}` (agent files use `{profile['agent_model']}`)"


def with_context(model: str, context: str | None) -> str:
    """Set the context option of a Cursor `id[opt=val,...]` model for a variant agent."""
    if not context:
        return model
    if CONTEXT_OPTION.search(model):
        return CONTEXT_OPTION.sub(f"context={context}", model)
    if model.endswith("]"):
        return f"{model[:-1]},context={context}]"
    return f"{model}[context={context}]"


def render_agent(agent: render.Agent, profile: dict) -> render.RenderedFile:
    """Cursor can only restrict a role by `readonly`, which also removes the shell."""
    readonly = "false" if agent.role.capabilities["shell"] else "true"
    lines = ["---", f"name: {agent.name}", f"description: {json.dumps(agent.role.description)}",
             f"model: {with_context(profile['agent_model'], agent.context)}", f"readonly: {readonly}", "---", ""]
    return render.RenderedFile(f"{AGENTS_DIR}/{agent.name}.md", "\n".join(lines) + "\n" + agent.body.strip() + "\n")


def enforced_limits(capabilities: dict) -> frozenset[str]:
    """`readonly` blocks edits and the shell; everything else a shell role could do anyway."""
    return frozenset() if capabilities["shell"] else frozenset({"shell", "write"})


def wrap_policy(text: str) -> str:
    return POLICY_FRONTMATTER + text.strip() + "\n"


def runtime_mapping(config: dict) -> render.RuntimeMapping:
    return render.RuntimeMapping(
        runtime=RUNTIME, runtime_name="Cursor", handoff_dir=config["paths"]["handoff_dir"], tiers=config["tiers"],
        override_syntax=OVERRIDE_SYNTAX, transcripts=TRANSCRIPTS, channel="rule_file", describe_tier=describe_tier,
        render_agent=render_agent, skills_dir=SKILLS_DIR, policy_file=POLICY_FILE, wrap_policy=wrap_policy,
        enforced_limits=enforced_limits)


def hook_command(python: Path, kit: Path, native: str) -> str:
    return shlex.join([str(python), str(kit / HOOK_SCRIPT), native])


def managed(entry: object, script: str) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        return False
    try:
        tokens = shlex.split(entry["command"])
    except ValueError:
        return False
    return script in tokens


def hook_lists(doc: dict) -> dict:
    hooks = doc.get("hooks", {})
    if not isinstance(hooks, dict) or not all(isinstance(entries, list) for entries in hooks.values()):
        raise ValueError("hooks.json: hooks must map event names to lists")
    return hooks


def unmerge_hooks(doc: dict, script: str) -> dict:
    """Remove managed entries and emptied events; keep every other event, entry and key.

    An emptied `hooks` object goes too, and a `version` left alone means nothing else
    remains, so a hooks.json the install created can be deleted on uninstall.
    """
    result = json.loads(json.dumps(doc))
    hooks = hook_lists(result)
    for event in list(hooks):
        hooks[event] = [entry for entry in hooks[event] if not managed(entry, script)]
        if not hooks[event]:
            del hooks[event]
    if "hooks" in result and not hooks:
        del result["hooks"]
    return {} if set(result) == {"version"} else result


def merge_hooks(doc: dict, registrations: dict[str, dict], script: str) -> dict:
    """Register our entries; a file already holding exactly them is returned as is, so user hooks
    added after ours are not reordered."""
    owned = {event: [entry for entry in entries if managed(entry, script)]
             for event, entries in hook_lists(doc).items()}
    expected = {native: [entry] for native, entry in registrations.items()}
    if "version" in doc and {event: found for event, found in owned.items() if found} == expected:
        return doc
    result = unmerge_hooks(doc, script)
    result.setdefault("version", 1)
    hooks = result.setdefault("hooks", {})
    for native, entry in registrations.items():
        hooks.setdefault(native, []).append(entry)
    return result


def registrations(config: dict, python: Path, kit: Path) -> dict[str, dict]:
    return {native: {"command": hook_command(python, kit, native), **spec.get("hook", {})}
            for native, spec in config["hook_events"].items()}


def kit_signatures(home: Path, mapping: render.RuntimeMapping) -> dict[Path, tuple[str, ...]]:
    """An unrecorded kit agent carries its role's name and the first line of its body."""
    signatures = {}
    for agent in render.agents(mapping):
        first = agent.body.strip().splitlines()[0].strip()
        signatures[home / AGENTS_DIR / f"{agent.name}.md"] = ("---", f"name: {agent.name}", first)
    return signatures


def install_spec(home: Path, kit: Path, python: Path, config: dict) -> InstallSpec:
    mapping = runtime_mapping(config)
    files = {home / item.path: item.text for item in render.rendered_files(mapping)}
    script = str(kit / HOOK_SCRIPT)
    hooks = JsonMerge(home / HOOKS_FILE, partial(merge_hooks, registrations=registrations(config, python, kit),
                                                 script=script),
                      partial(unmerge_hooks, script=script))
    return InstallSpec(RUNTIME, files=files, json_merges=[hooks],
                       kit_signatures=kit_signatures(home, mapping))
