"""Codex provider for the optional runner: one `codex exec` call, read-only, with tools and hooks off."""
import json
import sys
from functools import partial
from pathlib import Path

from core import config as config_mod
from core.runner import service
from core.runner.provider import Invocation, Provider

RUNTIME = "codex"
EXECUTABLE = "codex"
ADAPTER_DIR = Path(__file__).resolve().parent
ANSWER_FILE = "answer.txt"
SCHEMA_FILE = "schema.json"
DISABLED = ("features.shell_tool=false", "features.hooks=false", "features.apps=false",
            "features.plugins=false", "features.multi_agent=false", 'web_search="disabled"')
MESSAGE_ITEMS = (None, "agent_message", "reasoning")


def cheap_profile(config: dict) -> dict:
    return config["tiers"]["cheap"]


def command(effort: str, program: str, model: str, prompt: str, scratch: Path, schema: dict | None) -> Invocation:
    argv = [program, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "--json", "--model", model, "--output-last-message", str(scratch / ANSWER_FILE)]
    if schema:
        (scratch / SCHEMA_FILE).write_text(json.dumps(schema))
        argv += ["--output-schema", str(scratch / SCHEMA_FILE)]
    for setting in (f"model_reasoning_effort={json.dumps(effort)}", *DISABLED):
        argv += ["-c", setting]
    return Invocation([*argv, "-"], input=prompt)


def only_messages(stdout: str) -> bool:
    """A run whose JSON event stream shows any tool item is discarded."""
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
    if not only_messages(stdout):
        return None
    try:
        return (scratch / ANSWER_FILE).read_text()
    except (OSError, UnicodeDecodeError):
        return None


def provider(config: dict) -> Provider:
    return Provider(EXECUTABLE, partial(command, cheap_profile(config)["effort"]), answer)


def services(home: Path) -> service.Services:
    loader = config_mod.ConfigLoader(config_mod.sources_for(RUNTIME, home, ADAPTER_DIR / "config.json"))
    config = loader.load()
    return service.Services(loader, home, RUNTIME, provider(config), cheap_profile(config)["model"],
                            [sys.executable, str(ADAPTER_DIR / "hooks.py")])
