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
SHELL_WRITES = {">", ">>", "tee"}


def task_filename(label: str, identity: str) -> str:
    """Render a provisional label and the caller's complete chat identity digest."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40].rstrip("-") or "task"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{suffix}.md"


def saved_task_path(path: str) -> str:
    """Find the first-save semantic name sharing this path's identity suffix.

    Existing paths always win; missing or ambiguous matches keep the requested path.
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
        candidates = [candidate for candidate in original.parent.glob("*" + ending)
                      if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*" + re.escape(ending), candidate.name)
                      and not candidate.is_symlink() and candidate.is_file()]
    except (OSError, UnicodeError):
        return path
    return str(candidates[0]) if len(candidates) == 1 else path


def capture_label(state: dict, label: str) -> str:
    """Keep the first task label, ignoring empty titles and bookkeeping turns."""
    label = label.strip()
    if label and label.casefold() not in PLACEHOLDER_LABELS and not label.startswith(BOOKKEEPING_PREFIX):
        state.setdefault("handoff_label", label[:500])
    return state.get("handoff_label", "")


def session_handoff_path(state: dict, identity: str, directory: Path, label: str = "") -> str:
    """Return the session's handoff path, remembering it once it is meaningful."""
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
    if not path or not path.endswith(".md"):
        return False
    try:
        resolved, store = expand_path(path, home).resolve(), directory.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved.parent == store


def resolve(path: str, cwd: str, home: Path) -> str:
    candidate = expand_path(path, home)
    return str(candidate if candidate.is_absolute() or not cwd else Path(cwd) / candidate)


def store_path(paths: list[str], cwd: str, home: Path, store: Path) -> str | None:
    resolved = [resolve(path, cwd, home) for path in paths]
    return next((path for path in resolved if in_store(path, store, home)), None)


def shell_access(command: str, cwd: str, home: Path, store: Path) -> tuple[str, str] | None:
    """(read or write, path) for the first handoff a shell command names; a redirect or tee target is a write."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for previous, word in zip(["", *words], words):
        target = word.lstrip(">")
        path = store_path([target], cwd, home, store) if target.endswith(".md") else None
        if path is not None:
            return (WRITE if previous in SHELL_WRITES or word.startswith(">") else READ), path
    return None


@dataclass(frozen=True)
class Entry:
    path: Path
    mtime: float
    title: str
    status: str
    workspace: str
    goal: str


def read_entry(path: Path) -> Entry:
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
    """Open handoffs, newest first, with one entry per file identity."""
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
    match = IDENTITY_SUFFIX.search(entry.path.name)
    if match:
        return match[1]
    try:
        return str(entry.path.resolve())
    except (OSError, RuntimeError):
        return str(entry.path)


def dedupe(entries: list[Entry]) -> list[Entry]:
    seen, kept = set(), []
    for entry in entries:
        key = identity(entry)
        if key not in seen:
            seen.add(key)
            kept.append(entry)
    return kept


def project_root(cwd: str, home: Path) -> Path | None:
    """The git root containing cwd, unless it is the home or a scratch directory."""
    if not cwd:
        return None
    try:
        start = expand_path(cwd, home).resolve()
        root = next((parent for parent in (start, *start.parents) if (parent / ".git").exists()), None)
        scratch = {(home / name).resolve() for name in SCRATCH_DIRS}
    except (OSError, RuntimeError):
        return None
    return None if root is None or root in scratch else root


def workspace_rank(entry: Entry, roots: tuple[str, ...]) -> int:
    workspace = entry.workspace
    if workspace and workspace in roots:
        return 0
    if workspace and any(workspace.startswith(root + "/") or root.startswith(workspace + "/") for root in roots):
        return 1
    if not workspace or Path(workspace).exists():
        return 2
    return 3


def ranked(entries: list[Entry], roots: tuple[str, ...]) -> list[tuple[int, Entry]]:
    """Same workspace first, then related, other, and missing workspaces; newest first within each."""
    return sorted(((workspace_rank(entry, roots), entry) for entry in entries),
                  key=lambda pair: (pair[0], -pair[1].mtime))


def entry_line(rank: int, entry: Entry, likely: bool) -> str:
    updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
    where = {0: ", this workspace", 1: ", related workspace", 3: ", workspace missing"}.get(rank, "")
    marker = " (likely match)" if likely else ""
    goal = f": {entry.goal}" if entry.goal else ""
    return f"- {entry.title} — {entry.path} (updated {updated}{where}){marker}{goal}"


def listing(directory: Path, cwd: str, home: Path, limit: int) -> str:
    """Rank open handoffs for a session; outside a project, point at the store instead."""
    entries = open_entries(directory)
    if not entries:
        return ""
    root = project_root(cwd, home)
    if root is None:
        return f"Open handoffs from earlier chats are in {directory}; if this request continues one, search it by keyword."
    roots = tuple(dict.fromkeys((str(root), str(expand_path(cwd, home).resolve()))))
    order = ranked(entries, roots)
    lines = [f"Open handoffs in {directory} (most relevant first):"]
    lines += [entry_line(rank, entry, index == 0 and rank == 0) for index, (rank, entry) in enumerate(order[:limit])]
    if len(order) > limit:
        lines.append(f"{len(order) - limit} more in {directory}; search by keyword.")
    lines.append("If the request continues one of these, read it before anything else and confirm the match in "
                 "one line; if two could match, ask which. Ignore the list for unrelated requests.")
    return "\n".join(lines)


def publish_pending(job: dict, directory: Path, logs: dict) -> Path:
    """Publish a final runner report into the store independently of its retained job logs."""
    if job.get("status") not in FINAL_JOB_STATUSES:
        raise ValueError("only final jobs can be published")
    job_id = job["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise ValueError("invalid job id")
    entry = {
        "id": job_id, "workspace": str(Path(job["workspace"]).resolve()),
        "conversation": job.get("conversation") or None,
        "handoff": None if job.get("handoff") in (None, "none", "") else job["handoff"],
        "execution_status": job["status"], "task_status": "unverified",
        "request": str(job.get("prompt") or "")[:2500],
        "report": str(job.get("report") or job.get("error") or "")[-2500:],
        "finished_at": time.time(), "seconds": job.get("seconds"),
        "logs": logs, "denials": [
            {"subject": str(g.get("subject") or "")[:500], "reason": str(g["denied"])[:500]}
            for g in job.get("guard") or [] if g.get("denied")
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
    try:
        if path.stat().st_size > MAX_PENDING_BYTES:
            return None
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        entry = json.loads(text)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def pending_entries(roots: list[str], directory: Path) -> list[tuple[Path, dict]]:
    """Read exact-workspace reports; malformed or unavailable files fail open."""
    workspaces = {str(Path(root).resolve()) for root in roots if root}
    try:
        paths = sorted((directory / "pending").glob("*.json"))
    except OSError:
        return []
    entries = []
    for path in paths:
        entry = read_pending(path)
        if (entry is not None and isinstance(entry.get("workspace"), str) and entry["workspace"] in workspaces
                and entry.get("execution_status") in FINAL_JOB_STATUSES and isinstance(entry.get("id"), str)):
            entries.append((path, entry))
    return entries


def pending_context(roots: list[str], directory: Path) -> str:
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
