"""Track Cursor Task subagents so a parent learns about a subagent that is lost, orphaned or silent.

Each spawn is recorded under the parent's Task `tool_use_id`. A foreground spawn stays open
until the parent's Task result arrives; a parent stop turns every foreground spawn still open
into an unreturned note. A background spawn, or a foreground one whose parent moved on without
its result (orphaned, e.g. after Cursor retried a dropped parent request), is detached: it runs
until the subagent's own stop hook completes or its transcript ends with a `turn_ended` record.
Any spawn silent past the configured threshold raises a note once per silence period. Notes
wait in the parent's state and reach it on its next tool result or session start.
"""

import json
import time
from dataclasses import replace
from pathlib import Path

from adapters.cursor.mapping import RUNTIME
from core import events, policy, state as state_mod
from core.config import valid_text
from core.observation import CONTEXT, NOTE, SESSION, SPAWN, STOP, TOOL, Decision, Observation

SPAWN_RECORD = "spawn\0"
OPEN_SPAWNS, DETACHED, NOTES = "open_spawns", "detached_spawns", "subagent_notes"
PARENT_CALL = "parent_call"
ACTIVITY_SECONDS = 60
ORPHAN_GRACE_SECONDS = 30
SUBAGENT_DIR = "subagents"
TAIL_BYTES = 4096
TURN_ENDED = "turn_ended"
RECOVERY = "It may have died or be stuck: check its progress file and transcript; cancel its jobs before re-spawning."


def clock() -> float:
    """Read the current time.

    Returns:
        Epoch seconds.
    """
    return time.time()


def state_path(directory: Path, key: str) -> Path:
    """Name the state file of a Cursor session or spawn record.

    Args:
        directory: Cursor state directory.
        key: A conversation id, or SPAWN_RECORD followed by the parent's Task `tool_use_id`.

    Returns:
        The JSON state file path for that key.
    """
    return directory / f"{state_mod.session_key(RUNTIME, key)}.json"


def read_adapter(path: Path) -> dict:
    """Read the adapter section of a state file without locking it.

    Args:
        path: The state file.

    Returns:
        The validated adapter section, empty when the file is missing or damaged.
    """
    return state_mod.validated(state_mod.read_json(path)).get("adapter", {})


def read_record(directory: Path, call: str) -> dict:
    """Read a spawn record without locking it.

    Args:
        directory: Cursor state directory.
        call: The parent's Task `tool_use_id`.

    Returns:
        The adapter section of the spawn record, empty when unknown.
    """
    return read_adapter(state_path(directory, SPAWN_RECORD + call))


def listed(adapter: dict, key: str) -> list[str]:
    """Read a list of ids from an adapter section.

    Args:
        adapter: The adapter section of a state file.
        key: The list's key.

    Returns:
        The text items of the list, empty when it is missing or not a list.
    """
    items = adapter.get(key)
    return [item for item in items if valid_text(item) and item] if isinstance(items, list) else []


def spawned_role(directory: Path, call: str | None) -> str | None:
    """Look up the role a subagent was spawned as.

    Args:
        directory: Cursor state directory.
        call: The parent's Task `tool_use_id`, or None for the main chat.

    Returns:
        The recorded role, or None for the main chat or an unrecorded spawn.
    """
    role = read_record(directory, call).get("role") if call else None
    return role if valid_text(role) and role else None


def transcript_dir(payload: dict[str, object]) -> str | None:
    """Derive where a parent's subagent transcripts live from its own transcript path.

    Args:
        payload: The validated hook payload of the parent.

    Returns:
        The `subagents` directory beside the parent transcript, or None when no path is reported.
    """
    path = payload.get("transcript_path")
    return str(Path(path).parent / SUBAGENT_DIR) if valid_text(path) and path else None


def remember_spawn(directory: Path, payload: dict[str, object], role: str | None) -> None:
    """Record a spawn under the parent's Task `tool_use_id` and open it in the parent's state.

    Subagent tool hooks carry only that id, so this is how they learn their role. Spawns made by
    a subagent are not tracked as open.

    Args:
        directory: Cursor state directory.
        payload: The validated preToolUse payload of the Task call.
        role: The spawned role, if named.
    """
    call, parent = payload.get("tool_use_id"), payload.get("conversation_id")
    if not call:
        return
    with state_mod.session_state(directory, RUNTIME, SPAWN_RECORD + call) as record:
        fields = {"role": role, "parent": parent, "transcripts": transcript_dir(payload), "spawned": int(clock())}
        record.setdefault("adapter", {}).update({key: value for key, value in fields.items() if value})
    if parent and not payload.get("parent_tool_call_id"):
        with state_mod.session_state(directory, RUNTIME, parent) as state:
            spawns = state.setdefault("adapter", {}).setdefault(OPEN_SPAWNS, [])
            if call not in spawns:
                spawns.append(call)


def touch(directory: Path, payload: dict[str, object], now: float) -> None:
    """Record a subagent's agent id and last activity on its spawn record.

    The activity time is kept to ACTIVITY_SECONDS so most tool hooks do not write state.

    Args:
        directory: Cursor state directory.
        payload: The validated tool hook payload of a subagent.
        now: Current time in epoch seconds.
    """
    call, agent = payload.get("parent_tool_call_id"), payload.get("conversation_id")
    record = read_record(directory, call) if call and agent else {}
    active = record.get("active")
    if not record.get("parent") or (
        record.get("agent") == agent and isinstance(active, int) and now - active < ACTIVITY_SECONDS
    ):
        return
    with state_mod.session_state(directory, RUNTIME, SPAWN_RECORD + call) as state:
        state.setdefault("adapter", {}).update({"agent": agent, "active": int(now)})


def move_spawns(
    directory: Path, parent: str, calls: list[str], *, detach: bool, notes: list[str] | None = None
) -> None:
    """Take spawns out of a parent's open and detached lists, optionally detach them, and queue notes.

    Args:
        directory: Cursor state directory.
        parent: The parent's conversation id.
        calls: The parent's Task `tool_use_id`s to move.
        detach: Whether the spawns join the detached list rather than leave tracking.
        notes: Notes to queue for the parent.
    """
    with state_mod.session_state(directory, RUNTIME, parent) as state:
        adapter = state.setdefault("adapter", {})
        for key in (OPEN_SPAWNS, DETACHED):
            adapter[key] = [item for item in listed(adapter, key) if item not in calls]
        if detach:
            adapter[DETACHED] += calls
        if notes:
            adapter[NOTES] = [*listed(adapter, NOTES), *notes]


def task_result(output: object) -> dict:
    """Parse a parent's Task result.

    Args:
        output: The postToolUse `tool_output`, JSON text such as
            `{"status": "success", "agentId": ..., "isBackground": false}`.

    Returns:
        The decoded object, or an empty dict when the output is not a JSON object.
    """
    try:
        result = json.loads(output) if valid_text(output) else None
    except ValueError:
        return {}
    return result if isinstance(result, dict) else {}


def mark_returned(directory: Path, payload: dict[str, object]) -> None:
    """Close a parent's spawn once its foreground Task result arrived, or detach a background one.

    A status other than `success` leaves the spawn open; output without a status counts as
    returned, so changed telemetry raises no note.

    Args:
        directory: Cursor state directory.
        payload: The validated postToolUse payload of the parent's Task call.
    """
    call, parent = payload.get("tool_use_id"), payload.get("conversation_id")
    result = task_result(payload.get("tool_output"))
    status, agent = result.get("status"), result.get("agentId")
    if not call or not parent or (valid_text(status) and status != "success"):
        return
    adapter = read_adapter(state_path(directory, parent))
    if call not in listed(adapter, OPEN_SPAWNS) + listed(adapter, DETACHED):
        return
    background = result.get("isBackground") is True
    if background and valid_text(agent) and agent:
        with state_mod.session_state(directory, RUNTIME, SPAWN_RECORD + call) as record:
            record.setdefault("adapter", {}).setdefault("agent", agent)
    move_spawns(directory, parent, [call], detach=background)


def transcript(record: dict) -> Path | None:
    """Locate a spawn's transcript, or its directory while the agent id is unknown.

    Args:
        record: The adapter section of the spawn record.

    Returns:
        `<transcript dir>/<agent id>.jsonl`, the directory alone, or None when unknown.
    """
    directory, agent = record.get("transcripts"), record.get("agent")
    if not valid_text(directory) or not directory:
        return None
    return Path(directory) / f"{agent}.jsonl" if valid_text(agent) and agent else Path(directory)


def finished(record: dict) -> bool:
    """Tell whether a spawn's transcript ends with a `turn_ended` record.

    Only the tail of the transcript is read.

    Args:
        record: The adapter section of the spawn record.

    Returns:
        True when the last transcript line is a `turn_ended` record; False when it is not or
        the transcript is unknown or unreadable.
    """
    path = transcript(record)
    if path is None or path.suffix != ".jsonl":
        return False
    try:
        with path.open("rb") as file:
            file.seek(max(0, file.seek(0, 2) - TAIL_BYTES))
            lines = file.read().splitlines()
        last = json.loads(next((line for line in reversed(lines) if line.strip()), b"null"))
    except OSError, ValueError:
        return False
    return isinstance(last, dict) and last.get("type") == TURN_ENDED


def subject(record: dict) -> str:
    """Name a spawn in a note by role and agent id, leaving out unknown parts.

    Args:
        record: The adapter section of the spawn record.

    Returns:
        Text such as "Subagent `implementer` (agent <id>)".
    """
    role, agent = record.get("role"), record.get("agent")
    text = "Subagent" + (f" `{role}`" if valid_text(role) else "")
    return text + (f" (agent {agent})" if valid_text(agent) else "")


def unreturned_note(record: dict) -> str:
    """Tell the parent that a subagent's result never arrived and how to recover it.

    Args:
        record: The adapter section of the spawn record; unknown parts are left out.

    Returns:
        The note text.
    """
    agent, active, path = record.get("agent"), record.get("active"), transcript(record)
    parts = [f"[context-guard] {subject(record)} did not return its result"]
    if isinstance(active, int):
        parts[0] += f"; last activity {time.strftime('%H:%M', time.gmtime(active))} UTC"
    if path is not None:
        parts.append(f"Transcript: {path}")
    parts.append("It may still be running after a reconnect: stop its jobs before starting a replacement")
    resume = "resume it by agent id or re-spawn" if valid_text(agent) else "re-spawn it"
    parts.append(f"Read the transcript for progress, then {resume} from its progress file.")
    return ". ".join(parts)


def orphan_note(record: dict) -> str:
    """Tell the parent it moved on without a foreground subagent's result.

    Args:
        record: The adapter section of the spawn record; unknown parts are left out.

    Returns:
        The note text.
    """
    path = transcript(record)
    parts = [f"[context-guard] {subject(record)} was orphaned: this chat moved on without its result"]
    parts.append("It may still be running" + (f". Transcript: {path}" if path is not None else ""))
    parts.append("Read the transcript for progress and cancel its jobs before re-spawning.")
    return ". ".join(parts)


def silent_note(record: dict, minutes: int) -> str:
    """Tell the parent a subagent has shown no activity for a while.

    Args:
        record: The adapter section of the spawn record; unknown parts are left out.
        minutes: Whole minutes since its last activity or spawn.

    Returns:
        The note text.
    """
    path = transcript(record)
    where = f" Transcript: {path}." if path is not None else ""
    return f"[context-guard] {subject(record)} has been silent for {minutes} minutes.{where} {RECOVERY}"


def detail(record: dict, **extra: object) -> dict[str, object]:
    """Describe a spawn for an event record.

    Args:
        record: The adapter section of the spawn record.
        **extra: Further detail fields.

    Returns:
        The role, agent id and extra fields.
    """
    return {"role": record.get("role"), "agent": record.get("agent"), **extra}


def close_spawns(config: dict, directory: Path, payload: dict[str, object]) -> None:
    """Turn a parent's open foreground spawns into waiting notes at its stop and log each as `subagent_unreturned`.

    Runs for completed, aborted and errored stops; our own follow-up stops (loop_count > 0) are
    skipped. Detached spawns stay tracked.

    Args:
        config: Loaded Cursor adapter configuration.
        directory: Cursor state directory.
        payload: The validated stop payload.
    """
    parent = payload.get("conversation_id")
    if payload.get("loop_count", 0) > 0 or not parent:
        return
    calls = listed(read_adapter(state_path(directory, parent)), OPEN_SPAWNS)
    if not calls:
        return
    records = [read_record(directory, call) for call in calls]
    move_spawns(directory, parent, calls, detach=False, notes=list(map(unreturned_note, records)))
    obs, status = Observation(RUNTIME, parent, STOP), payload.get("status") or "completed"
    logged = [events.record("subagent_unreturned", obs, detail=detail(item, status=status)) for item in records]
    policy.log_events(config, directory, logged)


def finish_spawn(directory: Path, payload: dict[str, object]) -> None:
    """Stop tracking a detached spawn when its subagent's own stop hook reports a completed turn.

    Subagent stop payloads carry no `parent_tool_call_id`; the call comes from PARENT_CALL,
    recorded by the subagent's earlier tool hooks.

    Args:
        directory: Cursor state directory.
        payload: The validated stop payload.
    """
    conversation = payload.get("conversation_id")
    if payload.get("status") != "completed" or not conversation:
        return
    call = read_adapter(state_path(directory, conversation)).get(PARENT_CALL)
    parent = read_record(directory, call).get("parent") if valid_text(call) and call else None
    if not valid_text(parent) or not parent:
        return
    if call in listed(read_adapter(state_path(directory, parent)), DETACHED):
        move_spawns(directory, parent, [call], detach=False)


def detect_orphans(config: dict, directory: Path, payload: dict[str, object], now: float) -> None:
    """Detach open foreground spawns whose parent ran a later non-Task tool, with a note and `subagent_orphaned`.

    A tool started within ORPHAN_GRACE_SECONDS of the spawn may run beside it in the same model
    step, so only a tool started later counts; a payload without a numeric `duration` (ms)
    counts as none.

    Args:
        config: Loaded Cursor adapter configuration.
        directory: Cursor state directory.
        payload: The validated postToolUse payload of a parent's non-Task tool.
        now: Current time in epoch seconds.
    """
    parent, duration = payload.get("conversation_id"), payload.get("duration")
    if not parent or type(duration) not in {int, float} or duration < 0:
        return
    calls = listed(read_adapter(state_path(directory, parent)), OPEN_SPAWNS)
    started = now - duration / 1000
    records = {call: read_record(directory, call) for call in calls}
    orphans = [
        call
        for call, record in records.items()
        if isinstance(record.get("spawned"), int) and record["spawned"] + ORPHAN_GRACE_SECONDS < started
    ]
    if not orphans:
        return
    move_spawns(directory, parent, orphans, detach=True, notes=[orphan_note(records[call]) for call in orphans])
    obs = Observation(RUNTIME, parent, TOOL)
    logged = [events.record("subagent_orphaned", obs, detail=detail(records[call])) for call in orphans]
    policy.log_events(config, directory, logged)


def silence_scan(
    directory: Path, adapter: dict, threshold: float, now: float
) -> tuple[list[str], list[tuple[str, dict, int]]]:
    """Find a parent's detached spawns that finished and spawns silent past the threshold.

    Transcripts are read only for detached spawns idle at least ACTIVITY_SECONDS and for spawns
    idle past the threshold. A spawn with no activity yet is measured from its spawn time.

    Args:
        directory: Cursor state directory.
        adapter: The adapter section of the parent's state.
        threshold: Seconds of silence that raise a note.
        now: Current time in epoch seconds.

    Returns:
        The finished detached calls, and (call, record, silence mark) for each spawn not yet
        warned about in its current silence period.
    """
    detached, done, silent = listed(adapter, DETACHED), [], []
    for call in listed(adapter, OPEN_SPAWNS) + detached:
        record = read_record(directory, call)
        mark = record.get("active", record.get("spawned"))
        floor = min(threshold, ACTIVITY_SECONDS) if call in detached else threshold
        if not isinstance(mark, int) or now - mark < floor:
            continue
        if finished(record):
            done += [call] if call in detached else []
        elif now - mark >= threshold and record.get("warned") != mark:
            silent.append((call, record, mark))
    return done, silent


def check_silence(config: dict, directory: Path, payload: dict[str, object], now: float) -> None:
    """Retire a parent's finished detached spawns and queue one note per silent spawn and silence period.

    Each note is logged as `subagent_silent` with the minutes of silence.

    Args:
        config: Loaded Cursor adapter configuration.
        directory: Cursor state directory.
        payload: The validated postToolUse or sessionStart payload of a parent.
        now: Current time in epoch seconds.
    """
    parent = payload.get("conversation_id")
    adapter = read_adapter(state_path(directory, parent)) if parent else {}
    if not listed(adapter, OPEN_SPAWNS) and not listed(adapter, DETACHED):
        return
    threshold = config["subagents"]["silence_minutes"] * 60
    done, silent = silence_scan(directory, adapter, threshold, now)
    if not done and not silent:
        return
    for call, _, mark in silent:
        with state_mod.session_state(directory, RUNTIME, SPAWN_RECORD + call) as state:
            state.setdefault("adapter", {})["warned"] = mark
    minutes = [int(now - mark) // 60 for _, _, mark in silent]
    notes = [silent_note(record, count) for (_, record, _), count in zip(silent, minutes, strict=True)]
    move_spawns(directory, parent, done, detach=False, notes=notes)
    obs = Observation(RUNTIME, parent, TOOL)
    logged = [
        events.record("subagent_silent", obs, detail=detail(record, minutes=count))
        for (_, record, _), count in zip(silent, minutes, strict=True)
    ]
    policy.log_events(config, directory, logged)


def track(config: dict, directory: Path, native: str, payload: dict[str, object]) -> None:
    """Update spawn bookkeeping for one hook call, whatever the hook answers.

    Args:
        config: Loaded Cursor adapter configuration.
        directory: Cursor state directory.
        native: Cursor hook event name.
        payload: The validated hook payload.
    """
    now = clock()
    if native == "stop":
        close_spawns(config, directory, payload)
        finish_spawn(directory, payload)
    elif native == "postToolUse" and payload.get("parent_tool_call_id"):
        touch(directory, payload, now)
    elif native == "postToolUse":
        if payload.get("tool_name") in config["tools"]["spawn"]:
            mark_returned(directory, payload)
        else:
            detect_orphans(config, directory, payload, now)
        check_silence(config, directory, payload, now)
    elif native == "sessionStart":
        check_silence(config, directory, payload, now)


def deliver(obs: Observation, decision: Decision, directory: Path) -> Decision:
    """Append waiting subagent notes to a main chat's tool result or session start, once.

    Args:
        obs: The observation being answered.
        decision: The decision to render.
        directory: Cursor state directory.

    Returns:
        The decision with the notes appended to its message, or unchanged when none wait.
    """
    if obs.is_subagent or obs.event not in (TOOL, SESSION) or (obs.tool and obs.tool.action == SPAWN):
        return decision
    if not read_adapter(state_path(directory, obs.session_id)).get(NOTES):
        return decision
    with state_mod.session_state(directory, RUNTIME, obs.session_id, commit=True) as state:
        notes = state.setdefault("adapter", {}).pop(NOTES, [])
    texts = [note for note in notes if valid_text(note)] if isinstance(notes, list) else []
    if not texts:
        return decision
    message = "\n\n".join(part for part in (decision.message, *texts) if part)
    return replace(decision, kind=NOTE if obs.event == TOOL else CONTEXT, message=message)
