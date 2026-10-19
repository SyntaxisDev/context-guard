"""Hook commands and ownership for JSON hook files, and the grouped `{"hooks": {event: [group]}}` merge.

A group is `{"matcher"?: ..., "hooks": [handler]}` (Codex hooks.json, Claude settings.json). A handler
is the kit's when its command holds the kit hook script as one shell token. Every user event, group
and handler is kept, empty ones included; only what removing the kit's handlers emptied goes.
"""

import copy
import shlex
from functools import partial
from pathlib import Path

from core.install_spec import JsonMerge


def hook_command(python: Path, script: Path, event: str) -> str:
    """Build the shell command that runs one hook event.

    Args:
        python: The interpreter that runs the hook.
        script: The hook script.
        event: The event name passed as the script's argument.

    Returns:
        The shell-quoted command line.
    """
    return shlex.join([str(python), str(script), event])


def runs_script(handler: object, script: str) -> bool:
    """Tell whether a hook handler's command runs the given script.

    Args:
        handler: One handler entry; anything but a dict with a string `command` is not the kit's.
        script: The kit hook script path.

    Returns:
        True when the script is one shell token of the command; False for unparsable commands.
    """
    command = handler.get("command") if isinstance(handler, dict) else None
    if not isinstance(command, str):
        return False
    try:
        return script in shlex.split(command)
    except ValueError:
        return False


def event_lists(data: dict, label: str) -> dict:
    """Return the file's `hooks` mapping after checking its shape.

    Args:
        data: The parsed hook file.
        label: File name used in the error message.

    Returns:
        The `hooks` object itself (not a copy), or an empty dict when absent.

    Raises:
        ValueError: `hooks` is not an object mapping event names to lists.
    """
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or not all(isinstance(entries, list) for entries in hooks.values()):
        msg = f"{label}: hooks must map event names to lists"
        raise ValueError(msg)
    return hooks


def handler_group(group: object) -> bool:
    """Tell whether an event entry is a group holding a handler list.

    Args:
        group: One entry of an event's list.

    Returns:
        True for a dict whose `hooks` is a list.
    """
    return isinstance(group, dict) and isinstance(group.get("hooks"), list)


def owned_groups(data: dict, script: str, label: str) -> dict[str, list]:
    """Collect, per event, the groups holding at least one kit handler.

    Args:
        data: The parsed hook file.
        script: The kit hook script path.
        label: File name used in error messages.

    Returns:
        Event names mapped to their kit groups; events without one are left out.
    """
    found = {
        event: [
            group
            for group in groups
            if handler_group(group) and any(runs_script(hook, script) for hook in group["hooks"])
        ]
        for event, groups in event_lists(data, label).items()
    }
    return {event: groups for event, groups in found.items() if groups}


def without_handlers(group: object, script: str) -> object | None:
    """Drop the kit's handlers from one group.

    Args:
        group: One entry of an event's list; non-groups come back unchanged.
        script: The kit hook script path.

    Returns:
        The group without kit handlers, or None when removing them emptied it.
    """
    if not handler_group(group):
        return group
    remaining = [hook for hook in group["hooks"] if not runs_script(hook, script)]
    return {**group, "hooks": remaining} if remaining or not group["hooks"] else None


def unmerge_groups(data: dict, script: str, label: str) -> dict:
    """Remove every kit handler, then the groups, events and `hooks` key that removal emptied.

    Args:
        data: The parsed hook file; not modified.
        script: The kit hook script path.
        label: File name used in error messages.

    Returns:
        A copy of the file without the kit's handlers.
    """
    result = copy.deepcopy(data)
    hooks = event_lists(result, label)
    emptied = False
    for event, groups in list(hooks.items()):
        kept = [group for group in (without_handlers(group, script) for group in groups) if group is not None]
        if groups and not kept:
            del hooks[event]
            emptied = True
        else:
            hooks[event] = kept
    if emptied and not hooks:
        del result["hooks"]
    return result


def merge_groups(data: dict, groups: dict[str, dict], script: str, label: str) -> dict:
    """Put exactly one kit group under each event, appended after the user's groups.

    A file already holding exactly the kit's groups is returned as is, so later user hooks keep their place.

    Args:
        data: The parsed hook file; not modified.
        groups: Event names mapped to the kit group for that event.
        script: The kit hook script path.
        label: File name used in error messages.

    Returns:
        The merged file.
    """
    if owned_groups(data, script, label) == {event: [group] for event, group in groups.items()}:
        return data
    result = unmerge_groups(data, script, label)
    hooks = result.setdefault("hooks", {})
    for event, group in groups.items():
        hooks.setdefault(event, []).append(copy.deepcopy(group))
    return result


def group_merge(path: Path, groups: dict[str, dict], script: Path) -> JsonMerge:
    """Describe the grouped merge of the kit's hook groups into one hook file.

    Args:
        path: The hook file.
        groups: Event names mapped to the kit group for that event.
        script: The kit hook script the groups run.

    Returns:
        The reversible merge for the installer.
    """
    return JsonMerge(
        path,
        partial(merge_groups, groups=groups, script=str(script), label=path.name),
        partial(unmerge_groups, script=str(script), label=path.name),
    )
