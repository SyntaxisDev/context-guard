"""Intent label parsing and per-runtime persistent caches."""
import re
import time
from pathlib import Path
from core.state import file_lock, read_json, write_json

LABELS = ('MECHANICAL', 'NEW_TASK', 'CONTINUE')
LABEL_WORDS = re.compile(r'\b(MECHANICAL|NEW_TASK|CONTINUE)\b')
CATEGORIES = (
    'MECHANICAL (git commit/push/rebase/PR chores, re-running or checking a job/test/status, formatting, '
    'trivial confirmations to do such chores), NEW_TASK (unrelated new work), or CONTINUE (substantive '
    'continuation: design, questions, fixes, reviews)'
)
CLASSIFIER_PROMPT = (
    f'Classify the user message sent into a long coding-agent chat. Reply with exactly one word: {CATEGORIES}.'
    '\nMessage: '
)


def normalise(prompt: str) -> str:
    return re.sub(r'\s+', ' ', prompt.strip().lower()).rstrip(' .!')


def parse_label(output: str) -> str | None:
    words = LABEL_WORDS.findall(output or '')
    return words[-1] if words else None


class LabelCache:
    """Own a single cache path; serialize merge-and-replace across child processes."""
    def __init__(self, path: Path, limit: int):
        self.path = Path(path)
        self.limit = limit

    def load(self) -> dict:
        return {key: value for key, value in read_json(self.path).items()
                if isinstance(key, str) and value in LABELS}

    def remember(self, entries: dict) -> None:
        with file_lock(self.path.with_suffix('.lock')):
            cache = self.load()
            for prompt, label in entries.items():
                if label not in LABELS:
                    continue
                key = normalise(prompt)
                cache.pop(key, None)
                cache[key] = label
            trimmed = dict(list(cache.items())[-self.limit:]) if self.limit > 0 else {}
            write_json(self.path, trimmed)


def classify(prompt: str, conf: dict, cache, ask_model) -> tuple:
    cached = cache.load().get(normalise(prompt))
    if cached in LABELS:
        return cached, 0.0, True
    started = time.monotonic()
    output = ask_model(conf['model'], CLASSIFIER_PROMPT + prompt, conf['timeout_seconds'])
    latency = round(time.monotonic() - started, 2)
    label = parse_label(output)
    if label:
        cache.remember({prompt: label})
    return label, latency, False
