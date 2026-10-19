"""Headless model calls through a runtime CLI: the adapter builds the argv, core bounds and reads the run.

Calls never get execution tools: the adapter's argv disables them or, where the CLI has no
such switch, runs a read-only mode in an empty scratch workspace. Inside a hook the call
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
    """One model run as the adapter built it.

    Attributes:
        argv: The full command line, starting with the resolved program.
        input: Text written to the run's stdin, or None for no input.
        unset_env: Environment variables removed from the child's environment.
    """

    argv: list[str]
    input: str | None = None
    unset_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provider:
    """How one runtime's CLI runs a model call and reports its answer.

    Attributes:
        executable: The program name looked up on PATH.
        command: Called as `command(program, model, prompt, scratch, schema)`; builds the run inside
            the `scratch` directory, constrained to the JSON `schema` when one is given.
        answer: Called as `answer(stdout, scratch)` after a successful run; returns the answer text or None.
    """

    executable: str
    command: Callable[[str, str, str, Path, dict | None], Invocation]
    answer: Callable[[str, Path], str | None]


def environment(unset: tuple[str, ...] = ()) -> dict[str, str]:
    """Return the parent environment marked internal, so the runtime's own hooks stay quiet in child runs.

    Args:
        unset: Variables to leave out of the copy.

    Returns:
        A copy of the process environment without `unset`, with `INTERNAL_ENV` set to "1".
    """
    env = {key: value for key, value in os.environ.items() if key not in unset}
    env[INTERNAL_ENV] = "1"
    return env


def complete(provider: Provider, model: str, prompt: str, timeout: float, schema: dict | None = None) -> str | None:
    """Run one headless model call in an empty scratch directory, bounded by the active hook deadline.

    Args:
        provider: The runtime's CLI provider.
        model: The model to call; an empty value skips the call.
        prompt: The full prompt text.
        timeout: The longest the call may take, in seconds, before the deadline shortens it.
        schema: A JSON schema the answer must match, or None for free text.

    Returns:
        The provider's answer, or None when the CLI or model is missing, no time is left,
        the run fails, times out or exits non-zero.
    """
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
            result = processes.run(
                call.argv,
                input=call.input,
                text=True,
                capture_output=True,
                cwd=scratch,
                env=environment(call.unset_env),
                timeout=timeout,
                **cleanup,
            )
        except OSError, subprocess.TimeoutExpired:
            return None
        return None if result.returncode else provider.answer(result.stdout, scratch)


def json_answer(stdout: str, _scratch: Path) -> str | None:
    """Read a CLI's `--output-format json` result: `structured_output` as JSON, else the `result` text.

    Args:
        stdout: The run's standard output.
        _scratch: The run's scratch directory; unused, part of the `Provider.answer` contract.

    Returns:
        The answer text, or None for invalid JSON, an error result or no answer field.
    """
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("is_error"):
        return None
    if isinstance(data.get("structured_output"), dict):
        return json.dumps(data["structured_output"])
    return data["result"] if isinstance(data.get("result"), str) else None


def json_object(text: str | None) -> dict | None:
    """Return the object a model returned, also when it wrapped the JSON in prose or a code fence.

    Args:
        text: The model's answer, or None when the call failed.

    Returns:
        The outermost `{...}` span parsed as a JSON object, or None when there is none.
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
