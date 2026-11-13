"""Runtime-neutral capability and guarantee contracts."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, unique


@unique
class Feature(Enum):
    """Independently supportable capabilities that can have documented gaps."""

    SESSION_CONTEXT = "session_context"
    PROMPT_WARNING = "prompt_warning"
    PROMPT_BLOCK = "prompt_block"
    TOOL_WARNING = "tool_warning"
    HANDOFF_REFRESH = "handoff_refresh"
    COMPACTION_RECOVERY = "compaction_recovery"
    SUBAGENT_BUDGET = "subagent_budget"
    SILENT_SUBAGENT_NOTE = "silent_subagent_note"
    BACKGROUND_RESULT_WAKE = "background_result_wake"
    HANDOFF_ACCESS = "handoff_access"
    ROLES = "roles"
    RUNNER = "runner"


@unique
class Guarantee(Enum):
    """Universal contracts that adapters cannot declare unsupported."""

    HOOK_BOUNDARY = "hook_boundary"
    INSTALL_PRESERVATION = "install_preservation"
    DOCTOR = "doctor"
    SKILLS = "skills"
    EVENT_LOGGING = "event_logging"


@unique
class GapKind(Enum):
    """Reason a capability is absent from an adapter."""

    RUNTIME_LIMIT = "runtime_limit"
    NOT_APPLICABLE = "not_applicable"
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True)
class Gap:
    """A missing capability and its evidence.

    Args:
        kind: Classification of the capability gap.
        reason: Why the runtime does not support the capability.
        documentation: Repository-relative Markdown link to runtime documentation.
    """

    kind: GapKind
    reason: str
    documentation: str


@dataclass(frozen=True)
class Adapter:
    """Lazy runtime declaration for registry consumers.

    Args:
        name: Unique runtime name matching the adapter package.
        hooks: Import path of the runtime hook module.
        mapping: Import path of the runtime installation and rendering module.
        gaps: Unsupported capabilities with reasons and documentation.
    """

    name: str
    hooks: str
    mapping: str
    gaps: Mapping[Feature, Gap]
