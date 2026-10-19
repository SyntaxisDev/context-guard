"""Codex provider for the optional runner: one `codex exec` call, read-only, with tools and hooks off."""

import json
from functools import partial
from pathlib import Path

from core.runner import service
from core.runner.provider import Invocation, Provider

RUNTIME = "codex"
EXECUTABLE = "codex"
ADAPTER_DIR = Path(__file__).resolve().parent
ANSWER_FILE = "answer.txt"
SCHEMA_FILE = "schema.json"
DISABLED = (
    "features.shell_tool=false",
    "features.hooks=false",
    "features.apps=false",
    "features.plugins=false",
    "features.multi_agent=false",
    'web_search="disabled"',
)
MESSAGE_ITEMS = (None, "agent_message", "reasoning")


def command(effort: str, program: str, model: str, prompt: str, scratch: Path, schema: dict | None) -> Invocation:
    """Build a read-only, ephemeral `codex exec` call with tools and hooks off that reads the prompt from stdin.

    Args:
        effort: The reasoning effort setting.
        program: The resolved `codex` path.
        model: The model to call.
        prompt: The full prompt text.
        scratch: The scratch directory that receives the last message and the schema file.
        schema: A JSON schema the answer must match, written to the scratch directory; None for free text.

    Returns:
        The invocation.
    """
    argv = [
        program,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--model",
        model,
        "--output-last-message",
        str(scratch / ANSWER_FILE),
    ]
    if schema:
        (scratch / SCHEMA_FILE).write_text(json.dumps(schema))
        argv += ["--output-schema", str(scratch / SCHEMA_FILE)]
    for setting in (f"model_reasoning_effort={json.dumps(effort)}", *DISABLED):
        argv += ["-c", setting]
    return Invocation([*argv, "-"], input=prompt)


def only_messages(stdout: str) -> bool:
    """Return whether a run's JSON event stream holds only message items; a run with any tool item is discarded.

    Args:
        stdout: The run's `--json` event stream.

    Returns:
        False when a line is not JSON or carries a non-message item.
    """
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            return False
        item = event.get("item", {}) if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get("type") not in MESSAGE_ITEMS:
            return False
    return True


def answer(stdout: str, scratch: Path) -> str | None:
    """Read the last message of a run that used no tools.

    Args:
        stdout: The run's `--json` event stream.
        scratch: The scratch directory holding the last-message file.

    Returns:
        The last message, or None when the run used a tool or the file cannot be read.
    """
    if not only_messages(stdout):
        return None
    try:
        return (scratch / ANSWER_FILE).read_text()
    except OSError, UnicodeDecodeError:
        return None


def provider(config: dict) -> Provider:
    """Build the Codex provider at the cheap tier's reasoning effort.

    Args:
        config: The merged configuration.

    Returns:
        The provider.
    """
    return Provider(EXECUTABLE, partial(command, config["tiers"]["cheap"]["effort"]), answer)


def services(home: Path) -> service.Services:
    """Build Codex's prompt services.

    Args:
        home: The home directory that config, state and handoff paths resolve against.

    Returns:
        The services bound to the Codex provider and the cheap tier's `model`.
    """
    return service.build(RUNTIME, home, ADAPTER_DIR, provider, "model")
