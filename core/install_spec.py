"""The runtime-neutral description of one install: owned files, JSON merges and text edits."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from core.render import Agent


@dataclass(frozen=True)
class JsonMerge:
    """A reversible change to a JSON object file the user also owns.

    Attributes:
        path: The JSON file to change.
        merge: Return the object with the kit's entries in place; must be idempotent.
        unmerge: Return the object with only the kit's entries removed.
    """

    path: Path
    merge: Callable[[dict], dict]
    unmerge: Callable[[dict], dict]


@dataclass(frozen=True)
class TextEdit:
    """A reversible change to a text file the user also owns.

    Attributes:
        path: The text file to change.
        apply: Return the text with the kit's edit in place; must be idempotent.
        revert: Return the text with only the kit's edit removed.
        notice: Given the text after `apply`, return a note for the user about a part of the
            edit it left out, or an empty string; None for no notes.
    """

    path: Path
    apply: Callable[[str], str]
    revert: Callable[[str], str]
    notice: Callable[[str], str] | None = None


@dataclass(frozen=True)
class InstallSpec:
    """Everything one runtime (or the kit) installs, as absolute paths.

    Attributes:
        runtime: Manifest name of the runtime, or "kit".
        files: Fully owned files mapped to their rendered text.
        json_merges: Changes to shared JSON files.
        text_edits: Changes to shared text files.
        kit_signatures: For an owned file path, the lines whose joint presence marks
            unrecorded content as stale kit output that may be replaced without force.
    """

    runtime: str
    files: dict[Path, str] = field(default_factory=dict)
    json_merges: list[JsonMerge] = field(default_factory=list)
    text_edits: list[TextEdit] = field(default_factory=list)
    kit_signatures: dict[Path, tuple[str, ...]] = field(default_factory=dict)


def frontmatter_signatures(directory: Path, agents: Iterable[Agent]) -> dict[Path, tuple[str, ...]]:
    """Build kit signatures for agent files written as `<directory>/<name>.md`.

    An unrecorded `<name>.md` kit agent carries its frontmatter `name:` line and the first line of its body.

    Args:
        directory: The directory holding the agent files.
        agents: The agents rendered into that directory.

    Returns:
        Each agent file path mapped to its signature lines.
    """
    return {
        directory / f"{agent.name}.md": ("---", f"name: {agent.name}", agent.body.strip().splitlines()[0].strip())
        for agent in agents
    }
