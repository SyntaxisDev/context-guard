"""Optional prompt classification and decision logging with explicit runtime inputs."""
import collections
import json
from pathlib import Path
from core.runner import intent_labels, prefilter
from core.state import file_lock

TRIM_SLACK = 100


def decide(prompt: str, config: dict, cache, ask_model=None, needed=True) -> dict:
    conf = config['classifier']
    found = {'prefilter': prefilter.passes(prompt, conf), 'label': None,
             'latency_s': None, 'cached': False, 'asked': False}
    if needed and found['prefilter'] and conf['enabled'] and ask_model is not None:
        found['asked'] = True
        found['label'], found['latency_s'], found['cached'] = intent_labels.classify(prompt, conf, cache, ask_model)
    return found


def log_decision(path: Path, record: dict, window: int) -> None:
    try:
        with file_lock(path.with_suffix('.lock')):
            with path.open('a') as handle:
                handle.write(json.dumps(record) + '\n')
            with path.open() as handle:
                lines = collections.deque(handle, maxlen=window + TRIM_SLACK + 1)
            if len(lines) > window + TRIM_SLACK:
                # The lock is held by every service writer, including while trimming.
                with path.open('w') as handle:
                    handle.writelines(list(lines)[-window:])
    except OSError:
        return
