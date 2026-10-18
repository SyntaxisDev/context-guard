"""Headless model calls through a runtime CLI: the adapter builds the argv, core bounds and reads the run.

Calls never get execution tools; the adapter's argv disables them. Inside a hook the call
leaves the policy reserve of the active deadline; a timeout or failed run yields None.
"""
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core import deadline, processes

INTERNAL_ENV = "CONTEXT_GUARD_INTERNAL"


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    input: str | None = None
    unset_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provider:
    """`command(program, model, prompt, scratch, schema)` builds the run; `answer(stdout, scratch)` reads its text."""
    executable: str
    command: Callable[[str, str, str, Path, dict | None], Invocation]
    answer: Callable[[str, Path], str | None]


def environment(unset: tuple[str, ...] = ()) -> dict:
    """The parent environment marked internal, so the runtime's own hooks stay quiet in child runs."""
    env = {key: value for key, value in os.environ.items() if key not in unset}
    env[INTERNAL_ENV] = "1"
    return env


def complete(provider: Provider, model: str, prompt: str, timeout: float, schema: dict | None = None) -> str | None:
    program = shutil.which(provider.executable)
    if not program or not model:
        return None
    limit = deadline.active()
    timeout = deadline.model_timeout(timeout, limit)
    if timeout <= 0:
        return None
    cleanup = {} if limit is None else {"cleanup_timeout": deadline.MODEL_CLEANUP_SECONDS}
    with tempfile.TemporaryDirectory(prefix="context-guard-model-") as name:
        scratch = Path(name)
        call = provider.command(program, model, prompt, scratch, schema)
        try:
            result = processes.run(call.argv, input=call.input, text=True, capture_output=True, cwd=scratch,
                                   env=environment(call.unset_env), timeout=timeout, **cleanup)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return None if result.returncode else provider.answer(result.stdout, scratch)


def json_object(text: str | None) -> dict | None:
    """The object a model returned, also when it wrapped the JSON in prose or a code fence."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
