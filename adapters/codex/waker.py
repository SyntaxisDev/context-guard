"""Wake an idle Codex root thread when one of its background subagents finishes.

Codex starts no root turn when a child's final answer reaches an idle root. The SubagentStop hook
starts `python waker.py <job JSON>` detached; it waits for the child's completion record in the
parent rollout, then queues a short user message on the root with `codex queue`, which starts a
turn that receives the child's result. Every outcome is logged as `subagent_wake`; failures change
nothing else.
"""

import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from adapters.codex import mapping, rollout, runner  # noqa: E402
from core import config as config_mod, events, policy, processes  # noqa: E402
from core.observation import STOP, Observation  # noqa: E402
from core.state import file_lock, read_json, write_json  # noqa: E402
from core.tail import tail_lines  # noqa: E402

RUNTIME = mapping.RUNTIME
NATIVE_FEATURE = "wake_on_agent_messages"
ROOT_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
AGENT_PATH = re.compile(r"[\w/.-]{1,128}")
MESSAGE = "[context-guard] subagent {name} finished; its result is waiting."
POLL_SECONDS = 0.5
POLL_LIMIT_SECONDS = 15.0
SETTLE_SECONDS = 1.0
QUEUE_TIMEOUT_SECONDS = 30.0
PENDING_SECONDS = 60.0
MARKER_DAYS = 1
LOG_FILE = "waker.log"
LOG_LIMIT_BYTES = 256 * 1024
WAKES_DIR = "wakes"
TURN_STARTED = "task_started"
TURN_ENDED = frozenset({"task_complete", "turn_aborted"})
SENT, SKIPPED, FAILED = "sent", "skipped", "failed"


@dataclasses.dataclass(frozen=True)
class Job:
    """One wake request, passed from the hook to the detached waker as JSON.

    Attributes:
        rollout: The parent (root) rollout file.
        root: The root thread id.
        agent: The finished subagent's thread id.
        codex_home: The Codex home `codex queue` runs against.
        state_dir: The Codex state directory for the log, markers and events.
        tail_bytes: How many trailing rollout bytes to scan.
        events: The `events` configuration section.
    """

    rollout: str
    root: str
    agent: str
    codex_home: str
    state_dir: str
    tail_bytes: int
    events: dict

    def to_json(self) -> str:
        """Encode the job for the waker's command line.

        Returns:
            The job as one JSON object.
        """
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Job:
        """Decode and validate a job from the waker's command line.

        Args:
            text: The JSON object from `to_json`.

        Returns:
            The job.

        Raises:
            ValueError: The text is not a job object with the expected field types.
        """
        data = json.loads(text)
        kinds = {field.name: field.type for field in dataclasses.fields(cls)}
        if not isinstance(data, dict) or set(data) != set(kinds):
            msg = "wake job must be an object with exactly the job fields"
            raise ValueError(msg)
        if any(type(data[name]) is not kind for name, kind in kinds.items()):
            msg = "wake job field has the wrong type"
            raise ValueError(msg)
        return cls(**data)


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What one wake attempt did.

    Attributes:
        outcome: SENT, SKIPPED or FAILED.
        reason: Why it skipped or failed; empty when sent.
        name: The subagent name used in the message: its agent path, else its thread id.
        matched: True when the parent rollout showed the subagent's completion record.
    """

    outcome: str
    reason: str
    name: str
    matched: bool


def root_thread(transcript: object) -> str | None:
    """Read the root thread id from a rollout file name.

    Args:
        transcript: Untrusted `transcript_path` from the hook payload.

    Returns:
        The thread id the file name ends with, or None for anything else.
    """
    if not config_mod.valid_text(transcript):
        return None
    match = ROOT_ID.search(transcript)
    return match[1] if match else None


def native_wake(codex_home: Path) -> bool:
    """Tell whether Codex's own child-completion wake is enabled in config.toml.

    Args:
        codex_home: The Codex home holding config.toml.

    Returns:
        True when `[features] wake_on_agent_messages` is true or a table with `enabled = true`;
        False when it is off or the file is missing or unreadable.
    """
    try:
        features = tomllib.loads((codex_home / "config.toml").read_text()).get("features")
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        return False
    value = features.get(NATIVE_FEATURE) if isinstance(features, dict) else None
    return value is True or (isinstance(value, dict) and value.get("enabled") is True)


def start(
    payload: Mapping[str, object],
    agent: str,
    config: dict,
    home: Path,
    environ: Mapping[str, str],
    spawn: Callable[..., object] | None = None,
) -> None:
    """Start a detached waker for a finished subagent of the root thread.

    Nothing starts when `subagents.wake_parent` is off, the parent rollout is not a root rollout
    file, the subagent's parent is not the root, or Codex's native wake is enabled (logged as
    skipped). OSError from creating the state directory, the log or the process propagates.

    Args:
        payload: Validated SubagentStop payload.
        agent: Validated subagent thread id.
        config: Loaded Codex configuration.
        home: Home directory of the context-guard install.
        environ: Hook environment; it may set CODEX_HOME.
        spawn: Starts the process, with `subprocess.Popen`'s signature; None uses `subprocess.Popen`.
    """
    if not config["subagents"]["wake_parent"]:
        return
    transcript = payload.get("transcript_path")
    root = root_thread(transcript)
    if root is None or payload.get("session_id") not in (None, root) or not Path(transcript).is_file():
        return
    job = Job(
        rollout=transcript,
        root=root,
        agent=agent,
        codex_home=str(mapping.codex_home(home, environ)),
        state_dir=str(config_mod.state_dir(config, home, RUNTIME)),
        tail_bytes=config["rollout"]["tail_bytes"],
        events=config["events"],
    )
    if native_wake(Path(job.codex_home)):
        record_event(job, Outcome(SKIPPED, f"native {NATIVE_FEATURE} is enabled", agent, matched=False))
        return
    launch(job, environ, spawn or subprocess.Popen)


def launch(job: Job, environ: Mapping[str, str], spawn: Callable[..., object]) -> None:
    """Start the waker in its own session with no inherited pipes, its output appended to the log.

    The log restarts once it exceeds LOG_LIMIT_BYTES.

    Args:
        job: The wake request.
        environ: Hook environment; the waker gets it with CODEX_HOME set to the job's Codex home.
        spawn: Starts the process, with `subprocess.Popen`'s signature.
    """
    state = Path(job.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    log = state / LOG_FILE
    mode = "w" if log.is_file() and log.stat().st_size > LOG_LIMIT_BYTES else "a"
    with log.open(mode) as handle:
        spawn(
            [sys.executable, str(Path(__file__).resolve()), job.to_json()],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            cwd=state,
            env={**environ, mapping.CODEX_HOME_ENV: job.codex_home},
            start_new_session=True,
            close_fds=True,
        )


def records(path: Path, tail_bytes: int, since: int = 0) -> list[dict]:
    """Decode the rollout records in the tail, never before an offset.

    Args:
        path: The rollout file.
        tail_bytes: How many trailing bytes to scan.
        since: Byte offset to start at, at the earliest.

    Returns:
        The JSON object records in file order.
    """
    return [record for record in map(rollout.parse, tail_lines(path, tail_bytes, since)) if record is not None]


def body(record: dict) -> dict:
    """Return a rollout record's payload object.

    Args:
        record: Decoded rollout record.

    Returns:
        The payload, or an empty dict when it is not an object.
    """
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def completion(found: list[dict], agent: str) -> str | None:
    """Find a subagent's completion record among rollout records.

    Args:
        found: Decoded parent rollout records.
        agent: The subagent's thread id.

    Returns:
        The subagent's agent path (or its thread id when the path is missing or unusual) once an
        `item_completed` SubAgentActivity of kind `completed` names it, else None.
    """
    for record in found:
        payload = body(record)
        item = payload.get("item") if payload.get("type") == "item_completed" else None
        if (
            isinstance(item, dict)
            and item.get("type") == "SubAgentActivity"
            and item.get("kind") == "completed"
            and item.get("agent_thread_id") == agent
        ):
            path = item.get("agent_path")
            return path if isinstance(path, str) and AGENT_PATH.fullmatch(path) else agent
    return None


def busy(found: list[dict]) -> bool:
    """Tell whether the root is in a turn: its last turn start has no later turn end.

    Args:
        found: Decoded parent rollout records.

    Returns:
        True when the last turn record is a `task_started`.
    """
    running = False
    for record in found:
        kind = body(record).get("type")
        if kind == TURN_STARTED:
            running = True
        elif kind in TURN_ENDED:
            running = False
    return running


def started_since(path: Path, tail_bytes: int, offset: int) -> bool:
    """Tell whether the root started a turn after a rollout byte offset.

    Args:
        path: The parent rollout file.
        tail_bytes: How many trailing bytes to scan.
        offset: The rollout size at the point of interest.

    Returns:
        True when a `task_started` record follows the offset.
    """
    return any(body(record).get("type") == TURN_STARTED for record in records(path, tail_bytes, offset))


def await_completion(job: Job, clock: Callable[[], float], sleep: Callable[[float], None]) -> tuple[str | None, int]:
    """Poll the parent rollout for the subagent's completion record.

    The hook can fire just before Codex writes the record, so polling covers a short gap.
    OSError from reading the rollout propagates.

    Args:
        job: The wake request.
        clock: Monotonic clock in seconds.
        sleep: Waits a number of seconds.

    Returns:
        The subagent name, or None after POLL_LIMIT_SECONDS without the record, and the rollout
        size when polling stopped.
    """
    path, limit = Path(job.rollout), clock() + POLL_LIMIT_SECONDS
    while True:
        size = path.stat().st_size
        name = completion(records(path, job.tail_bytes), job.agent)
        if name is not None or clock() >= limit:
            return name, size
        sleep(POLL_SECONDS)


def pending(marker: Path, path: Path, tail_bytes: int) -> bool:
    """Tell whether an earlier wake of this root is queued and its turn has not started.

    Args:
        marker: The root's marker file with the rollout `offset` and epoch `at` of the last wake.
        path: The parent rollout file.
        tail_bytes: How many trailing bytes to scan.

    Returns:
        True when the marker is younger than PENDING_SECONDS and no turn started after its offset.
    """
    data = read_json(marker)
    offset, at = data.get("offset"), data.get("at")
    if type(offset) is not int or type(at) not in (int, float) or time.time() - at > PENDING_SECONDS:
        return False
    return not started_since(path, tail_bytes, offset)


def skip_reason(job: Job, mark: int, marker: Path) -> str:
    """Decide, after the settle delay, whether the wake is still needed.

    Args:
        job: The wake request.
        mark: The rollout size when the completion record was seen.
        marker: The root's marker file of the last queued wake.

    Returns:
        Why the wake is not needed, or an empty string.
    """
    path = Path(job.rollout)
    if busy(records(path, job.tail_bytes)):
        return "root is in a turn"
    if started_since(path, job.tail_bytes, mark):
        return "root already took a turn"
    if pending(marker, path, job.tail_bytes):
        return "a wake is already queued"
    return ""


def queue(job: Job, name: str, run: Callable[..., subprocess.CompletedProcess], which: Callable) -> tuple[str, str]:
    """Queue the wake message on the root with `codex queue`.

    Args:
        job: The wake request.
        name: The subagent name for the message.
        run: Runs a command like `core.processes.run`.
        which: Finds an executable on PATH like `shutil.which`.

    Returns:
        SENT with an empty reason, or FAILED with the reason.
    """
    program = which(runner.EXECUTABLE)
    if not program:
        return FAILED, f"{runner.EXECUTABLE} is not on PATH"
    argv = [program, "queue", "--thread", job.root, "--message", MESSAGE.format(name=name)]
    try:
        result = run(
            argv,
            cwd=None,
            env={**os.environ, mapping.CODEX_HOME_ENV: job.codex_home},
            timeout=QUEUE_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return FAILED, f"codex queue: {error}"
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        return FAILED, f"codex queue exited {result.returncode}: {detail[0] if detail else ''}".rstrip(": ")
    return SENT, ""


def prune_markers(directory: Path, keep: Path) -> None:
    """Remove wake markers and locks older than MARKER_DAYS, except the current root's.

    Args:
        directory: The wakes directory.
        keep: The current root's marker; it and its lock are kept.
    """
    cutoff = time.time() - MARKER_DAYS * 86400
    try:
        entries = [entry for entry in directory.iterdir() if entry.stem != keep.stem]
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def wake(
    job: Job,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    run: Callable[..., subprocess.CompletedProcess] = processes.run,
    which: Callable = shutil.which,
) -> Outcome:
    """Wait for the subagent's completion, then wake the root unless it is busy or already woken.

    Without a completion record after the poll limit the wake still proceeds: the hook fired, so the
    subagent stopped. Wakers of one root run one at a time under a lock, after a settle delay that
    lets sibling completions share one wake. OSError from the rollout or the lock propagates.

    Args:
        job: The wake request.
        clock: Monotonic clock in seconds.
        sleep: Waits a number of seconds.
        run: Runs a command like `core.processes.run`.
        which: Finds an executable on PATH like `shutil.which`.

    Returns:
        The outcome.
    """
    found, mark = await_completion(job, clock, sleep)
    name = found or job.agent
    if busy(records(Path(job.rollout), job.tail_bytes)):
        return Outcome(SKIPPED, "root is in a turn", name, matched=found is not None)
    wakes = Path(job.state_dir) / WAKES_DIR
    marker = wakes / f"{job.root}.json"
    with file_lock(wakes / f"{job.root}.lock"):
        sleep(SETTLE_SECONDS)
        reason = skip_reason(job, mark, marker)
        if reason:
            return Outcome(SKIPPED, reason, name, matched=found is not None)
        offset = Path(job.rollout).stat().st_size
        outcome, reason = queue(job, name, run, which)
        if outcome == SENT:
            write_json(marker, {"offset": offset, "at": time.time()})
    prune_markers(wakes, marker)
    return Outcome(outcome, reason, name, matched=found is not None)


def record_event(job: Job, result: Outcome) -> None:
    """Log a wake outcome as `subagent_wake` in the Codex event log.

    Args:
        job: The wake request.
        result: What the attempt did.
    """
    detail = {
        "outcome": result.outcome,
        "reason": result.reason,
        "agent": job.agent,
        "name": result.name,
        "matched": result.matched,
    }
    obs = Observation(RUNTIME, job.root, STOP)
    policy.log_events({"events": job.events}, Path(job.state_dir), [events.record("subagent_wake", obs, detail=detail)])


def log_line(job: Job, result: Outcome) -> None:
    """Write one line about a wake outcome to stderr, which the hook points at the waker log.

    Args:
        job: The wake request.
        result: What the attempt did.
    """
    stamp = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
    reason = f": {result.reason}" if result.reason else ""
    print(f"{stamp} root={job.root} agent={job.agent} {result.outcome}{reason}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    """Run one detached wake: argv holds the job JSON.

    Args:
        argv: Command-line arguments after the script; None reads `sys.argv`.

    Returns:
        0 after any wake outcome, 2 for an invalid job.
    """
    args = sys.argv[1:] if argv is None else argv
    try:
        job = Job.from_json(args[0])
    except (IndexError, ValueError) as error:
        print(f"invalid wake job: {error}", file=sys.stderr, flush=True)
        return 2
    try:
        result = wake(job)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result = Outcome(FAILED, str(error), job.agent, matched=False)
    log_line(job, result)
    record_event(job, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
