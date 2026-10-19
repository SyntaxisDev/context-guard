"""Optional prompt classification with explicit runtime inputs."""

from collections.abc import Callable

from core.runner import intent_labels, prefilter


def decide(
    prompt: str,
    classifier: dict,
    cache: intent_labels.LabelCache,
    ask_model: Callable[[str, str, float], str | None],
    *,
    ask: bool,
) -> dict:
    """Run the prefilter and, when `ask` is set and it passes, ask the classifier (cached).

    Args:
        prompt: The user's prompt text.
        classifier: The `classifier` config section.
        cache: The runtime's label cache.
        ask_model: Called as `ask_model(model, prompt, timeout)`; returns the reply text or None.
        ask: Whether the classifier may be asked at all.

    Returns:
        A decision record: `prefilter` (bool), `label` (str or None), `latency_s` (float or None),
        `cached` and `asked` (bool).
    """
    found = {
        "prefilter": prefilter.passes(prompt, classifier),
        "label": None,
        "latency_s": None,
        "cached": False,
        "asked": False,
    }
    if ask and found["prefilter"]:
        found["asked"] = True
        found["label"], found["latency_s"], found["cached"] = intent_labels.classify(
            prompt, classifier, cache, ask_model
        )
    return found
