"""Claude Code provider for the optional runner: one `claude -p` call with tools, MCP and hooks off."""
import json
import sys
from pathlib import Path

from core import config as config_mod
from core.runner import service
from core.runner.provider import Invocation, Provider

RUNTIME = "claude"
EXECUTABLE = "claude"
ADAPTER_DIR = Path(__file__).resolve().parent
PARENT_SESSION_ENV = "CLAUDECODE"
ISOLATION = ("--safe-mode", "--no-session-persistence", "--tools", "", "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}', "--disallowedTools", "mcp__*", "--setting-sources", "",
             "--settings", '{"disableAllHooks":true}')


def cheap_model(config: dict) -> str:
    return config["tiers"]["cheap"]["model"]


def command(program: str, model: str, prompt: str, scratch: Path, schema: dict | None) -> Invocation:
    argv = [program, "--print", "--model", model, "--output-format", "json", *ISOLATION]
    if schema:
        argv += ["--json-schema", json.dumps(schema)]
    return Invocation(argv, input=prompt, unset_env=(PARENT_SESSION_ENV,))


def answer(stdout: str, scratch: Path) -> str | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("is_error"):
        return None
    if isinstance(data.get("structured_output"), dict):
        return json.dumps(data["structured_output"])
    return data["result"] if isinstance(data.get("result"), str) else None


PROVIDER = Provider(EXECUTABLE, command, answer)


def services(home: Path) -> service.Services:
    loader = config_mod.ConfigLoader(config_mod.sources_for(RUNTIME, home, ADAPTER_DIR / "config.json"))
    return service.Services(loader, home, RUNTIME, PROVIDER, cheap_model(loader.load()),
                            [sys.executable, str(ADAPTER_DIR / "hooks.py")])
