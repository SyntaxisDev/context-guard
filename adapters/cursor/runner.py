"""Cursor provider for the optional runner: one headless `cursor-agent` call in ask mode, no tools."""
import json
import sys
from pathlib import Path

from core import config as config_mod
from core.runner import service
from core.runner.provider import Invocation, Provider

RUNTIME = "cursor"
EXECUTABLE = "cursor-agent"
ADAPTER_DIR = Path(__file__).resolve().parent
SCHEMA_INSTRUCTION = "\n\nReply with only a JSON object matching this schema: "


def cheap_model(config: dict) -> str:
    return config["tiers"]["cheap"]["call"]


def command(program: str, model: str, prompt: str, scratch: Path, schema: dict | None) -> Invocation:
    text = prompt + (SCHEMA_INSTRUCTION + json.dumps(schema) if schema else "")
    return Invocation([program, "-p", "--trust", "--mode", "ask", "--workspace", str(scratch),
                       "--model", model, "--output-format", "json", text])


def answer(stdout: str, scratch: Path) -> str | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("is_error"):
        return None
    return data["result"] if isinstance(data.get("result"), str) else None


PROVIDER = Provider(EXECUTABLE, command, answer)


def services(home: Path) -> service.Services:
    loader = config_mod.ConfigLoader(config_mod.sources_for(RUNTIME, home, ADAPTER_DIR / "config.json"))
    return service.Services(loader, home, RUNTIME, PROVIDER, cheap_model(loader.load()),
                            [sys.executable, str(ADAPTER_DIR / "hooks.py")])
