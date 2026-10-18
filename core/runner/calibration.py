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
from pathlib import Path
from core.runner import intent_labels as labels, prefilter
from core.state import file_lock, read_json, write_json

COMMANDS = ("apply", "auto", "keep")

COUNT_KEYS = ("TP", "FP", "TN", "FN")

TUNED_LISTS = ("keywords", "short_keywords")

TUNED_NUMBERS = (("short_max_words", (6, 8, 10, 12)), ("max_chars", (150, 200, 250, 300)))

TUNED_KEYS = (*TUNED_LISTS, *(key for key, _ in TUNED_NUMBERS))

LOCK_STALE_SECONDS = 2 * 3600

BATCH_TIMEOUT_SECONDS = 180

SAMPLE_SEED = 1

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

def count_lines(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))
    except OSError:
        return 0

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True

def batch_prompt(batch: list[dict]) -> str:
    numbered = "\n".join(f"{number}: {json.dumps(item['text'])}" for number, item in enumerate(batch, 1))
    return (
        f"Classify each numbered user message, sent into a long coding-agent chat, as {labels.CATEGORIES}. "
        "Reply with one line per message formatted `N: LABEL` and nothing else.\n\n" + numbered
    )

def parse_labels(output: str, size: int) -> dict[int, str]:
    found = {}
    for number, label in LABEL_LINE.findall(output):
        index = int(number) - 1
        if 0 <= index < size:
            found[index] = label
    return found

def confusion(classifier: dict, items: list[dict]) -> dict[str, float]:
    counts = dict.fromkeys(COUNT_KEYS, 0.0)
    for item in items:
        hit = prefilter.passes(item["text"], classifier)
        counts[("T" if hit == (item["label"] == "MECHANICAL") else "F") + ("P" if hit else "N")] += item["weight"]
    return counts

def score(counts: dict[str, float], ratio: float) -> float:
    return ratio * counts["TP"] - counts["FP"]

def improves(old: dict[str, float], new: dict[str, float], ratio: float) -> bool:
    gained_tp = new["TP"] - old["TP"]
    extra_fp = new["FP"] - old["FP"]
    if gained_tp > EPSILON:
        return extra_fp <= ratio * gained_tp + EPSILON
    if gained_tp < -EPSILON:
        return -extra_fp > -ratio * gained_tp + EPSILON
    return extra_fp < -EPSILON

def in_holdout(key: str) -> bool:
    return hashlib.sha256(key.encode()).digest()[0] % 2 == 1

def candidate_words(classifier: dict, items: list[dict]) -> list[str]:
    counts: dict[str, int] = collections.Counter()
    for item in items:
        if item["label"] == "MECHANICAL" and not prefilter.passes(item["text"], classifier):
            counts.update(word for word in set(WORD.findall(item["text"].lower())) if word not in STOPWORDS)
    present = {*classifier["keywords"], *classifier["short_keywords"]}
    ranked = sorted((word for word, count in counts.items() if count >= 2), key=lambda word: (-counts[word], word))
    return [re.escape(word) for word in ranked if re.escape(word) not in present]

def tune(classifier: dict, items: list[dict], ratio: float) -> dict:
    best, counts = classifier, confusion(classifier, items)

    def attempt(trial: dict) -> bool:
        nonlocal best, counts
        trial_counts = confusion(trial, items)
        if not improves(counts, trial_counts, ratio):
            return False
        best, counts = trial, trial_counts
        return True

    for word in candidate_words(classifier, items):
        for key in TUNED_LISTS:
            if attempt({**best, key: [*best[key], word]}):
                break
    for key in TUNED_LISTS:
        for fragment in classifier[key]:
            attempt({**best, key: [kept for kept in best[key] if kept != fragment]})
    for key, values in TUNED_NUMBERS:
        for value in values:
            if value != best[key]:
                attempt({**best, key: value})
    return best

def describe(classifier: dict, changes: dict) -> list[str]:
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
    return {key: round(counts[key], 1) for key in COUNT_KEYS}

def timestamp(record: dict) -> float:
    value = record.get('ts', 0)
    if type(value) not in (int, float):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


class Calibration:
    """Own one runtime's calibration paths, configuration and classifier callback."""
    def __init__(self, loader, state_dir: Path, ask_model=None, cache=None):
        self.loader = loader
        self.state = Path(state_dir)
        self.ask_model = ask_model
        self.cache = cache or labels.LabelCache(self.state / "classifier_cache.json", loader.load()["classifier"]["cache_entries"])
        self.dry_run = False
        self.preview = {}

    def write_json(self, path, data):
        if not self.dry_run:
            write_json(path, data)


    def calib_dir(self) -> Path:
        return self.state / 'calibration'

    def log_path(self) -> Path:
        return self.state / 'decisions.jsonl'

    def lock_path(self) -> Path:
        return self.calib_dir() / 'lock'

    def proposal_path(self) -> Path:
        return self.calib_dir() / 'proposal.json'

    def settings(self) -> dict:
        return self.loader.load()['calibration']

    def is_due(self) -> bool:
        conf = self.settings()
        if not conf['enabled']:
            return False
        last = read_json(self.calib_dir() / 'last_run.json')
        if last and time.time() - timestamp(last) < conf['interval_days'] * 86400:
            return False
        return count_lines(self.log_path()) >= conf['first_run_prompts']

    def locked(self) -> bool:
        lock = read_json(self.lock_path())
        pid = lock.get('pid')
        fresh = time.time() - timestamp(lock) < LOCK_STALE_SECONDS
        return type(pid) is int and pid > 0 and fresh and pid_alive(pid)

    def acquire_lock(self) -> bool:
        with file_lock(self.calib_dir() / 'lock.mutex'):
            if self.locked():
                return False
            write_json(self.lock_path(), {'pid': os.getpid(), 'ts': time.time()})
            return True

    def maybe_start(self, command, env=None) -> None:
        """Spawn a detached `calibrate.py run` when a run is due and none is running; returns in milliseconds."""
        try:
            if not self.is_due() or self.locked():
                return
            self.calib_dir().mkdir(parents=True, exist_ok=True)
            with (self.calib_dir() / 'run.log').open('a') as log:
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, env=env)
        except OSError:
            return

    def read_window(self, size: int) -> list[dict]:
        try:
            with self.log_path().open(errors='replace') as handle:
                lines = collections.deque(handle, maxlen=size)
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get('prompt'), str):
                records.append(record)
        return records

    def collect(self, records: list[dict], classifier: dict) -> list[dict]:
        cache = self.cache.load()
        items: dict[str, dict] = {}
        for record in records:
            key = labels.normalise(record['prompt'])
            if not key:
                continue
            item = items.setdefault(key, {'key': key, 'text': record['prompt'], 'label': None, 'weight': 1.0})
            if record.get('label') in labels.LABELS:
                item['label'] = record['label']
        for item in items.values():
            if not item['label'] and cache.get(item['key']) in labels.LABELS:
                item['label'] = cache[item['key']]
            item['passes'] = prefilter.passes(item['text'], classifier)
        return list(items.values())

    def label_missing(self, items: list[dict], model: str, batch_size: int) -> None:
        missing = [item for item in items if not item['label']]
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            found = parse_labels(self.ask_model(model, batch_prompt(batch), BATCH_TIMEOUT_SECONDS) or '', len(batch))
            for index, label in found.items():
                batch[index]['label'] = label
            if found:
                self.cache.remember({batch[index]['text']: label for index, label in found.items()})

    def write_report(self, status: str, stats: dict, change_lines: list[str], tables: dict) -> None:
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
            lines += [f"| {name} | " + " | ".join(f"{c[k]:.0f}" for k in COUNT_KEYS) + " |" for name, c in tables.items()]
        if change_lines:
            lines += ["", *(f"- {line}" for line in change_lines)]
        if self.dry_run:
            return
        (self.calib_dir() / "report.md").write_text("\n".join(lines) + "\n")

    def gather(self, config: dict) -> tuple[list[dict], dict]:
        conf, classifier = (config['calibration'], config['classifier'])
        items = self.collect(self.read_window(conf['window']), classifier)
        passing = [item for item in items if item['passes']]
        rejected = [item for item in items if not item['passes']]
        sampled = random.Random(SAMPLE_SEED).sample(rejected, min(conf['sample_rejected'], len(rejected)))
        if any((not item['label'] for item in passing + sampled)):
            if not self.dry_run:
                if self.ask_model is None:
                    raise RuntimeError("classifier backend unavailable")
                self.label_missing(passing + sampled, conf['model'] or classifier['model'], conf['batch_size'])
        labelled_sample = [item for item in sampled if item['label']]
        weight = len(rejected) / len(labelled_sample) if labelled_sample else 0.0
        for item in labelled_sample:
            item['weight'] = weight
        labelled = [item for item in passing if item['label']] + labelled_sample
        stats = {'prompts': len(items), 'passing': len(passing), 'rejected': len(rejected), 'sampled': len(labelled_sample), 'weight': weight, 'mechanical': sum((item['label'] == 'MECHANICAL' for item in labelled)), 'unlabelled': len(passing) + len(sampled) - len(labelled)}
        return (labelled, stats)

    def calibrate(self) -> str:
        config = self.loader.load()
        conf, classifier = (config['calibration'], config['classifier'])
        if not self.dry_run:
            self.proposal_path().unlink(missing_ok=True)
        labelled, stats = self.gather(config)
        self.preview = {'stats': stats, 'changes': {}}
        if stats['mechanical'] < conf['min_mechanical']:
            self.write_report('not enough data', stats, [], {})
            return 'not enough data'
        ratio = conf['max_fp_per_tp']
        holdout = [item for item in labelled if in_holdout(item['key'])]
        tuned = tune(classifier, [item for item in labelled if not in_holdout(item['key'])], ratio)
        changes = {key: tuned[key] for key in TUNED_KEYS if tuned[key] != classifier[key]}
        tables = {'before': confusion(classifier, labelled), 'after': confusion(tuned, labelled), 'holdout before': confusion(classifier, holdout), 'holdout after': confusion(tuned, holdout)}
        self.preview.update(changes=changes, tables=tables)
        change_lines = describe(classifier, changes)
        if not changes:
            status = 'no change'
        elif score(tables['holdout after'], ratio) < score(tables['holdout before'], ratio) - EPSILON:
            status = 'holdout rejected'
        else:
            status = self.propose(changes, change_lines, tables, conf['mode'] == 'auto')
        self.write_report(status, stats, change_lines, tables)
        return status

    def propose(self, changes: dict, change_lines: list[str], tables: dict, auto: bool) -> str:
        before, after = tables["before"], tables["after"]
        effect = (
            f"{after['TP'] - before['TP']:+.0f} chores caught, "
            f"{after['FP'] - before['FP']:+.0f} extra classifier calls on the last {sum(before.values()):.0f} prompts"
        )
        proposal = {
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": "pending",
            "noted": False,
            "summary": "; ".join([*change_lines, effect]),
            "changes": changes,
            "before": rounded(before),
            "after": rounded(after),
            "holdout": {"before": rounded(tables["holdout before"]), "after": rounded(tables["holdout after"])},
        }
        if auto and not self.dry_run:
            self.loader.update_user_config({"classifier": changes})
            proposal["status"] = "applied"
        self.write_json(self.proposal_path(), proposal)
        return "applied" if auto else "proposed"

    def run(self, dry_run=False) -> int:
        self.dry_run = dry_run
        if dry_run:
            status = self.calibrate()
            print(json.dumps({'status': status, **self.preview}, indent=2))
            return 0
        if not self.acquire_lock():
            print('calibration already running')
            return 1
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} calibration started", flush=True)
        try:
            try:
                status = self.calibrate()
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                status = f'error: {exc!r}'
            self.write_json(self.calib_dir() / 'last_run.json', {'ts': time.time(), 'status': status})
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} calibration {status}", flush=True)
        finally:
            self.lock_path().unlink(missing_ok=True)
        return 1 if status.startswith('error') else 0

    def session_note(self) -> str:
        if not self.proposal_path().exists():
            return ''
        try:
            with file_lock(self.calib_dir() / 'proposal.lock'):
                return self._session_note()
        except OSError:
            return ''

    def _session_note(self) -> str:
        """One-time note about a new proposal or an auto-applied update, for the session context; '' if none."""
        try:
            proposal = read_json(self.proposal_path())
            if not proposal or proposal.get('noted') or (not self.settings()['enabled']):
                return ''
            proposal['noted'] = True
            self.write_json(self.proposal_path(), proposal)
        except OSError:
            return ''
        details = f"{proposal.get('summary', '')}; report: {self.calib_dir() / 'report.md'}"
        if proposal.get('status') == 'applied':
            return f"context-guard calibration applied a pre-filter update ({details}). Tell the user in one line; they can reply 'calibration keep' to get future updates as proposals instead."
        return f"context-guard calibration proposes a pre-filter update ({details}). Tell the user in one line that they can reply 'calibration apply' (apply it), 'calibration auto' (apply it and future updates automatically) or 'calibration keep' (keep the current filter)."

    def command(self, word: str) -> str:
        """Handle the user's 'calibration apply|auto|keep' reply; returns the message to show."""
        if word not in COMMANDS:
            return f'Unknown calibration command {word!r}; use apply, auto or keep.'
        proposal = read_json(self.proposal_path())
        pending = proposal if proposal.get('status') == 'pending' else {}
        updates: dict = {}
        if pending and word != 'keep':
            updates['classifier'] = pending['changes']
        if word != 'apply':
            updates['calibration'] = {'mode': 'auto' if word == 'auto' else 'propose'}
        try:
            if updates:
                self.loader.update_user_config(updates)
            if pending:
                self.proposal_path().unlink(missing_ok=True)
        except (OSError, ValueError, TypeError) as exc:
            return f'Calibration unchanged: {exc}'
        return self.message(word, pending.get('summary') if pending else None)

    def message(self, word: str, summary: str | None) -> str:
        config_path = self.loader.user_config_path()
        if word == 'apply' and summary:
            return f'Applied the calibration proposal to {config_path}: {summary}.'
        if word == 'apply':
            return 'No pending calibration proposal; nothing changed.'
        if word == 'auto':
            head = f'Applied the calibration proposal: {summary}. ' if summary else 'No pending calibration proposal. '
            return head + f'Future calibrations apply automatically (calibration.mode=auto in {config_path}).'
        head = 'Discarded the calibration proposal. ' if summary else 'No pending calibration proposal. '
        return head + f'Future calibrations will be proposed first (calibration.mode=propose in {config_path}).'
