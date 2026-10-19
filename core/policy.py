"""Context policy: dynamic tiers, once-per-tier notes, resend pass, leaf mode and refreshes.

decide() is pure apart from reading the handoff store for session listings;
evaluate() adds the locked session state and the bounded event log.
"""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from core import config as config_mod, events, handoffs, install, kit, render, state as state_mod
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
    """Token levels at which each context tier fires.

    Attributes:
        note: Tokens for the advisory note.
        firm: Tokens for the firm note.
        block: Tokens at which prompts are blocked.
    """

    note: int
    firm: int
    block: int

    def reached(self, tokens: int | None) -> str | None:
        """Return the highest tier a token reading has reached.

        Args:
            tokens: Estimated live context tokens, or None when unknown.

        Returns:
            The tier name, or None below every tier or without a reading.
        """
        if tokens is None:
            return None
        return next((tier for tier in reversed(TIERS) if tokens >= getattr(self, tier)), None)


@dataclass(frozen=True)
class Context:
    """Explicit inputs policy needs besides the observation and session state.

    Attributes:
        config: Loaded configuration.
        handoff_dir: The handoff store.
        home: Home directory that `~` stands for.
        policy_text: Policy text for session and compaction context.
        now: Current time in epoch seconds; None reads the clock.
        content: Shipped content directory holding roles.json.
    """

    config: dict
    handoff_dir: Path
    home: Path
    policy_text: str = ""
    now: float | None = None
    content: Path = render.CONTENT_DIR

    def time(self) -> float:
        """Return the current time.

        Returns:
            `now` when set, else the clock in epoch seconds.
        """
        return time.time() if self.now is None else self.now


def thresholds(config: dict, window: int | None) -> Thresholds:
    """Each tier is the lower of its window fraction and absolute cap; caps alone without a window.

    Args:
        config: Loaded configuration with `thresholds.<tier>_fraction` and `thresholds.<tier>_cap`.
        window: The model's context window in tokens, or None when unknown.

    Returns:
        The token level of every tier.
    """
    conf = config["thresholds"]

    def level(tier: str) -> int:
        """Return one tier's token level.

        Args:
            tier: Tier name.

        Returns:
            The cap, lowered to the window fraction when the window is known.
        """
        cap = conf[f"{tier}_cap"]
        return cap if window is None else min(int(conf[f"{tier}_fraction"] * window), cap)

    return Thresholds(*(level(tier) for tier in TIERS))


def k(tokens: int) -> int:
    """Convert tokens to whole thousands.

    Args:
        tokens: Token count.

    Returns:
        `tokens` divided by 1000, rounded down.
    """
    return tokens // 1000


def note_message(tier: str, tokens: int, limits: Thresholds, window: int | None, path: str) -> str:
    """Render the context note for the note or firm tier.

    Args:
        tier: "note" for the advisory note; any other tier renders the firm note.
        tokens: Estimated live context tokens.
        limits: The session's tier levels; the firm note names the block level.
        window: The model's context window in tokens, or None when unknown.
        path: The session's handoff file.

    Returns:
        The note for the model.
    """
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
    """Render the message shown to the user when a prompt is blocked.

    Args:
        tokens: Estimated live context tokens.
        limits: The session's tier levels; used when the window is unknown.
        window: The model's context window in tokens, or None when unknown.

    Returns:
        The block message.
    """
    limit = f"the {k(window)}k context window" if window else f"the {k(limits.block)}k context limit"
    return (
        f"Context check: this chat is at ~{k(tokens)}k tokens, close to {limit}. "
        "Send the same message again to continue here, or start a fresh chat: it picks up the saved handoff."
    )


def refresh_message(path: str, tokens: int, *, unwritten: bool = False) -> str:
    """Render the bookkeeping follow-up that asks the model to write or update the handoff.

    Args:
        path: The session's handoff file.
        tokens: Estimated live context tokens.
        unwritten: Say that this turn's note or handoff request got no write.

    Returns:
        The follow-up message, starting with the bookkeeping prefix.
    """
    lead = f"The handoff file was not modified this turn: {path}. " if unwritten else ""
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
    """Return the session's handoff path, remembering it in the state once it is meaningful.

    Args:
        obs: The observation; its runtime and session id form the identity, its prompt the label.
        state: Session state, updated in place.
        ctx: Policy inputs.

    Returns:
        The handoff file path.
    """
    identity = obs.runtime + "\0" + obs.session_id
    return handoffs.session_handoff_path(state, identity, ctx.handoff_dir, obs.prompt or "")


def tool_events(obs: Observation, state: dict, ctx: Context) -> list[dict]:
    """Log handoff reads/writes and subagent spawns; any write into the store is a fresh handoff.

    A write records the handoff path, its turn and the acknowledged token count in the state.

    Args:
        obs: A tool observation.
        state: Session state, updated in place.
        ctx: Policy inputs.

    Returns:
        The event records for this tool call; empty for other tools or paths outside the store.
    """
    tool = obs.tool
    if tool is None:
        return []
    if tool.action == SPAWN:
        detail = {key: tool.detail.get(key) for key in ("role", "tier", "model", "effort")}
        return [events.record("subagent_spawn", obs, detail=detail)]
    if tool.action not in (READ, WRITE) or not handoffs.in_store(tool.path, ctx.handoff_dir, ctx.home):
        return []
    if tool.action == READ:
        return [events.record("handoff_read", obs, detail={"path": tool.path})]
    state.update(
        handoff_path=str(config_mod.expand_path(tool.path, ctx.home)),
        handoff_turn=state.get("turn", 0),
    )
    state.pop("handoff_requested_at", None)
    requested = state.pop("pending_handoff_tokens", None)
    acknowledged = obs.tokens or requested
    if acknowledged is not None:
        state["handoff_at"] = acknowledged
    return [events.record("handoff_written", obs, detail={"path": tool.path})]


def role_budget(config: dict, role: str | None, content: Path) -> int:
    """A role's optional roles.json `budget_tokens`, else `subagent_budget.tokens`.

    Args:
        config: Loaded configuration with `subagent_budget.tokens`.
        role: The subagent's role, or None.
        content: Shipped content directory holding roles.json.

    Returns:
        The role's positive budget, or the configured default when it has none or roles are unreadable.
    """
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
    """Subagents only ever get the one advisory budget message.

    Args:
        obs: A subagent observation.
        state: Session state; records that the budget message was sent.
        ctx: Policy inputs.

    Returns:
        A note with the budget message the first time a prompt or tool reading reaches the budget,
        else a pass.
    """
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
        events=[events.record("subagent_budget", obs, detail={"role": obs.role, "budget": budget})],
    )


def kit_notice(ctx: Context) -> str | None:
    """Start a configured kit auto-update, else report a stale installed kit.

    Args:
        ctx: Policy inputs; `kit_check.auto_update` selects the auto-update.

    Returns:
        The auto-update or staleness notice, or None when there is nothing to say.
    """
    manifest = install.read_manifest(install.manifest_path(ctx.home, kit.KIT_RUNTIME))
    started = kit.auto_update(ctx.home, manifest) if ctx.config["kit_check"]["auto_update"] else None
    return started or kit.staleness(ctx.home, manifest)


def session_context(obs: Observation, ctx: Context) -> Decision:
    """Build the context for a session start or compaction.

    It joins the policy text, the open handoff listing, pending runner reports for the workspace and,
    on session start, the kit notice, as configured.

    Args:
        obs: A session or compact observation.
        ctx: Policy inputs.

    Returns:
        A context decision, or a pass when there is nothing to add.
    """
    parts = [ctx.policy_text.strip()] if ctx.policy_text.strip() else []
    if ctx.config["handoff_list"]["enabled"]:
        parts.append(handoffs.listing(ctx.handoff_dir, obs.cwd, ctx.home, ctx.config["handoff_list"]["limit"]))
    parts.append(handoffs.pending_context([obs.cwd] if obs.cwd else [], ctx.handoff_dir))
    if obs.event == SESSION and ctx.config["kit_check"]["enabled"]:
        parts.append(kit_notice(ctx))
    message = "\n\n".join(part for part in parts if part)
    return Decision(CONTEXT, message) if message else Decision(PASS)


def prompt_digest(prompt: str) -> str:
    """Digest a prompt so a resend can be recognised without storing its text.

    Args:
        prompt: Prompt text; surrounding whitespace is ignored.

    Returns:
        The SHA-256 hex digest.
    """
    return hashlib.sha256(prompt.strip().encode("utf-8", "surrogatepass")).hexdigest()


def fire_tier(obs: Observation, state: dict, ctx: Context, tier: str, limits: Thresholds) -> Decision:
    """Record a newly reached tier and return its note or block.

    Args:
        obs: The observation with a token reading.
        state: Session state; records the tier and the blocked prompt or the turn the note asked.
        ctx: Policy inputs.
        tier: The tier reached.
        limits: The session's tier levels.

    Returns:
        A block decision for the block tier, else a note asking for the handoff.
    """
    state["tier"] = tier
    if tier == "block":
        state["blocked_prompt"] = prompt_digest(obs.prompt or "")
        message = block_message(obs.tokens, limits, obs.window)
        return Decision(BLOCK, message, tier, [events.record("block", obs, tier)])
    state["asked_turn"] = state.get("turn", 0)
    message = note_message(tier, obs.tokens, limits, obs.window, handoff_path(obs, state, ctx))
    return Decision(NOTE, message, tier, [events.record("note", obs, tier)])


def on_prompt(obs: Observation, state: dict, ctx: Context) -> Decision:
    """Decide a main-session prompt.

    Bookkeeping prompts pass untouched; a resend of the blocked prompt and an explicit handoff request
    pass; otherwise a tier above the one already fired fires.

    Args:
        obs: A prompt observation.
        state: Session state; advances the turn and captures the task label.
        ctx: Policy inputs.

    Returns:
        The decision, with the prompt event and any follow-up events.
    """
    prompt = (obs.prompt or "").strip()
    if prompt.startswith(handoffs.BOOKKEEPING_PREFIX):
        return Decision(PASS)
    state["turn"] = state.get("turn", 0) + 1
    handoffs.capture_label(state, prompt)
    logged = [events.record("prompt", obs)]
    blocked = state.pop("blocked_prompt", None)
    if blocked is not None and blocked == prompt_digest(prompt):
        return Decision(PASS, events=[*logged, events.record("resend", obs, state.get("tier"))])
    if handoffs.HANDOFF_REQUEST.search(prompt):
        state["asked_turn"] = state["turn"]
        return Decision(
            PASS,
            events=[*logged, events.record("handoff_requested", obs, detail={"source": "user"})],
        )
    limits = thresholds(ctx.config, obs.window)
    tier = limits.reached(obs.tokens)
    if RANK[tier] <= RANK[state.get("tier")]:
        return Decision(PASS, events=logged)
    decision = fire_tier(obs, state, ctx, tier, limits)
    return Decision(decision.kind, decision.message, decision.tier, logged + decision.events)


def on_tool(obs: Observation, state: dict, ctx: Context) -> Decision:
    """Decide a main-session tool call; tool calls never block, so the block tier fires as firm.

    Args:
        obs: A tool observation.
        state: Session state.
        ctx: Policy inputs.

    Returns:
        A note when a tier above the one already fired is reached, else a pass.
    """
    limits = thresholds(ctx.config, obs.window)
    tier = limits.reached(obs.tokens)
    tier = "firm" if tier == "block" else tier
    if RANK[tier] <= RANK[state.get("tier")]:
        return Decision(PASS)
    return fire_tier(obs, state, ctx, tier, limits)


def refresh_due(tokens: int, state: dict, ctx: Context) -> bool:
    """Tell whether context has grown enough, for long enough, to ask for a handoff refresh.

    Due after `refresh_tokens` of growth since the last handoff write or refresh request, and
    `min_seconds` since that request; a reading below the baseline (compaction) restarts the count.

    Args:
        tokens: Estimated live context tokens.
        state: Session state.
        ctx: Policy inputs with `handoff_refresh` settings.

    Returns:
        True when a refresh is due.
    """
    conf = ctx.config["handoff_refresh"]
    baseline = max(state.get("handoff_at", 0), state.get("pending_handoff_tokens", 0))
    if tokens < baseline:
        baseline = 0
    return (
        tokens - baseline >= conf["refresh_tokens"]
        and ctx.time() - state.get("handoff_requested_at", 0) >= conf["min_seconds"]
    )


def request_refresh(obs: Observation, state: dict, ctx: Context, *, unwritten: bool) -> Decision:
    """Ask for a handoff refresh and record the request in the state.

    Args:
        obs: A stop observation with a token reading.
        state: Session state; records the requested tokens, time and turn.
        ctx: Policy inputs.
        unwritten: The request follows a note or handoff request that got no write this turn.

    Returns:
        A context decision carrying the bookkeeping follow-up.
    """
    path = handoff_path(obs, state, ctx)
    state.update(
        pending_handoff_tokens=obs.tokens,
        handoff_requested_at=ctx.time(),
        refresh_turn=state.get("turn", 0),
    )
    source = "unwritten" if unwritten else "refresh"
    return Decision(
        CONTEXT,
        refresh_message(path, obs.tokens, unwritten=unwritten),
        events=[events.record("handoff_requested", obs, detail={"source": source})],
    )


def on_stop(obs: Observation, state: dict, ctx: Context) -> Decision:
    """Decide the end of a main-session turn: at most one handoff refresh per turn.

    None in a turn that wrote the handoff; at once when this turn's note or handoff request got no
    write, otherwise when `refresh_due`. Nothing below the note tier or when refreshes are disabled.

    Args:
        obs: A stop observation.
        state: Session state.
        ctx: Policy inputs.

    Returns:
        A refresh request, or a pass.
    """
    conf, tokens, turn = ctx.config["handoff_refresh"], obs.tokens, state.get("turn", 0)
    if not conf["enabled"] or tokens is None or tokens < thresholds(ctx.config, obs.window).note:
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

    Args:
        obs: The observation.
        state: Session state.
        ctx: Policy inputs.

    Returns:
        True when fired tiers should be cleared.
    """
    if obs.compacted or obs.event == COMPACT:
        return True
    return bool(obs.tokens) and "tier" in state and obs.tokens < thresholds(ctx.config, obs.window).note


def decide(obs: Observation, state: dict, ctx: Context) -> Decision:
    """Mutate the session state and return the decision for one observation.

    Args:
        obs: The observation.
        state: Session state, updated in place.
        ctx: Policy inputs.

    Returns:
        The decision, with tool events ahead of the decision's own events.
    """
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
    return Decision(decision.kind, decision.message, decision.tier, logged + decision.events)


def evaluate(obs: Observation, config: dict, home: Path, policy_text: str = "") -> Decision:
    """Load locked session state, decide, persist state and append the decision's events.

    The save commits the hook deadline, so a saved decision is never replaced by the pass fallback.

    Args:
        obs: The observation.
        config: Loaded configuration.
        home: Home directory the hook acts on.
        policy_text: Policy text for session and compaction context.

    Returns:
        The decision.
    """
    directory = config_mod.state_dir(config, home, obs.runtime)
    ctx = Context(config, config_mod.handoff_dir(config, home), home, policy_text)
    with state_mod.session_state(directory, obs.runtime, obs.session_id, commit=True) as state:
        decision = decide(obs, state, ctx)
    log_events(config, directory, decision.events)
    return decision


def log_events(config: dict, directory: Path, records: list[dict]) -> None:
    """Append records to the runtime's event log when events are enabled.

    Args:
        config: Loaded configuration with `events.enabled` and `events.max_lines`.
        directory: The runtime's state directory holding the log.
        records: Event records to append.
    """
    if config["events"]["enabled"]:
        events.append(directory / events.EVENTS_FILE, records, config["events"]["max_lines"])


def log_delivered(obs: Observation, decision: Decision, config: dict, home: Path) -> None:
    """Log that a tier note reached the model; call when the native response hands it over as context.

    Args:
        obs: The observation the decision answered.
        decision: The decision rendered; only tier notes are logged.
        config: Loaded configuration.
        home: Home directory the hook acts on.
    """
    if decision.kind == NOTE and decision.tier in TIERS:
        log_events(
            config,
            config_mod.state_dir(config, home, obs.runtime),
            [events.record("note_delivered", obs, decision.tier)],
        )
