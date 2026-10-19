"""Handoff store: naming, discovery, ranked listing and pending runner reports."""

import hashlib
import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from core.config import expand_path
from core.observation import READ, WRITE

PLACEHOLDER_LABELS = {"new chat", "new conversation", "untitled", "untitled chat", "untitled conversation"}
BOOKKEEPING_PREFIX = "[context-guard]"
HANDOFF_REQUEST = re.compile(r"\b(handoff|hand-off|wrap up|new chat)\b", re.IGNORECASE)
IDENTITY_SUFFIX = re.compile(r"-([0-9a-f]{16})\.md$")
SCRATCH_DIRS = ("", "Documents", "Desktop", "Downloads")
FINAL_JOB_STATUSES = ("done", "failed", "timed out")
MAX_PENDING_BYTES = 65536
SHELL_SEPARATORS = {"|", "||", "&&", ";", "&"}
SHELL_REDIRECTS = {">", ">>"}
COPY_COMMANDS = {"cp", "mv", "install"}
COMMAND_WRAPPERS = {"sudo", "env", "command", "nohup", "time"}
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def task_slug(label: str) -> str:
    """Turn a task label into a file name slug.

    Args:
        label: Free-text task label.

    Returns:
        Lowercase hyphen-separated alphanumerics, at most 40 characters, or "task" when nothing is left.
    """
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40].rstrip("-") or "task"


def task_filename(label: str, identity: str) -> str:
    """Render a provisional label and the caller's complete chat identity digest.

    Args:
        label: Task label for the slug.
        identity: The chat's complete identity; its digest is the file's identity suffix.

    Returns:
        `<slug>-<16 hex digits>.md`.
    """
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{task_slug(label)}-{suffix}.md"


def saved_task_path(path: str) -> str:
    """Find the first-save semantic name sharing this path's identity suffix.

    Existing paths always win; missing or ambiguous matches keep the requested path.

    Args:
        path: A handoff path, possibly provisional.

    Returns:
        The single saved file with the same identity suffix, else `path`; unreadable stores keep `path`.
    """
    if "\0" in path:
        return path
    original = Path(path)
    match = IDENTITY_SUFFIX.search(original.name)
    if match is None:
        return path
    ending = "-" + match[1] + ".md"
    try:
        if original.exists() or original.is_symlink():
            return path
        candidates = [
            candidate
            for candidate in original.parent.glob("*" + ending)
            if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*" + re.escape(ending), candidate.name)
            and not candidate.is_symlink()
            and candidate.is_file()
        ]
    except OSError, UnicodeError:
        return path
    return str(candidates[0]) if len(candidates) == 1 else path


def capture_label(state: dict, label: str) -> str:
    """Keep the first task label's slug, ignoring empty titles and bookkeeping turns.

    Args:
        state: Session state; `handoff_label` is set once.
        label: Candidate label, usually the prompt.

    Returns:
        The session's label slug, or empty when none has been captured.
    """
    label = label.strip()
    if label and label.casefold() not in PLACEHOLDER_LABELS and not label.startswith(BOOKKEEPING_PREFIX):
        state.setdefault("handoff_label", task_slug(label))
    return state.get("handoff_label", "")


def session_handoff_path(state: dict, identity: str, directory: Path, label: str = "") -> str:
    """Return the session's handoff path, remembering it once it is meaningful.

    A path is remembered once the session has a label or a saved file with its identity exists.

    Args:
        state: Session state; `handoff_path` and `handoff_label` are updated.
        identity: The chat's complete identity.
        directory: The handoff store.
        label: Candidate task label.

    Returns:
        The saved or provisional handoff path.
    """
    if state.get("handoff_path"):
        state["handoff_path"] = saved_task_path(state["handoff_path"])
        return state["handoff_path"]
    label = capture_label(state, label)
    provisional = str(directory / task_filename(label, identity))
    path = saved_task_path(provisional)
    if label or path != provisional:
        state["handoff_path"] = path
    return path


def in_store(path: str | None, directory: Path, home: Path) -> bool:
    """Tell whether a path is a Markdown file directly inside the handoff store.

    Args:
        path: Candidate path; `~` expands against `home`.
        directory: The handoff store.
        home: Home directory that `~` stands for.

    Returns:
        True when the resolved path's parent is the resolved store; unresolvable paths give False.
    """
    if not path or not path.endswith(".md"):
        return False
    try:
        resolved, store = expand_path(path, home).resolve(), directory.resolve()
    except OSError, RuntimeError, ValueError:
        return False
    return resolved.parent == store


def resolve(path: str, cwd: str, home: Path) -> str:
    """Make a command's path absolute against its working directory.

    Args:
        path: Path as written; `~` expands against `home`.
        cwd: The command's working directory, or empty when unknown.
        home: Home directory that `~` stands for.

    Returns:
        The expanded path, joined to `cwd` when relative and `cwd` is known.
    """
    candidate = expand_path(path, home)
    return str(candidate if candidate.is_absolute() or not cwd else Path(cwd) / candidate)


def store_path(paths: list[str], cwd: str, home: Path, store: Path) -> str | None:
    """Return the first path that names a handoff in the store.

    Args:
        paths: Paths as written.
        cwd: The command's working directory, or empty when unknown.
        home: Home directory that `~` stands for.
        store: The handoff store.

    Returns:
        The first resolved path inside the store, or None.
    """
    resolved = [resolve(path, cwd, home) for path in paths]
    return next((path for path in resolved if in_store(path, store, home)), None)


def command_segments(words: list[str]) -> list[list[str]]:
    """Split shell words into simple commands at pipes, lists and background separators.

    Args:
        words: Shell words of one command line.

    Returns:
        The non-empty simple commands.
    """
    segments: list[list[str]] = [[]]
    for word in words:
        if word in SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(word)
    return [segment for segment in segments if segment]


def split_redirects(words: list[str]) -> tuple[list[str], list[str]]:
    """One simple command's arguments and its output redirect targets.

    Args:
        words: Shell words of one simple command.

    Returns:
        The words that are not redirects, and the targets of `>` and `>>`.
    """
    arguments, targets, previous = [], [], ""
    for word in words:
        if previous in SHELL_REDIRECTS:
            targets.append(word)
        elif word.startswith(">"):
            targets += [word.lstrip(">")] if word.lstrip(">") else []
        else:
            arguments.append(word)
        previous = word
    return arguments, targets


def command_words(arguments: list[str]) -> list[str]:
    """The command and its operands, past leading NAME=value assignments, wrappers and their options.

    Args:
        arguments: One simple command's arguments without redirects.

    Returns:
        The arguments from the wrapped command's name on.
    """
    index = 0
    while index < len(arguments) and (
        ENV_ASSIGNMENT.match(arguments[index])
        or Path(arguments[index]).name in COMMAND_WRAPPERS
        or arguments[index].startswith("-")
    ):
        index += 1
    return arguments[index:]


def segment_accesses(words: list[str]) -> list[tuple[str, str]]:
    """Redirect and tee targets and cp/mv/install destinations are writes; other operands are reads.

    Args:
        words: Shell words of one simple command.

    Returns:
        (READ or WRITE, word) for every redirect target and non-option operand.
    """
    arguments, targets = split_redirects(words)
    arguments = command_words(arguments)
    name = Path(arguments[0]).name if arguments else ""
    operands = [word for word in arguments[1:] if not word.startswith("-")]
    split = 0 if name == "tee" else len(operands) - 1 if name in COPY_COMMANDS else len(operands)
    return (
        [(WRITE, word) for word in targets]
        + [(READ, word) for word in operands[:split]]
        + [(WRITE, word) for word in operands[split:]]
    )


def shell_access(command: str, cwd: str, home: Path, store: Path) -> tuple[str, str] | None:
    """Classify the handoff a shell command names, preferring the first write.

    Unbalanced quoting falls back to splitting on whitespace.

    Args:
        command: The shell command line.
        cwd: The command's working directory, or empty when unknown.
        home: Home directory that `~` stands for.
        store: The handoff store.

    Returns:
        (READ or WRITE, resolved path) for a Markdown file in the store, or None when it names none.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    found = [
        (kind, path)
        for segment in command_segments(words)
        for kind, word in segment_accesses(segment)
        if word.endswith(".md") and (path := store_path([word], cwd, home, store)) is not None
    ]
    return next((access for access in found if access[0] == WRITE), found[0] if found else None)


@dataclass(frozen=True)
class Entry:
    """One handoff file as listed for a session.

    Attributes:
        path: The handoff file.
        mtime: Its modification time in epoch seconds.
        title: Its first `# ` heading, else the file stem.
        status: Its `Status:` value, "open" when absent.
        workspace: Its `Workspace:` value without a trailing slash, or empty.
        goal: The first line under `## Goal`, at most 160 characters, or empty.
    """

    path: Path
    mtime: float
    title: str
    status: str
    workspace: str
    goal: str


def read_entry(path: Path) -> Entry:
    """Read a handoff's title, status, workspace and goal from its first 30 lines.

    Args:
        path: The handoff file.

    Returns:
        The entry; undecodable bytes are ignored.
    """
    title, status, workspace, goal = path.stem, "open", "", ""
    lines = path.read_text(errors="ignore").splitlines()[:30]
    for index, line in enumerate(lines):
        if line.startswith("# ") and title == path.stem:
            title = line[2:].strip()
        if match := re.search(r"\bstatus:\s*([^·]+)", line, re.IGNORECASE):
            status = match[1].strip()
        if match := re.search(r"\bworkspace:\s*([^·]+)", line, re.IGNORECASE):
            workspace = match[1].strip().rstrip("/")
        if line.strip().lower() == "## goal" and index + 1 < len(lines):
            goal = lines[index + 1].strip()[:160]
    return Entry(path, path.stat().st_mtime, title, status, workspace, goal)


def open_entries(directory: Path) -> list[Entry]:
    """Open handoffs, newest first, with one entry per file identity.

    Args:
        directory: The handoff store.

    Returns:
        Entries whose status does not start with "done"; unreadable stores and files are skipped.
    """
    try:
        paths = list(directory.glob("*.md"))
    except OSError:
        return []
    entries = []
    for path in paths:
        try:
            entry = read_entry(path)
        except OSError:
            continue
        if not entry.status.lower().startswith("done"):
            entries.append(entry)
    entries.sort(key=lambda entry: -entry.mtime)
    return dedupe(entries)


def identity(entry: Entry) -> str:
    """Return the key that identifies a handoff across renames.

    Args:
        entry: The handoff entry.

    Returns:
        Its identity suffix, else its resolved path, else its path as given.
    """
    match = IDENTITY_SUFFIX.search(entry.path.name)
    if match:
        return match[1]
    try:
        return str(entry.path.resolve())
    except OSError, RuntimeError:
        return str(entry.path)


def dedupe(entries: list[Entry]) -> list[Entry]:
    """Keep the first entry of each identity.

    Args:
        entries: Entries in priority order.

    Returns:
        The entries without later duplicates, order preserved.
    """
    seen, kept = set(), []
    for entry in entries:
        key = identity(entry)
        if key not in seen:
            seen.add(key)
            kept.append(entry)
    return kept


def project_root(cwd: str, home: Path) -> Path | None:
    """The git root containing cwd, unless it is the home or a scratch directory.

    Args:
        cwd: The session's working directory, or empty when unknown.
        home: Home directory that `~` stands for and whose scratch directories are excluded.

    Returns:
        The resolved project root, or None outside a project or when the path cannot be resolved.
    """
    if not cwd:
        return None
    try:
        start = expand_path(cwd, home).resolve()
        root = next((parent for parent in (start, *start.parents) if (parent / ".git").exists()), None)
        scratch = {(home / name).resolve() for name in SCRATCH_DIRS}
    except OSError, RuntimeError:
        return None
    return None if root is None or root in scratch else root


def workspace_rank(entry: Entry, roots: tuple[str, ...]) -> int:
    """Rank how closely a handoff's workspace matches the session's.

    Args:
        entry: The handoff entry.
        roots: The session's project root and working directory.

    Returns:
        0 for the same workspace, 1 for a parent or child, 2 for another existing or unnamed workspace,
        3 for a missing one.
    """
    workspace = entry.workspace
    if workspace and workspace in roots:
        return 0
    if workspace and any(workspace.startswith(root + "/") or root.startswith(workspace + "/") for root in roots):
        return 1
    if not workspace or Path(workspace).exists():
        return 2
    return 3


def ranked(entries: list[Entry], roots: tuple[str, ...]) -> list[tuple[int, Entry]]:
    """Same workspace first, then related, other, and missing workspaces; newest first within each.

    Args:
        entries: Open handoff entries.
        roots: The session's project root and working directory.

    Returns:
        (rank, entry) pairs in listing order.
    """
    return sorted(
        ((workspace_rank(entry, roots), entry) for entry in entries), key=lambda pair: (pair[0], -pair[1].mtime)
    )


def entry_line(rank: int, entry: Entry, *, likely: bool) -> str:
    """Render one listing line.

    Args:
        rank: The entry's workspace rank.
        entry: The handoff entry.
        likely: Mark the entry as the likely match.

    Returns:
        A Markdown list item with title, path, local update time, workspace relation and goal.
    """
    updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
    where = {0: ", this workspace", 1: ", related workspace", 3: ", workspace missing"}.get(rank, "")
    marker = " (likely match)" if likely else ""
    goal = f": {entry.goal}" if entry.goal else ""
    return f"- {entry.title} — {entry.path} (updated {updated}{where}){marker}{goal}"


def listing(directory: Path, cwd: str, home: Path, limit: int) -> str:
    """Rank open handoffs for a session; outside a project, point at the store instead.

    Args:
        directory: The handoff store.
        cwd: The session's working directory, or empty when unknown.
        home: Home directory that `~` stands for.
        limit: Most entries listed.

    Returns:
        The listing for the model, or empty when no handoff is open.
    """
    entries = open_entries(directory)
    if not entries:
        return ""
    root = project_root(cwd, home)
    if root is None:
        return (
            f"Open handoffs from earlier chats are in {directory}; if this request continues one, search it by keyword."
        )
    roots = tuple(dict.fromkeys((str(root), str(expand_path(cwd, home).resolve()))))
    order = ranked(entries, roots)
    lines = [f"Open handoffs in {directory} (most relevant first):"]
    lines += [
        entry_line(rank, entry, likely=index == 0 and rank == 0) for index, (rank, entry) in enumerate(order[:limit])
    ]
    if len(order) > limit:
        lines.append(f"{len(order) - limit} more in {directory}; search by keyword.")
    lines.append(
        "If the request continues one of these, read it before anything else and confirm the match in "
        "one line; if two could match, ask which. Ignore the list for unrelated requests."
    )
    return "\n".join(lines)


def publish_pending(job: dict, directory: Path, logs: dict) -> Path:
    """Publish a final runner report into the store independently of its retained job logs.

    The report is written atomically to `<directory>/pending/<job id>.json`, with request and report
    text truncated and task completion marked unverified.

    Args:
        job: The runner job record with `id`, `status` and `workspace`.
        directory: The handoff store.
        logs: References to the job's retained logs, stored as given.

    Returns:
        The published report file.

    Raises:
        ValueError: When the job is not final or its id is not a safe file name.
    """
    if job.get("status") not in FINAL_JOB_STATUSES:
        msg = "only final jobs can be published"
        raise ValueError(msg)
    job_id = job["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        msg = "invalid job id"
        raise ValueError(msg)
    entry = {
        "id": job_id,
        "workspace": str(Path(job["workspace"]).resolve()),
        "conversation": job.get("conversation") or None,
        "handoff": None if job.get("handoff") in (None, "none", "") else job["handoff"],
        "execution_status": job["status"],
        "task_status": "unverified",
        "request": str(job.get("prompt") or "")[:2500],
        "report": str(job.get("report") or job.get("error") or "")[-2500:],
        "finished_at": time.time(),
        "seconds": job.get("seconds"),
        "logs": logs,
        "denials": [
            {"subject": str(g.get("subject") or "")[:500], "reason": str(g["denied"])[:500]}
            for g in job.get("guard") or []
            if g.get("denied")
        ][:20],
    }
    pending = directory / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    destination = pending / (job_id + ".json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=str(pending), suffix=".tmp", delete=False) as out:
            temporary = Path(out.name)
            json.dump(entry, out, indent=1)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def read_pending(path: Path) -> dict | None:
    """Read one pending runner report, failing open.

    Args:
        path: The report file.

    Returns:
        The report object, or None when it is unreadable, larger than MAX_PENDING_BYTES, malformed or
        not an object.
    """
    try:
        if path.stat().st_size > MAX_PENDING_BYTES:
            return None
        text = path.read_text()
    except OSError, UnicodeDecodeError:
        return None
    try:
        entry = json.loads(text)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def pending_entries(roots: list[str], directory: Path) -> list[tuple[Path, dict]]:
    """Read exact-workspace reports; malformed or unavailable files fail open.

    Args:
        roots: Workspaces to match exactly after resolving; empty entries are ignored.
        directory: The handoff store.

    Returns:
        (file, report) pairs of final reports for those workspaces, sorted by file name.
    """
    workspaces = {str(Path(root).resolve()) for root in roots if root}
    try:
        paths = sorted((directory / "pending").glob("*.json"))
    except OSError:
        return []
    entries = []
    for path in paths:
        entry = read_pending(path)
        if (
            entry is not None
            and isinstance(entry.get("workspace"), str)
            and entry["workspace"] in workspaces
            and entry.get("execution_status") in FINAL_JOB_STATUSES
            and isinstance(entry.get("id"), str)
        ):
            entries.append((path, entry))
    return entries


def pending_context(roots: list[str], directory: Path) -> str:
    """Render the pending runner reports for the session's workspaces with merge instructions.

    Args:
        roots: Workspaces to match exactly.
        directory: The handoff store.

    Returns:
        The context for the model, or empty when there are no reports.
    """
    entries = pending_entries(roots, directory)
    if not entries:
        return ""
    lines = ["Pending runner reports for this exact workspace:"]
    for path, entry in entries:
        handoff = entry.get("handoff")
        target = saved_task_path(handoff) if isinstance(handoff, str) and handoff else "no handoff yet"
        lines.append(f"- {path} — execution {entry['execution_status']}; task completion unverified; target: {target}")
    lines.append(
        "Before continuing work, read these exact reports and their logs as needed. Merge the request, result, "
        "verified completion and remaining work into the matching task handoff (create one if needed). Save the "
        "handoff successfully FIRST, then delete ONLY the exact consumed reports. A failed save deletes nothing. "
        "Process success alone does not prove the task is complete."
    )
    return "\n".join(lines)
