"""Claude Code provider for the optional runner: one `claude -p` call with tools, MCP and hooks off."""

import json
from pathlib import Path

from core.runner import service
from core.runner.provider import Invocation, Provider, json_answer

RUNTIME = "claude"
EXECUTABLE = "claude"
ADAPTER_DIR = Path(__file__).resolve().parent
PARENT_SESSION_ENV = "CLAUDECODE"
ISOLATION = (
    "--safe-mode",
    "--no-session-persistence",
    "--tools",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--disallowedTools",
    "mcp__*",
    "--setting-sources",
    "",
    "--settings",
    '{"disableAllHooks":true}',
)


def command(program: str, model: str, prompt: str, _scratch: Path, schema: dict | None) -> Invocation:
    """Build an isolated `claude --print` call that reads the prompt from stdin.

    Args:
        program: The resolved `claude` path.
        model: The model to call.
        prompt: The full prompt text.
        _scratch: The scratch directory; unused, part of the `Provider.command` contract.
        schema: A JSON schema the answer must match, or None for free text.

    Returns:
        The invocation, run outside the parent Claude Code session.
    """
    argv = [program, "--print", "--model", model, "--output-format", "json", *ISOLATION]
    if schema:
        argv += ["--json-schema", json.dumps(schema)]
    return Invocation(argv, input=prompt, unset_env=(PARENT_SESSION_ENV,))


PROVIDER = Provider(EXECUTABLE, command, json_answer)


def services(home: Path) -> service.Services:
    """Build Claude Code's prompt services.

    Args:
        home: The home directory that config, state and handoff paths resolve against.

    Returns:
        The services bound to the Claude Code provider and the cheap tier's `model`.
    """
    return service.build(RUNTIME, home, ADAPTER_DIR, lambda _config: PROVIDER, "model")
