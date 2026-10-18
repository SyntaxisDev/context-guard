"""Context policy: dynamic tiers, once-per-tier notes, resend pass, leaf mode and refreshes.

decide() is pure apart from reading the handoff store for session listings;
evaluate() adds the locked session state and the bounded event log.
"""

import time
from dataclasses import dataclass
from pathlib import Path

from core import config as config_mod
from core import events, handoffs, install, kit, render
from core import state as state_mod
from core.observation import (
    BLOCK,
    COMPACT,
    CONTEXT,
    NOTE,
    PASS,
    PROMPT,
    READ,
    SESSION,
    SPAWN,
    TOOL,
    WRITE,
    Decision,
    Observation,
)

TIERS = ("note", "firm", "block")
RANK = {None: 0, "note": 1, "firm": 2, "block": 3}
BUDGET_MESSAGE = (
    "[context-guard] Your live context is ~{tokens}k tokens, past your ~{budget}k subagent budget. "
    "Report what is done and what is left, and say explicitly if the remaining work cannot be "
    "split into smaller tasks. Then stop, unless finishing is only a few steps away."
)
NO_FALSE_CLAIM = "Never say the handoff is updated unless you wrote it in this turn."


@dataclass(frozen=True)
class Thresholds:
    note: int
    firm: int
    block: int

    def reached(self, tokens: int | None) -> str | None:
        if tokens is None:
            return None
        return next(
            (tier for tier in reversed(TIERS) if tokens >= getattr(self, tier)), None
        )


@dataclass(frozen=True)
class Context:
    """Explicit inputs policy needs besides the observation and session state."""

    config: dict
    handoff_dir: Path
    home: Path
    policy_text: str = ""
    now: float | None = None
    content: Path = render.CONTENT_DIR

    def time(self) -> float:
        return time.time() if self.now is None else self.now


def thresholds(config: dict, window: int | None) -> Thresholds:
    """Each tier is the lower of its window fraction and absolute cap; caps alone without a window."""
    conf = config["thresholds"]

    def level(tier: str) -> int:
        cap = conf[f"{tier}_cap"]
        return (
            cap if window is None else min(int(conf[f"{tier}_fraction"] * window), cap)
        )

    return Thresholds(*(level(tier) for tier in TIERS))


def k(tokens: int) -> int:
    return tokens // 1000


def note_message(
    tier: str, tokens: int, limits: Thresholds, window: int | None, path: str
) -> str:
    size = f"~{k(tokens)}k tokens" + (f" of a {k(window)}k window" if window else "")
    if tier == "note":
        return (
            f"Context check: estimated live context {size}. Update the handoff file {path} in this same turn "
            f"with a file-writing tool (handoff skill), then answer the user as if this note were not there: "
            f"do not mention the handoff or its path. {NO_FALSE_CLAIM}"
        )
    return (
        f"Context check: estimated live context {size}; the chat blocks at ~{k(limits.block)}k. Update the "
        f"handoff file {path} now, in this same turn, with a file-writing tool (handoff skill), then answer "
        "the user and end with one short line suggesting a fresh chat, which picks the handoff up by itself; "
        f"do not name the path. {NO_FALSE_CLAIM} Chat length is never a reason to abandon the task."
    )


def block_message(tokens: int, limits: Thresholds, window: int | None) -> str:
    limit = (
        f"the {k(window)}k context window"
        if window
        else f"the {k(limits.block)}k context limit"
    )
    return (
        f"Context check: this chat is at ~{k(tokens)}k tokens, close to {limit}. "
        "Send the same message again to continue here, or start a fresh chat: it picks up the saved handoff."
    )


def refresh_message(path: str, tokens: int, unwritten: bool = False) -> str:
    lead = (
        f"A handoff update was asked for, but the handoff file was not modified this turn: {path}. "
        if unwritten
        else ""
    )
    return (
        f"[context-guard] Estimated context ~{k(tokens)}k tokens. {lead}Write or update the handoff at {path} "
        f"with a file-writing tool, using the handoff skill. {NO_FALSE_CLAIM} If this file does not exist, "
        "choose a short descriptive 3–6-word task "
        "slug from context for its first save (at most 40 slug characters), keeping this directory and the "
        "exact identity suffix. Never copy the opening prompt; use task if unclear. Keep an existing "
        "file's path. Write from what is already in context. Do not re-read files or re-run checks for "
        "this. Keep it under ~40 lines, record the exact Workspace, and then end without reply text. "
        "This is a bookkeeping continuation, not a new task."
    )


def handoff_path(obs: Observation, state: dict, ctx: Context) -> str:
    identity = obs.runtime + "\0" + obs.session_id
    return handoffs.session_handoff_path(
        state, identity, ctx.handoff_dir, obs.prompt or ""
    )


def tool_events(obs: Observation, state: dict, ctx: Context) -> list[dict]:
    """Log handoff reads/writes and subagent spawns; any write into the store is a fresh handoff."""
    tool = obs.tool
    if tool is None:
        return []
    if tool.action == SPAWN:
        detail = {
            key: tool.detail.get(key) for key in ("role", "tier", "model", "effort")
        }
        return [events.record("subagent_spawn", obs, detail=detail)]
    if tool.action not in (READ, WRITE) or not handoffs.in_store(
        tool.path, ctx.handoff_dir, ctx.home
    ):
        return []
    if tool.action == READ:
        return [events.record("handoff_read", obs, detail={"path": tool.path})]
    state.update(
        handoff_path=str(config_mod.expand_path(tool.path, ctx.home)),
        handoff_turn=state.get("turn", 0),
    )
    state.pop("handoff_requested_at", None)
    requested = state.pop("pending_handoff_tokens", None)
    acknowledged = obs.tokens if obs.tokens else requested
    if acknowledged is not None:
        state["handoff_at"] = acknowledged
    return [events.record("handoff_written", obs, detail={"path": tool.path})]


def role_budget(config: dict, role: str | None, content: Path) -> int:
    """A role's optional roles.json `budget_tokens`, else `subagent_budget.tokens`."""
    fallback = config["subagent_budget"]["tokens"]
    if not role:
        return fallback
    try:
        spec = render.read_roles_file(content)["roles"].get(role)
    except render.RenderError:
        return fallback
    value = spec.get("budget_tokens") if isinstance(spec, dict) else None
    return value if type(value) is int and value > 0 else fallback


def leaf(obs: Observation, state: dict, ctx: Context) -> Decision:
    """Subagents only ever get the one advisory budget message."""
    if (
        obs.event not in (PROMPT, TOOL)
        or not ctx.config["subagent_budget"]["enabled"]
        or obs.tokens is None
        or state.get("budget_sent")
    ):
        return Decision(PASS)
    budget = role_budget(ctx.config, obs.role, ctx.content)
    if obs.tokens < budget:
        return Decision(PASS)
    state["budget_sent"] = True
    return Decision(
        NOTE,
        BUDGET_MESSAGE.format(tokens=k(obs.tokens), budget=k(budget)),
        events=[
            events.record(
                "subagent_budget", obs, detail={"role": obs.role, "budget": budget}
            )
        ],
    )


def kit_notice(ctx: Context) -> str | None:
    manifest = install.read_manifest(install.manifest_path(ctx.home, kit.KIT_RUNTIME))
    started = kit.auto_update(ctx.home, manifest) if ctx.config["kit_check"]["auto_update"] else None
    return started or kit.staleness(manifest)


def session_context(obs: Observation, ctx: Context) -> Decision:
    parts = [ctx.policy_text.strip()] if ctx.policy_text.strip() else []
    if ctx.config["handoff_list"]["enabled"]:
        parts.append(
            handoffs.listing(
                ctx.handoff_dir, obs.cwd, ctx.home, ctx.config["handoff_list"]["limit"]
            )
        )
    parts.append(
        handoffs.pending_context([obs.cwd] if obs.cwd else [], ctx.handoff_dir)
    )
    if obs.event == SESSION and ctx.config["kit_check"]["enabled"]:
        parts.append(kit_notice(ctx))
    message = "\n\n".join(part for part in parts if part)
    return Decision(CONTEXT, message) if message else Decision(PASS)


def fire_tier(
    obs: Observation, state: dict, ctx: Context, tier: str, limits: Thresholds
) -> Decision:
    state["tier"] = tier
    if tier == "block":
        state["blocked_prompt"] = (obs.prompt or "").strip()
        message = block_message(obs.tokens, limits, obs.window)
        return Decision(BLOCK, message, tier, [events.record("block", obs, tier)])
    state["asked_turn"] = state.get("turn", 0)
    message = note_message(
        tier, obs.tokens, limits, obs.window, handoff_path(obs, state, ctx)
    )
    return Decision(NOTE, message, tier, [events.record("note", obs, tier)])


def on_prompt(obs: Observation, state: dict, ctx: Context) -> Decision:
    prompt = (obs.prompt or "").strip()
    if prompt.startswith(handoffs.BOOKKEEPING_PREFIX):
        return Decision(PASS)
    state["turn"] = state.get("turn", 0) + 1
    handoffs.capture_label(state, prompt)
    logged = [events.record("prompt", obs)]
    blocked = state.pop("blocked_prompt", None)
    if blocked is not None and blocked == prompt:
        return Decision(
            PASS, events=logged + [events.record("resend", obs, state.get("tier"))]
        )
    if handoffs.HANDOFF_REQUEST.search(prompt):
        state["asked_turn"] = state["turn"]
        return Decision(
            PASS,
            events=logged
            + [events.record("handoff_requested", obs, detail={"source": "user"})],
        )
    limits = thresholds(ctx.config, obs.window)
    tier = limits.reached(obs.tokens)
    if RANK[tier] <= RANK[state.get("tier")]:
        return Decision(PASS, events=logged)
    decision = fire_tier(obs, state, ctx, tier, limits)
    return Decision(
        decision.kind, decision.message, decision.tier, logged + decision.events
    )


def on_tool(obs: Observation, state: dict, ctx: Context) -> Decision:
    limits = thresholds(ctx.config, obs.window)
    tier = limits.reached(obs.tokens)
    tier = "firm" if tier == "block" else tier
    if RANK[tier] <= RANK[state.get("tier")]:
        return Decision(PASS)
    return fire_tier(obs, state, ctx, tier, limits)


def refresh_due(tokens: int, state: dict, ctx: Context) -> bool:
    """`refresh_tokens` of growth since the last handoff write or refresh request, and `min_seconds` since
    that request; a reading below the baseline (compaction) restarts the count."""
    conf = ctx.config["handoff_refresh"]
    baseline = max(state.get("handoff_at", 0), state.get("pending_handoff_tokens", 0))
    if tokens < baseline:
        baseline = 0
    return (
        tokens - baseline >= conf["refresh_tokens"]
        and ctx.time() - state.get("handoff_requested_at", 0) >= conf["min_seconds"]
    )


def request_refresh(
    obs: Observation, state: dict, ctx: Context, unwritten: bool
) -> Decision:
    path = handoff_path(obs, state, ctx)
    state.update(
        pending_handoff_tokens=obs.tokens,
        handoff_requested_at=ctx.time(),
        refresh_turn=state.get("turn", 0),
    )
    source = "unwritten" if unwritten else "refresh"
    return Decision(
        CONTEXT,
        refresh_message(path, obs.tokens, unwritten),
        events=[events.record("handoff_requested", obs, detail={"source": source})],
    )


def on_stop(obs: Observation, state: dict, ctx: Context) -> Decision:
    """At most one refresh per turn, none in a turn that wrote the handoff: at once when this turn's note
    or handoff request got no write, otherwise when `refresh_due`."""
    conf, tokens, turn = ctx.config["handoff_refresh"], obs.tokens, state.get("turn", 0)
    if (
        not conf["enabled"]
        or tokens is None
        or tokens < thresholds(ctx.config, obs.window).note
    ):
        return Decision(PASS)
    if turn in (state.get("handoff_turn"), state.get("refresh_turn")):
        return Decision(PASS)
    if state.get("asked_turn") == turn:
        return request_refresh(obs, state, ctx, unwritten=True)
    if not refresh_due(tokens, state, ctx):
        return Decision(PASS)
    return request_refresh(obs, state, ctx, unwritten=False)


def rearmed(obs: Observation, state: dict, ctx: Context) -> bool:
    """Compaction, or live context back under the first tier (e.g. a summarised chat), resets fired tiers.

    A zero reading is missing telemetry (no usage recorded yet), not an empty chat.
    """
    if obs.compacted or obs.event == COMPACT:
        return True
    return (
        bool(obs.tokens)
        and "tier" in state
        and obs.tokens < thresholds(ctx.config, obs.window).note
    )


def decide(obs: Observation, state: dict, ctx: Context) -> Decision:
    """Mutate the session state and return the decision for one observation."""
    if rearmed(obs, state, ctx):
        state.pop("tier", None)
        state.pop("blocked_prompt", None)
    logged = tool_events(obs, state, ctx) if obs.event == TOOL else []
    if obs.is_subagent:
        decision = leaf(obs, state, ctx)
    elif obs.event in (SESSION, COMPACT):
        decision = session_context(obs, ctx)
    elif obs.event == PROMPT:
        decision = on_prompt(obs, state, ctx)
    elif obs.event == TOOL:
        decision = on_tool(obs, state, ctx)
    else:
        decision = on_stop(obs, state, ctx)
    return Decision(
        decision.kind, decision.message, decision.tier, logged + decision.events
    )


def evaluate(
    obs: Observation, config: dict, home: Path, policy_text: str = ""
) -> Decision:
    """Load locked session state, decide, persist state and append the decision's events.

    The save commits the hook deadline, so a saved decision is never replaced by the pass fallback.
    """
    directory = config_mod.state_dir(config, home, obs.runtime)
    ctx = Context(config, config_mod.handoff_dir(config, home), home, policy_text)
    with state_mod.session_state(
        directory, obs.runtime, obs.session_id, commit=True
    ) as state:
        decision = decide(obs, state, ctx)
    log_events(config, directory, decision.events)
    return decision


def log_events(config: dict, directory: Path, records: list[dict]) -> None:
    if config["events"]["enabled"]:
        events.append(
            directory / events.EVENTS_FILE, records, config["events"]["max_lines"]
        )


def log_delivered(
    obs: Observation, decision: Decision, config: dict, home: Path
) -> None:
    """Call when the native response hands the decision's message to the model as context."""
    if decision.kind == NOTE and decision.tier in TIERS:
        log_events(
            config,
            config_mod.state_dir(config, home, obs.runtime),
            [events.record("note_delivered", obs, decision.tier)],
        )
