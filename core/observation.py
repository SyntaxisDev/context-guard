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

PASS = "pass"  # noqa: S105
NOTE = "note"
BLOCK = "block"
CONTEXT = "context"
DECISION_KINDS = (PASS, NOTE, BLOCK, CONTEXT)


def optional_count(name: str, value: object) -> None:
    """Validate an optional token count.

    Args:
        name: Field name used in the error message.
        value: The count; None means unknown.

    Raises:
        ValueError: When `value` is neither None nor a non-negative int.
    """
    if value is not None and (type(value) is not int or value < 0):
        msg = f"{name} must be a non-negative int or None"
        raise ValueError(msg)


@dataclass(frozen=True)
class ToolUse:
    """A completed tool call as classified by the adapter.

    Attributes:
        name: The runtime's tool name.
        action: One of TOOL_ACTIONS.
        path: The file read or written, if any.
        detail: Spawn details: role, tier, model and effort.

    Raises:
        ValueError: When `action` is not one of TOOL_ACTIONS.
    """

    name: str
    action: str = OTHER
    path: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in TOOL_ACTIONS:
            msg = f"unknown tool action {self.action!r}"
            raise ValueError(msg)


@dataclass(frozen=True)
class Observation:
    """One hook event as policy sees it, independent of the runtime that produced it.

    Attributes:
        runtime: Runtime name; required.
        session_id: The runtime's chat or session id; required.
        event: One of OBSERVED_EVENTS.
        tokens: Estimated live context tokens, or None when telemetry is missing.
        window: The model's context window in tokens, or None when unknown.
        model: Model name, if reported.
        is_subagent: True when the event comes from a subagent.
        role: The subagent's role, if known.
        compacted: True when the runtime reports the context was compacted.
        rearm_on_usage: Whether falling usage alone may reset warning tiers.
        cwd: Working directory of the session, or empty when unknown.
        prompt: The user prompt on prompt events.
        tool: The completed tool call on tool events.

    Raises:
        ValueError: When `event` is unknown, `runtime` or `session_id` is empty, or a count is invalid.
    """

    runtime: str
    session_id: str
    event: str
    tokens: int | None = None
    window: int | None = None
    model: str | None = None
    is_subagent: bool = False
    role: str | None = None
    compacted: bool = False
    rearm_on_usage: bool = True
    cwd: str = ""
    prompt: str | None = None
    tool: ToolUse | None = None

    def __post_init__(self) -> None:
        if self.event not in OBSERVED_EVENTS:
            msg = f"unknown event {self.event!r}"
            raise ValueError(msg)
        if not self.runtime or not self.session_id:
            msg = "runtime and session_id are required"
            raise ValueError(msg)
        optional_count("tokens", self.tokens)
        optional_count("window", self.window)


@dataclass(frozen=True)
class Decision:
    """What policy asks the adapter to answer for one observation.

    Attributes:
        kind: PASS, NOTE (context for the model), BLOCK (stop the prompt) or CONTEXT. CONTEXT is
            session or compaction text on session and compact events, and a bookkeeping follow-up on
            stop events.
        message: Text for the model or the user; empty for a plain pass.
        tier: The context tier that fired, if any.
        events: Complete event records to append to the event log.

    Raises:
        ValueError: When `kind` is not one of DECISION_KINDS.
    """

    kind: str = PASS
    message: str = ""
    tier: str | None = None
    events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in DECISION_KINDS:
            msg = f"unknown decision kind {self.kind!r}"
            raise ValueError(msg)
