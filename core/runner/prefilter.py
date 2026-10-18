"""Recall-oriented pre-filter: decides which prompts are worth sending to the classifier.

All rules come from the `classifier` config section, so calibrate.py can tune them:
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
    if not fragments:
        return None
    return re.compile(r"\b(?:" + "|".join(fragments) + r")\b", re.IGNORECASE)


def matches(fragments: list[str], text: str) -> bool:
    pattern = word_pattern(tuple(fragments))
    return bool(pattern and pattern.search(text))


def passes(prompt: str, conf: dict) -> bool:
    text = prompt.strip()
    if not text or "\n" in text or len(text) > conf["max_chars"]:
        return False
    words = len(text.split())
    if "?" in text and words > conf["max_question_words"] and not matches(conf["polite_words"], text):
        return False
    if matches(conf["keywords"], text):
        return True
    return words <= conf["short_max_words"] and matches(conf["short_keywords"], text)
