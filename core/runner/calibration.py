"""Runtime-independent calibration, with explicit configuration and model callbacks."""

import collections
import hashlib
import json
import math
import os
import random
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from core.config import ConfigLoader
from core.runner import intent_labels as labels, prefilter
from core.state import file_lock, read_json, write_json

COMMANDS = ("apply", "auto", "keep")

DECISIONS_FILE = "decisions.jsonl"

PENDING, APPLIED = "pending", "applied"

COUNT_KEYS = ("TP", "FP", "TN", "FN")

TUNED_LISTS = ("keywords", "short_keywords")

TUNED_NUMBERS = (("short_max_words", (6, 8, 10, 12)), ("max_chars", (150, 200, 250, 300)))

TUNED_KEYS = (*TUNED_LISTS, *(key for key, _ in TUNED_NUMBERS))

LOCK_STALE_SECONDS = 2 * 3600

BATCH_TIMEOUT_SECONDS = 180

SAMPLE_SEED = 1

MIN_WORD_PROMPTS = 2

EPSILON = 1e-9

WORD = re.compile(r"[a-z][a-z0-9_-]+")

LABEL_LINE = re.compile(r"^\W*(\d+)\W+(MECHANICAL|NEW_TASK|CONTINUE)\b", re.MULTILINE)

STOPWORD_TEXT = (
    "about after all also am an and any are as at be been but by can could did do does for from get got had has "
    "have he her here his how if in into is it its just let me more my no not now of off on one or our out she "
    "so some that the their them then there these they this to too up us very was we were what when where which "
    "who why will with would yes you your"
)

STOPWORDS = frozenset(STOPWORD_TEXT.split())

type AskModel = Callable[[str, str, float], str | None]


def count_lines(path: Path) -> int:
    """Count the newline-terminated lines of a file.

    Args:
        path: The file to count.

    Returns:
        The number of newlines, or 0 when the file cannot be read.
    """
    try:
        with path.open("rb") as handle:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))
    except OSError:
        return 0


def pid_alive(pid: int) -> bool:
    """Return whether a process id is alive.

    Args:
        pid: The process id.

    Returns:
        True when the process exists, including one this user may not signal.
    """
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError, OverflowError:
        return False
    return True


def batch_prompt(batch: list[dict]) -> str:
    """Build the classifier prompt for a numbered batch of logged prompts.

    Args:
        batch: Calibration items; their `text` is listed from 1.

    Returns:
        The prompt asking for one `N: LABEL` line per message.
    """
    numbered = "\n".join(f"{number}: {json.dumps(item['text'])}" for number, item in enumerate(batch, 1))
    return (
        f"Classify each numbered user message, sent into a long coding-agent chat, as {labels.CATEGORIES}. "
        "Reply with one line per message formatted `N: LABEL` and nothing else.\n\n" + numbered
    )


def parse_labels(output: str, size: int) -> dict[int, str]:
    """Parse `N: LABEL` lines from a batch reply.

    Args:
        output: The model's reply.
        size: The batch size; numbers outside 1..size are ignored.

    Returns:
        Zero-based batch indexes mapped to their label; a repeated number keeps the last label.
    """
    found = {}
    for number, label in LABEL_LINE.findall(output):
        index = int(number) - 1
        if 0 <= index < size:
            found[index] = label
    return found


def passes(item: dict, classifier: dict) -> bool:
    """Return whether a logged prompt passes the pre-filter, judging length by the recorded prompt length.

    The logged text is truncated, so the length check uses the item's `chars`.

    Args:
        item: A calibration item with `text` and `chars`.
        classifier: The classifier config to test.

    Returns:
        True when the item passes the pre-filter under `classifier`.
    """
    if item["chars"] > classifier["max_chars"]:
        return False
    return prefilter.passes(item["text"], {**classifier, "max_chars": item["chars"]})


def confusion(classifier: dict, items: list[dict]) -> dict[str, float]:
    """Return the weighted confusion counts of a classifier config, with MECHANICAL as the positive class.

    Args:
        classifier: The classifier config to test.
        items: Labelled calibration items with a `weight`.

    Returns:
        Weighted `TP`, `FP`, `TN` and `FN` counts.
    """
    counts = dict.fromkeys(COUNT_KEYS, 0.0)
    for item in items:
        hit = passes(item, classifier)
        counts[("T" if hit == (item["label"] == "MECHANICAL") else "F") + ("P" if hit else "N")] += item["weight"]
    return counts


def score(counts: dict[str, float], ratio: float) -> float:
    """Score confusion counts, where one true positive is worth `ratio` false positives.

    Args:
        counts: Confusion counts.
        ratio: The accepted false positives per true positive.

    Returns:
        `ratio * TP - FP`.
    """
    return ratio * counts["TP"] - counts["FP"]


def improves(old: dict[str, float], new: dict[str, float], ratio: float) -> bool:
    """Return whether new confusion counts improve on old ones at the accepted false-positive ratio.

    Args:
        old: The current counts.
        new: The candidate counts.
        ratio: The accepted false positives per true positive.

    Returns:
        True when gained positives cost at most `ratio` false positives each, lost positives save
        more than `ratio` each, or positives are equal and false positives drop.
    """
    gained_tp = new["TP"] - old["TP"]
    extra_fp = new["FP"] - old["FP"]
    if gained_tp > EPSILON:
        return extra_fp <= ratio * gained_tp + EPSILON
    if gained_tp < -EPSILON:
        return -extra_fp > -ratio * gained_tp + EPSILON
    return extra_fp < -EPSILON


def in_holdout(key: str) -> bool:
    """Return whether a prompt belongs to the stable holdout half.

    Args:
        key: The normalised prompt.

    Returns:
        True for prompts whose SHA-256 digest starts with an odd byte.
    """
    return hashlib.sha256(key.encode()).digest()[0] % 2 == 1


def candidate_words(classifier: dict, items: list[dict]) -> list[str]:
    """Return keyword candidates from mechanical prompts the classifier config misses.

    Args:
        classifier: The current classifier config.
        items: Labelled calibration items.

    Returns:
        Escaped non-stopwords found in at least `MIN_WORD_PROMPTS` missed prompts and not already
        configured, most frequent first, then alphabetical.
    """
    counts: dict[str, int] = collections.Counter()
    for item in items:
        if item["label"] == "MECHANICAL" and not passes(item, classifier):
            counts.update(word for word in set(WORD.findall(item["text"].lower())) if word not in STOPWORDS)
    present = {*classifier["keywords"], *classifier["short_keywords"]}
    ranked = sorted(
        (word for word, count in counts.items() if count >= MIN_WORD_PROMPTS), key=lambda word: (-counts[word], word)
    )
    return [re.escape(word) for word in ranked if re.escape(word) not in present]


def adopt(
    best: dict, counts: dict[str, float], trial: dict, items: list[dict], ratio: float
) -> tuple[dict, dict[str, float]]:
    """Keep a trial classifier config when it improves on the current one.

    Args:
        best: The current classifier config.
        counts: The confusion counts of `best`.
        trial: The candidate classifier config.
        items: Labelled calibration items.
        ratio: The accepted false positives per true positive.

    Returns:
        `trial` and its counts when it improves, otherwise `best` and `counts`.
    """
    trial_counts = confusion(trial, items)
    if improves(counts, trial_counts, ratio):
        return trial, trial_counts
    return best, counts


def tune(classifier: dict, items: list[dict], ratio: float) -> dict:
    """Greedily tune the pre-filter: add candidate words, drop fragments, then retune the numeric limits.

    Args:
        classifier: The current classifier config.
        items: Labelled training items.
        ratio: The accepted false positives per true positive.

    Returns:
        The tuned classifier config; `classifier` itself when nothing improves.
    """
    best, counts = classifier, confusion(classifier, items)
    for word in candidate_words(classifier, items):
        for key in TUNED_LISTS:
            trial = {**best, key: [*best[key], word]}
            best, counts = adopt(best, counts, trial, items, ratio)
            if best is trial:
                break
    for key in TUNED_LISTS:
        for fragment in classifier[key]:
            trial = {**best, key: [kept for kept in best[key] if kept != fragment]}
            best, counts = adopt(best, counts, trial, items, ratio)
    for key, values in TUNED_NUMBERS:
        for value in values:
            if value != best[key]:
                best, counts = adopt(best, counts, {**best, key: value}, items, ratio)
    return best


def describe(classifier: dict, changes: dict) -> list[str]:
    """Describe tuned changes as report lines.

    Args:
        classifier: The current classifier config.
        changes: The tuned values that differ from it.

    Returns:
        One line per added or removed list fragment group and per changed number.
    """
    lines = []
    for key in TUNED_LISTS:
        if key in changes:
            added = [fragment for fragment in changes[key] if fragment not in classifier[key]]
            removed = [fragment for fragment in classifier[key] if fragment not in changes[key]]
            lines += [f"added {key}: {', '.join(added)}"] if added else []
            lines += [f"removed {key}: {', '.join(removed)}"] if removed else []
    for key, _ in TUNED_NUMBERS:
        if key in changes:
            lines.append(f"{key}: {classifier[key]} -> {changes[key]}")
    return lines


def rounded(counts: dict[str, float]) -> dict[str, float]:
    """Round confusion counts to one decimal.

    Args:
        counts: Confusion counts.

    Returns:
        The counts in `COUNT_KEYS` order, rounded.
    """
    return {key: round(counts[key], 1) for key in COUNT_KEYS}


def timestamp(record: dict) -> float:
    """Read a record's `ts` field.

    Args:
        record: A stored JSON record.

    Returns:
        The finite numeric `ts`, else 0.0.
    """
    value = record.get("ts", 0)
    if type(value) not in (int, float):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


class Calibration:
    """Own one runtime's calibration paths, configuration and classifier callback.

    Args:
        loader: The runtime's configuration loader; calibration may update its user config.
        state_dir: The runtime's state directory holding the decision log and `calibration/`.
        ask_model: Called as `ask_model(model, prompt, timeout)`; returns the reply text or None.
        cache: The runtime's label cache.
    """

    def __init__(self, loader: ConfigLoader, state_dir: Path, ask_model: AskModel, cache: labels.LabelCache) -> None:
        self.loader = loader
        self.state = Path(state_dir)
        self.ask_model = ask_model
        self.cache = cache
        self.dry_run = False
        self.preview = {}

    def write_json(self, path: Path, data: dict) -> None:
        """Write a JSON file unless this is a dry run.

        Args:
            path: The file to write.
            data: The JSON object.
        """
        if not self.dry_run:
            write_json(path, data)

    def calib_dir(self) -> Path:
        """Return the calibration directory.

        Returns:
            `calibration/` inside the state directory.
        """
        return self.state / "calibration"

    def log_path(self) -> Path:
        """Return the decision log.

        Returns:
            `DECISIONS_FILE` inside the state directory.
        """
        return self.state / DECISIONS_FILE

    def lock_path(self) -> Path:
        """Return the run lock record.

        Returns:
            The file holding the running calibration's pid and start time.
        """
        return self.calib_dir() / "lock"

    def proposal_path(self) -> Path:
        """Return the latest proposal record.

        Returns:
            `proposal.json` inside the calibration directory.
        """
        return self.calib_dir() / "proposal.json"

    def settings(self) -> dict:
        """Return the current `calibration` config section.

        Returns:
            The section from a fresh configuration load.
        """
        return self.loader.load()["calibration"]

    def is_due(self) -> bool:
        """Return whether a calibration run is due.

        Returns:
            True when calibration is enabled, the last run is at least `interval_days` old and the
            decision log holds at least `first_run_prompts` lines.
        """
        conf = self.settings()
        if not conf["enabled"]:
            return False
        last = read_json(self.calib_dir() / "last_run.json")
        if last and time.time() - timestamp(last) < conf["interval_days"] * 86400:
            return False
        return count_lines(self.log_path()) >= conf["first_run_prompts"]

    def locked(self) -> bool:
        """Return whether another calibration run holds the lock.

        Returns:
            True when the lock names a live process and is younger than `LOCK_STALE_SECONDS`.
        """
        lock = read_json(self.lock_path())
        pid = lock.get("pid")
        fresh = time.time() - timestamp(lock) < LOCK_STALE_SECONDS
        return type(pid) is int and pid > 0 and fresh and pid_alive(pid)

    def acquire_lock(self) -> bool:
        """Take the run lock for this process.

        Returns:
            True when taken; False when another run holds it.
        """
        with file_lock(self.calib_dir() / "lock.mutex"):
            if self.locked():
                return False
            write_json(self.lock_path(), {"pid": os.getpid(), "ts": time.time()})
            return True

    def maybe_start(self, command: list[str], env: dict[str, str] | None = None) -> None:
        """Spawn a detached calibration run when one is due and none is running; returns in milliseconds.

        Args:
            command: The fixed worker argv that runs `--calibrate`.
            env: The run's environment, or None to inherit this process's.
        """
        try:
            if not self.is_due() or self.locked():
                return
            self.calib_dir().mkdir(parents=True, exist_ok=True)
            with (self.calib_dir() / "run.log").open("a") as log:
                subprocess.Popen(  # noqa: S603  # fixed worker argv from the adapter, never model output
                    command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, env=env
                )
        except OSError:
            return

    def read_window(self, size: int) -> list[dict]:
        """Read the newest decision log records.

        Args:
            size: The number of log lines to read from the end.

        Returns:
            The valid records with a string `prompt`, oldest first; [] when the log cannot be read.
        """
        try:
            with self.log_path().open(errors="replace") as handle:
                lines = collections.deque(handle, maxlen=size)
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("prompt"), str):
                records.append(record)
        return records

    def collect(self, records: list[dict], classifier: dict) -> list[dict]:
        """Turn decision records into one calibration item per normalised prompt.

        Args:
            records: Decision log records.
            classifier: The current classifier config.

        Returns:
            Items with `key`, `text`, `chars`, `label` (from the log, else the cache, else None),
            `weight` 1.0 and whether they `passes` the current pre-filter.
        """
        cache = self.cache.load()
        items: dict[str, dict] = {}
        for record in records:
            key = labels.normalise(record["prompt"])
            if not key:
                continue
            chars = record.get("chars")
            item = items.setdefault(
                key,
                {
                    "key": key,
                    "text": record["prompt"],
                    "chars": chars if type(chars) is int else len(record["prompt"]),
                    "label": None,
                    "weight": 1.0,
                },
            )
            if record.get("label") in labels.LABELS:
                item["label"] = record["label"]
        for item in items.values():
            if not item["label"] and cache.get(item["key"]) in labels.LABELS:
                item["label"] = cache[item["key"]]
            item["passes"] = passes(item, classifier)
        return list(items.values())

    def label_missing(self, items: list[dict], model: str, batch_size: int) -> None:
        """Label unlabelled items in batches with the model, and cache the labels it returns.

        Args:
            items: Calibration items; labels are set in place.
            model: The labelling model.
            batch_size: The number of prompts per model call.
        """
        missing = [item for item in items if not item["label"]]
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            found = parse_labels(self.ask_model(model, batch_prompt(batch), BATCH_TIMEOUT_SECONDS) or "", len(batch))
            for index, label in found.items():
                batch[index]["label"] = label
            if found:
                self.cache.remember({batch[index]["text"]: label for index, label in found.items()})

    def write_report(self, status: str, stats: dict, change_lines: list[str], tables: dict) -> None:
        """Write the Markdown run report unless this is a dry run.

        Args:
            status: The run status.
            stats: The window statistics from `gather`.
            change_lines: The described changes.
            tables: Confusion counts by set name; empty for none.
        """
        lines = [
            f"# context-guard calibration {time.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"Status: {status}",
            "",
            f"- unique prompts in window: {stats['prompts']}, passing the current filter: {stats['passing']}",
            f"- rejected: {stats['rejected']}, labelled sample: {stats['sampled']} (weight {stats['weight']:.2f} each)",
            f"- labelled MECHANICAL: {stats['mechanical']}, unlabelled (skipped): {stats['unlabelled']}",
        ]
        if tables:
            lines += ["", "| set | TP | FP | TN | FN |", "| --- | --- | --- | --- | --- |"]
            lines += [
                f"| {name} | " + " | ".join(f"{c[k]:.0f}" for k in COUNT_KEYS) + " |" for name, c in tables.items()
            ]
        if change_lines:
            lines += ["", *(f"- {line}" for line in change_lines)]
        if self.dry_run:
            return
        (self.calib_dir() / "report.md").write_text("\n".join(lines) + "\n")

    def gather(self, config: dict) -> tuple[list[dict], dict]:
        """Collect the labelled items: every passing prompt plus a seeded sample of rejected ones.

        Missing labels are requested from the model except in a dry run. Each labelled rejected
        item is weighted to stand for all rejected prompts.

        Args:
            config: The merged configuration.

        Returns:
            The labelled items and the window statistics for the report.
        """
        conf, classifier = config["calibration"], config["classifier"]
        items = self.collect(self.read_window(conf["window"]), classifier)
        passing = [item for item in items if item["passes"]]
        rejected = [item for item in items if not item["passes"]]
        sampled = random.Random(SAMPLE_SEED).sample(  # noqa: S311  # reproducible sample, not a secret
            rejected, min(conf["sample_rejected"], len(rejected))
        )
        if not self.dry_run and any(not item["label"] for item in passing + sampled):
            self.label_missing(passing + sampled, conf["model"] or classifier["model"], conf["batch_size"])
        labelled_sample = [item for item in sampled if item["label"]]
        weight = len(rejected) / len(labelled_sample) if labelled_sample else 0.0
        for item in labelled_sample:
            item["weight"] = weight
        labelled = [item for item in passing if item["label"]] + labelled_sample
        stats = {
            "prompts": len(items),
            "passing": len(passing),
            "rejected": len(rejected),
            "sampled": len(labelled_sample),
            "weight": weight,
            "mechanical": sum(item["label"] == "MECHANICAL" for item in labelled),
            "unlabelled": len(passing) + len(sampled) - len(labelled),
        }
        return labelled, stats

    def calibrate(self) -> str:
        """Tune the pre-filter on the training half, check it on the holdout half, and propose or apply it.

        Returns:
            `not enough data`, `no change`, `holdout rejected`, `proposed` or `applied`.
        """
        config = self.loader.load()
        conf, classifier = config["calibration"], config["classifier"]
        if not self.dry_run:
            self.proposal_path().unlink(missing_ok=True)
        labelled, stats = self.gather(config)
        self.preview = {"stats": stats, "changes": {}}
        if stats["mechanical"] < conf["min_mechanical"]:
            self.write_report("not enough data", stats, [], {})
            return "not enough data"
        ratio = conf["max_fp_per_tp"]
        holdout = [item for item in labelled if in_holdout(item["key"])]
        tuned = tune(classifier, [item for item in labelled if not in_holdout(item["key"])], ratio)
        changes = {key: tuned[key] for key in TUNED_KEYS if tuned[key] != classifier[key]}
        tables = {
            "before": confusion(classifier, labelled),
            "after": confusion(tuned, labelled),
            "holdout before": confusion(classifier, holdout),
            "holdout after": confusion(tuned, holdout),
        }
        self.preview.update(changes=changes, tables=tables)
        change_lines = describe(classifier, changes)
        if not changes:
            status = "no change"
        elif score(tables["holdout after"], ratio) < score(tables["holdout before"], ratio) - EPSILON:
            status = "holdout rejected"
        else:
            status = self.propose(changes, change_lines, tables, auto=conf["mode"] == "auto")
        self.write_report(status, stats, change_lines, tables)
        return status

    def propose(self, changes: dict, change_lines: list[str], tables: dict, *, auto: bool) -> str:
        """Record a pending proposal, or apply it to the user config in auto mode.

        Args:
            changes: The tuned classifier values.
            change_lines: The described changes.
            tables: Confusion counts for `before`, `after`, `holdout before` and `holdout after`.
            auto: Whether to apply the changes now; a dry run never applies them.

        Returns:
            `applied` in auto mode, else `proposed`.
        """
        before, after = tables["before"], tables["after"]
        effect = (
            f"{after['TP'] - before['TP']:+.0f} chores caught, "
            f"{after['FP'] - before['FP']:+.0f} extra classifier calls on the last {sum(before.values()):.0f} prompts"
        )
        proposal = {
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": PENDING,
            "noted": False,
            "summary": "; ".join([*change_lines, effect]),
            "changes": changes,
            "before": rounded(before),
            "after": rounded(after),
            "holdout": {"before": rounded(tables["holdout before"]), "after": rounded(tables["holdout after"])},
        }
        if auto and not self.dry_run:
            self.loader.update_user_config({"classifier": changes})
            proposal["status"] = APPLIED
        self.write_json(self.proposal_path(), proposal)
        return "applied" if auto else "proposed"

    def run(self, *, dry_run: bool = False) -> int:
        """Run one calibration under the lock, recording its status; a dry run prints a preview and writes nothing.

        Args:
            dry_run: Print the would-be result as JSON instead of labelling, writing or applying.

        Returns:
            0 on completion; 1 when another run holds the lock or the run failed.
        """
        self.dry_run = dry_run
        if dry_run:
            status = self.calibrate()
            print(json.dumps({"status": status, **self.preview}, indent=2))
            return 0
        if not self.acquire_lock():
            print("calibration already running")
            return 1
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} calibration started", flush=True)
        try:
            try:
                status = self.calibrate()
            except (OSError, ValueError, TypeError, KeyError) as exc:
                status = f"error: {exc!r}"
            self.write_json(self.calib_dir() / "last_run.json", {"ts": time.time(), "status": status})
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} calibration {status}", flush=True)
        finally:
            self.lock_path().unlink(missing_ok=True)
        return 1 if status.startswith("error") else 0

    def awaiting_reply(self) -> bool:
        """Return whether a `calibration apply|auto|keep` reply is handled instead of going through.

        Returns:
            True while calibration is enabled and the latest proposal is pending or was just auto-applied.
        """
        if not self.settings()["enabled"]:
            return False
        return read_json(self.proposal_path()).get("status") in (PENDING, APPLIED)

    def session_note(self) -> str:
        """Return the one-time session note about the latest proposal, under the proposal lock.

        Returns:
            The note, or "" when there is none or it cannot be read.
        """
        if not self.proposal_path().exists():
            return ""
        try:
            with file_lock(self.calib_dir() / "proposal.lock"):
                return self._session_note()
        except OSError:
            return ""

    def _session_note(self) -> str:
        """Return a one-time note about a new proposal or an auto-applied update, and mark it noted.

        Returns:
            The note for the session context; "" when there is no unnoted proposal or calibration is off.
        """
        try:
            proposal = read_json(self.proposal_path())
            if not proposal or proposal.get("noted") or not self.settings()["enabled"]:
                return ""
            proposal["noted"] = True
            self.write_json(self.proposal_path(), proposal)
        except OSError:
            return ""
        details = f"{proposal.get('summary', '')}; report: {self.calib_dir() / 'report.md'}"
        if proposal.get("status") == APPLIED:
            return (
                f"context-guard calibration applied a pre-filter update ({details}). Tell the user in one line; "
                "they can reply 'calibration keep' to get future updates as proposals instead."
            )
        return (
            f"context-guard calibration proposes a pre-filter update ({details}). Tell the user in one line that "
            "they can reply 'calibration apply' (apply it), 'calibration auto' (apply it and future updates "
            "automatically) or 'calibration keep' (keep the current filter)."
        )

    def command(self, word: str) -> str:
        """Handle the user's `calibration apply|auto|keep` reply and retire the proposal.

        Args:
            word: `apply` applies a pending proposal, `auto` also switches to automatic updates,
                `keep` discards it and switches to proposals.

        Returns:
            The message shown to the user; config errors leave everything unchanged.
        """
        if word not in COMMANDS:
            return f"Unknown calibration command {word!r}; use apply, auto or keep."
        proposal = read_json(self.proposal_path())
        pending = proposal if proposal.get("status") == PENDING else {}
        updates: dict = {}
        if pending and word != "keep":
            updates["classifier"] = pending["changes"]
        if word != "apply":
            updates["calibration"] = {"mode": "auto" if word == "auto" else "propose"}
        try:
            if updates:
                self.loader.update_user_config(updates)
            self.proposal_path().unlink(missing_ok=True)
        except (OSError, ValueError, TypeError) as exc:
            return f"Calibration unchanged: {exc}"
        return self.message(word, pending.get("summary") if pending else None)

    def message(self, word: str, summary: str | None) -> str:
        """Build the reply message for a calibration command.

        Args:
            word: The handled command.
            summary: The retired pending proposal's summary, or None when none was pending.

        Returns:
            What changed and where the setting lives.
        """
        config_path = self.loader.user_config_path()
        if word == "apply" and summary:
            return f"Applied the calibration proposal to {config_path}: {summary}."
        if word == "apply":
            return "No pending calibration proposal; nothing changed."
        if word == "auto":
            head = f"Applied the calibration proposal: {summary}. " if summary else "No pending calibration proposal. "
            return head + f"Future calibrations apply automatically (calibration.mode=auto in {config_path})."
        head = "Discarded the calibration proposal. " if summary else "No pending calibration proposal. "
        return head + f"Future calibrations will be proposed first (calibration.mode=propose in {config_path})."
