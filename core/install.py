"""Plan, apply, reverse and check one InstallSpec against its ownership manifest.

Each runtime (and the shared kit, runtime "kit") has a manifest at
<home>/.context-guard/state/<runtime>/install-manifest.json recording the sha256 of every
owned file, the result of every JSON merge and text edit, and the directories the install
created. A kit-owned file whose current hash differs from the manifest was edited by the
user: it is reported as a conflict and kept unless forced. Unrecorded content that carries
every line of the spec's kit signature for its path is stale kit output and is replaced
without force. Every replaced or removed path is backed up first.
A JSON merge that rewrites an existing file records the file's original text, so uninstall can
put back the user's exact formatting when nothing but our merge changed since.
Planning never writes; dry runs only describe the plan.

JsonMerge.merge and TextEdit.apply must be idempotent: doctor treats merge(current) ==
current as installed, and reinstalling an unchanged home plans no changes.
"""

import copy
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.config import kit_root
from core.install_spec import InstallSpec, JsonMerge, TextEdit
from core.state import read_json, write_json

MANIFEST_NAME = "install-manifest.json"
BACKUPS_DIR = "backups"
OFFERS_DIR = "offers"
DIFF_LINES = 40
WRITE, REMOVE, REMOVE_TREE = "write", "remove", "remove-tree"
SECTIONS = ("files", "merges", "edits")
DIGEST = re.compile(r"[0-9a-f]{64}")
FORCE_HINT = "rerun with --force to replace it (a backup is kept)"
STALE_KIT = "stale kit output"
EMPTY_TEXTS = ("", "{}")


@dataclass(frozen=True)
class Change:
    """One planned file system change.

    Attributes:
        kind: WRITE, REMOVE or REMOVE_TREE.
        path: The path changed.
        text: The full new text for WRITE, None otherwise.
        reason: Why the change is planned, shown in the report.
        backup: Whether to back up the current path first.
        diff: A diff shown with the change, empty for none.
    """

    kind: str
    path: Path
    text: str | None = None
    reason: str = ""
    backup: bool = True
    diff: str = ""


@dataclass(frozen=True)
class Conflict:
    """A path left alone because changing it would lose user content.

    Attributes:
        path: The path left alone.
        message: Why, and how to resolve it.
    """

    path: Path
    message: str


@dataclass
class Plan:
    """The changes and conflicts for one runtime, and the manifest to record after applying.

    Attributes:
        runtime: The manifest name of the runtime.
        changes: Changes to apply, in order.
        conflicts: Paths skipped, reported to the user.
        manifest: The manifest to write, or None to delete it.
        prune: Directories to remove after applying when they are empty.
        notes: Parts of an edit left out for the user to finish; they are not conflicts.
    """

    runtime: str
    changes: list[Change] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    manifest: dict | None = None
    prune: list[str] = field(default_factory=list)
    notes: list[Conflict] = field(default_factory=list)


def digest(text: str) -> str:
    """Hash text the way the manifest records it.

    Args:
        text: The text.

    Returns:
        The sha256 hex digest of its UTF-8 encoding.
    """
    return hashlib.sha256(text.encode()).hexdigest()


def manifest_path(home: Path, runtime: str) -> Path:
    """Locate a runtime's install manifest.

    Args:
        home: The user home.
        runtime: The manifest name of the runtime.

    Returns:
        The manifest file path.
    """
    return kit_root(home) / "state" / runtime / MANIFEST_NAME


def profile_name(name: str, root: Path, default: Path) -> str:
    """Scope an installation or setup marker to its configuration directory.

    Args:
        name: The installation or optional setup step name.
        root: The selected configuration directory.
        default: The default configuration directory.

    Returns:
        The unchanged name for the default directory, otherwise a name with a stable path hash.
    """
    resolved = root.resolve()
    if resolved == default.resolve():
        return name
    suffix = hashlib.sha256(str(resolved).encode()).hexdigest()[:32]
    return f"{name}-{suffix}"


def backup_dir(home: Path) -> Path:
    """Name a fresh backup directory for one install or uninstall run.

    Args:
        home: The user home.

    Returns:
        A directory named after the current local time, not created.
    """
    return kit_root(home) / BACKUPS_DIR / dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def offer_marker(home: Path, name: str) -> Path:
    """Locate the marker recording that an optional setup step was offered once.

    Its path does not follow the configured state directory, so text shipped to a model can name it.

    Args:
        home: The user home.
        name: The setup step, named like the manifest it installs.

    Returns:
        `<home>/.context-guard/state/offers/<name>`.
    """
    return kit_root(home) / "state" / OFFERS_DIR / name


def record_offer(home: Path, name: str) -> None:
    """Create an offer marker so the step is not offered again; an existing one is kept.

    Args:
        home: The user home.
        name: The setup step, named like the manifest it installs.
    """
    marker = offer_marker(home, name)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def read_manifest(path: Path) -> dict:
    """Return a validated manifest; damaged entries are dropped so they read as unowned.

    Args:
        path: The manifest file.

    Returns:
        The validated manifest, or an empty dict when the file is missing or empty.
    """
    data = read_json(path)
    manifest = {
        key: data[key]
        for key in ("runtime", "installed_at", "version", "source", "commit")
        if isinstance(data.get(key), str)
    }
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    manifest["files"] = {k: v for k, v in files.items() if isinstance(v, str) and DIGEST.fullmatch(v)}
    for section in ("merges", "edits"):
        entries = data.get(section) if isinstance(data.get(section), dict) else {}
        manifest[section] = {
            k: v
            for k, v in entries.items()
            if isinstance(v, dict) and isinstance(v.get("created"), bool) and isinstance(v.get("original", ""), str)
        }
    dirs = data.get("dirs") if isinstance(data.get("dirs"), list) else []
    manifest["dirs"] = [item for item in dirs if isinstance(item, str)]
    return manifest if data else {}


def manifest_hash(manifest: dict) -> str:
    """Summarize a manifest's owned files as a short hash for status output.

    Args:
        manifest: A validated manifest.

    Returns:
        12 hex characters.
    """
    return digest(json.dumps(sorted(manifest.get("files", {}).items())))[:12]


def read_text(path: Path) -> str | None:
    """Read a file the installer may change.

    Args:
        path: The file.

    Returns:
        The file's text, or None when it does not exist.

    Raises:
        ValueError: The file exists but cannot be read as text.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read {path}: {exc}"
        raise ValueError(msg) from exc


def parse_object(path: Path, text: str | None) -> dict:
    """Parse a JSON object file's text.

    Args:
        path: The file, used in error messages.
        text: The file's text, or None for a missing file.

    Returns:
        The object; empty for a missing file.

    Raises:
        ValueError: The text is not JSON or not an object.
    """
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = f"{path} is not valid JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{path} must contain a JSON object"
        raise ValueError(msg)  # noqa: TRY004
    return data


def dump_object(data: dict) -> str:
    """Serialize a JSON object the way merged files are written.

    Args:
        data: The object.

    Returns:
        Indented JSON with a trailing newline.
    """
    return json.dumps(data, indent=2) + "\n"


def text_diff(path: Path, current: str, proposed: str) -> str:
    """Render a unified diff of a planned replacement, cut to DIFF_LINES.

    Args:
        path: The file, used in the diff headers.
        current: The text on disk.
        proposed: The text the kit would write.

    Returns:
        The diff lines joined by newlines.
    """
    lines = list(
        difflib.unified_diff(
            current.splitlines(), proposed.splitlines(), f"{path} (current)", f"{path} (context-guard)", lineterm=""
        )
    )
    if len(lines) > DIFF_LINES:
        lines = [*lines[:DIFF_LINES], f"... {len(lines) - DIFF_LINES} more diff lines"]
    return "\n".join(lines)


def stale_kit_output(text: str, signature: tuple[str, ...]) -> bool:
    """Tell whether unrecorded text is output of an earlier kit.

    Args:
        text: The text on disk.
        signature: Lines that together mark kit output; empty never matches.

    Returns:
        True when every signature line appears as a stripped line of `text`.
    """
    lines = {line.strip() for line in text.splitlines()}
    return bool(signature) and all(line in lines for line in signature)


class Planner:
    """Accumulate one plan; each path has at most one change.

    Args:
        spec: The spec being installed or reversed.
        manifest: The runtime's current manifest.
        force: Whether to replace or remove user-edited paths instead of reporting conflicts.

    Attributes:
        spec: The spec being installed or reversed.
        manifest: The runtime's current manifest.
        force: Whether user-edited paths are replaced or removed.
        plan: The plan being built.
        recorded: Per manifest section, the entries the new manifest will hold.
    """

    def __init__(self, spec: InstallSpec, manifest: dict, *, force: bool) -> None:
        self.spec, self.manifest, self.force = spec, manifest, force
        self.plan = Plan(spec.runtime)
        self.recorded = {section: {} for section in SECTIONS}

    def change(
        self, kind: str, path: Path, text: str | None = None, reason: str = "", *, backup: bool = True, diff: str = ""
    ) -> None:
        """Queue a change; unmodified owned files skip the backup because they are re-renderable.

        Args:
            kind: WRITE, REMOVE or REMOVE_TREE.
            path: The path changed.
            text: The full new text for WRITE.
            reason: Why the change is planned.
            backup: Whether to back up the current path first.
            diff: A diff shown with the change.
        """
        self.plan.changes.append(Change(kind, path, text, reason, backup, diff))

    def conflict(self, path: Path, message: str, section: str | None = None) -> None:
        """Report a conflict and keep the path's existing manifest entry so a later run can retry.

        Args:
            path: The path left alone.
            message: Why, and how to resolve it.
            section: The manifest section holding the path's entry, or None for none.
        """
        self.plan.conflicts.append(Conflict(path, message))
        kept = self.manifest.get(section, {}).get(str(path)) if section else None
        if kept is not None:
            self.recorded[section][str(path)] = kept

    def finish(self, extra: dict | None) -> Plan:
        """Complete the plan with its new manifest.

        `installed_at` is kept when nothing else in the manifest changed.

        Args:
            extra: Further manifest fields such as version and provenance, or None.

        Returns:
            The plan.
        """
        manifest = {
            "runtime": self.spec.runtime,
            **(extra or {}),
            **self.recorded,
            "dirs": list(self.manifest.get("dirs", [])),
        }
        unchanged = {k: v for k, v in self.manifest.items() if k != "installed_at"} == manifest
        manifest["installed_at"] = (
            self.manifest["installed_at"]
            if unchanged and "installed_at" in self.manifest
            else dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        )
        self.plan.manifest = manifest
        return self.plan


class InstallPlanner(Planner):
    """Plan an install: write owned files, retire stale ones, apply merges and edits.

    Args:
        spec: The spec to install.
        manifest: The runtime's current manifest.
        force: Whether to replace user-edited files instead of reporting conflicts.

    Attributes:
        known: Recorded digests of owned files by path.
    """

    def __init__(self, spec: InstallSpec, manifest: dict, *, force: bool) -> None:
        super().__init__(spec, manifest, force=force)
        self.known = manifest.get("files", {})

    def owned(self, path: Path, current: str) -> bool:
        """Tell whether a file still holds exactly what the kit recorded.

        Args:
            path: The file.
            current: Its text on disk.

        Returns:
            True when the recorded digest matches.
        """
        return self.known.get(str(path)) == digest(current)

    def stale_kit(self, path: Path, current: str) -> bool:
        """Tell whether an unrecorded file carries the spec's kit signature for its path.

        Args:
            path: The file.
            current: Its text on disk.

        Returns:
            True for unrecorded stale kit output.
        """
        return str(path) not in self.known and stale_kit_output(current, self.spec.kit_signatures.get(path, ()))

    def files(self) -> None:
        """Plan every owned file: write missing, owned or stale ones; others conflict unless forced."""
        for path, text in self.spec.files.items():
            try:
                current = read_text(path)
            except ValueError as exc:
                self.conflict(path, str(exc), "files")
                continue
            if current is None or (current != text and self.owned(path, current)):
                self.change(WRITE, path, text, "file", backup=False)
            elif current != text and self.stale_kit(path, current):
                self.change(WRITE, path, text, STALE_KIT, diff=text_diff(path, current, text))
            elif current != text and self.force:
                self.change(WRITE, path, text, "file (forced)")
            elif current != text:
                self.conflict(
                    path,
                    f"edited or not installed by context-guard; {FORCE_HINT}\n" + text_diff(path, current, text),
                    "files",
                )
                continue
            self.recorded["files"][str(path)] = digest(text)

    def stale_files(self) -> None:
        """Plan removal of recorded files the spec no longer installs; edited ones conflict unless forced."""
        for name, recorded in self.manifest.get("files", {}).items():
            path = Path(name)
            if path in self.spec.files:
                continue
            try:
                current = read_text(path)
            except ValueError as exc:
                self.conflict(path, str(exc), "files")
                continue
            if current is not None and digest(current) == recorded:
                self.change(REMOVE, path, reason="stale", backup=False)
            elif current is not None and self.force:
                self.change(REMOVE, path, reason="stale (forced)")
            elif current is not None:
                self.conflict(path, f"no longer installed but edited; remove it yourself or {FORCE_HINT}", "files")

    def merges(self) -> None:
        """Plan every JSON merge; an unparsable file conflicts."""
        for item in self.spec.json_merges:
            try:
                text = read_text(item.path)
                current = parse_object(item.path, text)
                merged = item.merge(copy.deepcopy(current))
            except ValueError as exc:
                self.conflict(item.path, str(exc), "merges")
                continue
            new = text if text is not None and merged == current else dump_object(merged)
            if text is None and not merged:
                continue
            if new != text:
                self.change(WRITE, item.path, new, "merge")
            self.record("merges", item.path, new, created=text is None, original=text if new != text else None)

    def edits(self) -> None:
        """Plan every text edit; an unreadable file conflicts."""
        for item in self.spec.text_edits:
            try:
                text = read_text(item.path)
                new = item.apply(text or "")
            except ValueError as exc:
                self.conflict(item.path, str(exc), "edits")
                continue
            note = item.notice(new) if item.notice else ""
            if note:
                self.plan.notes.append(Conflict(item.path, note))
            if text is None and not new:
                continue
            if new != text:
                self.change(WRITE, item.path, new, "edit")
            self.record("edits", item.path, new, created=text is None)

    def record(self, section: str, path: Path, text: str, *, created: bool, original: str | None = None) -> None:
        """Record a merge or edit result; an earlier entry's `created` and `original` win.

        Args:
            section: "merges" or "edits".
            path: The changed file.
            text: The file's text after the change.
            created: Whether the install creates the file.
            original: The file's text before the first change, or None when not recorded.
        """
        previous = self.manifest.get(section, {}).get(str(path), {})
        entry = {"sha": digest(text), "created": previous.get("created", created)}
        original = previous.get("original", original)
        self.recorded[section][str(path)] = entry | ({"original": original} if original is not None else {})


def plan(spec: InstallSpec, manifest: dict, *, force: bool = False, extra: dict | None = None) -> Plan:
    """Plan an install without writing anything.

    Args:
        spec: The spec to install.
        manifest: The runtime's current manifest.
        force: Whether to replace user-edited files instead of reporting conflicts.
        extra: Further manifest fields such as version and provenance.

    Returns:
        The plan, with its conflicts left out of the changes.
    """
    planner = InstallPlanner(spec, manifest, force=force)
    planner.files()
    planner.stale_files()
    planner.merges()
    planner.edits()
    return planner.finish(extra)


class UninstallPlanner(Planner):
    """Plan the reversal of a manifest."""

    def files(self) -> None:
        """Plan removal of every recorded file; edited ones conflict unless forced."""
        for name, recorded in self.manifest.get("files", {}).items():
            path = Path(name)
            try:
                current = read_text(path)
            except ValueError as exc:
                self.conflict(path, str(exc), "files")
                continue
            if current is not None and digest(current) == recorded:
                self.change(REMOVE, path, reason="file", backup=False)
            elif current is not None and self.force:
                self.change(REMOVE, path, reason="file (forced)")
            elif current is not None:
                self.conflict(path, "edited since install; kept (rerun with --force to remove it)", "files")

    def reverse[T: (JsonMerge, TextEdit)](
        self, section: str, targets: dict[Path, T], restore: Callable[[T, Path, str, dict], str]
    ) -> None:
        """Plan the reversal of every recorded merge or edit in one section.

        A recorded path the spec no longer describes conflicts. A file reversing leaves empty is
        removed (with a backup) when the install created it, or when it is a merge target whose
        recorded original was not empty: some runtimes reject an empty JSON object.

        Args:
            section: "merges" or "edits".
            targets: The spec's merges or edits by path.
            restore: Return a file's text with the change reversed, given the target, path,
                current text and manifest entry; raises ValueError for an unusable file.
        """
        for name, entry in self.manifest.get(section, {}).items():
            path = Path(name)
            if path not in targets:
                self.conflict(
                    path, "the current adapter no longer knows how to remove its change; edit it by hand", section
                )
                continue
            try:
                text = read_text(path)
                restored = None if text is None else restore(targets[path], path, text, entry)
            except ValueError as exc:
                self.conflict(path, str(exc), section)
                continue
            if restored is None:
                continue
            emptied = restored.strip() in EMPTY_TEXTS
            if emptied and (entry["created"] or (section == "merges" and restored != entry.get("original"))):
                self.change(REMOVE, path, reason=section)
            elif restored != text:
                self.change(WRITE, path, restored, section)


def original_object(path: Path, original: object) -> dict | None:
    """Parse a recorded original text.

    Args:
        path: The file, used in error messages.
        original: The manifest's recorded original, if any.

    Returns:
        The parsed object, or None when nothing usable was recorded.
    """
    try:
        return parse_object(path, original) if isinstance(original, str) else None
    except ValueError:
        return None


def unmerged(item: JsonMerge, path: Path, text: str, entry: dict) -> str:
    """Reverse a JSON merge; the recorded original comes back byte-identical when it parses to the result.

    Args:
        item: The merge to reverse.
        path: The merged file.
        text: Its current text.
        entry: Its manifest entry, possibly holding `original`.

    Returns:
        The current text when the merge is already absent, the original text when it matches,
        and otherwise the reversed object serialized.
    """
    current = parse_object(path, text)
    restored = item.unmerge(copy.deepcopy(current))
    if restored == current:
        return text
    if original_object(path, entry.get("original")) == restored:
        return entry["original"]
    return dump_object(restored)


def reverted(item: TextEdit, _path: Path, text: str, _entry: dict) -> str:
    """Reverse a text edit.

    Args:
        item: The edit to reverse.
        _path: The edited file; unused.
        text: Its current text.
        _entry: Its manifest entry; unused.

    Returns:
        The text with the edit reverted.
    """
    return item.revert(text)


def plan_uninstall(spec: InstallSpec, manifest: dict, *, force: bool = False) -> Plan:
    """Reverse a manifest; conflicted entries stay in a residual manifest for a later --force.

    Args:
        spec: The spec as currently rendered, which knows how to reverse merges and edits.
        manifest: The runtime's current manifest.
        force: Whether to remove user-edited files instead of reporting conflicts.

    Returns:
        The plan; its manifest is None when nothing is left, and the recorded directories are pruned.
    """
    planner = UninstallPlanner(spec, manifest, force=force)
    planner.files()
    planner.reverse("merges", {m.path: m for m in spec.json_merges}, unmerged)
    planner.reverse("edits", {e.path: e for e in spec.text_edits}, reverted)
    result = planner.finish({key: manifest[key] for key in ("version",) if key in manifest})
    if not any(result.manifest[section] for section in SECTIONS):
        result.manifest = None
    result.prune = list(manifest.get("dirs", []))
    return result


class Backup:
    """Copy each path before it is replaced or removed; the directory is created lazily.

    Args:
        directory: The backup directory for this run.
        runtime: Prefix for backup names.

    Attributes:
        directory: The backup directory for this run.
        runtime: Prefix for backup names.
        count: Backups saved so far, used to keep names unique.
    """

    def __init__(self, directory: Path, runtime: str) -> None:
        self.directory, self.runtime, self.count = directory, runtime, 0

    def save(self, path: Path) -> None:
        """Copy a file, symlink or directory and log its original path in `paths.jsonl`.

        Args:
            path: The path to back up; a missing path is skipped.
        """
        if not path.exists() and not path.is_symlink():
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        name = f"{self.runtime}-{self.count}-{path.name}"
        self.count += 1
        if path.is_dir() and not path.is_symlink():
            shutil.copytree(path, self.directory / name, symlinks=True)
        else:
            shutil.copy2(path, self.directory / name, follow_symlinks=path.exists())
        with (self.directory / "paths.jsonl").open("a") as handle:
            handle.write(json.dumps({"path": str(path), "backup": name}) + "\n")


def missing_parents(path: Path) -> list[str]:
    """List the ancestors of a path that do not exist yet.

    Args:
        path: The path about to be written.

    Returns:
        The missing directories, nearest first.
    """
    missing = []
    for parent in path.parents:
        if parent.exists():
            break
        missing.append(str(parent))
    return missing


def replace_text(path: Path, text: str) -> None:
    """Write beside the file and rename over it: a hook importing it mid-install never reads a partial file.

    A symlinked file is replaced at its target so the link survives; an existing file keeps its mode.

    Args:
        path: The file to write.
        text: Its complete new text.
    """
    target = path.resolve() if path.is_symlink() else path
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), "w") as handle:
            handle.write(text)
        if target.exists():
            temporary.chmod(target.stat().st_mode & 0o7777)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def apply_change(change: Change, backup: Backup) -> list[str]:
    """Apply one change, backing up the path first when the change asks for it.

    Args:
        change: The change.
        backup: The run's backup.

    Returns:
        The directories the change created.
    """
    if change.backup:
        backup.save(change.path)
    if change.kind == WRITE:
        created = missing_parents(change.path)
        change.path.parent.mkdir(parents=True, exist_ok=True)
        replace_text(change.path, change.text)
        return created
    if change.kind == REMOVE_TREE:
        shutil.rmtree(change.path)
    else:
        change.path.unlink(missing_ok=True)
    return []


def prune_dirs(dirs: list[str]) -> None:
    """Remove the given directories that are empty, deepest first.

    Args:
        dirs: Directory paths; non-empty or missing ones are skipped.
    """
    for name in sorted(set(dirs), key=lambda item: len(Path(item).parts), reverse=True):
        try:
            Path(name).rmdir()
        except OSError:
            continue


def record_manifest(plan: Plan, manifest_file: Path, created: list[str]) -> None:
    """Write the plan's manifest with every created directory, or delete it when the plan has none.

    Args:
        plan: The applied plan.
        manifest_file: The runtime's manifest file.
        created: Directories the applied changes created.
    """
    if plan.manifest is None:
        manifest_file.unlink(missing_ok=True)
        return
    manifest = dict(plan.manifest)
    manifest["dirs"] = sorted(set(manifest["dirs"]) | set(created) | set(missing_parents(manifest_file)))
    if read_json(manifest_file) != manifest:
        write_json(manifest_file, manifest)


def apply(plan: Plan, manifest_file: Path, backup_directory: Path) -> None:
    """Apply a plan (never call this for dry runs); conflicts were already left out of it.

    Args:
        plan: The plan.
        manifest_file: The runtime's manifest file.
        backup_directory: Where replaced and removed paths are backed up.
    """
    backup = Backup(backup_directory, plan.runtime)
    created = []
    for change in plan.changes:
        created += apply_change(change, backup)
    record_manifest(plan, manifest_file, created)
    prune_dirs(plan.prune)


def describe(plan: Plan, *, dry_run: bool) -> list[str]:
    """Describe a plan for the user.

    Args:
        plan: The plan.
        dry_run: Whether to phrase changes as what would happen.

    Returns:
        One line per change (with its diff) and per conflict, or an "up to date" line, then one per note.
    """
    prefix = f"[{plan.runtime}]"
    verb = "would " if dry_run else ""
    labels = {WRITE: "write", REMOVE: "remove", REMOVE_TREE: "remove directory"}
    lines = [
        f"{prefix} {verb}{labels[c.kind]} {c.path} ({c.reason})" + (f"\n{c.diff}" if c.diff else "")
        for c in plan.changes
    ]
    lines += [f"{prefix} conflict: {c.path}: {c.message}" for c in plan.conflicts]
    return (lines or [f"{prefix} up to date"]) + [f"{prefix} note: {n.path}: {n.message}" for n in plan.notes]


def file_problems(spec: InstallSpec, manifest: dict) -> list[str]:
    """Report owned files that are missing, differ from the rendering or are no longer part of the install.

    Args:
        spec: The spec as currently rendered.
        manifest: The runtime's manifest.

    Returns:
        One message per problem.
    """
    problems = []
    for path, text in spec.files.items():
        try:
            current = read_text(path)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if current is None:
            problems.append(f"missing {path}")
        elif current != text:
            problems.append(f"{path} differs from the rendered file")
    problems += [
        f"{name} is no longer part of the install"
        for name in manifest.get("files", {})
        if Path(name) not in spec.files and Path(name).exists()
    ]
    return problems


def change_problems(spec: InstallSpec) -> list[str]:
    """Report merges and edits that are not in place.

    Args:
        spec: The spec as currently rendered.

    Returns:
        One message per problem, including unreadable files.
    """
    problems = []
    for item in spec.json_merges:
        try:
            current = parse_object(item.path, read_text(item.path))
            present = item.merge(copy.deepcopy(current)) == current
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if not present:
            problems.append(f"{item.path} lacks the context-guard settings")
    for item in spec.text_edits:
        try:
            text = read_text(item.path) or ""
            present = item.apply(text) == text
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if not present:
            problems.append(f"{item.path} lacks the context-guard edit")
    return problems


def hook_problems(spec: InstallSpec, kit: Path, python: Path) -> list[str]:
    """Check that every reference to this runtime's hook entry point runs the kit copy with the kit python.

    Args:
        spec: The spec whose merged and edited files hold the hook commands.
        kit: The kit directory.
        python: The kit python.

    Returns:
        One message per file that runs the hook from elsewhere or with another interpreter.
    """
    marker = f"adapters/{spec.runtime}/hooks.py"
    problems = []
    for path in dict.fromkeys([m.path for m in spec.json_merges] + [e.path for e in spec.text_edits]):
        try:
            text = read_text(path) or ""
        except ValueError:
            continue
        starts = [match.start() for match in re.finditer(re.escape(marker), text)]
        if any(not text[:start].endswith(f"{kit}/") for start in starts):
            problems.append(f"{path} runs {marker} from outside {kit}")
        if starts and str(python) not in text:
            problems.append(f"{path} hook commands do not use {python}")
    return problems


def doctor(spec: InstallSpec, manifest: dict, kit: Path, python: Path) -> list[str]:
    """Re-render the spec and report drift between it and the installed home.

    Args:
        spec: The spec as currently rendered.
        manifest: The runtime's manifest.
        kit: The kit directory.
        python: The kit python.

    Returns:
        One message per problem; ["not installed"] without a manifest.
    """
    if not manifest:
        return ["not installed"]
    return file_problems(spec, manifest) + change_problems(spec) + hook_problems(spec, kit, python)
