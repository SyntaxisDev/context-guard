"""Runtime-neutral inputs to policy and its decisions."""
from collections.abc import Mapping
from dataclasses import dataclass, field

SESSION = "session"
PROMPT = "prompt"
TOOL = "tool"
STOP = "stop"
COMPACT = "compact"
OBSERVED_EVENTS = (SESSION, PROMPT, TOOL, STOP, COMPACT)

READ = "read"
WRITE = "write"
SPAWN = "spawn"
OTHER = "other"
TOOL_ACTIONS = (READ, WRITE, SPAWN, OTHER)

PASS = "pass"
NOTE = "note"
BLOCK = "block"
CONTEXT = "context"
DECISION_KINDS = (PASS, NOTE, BLOCK, CONTEXT)


def optional_count(name: str, value: object) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a non-negative int or None")


@dataclass(frozen=True)
class ToolUse:
    """A completed tool call as classified by the adapter.

    `path` is the file read or written; spawn `detail` carries role, tier, model and effort.
    """
    name: str
    action: str = OTHER
    path: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.action not in TOOL_ACTIONS:
            raise ValueError(f"unknown tool action {self.action!r}")


@dataclass(frozen=True)
class Observation:
    runtime: str
    session_id: str
    event: str
    tokens: int | None = None
    window: int | None = None
    model: str | None = None
    idle_seconds: float | None = None
    is_subagent: bool = False
    role: str | None = None
    compacted: bool = False
    cwd: str = ""
    prompt: str | None = None
    tool: ToolUse | None = None

    def __post_init__(self):
        if self.event not in OBSERVED_EVENTS:
            raise ValueError(f"unknown event {self.event!r}")
        if not self.runtime or not self.session_id:
            raise ValueError("runtime and session_id are required")
        optional_count("tokens", self.tokens)
        optional_count("window", self.window)


@dataclass(frozen=True)
class Decision:
    """kind is pass, note (context for the model), block (stop the prompt) or context.

    context is session/compaction text on session and compact events, and a
    bookkeeping follow-up on stop events. `events` are complete event records.
    """
    kind: str = PASS
    message: str = ""
    tier: str | None = None
    events: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.kind not in DECISION_KINDS:
            raise ValueError(f"unknown decision kind {self.kind!r}")
