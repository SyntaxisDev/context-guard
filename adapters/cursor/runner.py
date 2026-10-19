"""Cursor provider for the optional runner.

One headless `cursor-agent` call in read-only ask mode inside an empty scratch workspace.
"""

import json
from pathlib import Path

from core.runner import service
from core.runner.provider import Invocation, Provider, json_answer

RUNTIME = "cursor"
EXECUTABLE = "cursor-agent"
ADAPTER_DIR = Path(__file__).resolve().parent
SCHEMA_INSTRUCTION = "\n\nReply with only a JSON object matching this schema: "


def command(program: str, model: str, prompt: str, scratch: Path, schema: dict | None) -> Invocation:
    """Build a `cursor-agent` ask-mode call with the scratch directory as its workspace.

    Args:
        program: The resolved `cursor-agent` path.
        model: The model to call.
        prompt: The full prompt text.
        scratch: The empty scratch workspace.
        schema: A JSON schema the answer must match, appended to the prompt as an instruction; None for free text.

    Returns:
        The invocation, with the prompt passed as the last argument.
    """
    text = prompt + (SCHEMA_INSTRUCTION + json.dumps(schema) if schema else "")
    return Invocation(
        [
            program,
            "-p",
            "--trust",
            "--mode",
            "ask",
            "--workspace",
            str(scratch),
            "--model",
            model,
            "--output-format",
            "json",
            text,
        ]
    )


PROVIDER = Provider(EXECUTABLE, command, json_answer)


def services(home: Path) -> service.Services:
    """Build Cursor's prompt services.

    Args:
        home: The home directory that config, state and handoff paths resolve against.

    Returns:
        The services bound to the Cursor provider and the cheap tier's `call` model.
    """
    return service.build(RUNTIME, home, ADAPTER_DIR, lambda _config: PROVIDER, "call")
