"""Recall-oriented pre-filter: decides which prompts are worth sending to the classifier.

All rules come from the `classifier` config section, so calibration.py can tune them:
  max_chars           single-line prompts up to this length only
  max_question_words  a prompt with "?" passes only if it has at most this many words, or contains a polite word
  polite_words        regex fragments ("can you", "please", ...) that make a question an ask
  keywords            regex fragments for chore verbs and nouns; any match passes
  short_keywords      generic fragments ("run", "again", ...) that pass only in prompts of <= short_max_words words
"""

import functools
import re


@functools.lru_cache(maxsize=32)
def word_pattern(fragments: tuple[str, ...]) -> re.Pattern | None:
    """Compile regex fragments into one case-insensitive whole-word alternation.

    Args:
        fragments: Regex fragments to match between word boundaries.

    Returns:
        The compiled pattern, or None when there are no fragments.
    """
    if not fragments:
        return None
    return re.compile(r"\b(?:" + "|".join(fragments) + r")\b", re.IGNORECASE)


def matches(fragments: list[str], text: str) -> bool:
    """Return whether any fragment matches a whole word in the text.

    Args:
        fragments: Regex fragments from the classifier config.
        text: The prompt text to search.

    Returns:
        True when a fragment matches; False for no match or no fragments.
    """
    pattern = word_pattern(tuple(fragments))
    return bool(pattern and pattern.search(text))


def passes(prompt: str, conf: dict) -> bool:
    """Return whether a prompt is worth sending to the classifier.

    Args:
        prompt: The user's prompt text.
        conf: The `classifier` config section holding the rules described in the module docstring.

    Returns:
        True when the prompt is a short single line that matches a keyword, or a short keyword
        within `short_max_words` words, and is not a long impolite question.
    """
    text = prompt.strip()
    if not text or "\n" in text or len(text) > conf["max_chars"]:
        return False
    words = len(text.split())
    if "?" in text and words > conf["max_question_words"] and not matches(conf["polite_words"], text):
        return False
    if matches(conf["keywords"], text):
        return True
    return words <= conf["short_max_words"] and matches(conf["short_keywords"], text)
