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
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from core.config import kit_root
from core.install_spec import InstallSpec
from core.state import read_json, write_json

MANIFEST_NAME = "install-manifest.json"
BACKUPS_DIR = "backups"
DIFF_LINES = 40
WRITE, REMOVE, REMOVE_TREE = "write", "remove", "remove-tree"
SECTIONS = ("files", "merges", "edits")
DIGEST = re.compile(r"[0-9a-f]{64}")
FORCE_HINT = "rerun with --force to replace it (a backup is kept)"
STALE_KIT = "stale kit output"
EMPTY_TEXTS = ("", "{}")


@dataclass(frozen=True)
class Change:
    kind: str
    path: Path
    text: str | None = None
    reason: str = ""
    backup: bool = True
    diff: str = ""


@dataclass(frozen=True)
class Conflict:
    path: Path
    message: str


@dataclass
class Plan:
    runtime: str
    changes: list[Change] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    manifest: dict | None = None
    prune: list[str] = field(default_factory=list)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def manifest_path(home: Path, runtime: str) -> Path:
    return kit_root(home) / "state" / runtime / MANIFEST_NAME


def backup_dir(home: Path) -> Path:
    return kit_root(home) / BACKUPS_DIR / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def read_manifest(path: Path) -> dict:
    """Return a validated manifest; damaged entries are dropped so they read as unowned."""
    data = read_json(path)
    manifest = {key: data[key] for key in ("runtime", "installed_at", "version", "source", "commit")
                if isinstance(data.get(key), str)}
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    manifest["files"] = {k: v for k, v in files.items() if isinstance(v, str) and DIGEST.fullmatch(v)}
    for section in ("merges", "edits"):
        entries = data.get(section) if isinstance(data.get(section), dict) else {}
        manifest[section] = {k: v for k, v in entries.items()
                             if isinstance(v, dict) and isinstance(v.get("created"), bool)
                             and isinstance(v.get("original", ""), str)}
    dirs = data.get("dirs") if isinstance(data.get("dirs"), list) else []
    manifest["dirs"] = [item for item in dirs if isinstance(item, str)]
    return manifest if data else {}


def manifest_hash(manifest: dict) -> str:
    return digest(json.dumps(sorted(manifest.get("files", {}).items())))[:12]


def read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def parse_object(path: Path, text: str | None) -> dict:
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def dump_object(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def text_diff(path: Path, current: str, proposed: str) -> str:
    lines = list(difflib.unified_diff(current.splitlines(), proposed.splitlines(),
                                      f"{path} (current)", f"{path} (context-guard)", lineterm=""))
    if len(lines) > DIFF_LINES:
        lines = lines[:DIFF_LINES] + [f"... {len(lines) - DIFF_LINES} more diff lines"]
    return "\n".join(lines)


def stale_kit_output(text: str, signature: tuple[str, ...]) -> bool:
    lines = {line.strip() for line in text.splitlines()}
    return bool(signature) and all(line in lines for line in signature)


class Planner:
    """Accumulate one plan, chaining several edits of the same path through pending text."""

    def __init__(self, spec: InstallSpec, manifest: dict, force: bool):
        self.spec, self.manifest, self.force = spec, manifest, force
        self.plan = Plan(spec.runtime)
        self.pending: dict[Path, str | None] = {}
        self.recorded = {section: {} for section in SECTIONS}

    def read(self, path: Path) -> str | None:
        return self.pending[path] if path in self.pending else read_text(path)

    def change(self, kind: str, path: Path, text: str | None = None, reason: str = "",
               backup: bool = True, diff: str = "") -> None:
        """Queue a change; unmodified owned files skip the backup because they are re-renderable."""
        previous = [c for c in self.plan.changes if c.path == path]
        if previous:
            self.plan.changes.remove(previous[0])
            reason = f"{previous[0].reason}+{reason}"
            backup = backup or previous[0].backup
            diff = "\n".join(part for part in (previous[0].diff, diff) if part)
        self.pending[path] = text
        self.plan.changes.append(Change(kind, path, text, reason, backup, diff))

    def conflict(self, path: Path, message: str, section: str | None = None) -> None:
        self.plan.conflicts.append(Conflict(path, message))
        kept = self.manifest.get(section, {}).get(str(path)) if section else None
        if kept is not None:
            self.recorded[section][str(path)] = kept

    def finish(self, extra: dict | None) -> Plan:
        manifest = {"runtime": self.spec.runtime, **(extra or {}), **self.recorded,
                    "dirs": list(self.manifest.get("dirs", []))}
        unchanged = {k: v for k, v in self.manifest.items() if k != "installed_at"} == manifest
        manifest["installed_at"] = (self.manifest["installed_at"] if unchanged and "installed_at" in self.manifest
                                    else dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
        self.plan.manifest = manifest
        return self.plan


class InstallPlanner(Planner):
    def __init__(self, spec: InstallSpec, manifest: dict, force: bool):
        super().__init__(spec, manifest, force)
        self.known = manifest.get("files", {})

    def owned(self, path: Path, current: str) -> bool:
        return self.known.get(str(path)) == digest(current)

    def stale_kit(self, path: Path, current: str) -> bool:
        return stale_kit_output(current, self.spec.kit_signatures.get(path, ()))

    def files(self) -> None:
        for path, text in self.spec.files.items():
            try:
                current = self.read(path)
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
                self.conflict(path, f"edited or not installed by context-guard; {FORCE_HINT}\n"
                                    + text_diff(path, current, text), "files")
                continue
            self.recorded["files"][str(path)] = digest(text)

    def stale_files(self) -> None:
        for name, recorded in self.manifest.get("files", {}).items():
            path = Path(name)
            if path in self.spec.files:
                continue
            try:
                current = self.read(path)
            except ValueError as exc:
                self.conflict(path, str(exc), "files")
                continue
            if current is not None and digest(current) == recorded:
                self.change(REMOVE, path, reason="stale", backup=False)
            elif current is not None and self.force:
                self.change(REMOVE, path, reason="stale (forced)")
            elif current is not None:
                self.conflict(path, f"no longer installed but edited; remove it yourself or {FORCE_HINT}",
                              "files")

    def merges(self) -> None:
        for item in self.spec.json_merges:
            try:
                text = self.read(item.path)
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
            self.record("merges", item.path, new, text is None, text if new != text else None)

    def edits(self) -> None:
        for item in self.spec.text_edits:
            try:
                text = self.read(item.path)
                new = item.apply(text or "")
            except ValueError as exc:
                self.conflict(item.path, str(exc), "edits")
                continue
            if text is None and not new:
                continue
            if new != text:
                self.change(WRITE, item.path, new, "edit")
            self.record("edits", item.path, new, text is None)

    def record(self, section: str, path: Path, text: str, created: bool, original: str | None = None) -> None:
        previous = self.manifest.get(section, {}).get(str(path), {})
        entry = {"sha": digest(text), "created": previous.get("created", created)}
        original = previous.get("original", original)
        self.recorded[section][str(path)] = entry | ({"original": original} if original is not None else {})


def plan(spec: InstallSpec, manifest: dict, *, force: bool = False, extra: dict | None = None) -> Plan:
    planner = InstallPlanner(spec, manifest, force)
    planner.files()
    planner.stale_files()
    planner.merges()
    planner.edits()
    return planner.finish(extra)


class UninstallPlanner(Planner):
    def files(self) -> None:
        for name, recorded in self.manifest.get("files", {}).items():
            path = Path(name)
            try:
                current = self.read(path)
            except ValueError as exc:
                self.conflict(path, str(exc), "files")
                continue
            if current is not None and digest(current) == recorded:
                self.change(REMOVE, path, reason="file", backup=False)
            elif current is not None and self.force:
                self.change(REMOVE, path, reason="file (forced)")
            elif current is not None:
                self.conflict(path, "edited since install; kept (rerun with --force to remove it)", "files")

    def reverse(self, section: str, targets: dict, restore) -> None:
        for name, entry in self.manifest.get(section, {}).items():
            path = Path(name)
            if path not in targets:
                self.conflict(path, "the current adapter no longer knows how to remove its change; "
                                    "edit it by hand", section)
                continue
            try:
                text = self.read(path)
                restored = None if text is None else restore(targets[path], path, text, entry)
            except ValueError as exc:
                self.conflict(path, str(exc), section)
                continue
            if restored is None:
                continue
            if entry["created"] and restored.strip() in EMPTY_TEXTS:
                self.change(REMOVE, path, reason=section)
            elif restored != text:
                self.change(WRITE, path, restored, section)


def original_object(path: Path, original: object) -> dict | None:
    try:
        return parse_object(path, original) if isinstance(original, str) else None
    except ValueError:
        return None


def unmerged(item, path: Path, text: str, entry: dict) -> str:
    """The recorded original text comes back byte-identical when it parses to the unmerged object."""
    current = parse_object(path, text)
    restored = item.unmerge(copy.deepcopy(current))
    if restored == current:
        return text
    if original_object(path, entry.get("original")) == restored:
        return entry["original"]
    return dump_object(restored)


def reverted(item, path: Path, text: str, entry: dict) -> str:
    return item.revert(text)


def plan_uninstall(spec: InstallSpec, manifest: dict, *, force: bool = False) -> Plan:
    """Reverse a manifest; conflicted entries stay in a residual manifest for a later --force."""
    planner = UninstallPlanner(spec, manifest, force)
    planner.files()
    planner.reverse("merges", {m.path: m for m in spec.json_merges}, unmerged)
    planner.reverse("edits", {e.path: e for e in spec.text_edits}, reverted)
    result = planner.finish({key: manifest[key] for key in ("version",) if key in manifest})
    if not any(result.manifest[section] for section in SECTIONS):
        result.manifest = None
    result.prune = list(manifest.get("dirs", []))
    return result


class Backup:
    """Copy each path before it is replaced or removed; the directory is created lazily."""

    def __init__(self, directory: Path, runtime: str):
        self.directory, self.runtime, self.count = directory, runtime, 0

    def save(self, path: Path) -> None:
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
    missing = []
    for parent in path.parents:
        if parent.exists():
            break
        missing.append(str(parent))
    return missing


def apply_change(change: Change, backup: Backup) -> list[str]:
    """Apply one change and return the directories it created."""
    if change.backup:
        backup.save(change.path)
    if change.kind == WRITE:
        created = missing_parents(change.path)
        change.path.parent.mkdir(parents=True, exist_ok=True)
        change.path.write_text(change.text)
        return created
    if change.kind == REMOVE_TREE:
        shutil.rmtree(change.path)
    else:
        change.path.unlink(missing_ok=True)
    return []


def prune_dirs(dirs: list[str]) -> None:
    for name in sorted(set(dirs), key=lambda item: len(Path(item).parts), reverse=True):
        try:
            Path(name).rmdir()
        except OSError:
            continue


def record_manifest(plan: Plan, manifest_file: Path, created: list[str]) -> None:
    if plan.manifest is None:
        manifest_file.unlink(missing_ok=True)
        return
    manifest = dict(plan.manifest)
    manifest["dirs"] = sorted(set(manifest["dirs"]) | set(created) | set(missing_parents(manifest_file)))
    if read_json(manifest_file) != manifest:
        write_json(manifest_file, manifest)


def apply(plan: Plan, manifest_file: Path, backup_directory: Path) -> None:
    """Apply a plan (never call this for dry runs); conflicts were already left out of it."""
    backup = Backup(backup_directory, plan.runtime)
    created = []
    for change in plan.changes:
        created += apply_change(change, backup)
    record_manifest(plan, manifest_file, created)
    prune_dirs(plan.prune)


def describe(plan: Plan, dry_run: bool) -> list[str]:
    prefix = f"[{plan.runtime}]"
    verb = "would " if dry_run else ""
    labels = {WRITE: "write", REMOVE: "remove", REMOVE_TREE: "remove directory"}
    lines = [f"{prefix} {verb}{labels[c.kind]} {c.path} ({c.reason})" + (f"\n{c.diff}" if c.diff else "")
             for c in plan.changes]
    lines += [f"{prefix} conflict: {c.path}: {c.message}" for c in plan.conflicts]
    return lines or [f"{prefix} up to date"]


def file_problems(spec: InstallSpec, manifest: dict) -> list[str]:
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
    problems += [f"{name} is no longer part of the install" for name in manifest.get("files", {})
                 if Path(name) not in spec.files and Path(name).exists()]
    return problems


def change_problems(spec: InstallSpec) -> list[str]:
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
    """Every reference to this runtime's hook entry point must run the kit copy with the kit python."""
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
    """Re-render the spec and report drift between it and the installed home."""
    if not manifest:
        return ["not installed"]
    return file_problems(spec, manifest) + change_problems(spec) + hook_problems(spec, kit, python)
