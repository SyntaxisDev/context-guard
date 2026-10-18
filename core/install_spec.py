from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class JsonMerge:
    path: Path
    merge: Callable[[dict], dict]
    unmerge: Callable[[dict], dict]


@dataclass(frozen=True)
class TextEdit:
    path: Path
    apply: Callable[[str], str]
    revert: Callable[[str], str]


@dataclass(frozen=True)
class InstallSpec:
    runtime: str
    files: dict[Path, str] = field(default_factory=dict)
    json_merges: list[JsonMerge] = field(default_factory=list)
    text_edits: list[TextEdit] = field(default_factory=list)
    kit_signatures: dict[Path, tuple[str, ...]] = field(default_factory=dict)
