"""Intent label parsing and per-runtime persistent caches."""

import re
import time
from collections.abc import Callable
from pathlib import Path

from core.state import file_lock, read_json, write_json

LABELS = ("MECHANICAL", "NEW_TASK", "CONTINUE")
LABEL_WORDS = re.compile(r"\b(MECHANICAL|NEW_TASK|CONTINUE)\b")
CATEGORIES = (
    "MECHANICAL (git commit/push/rebase/PR chores, re-running or checking a job/test/status, formatting, "
    "trivial confirmations to do such chores), NEW_TASK (unrelated new work), or CONTINUE (substantive "
    "continuation: design, questions, fixes, reviews)"
)
CLASSIFIER_PROMPT = (
    f"Classify the user message sent into a long coding-agent chat. Reply with exactly one word: {CATEGORIES}."
    "\nMessage: "
)


def normalise(prompt: str) -> str:
    """Return the cache key for a prompt.

    Args:
        prompt: The user's prompt text.

    Returns:
        The prompt lower-cased with whitespace collapsed and trailing spaces, dots and `!` removed.
    """
    return re.sub(r"\s+", " ", prompt.strip().lower()).rstrip(" .!")


def parse_label(output: str | None) -> str | None:
    """Return the last intent label named in a model reply.

    Args:
        output: The model's reply text, or None when the call failed.

    Returns:
        The last of `LABELS` found as a whole word, or None when there is none.
    """
    words = LABEL_WORDS.findall(output or "")
    return words[-1] if words else None


class LabelCache:
    """Persist prompt labels in one file, serialising merge-and-replace across child processes.

    Args:
        path: The JSON cache file; its `.lock` sibling guards updates.
        limit: The number of most recent entries kept; zero or less keeps none.
    """

    def __init__(self, path: Path, limit: int) -> None:
        self.path = Path(path)
        self.limit = limit

    def load(self) -> dict[str, str]:
        """Return the cached labels.

        Returns:
            Normalised prompts mapped to their label; malformed entries are dropped.
        """
        return {key: value for key, value in read_json(self.path).items() if isinstance(key, str) and value in LABELS}

    def remember(self, entries: dict[str, str]) -> None:
        """Merge labels into the cache as the newest entries and trim it to the limit.

        Args:
            entries: Raw prompts mapped to their label; entries whose label is not in `LABELS` are skipped.
        """
        with file_lock(self.path.with_suffix(".lock")):
            cache = self.load()
            for prompt, label in entries.items():
                if label not in LABELS:
                    continue
                key = normalise(prompt)
                cache.pop(key, None)
                cache[key] = label
            trimmed = dict(list(cache.items())[-self.limit :]) if self.limit > 0 else {}
            write_json(self.path, trimmed)


def classify(
    prompt: str, conf: dict, cache: LabelCache, ask_model: Callable[[str, str, float], str | None]
) -> tuple[str | None, float, bool]:
    """Label a prompt from the cache, else by asking the classifier model and caching its answer.

    Args:
        prompt: The user's prompt text.
        conf: The `classifier` config section; `model` and `timeout_seconds` are used.
        cache: The runtime's label cache.
        ask_model: Called as `ask_model(model, prompt, timeout)`; returns the reply text or None.

    Returns:
        The label (None when the model gave none), the model latency in seconds (0.0 when cached)
        and whether the label came from the cache.
    """
    cached = cache.load().get(normalise(prompt))
    if cached in LABELS:
        return cached, 0.0, True
    started = time.monotonic()
    output = ask_model(conf["model"], CLASSIFIER_PROMPT + prompt, conf["timeout_seconds"])
    latency = round(time.monotonic() - started, 2)
    label = parse_label(output)
    if label:
        cache.remember({prompt: label})
    return label, latency, False
