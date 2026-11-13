"""GitHub Copilot CLI provider for the optional runner: one isolated, model-pinned `copilot` prompt-mode call.

Each call runs in a scratch `COPILOT_HOME`, so no user hook, plugin, MCP server, instruction file, skill,
session history or setting loads, and nothing is written to the user's Copilot state. Only the account
selection (host, login, kind) is copied from the user's `config.json`; credentials stay in the keychain
or the inherited environment and are never read here. Tools stay listed to the model: observed calls to
`view`, `bash` and `rg` were denied by `--deny-tool` rules and the run was refused. Prompt mode has no
approver, and a run whose scratch event log shows any tool call is discarded.
"""

import json
import os
import re
from collections.abc import Mapping
from functools import partial
from pathlib import Path

from adapters.copilot import mapping
from core.runner import service
from core.runner.provider import Invocation, Provider

RUNTIME = "copilot"
EXECUTABLE = "copilot"
ADAPTER_DIR = Path(__file__).resolve().parent
ENV_PROGRAM = "/usr/bin/env"
HOME_DIR = "copilot-home"
LOG_DIR = "logs"
CONFIG_FILE = "config.json"
SETTINGS_FILE = "settings.json"
SESSION_STATE = "session-state"
EVENTS_FILE = "events.jsonl"
TOOL_EVENT = "tool.execution_start"
ACCOUNT_KEYS = ("loggedInUsers", "lastLoggedInUser")
ACCOUNT_FIELDS = ("host", "login", "kind")
SCRATCH_SETTINGS = {"disableAllHooks": True}
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
DENIED_KINDS = ("shell", "write", "read", "memory", "url")
MIN_AI_CREDITS = "30"
FIXED_ENV = ("COPILOT_AUTO_UPDATE=false", "COPILOT_TELEMETRY_DISABLED=true")
# Variables that select an approval policy, a model or another provider, or add instructions.
CLEARED_ENV = (
    "COPILOT_ALLOW_ALL",
    "COPILOT_ASSISTED_APPROVAL",
    "COPILOT_MODEL",
    "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
    "COPILOT_OFFLINE",
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_TYPE",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_API_KEY_COMMAND",
    "COPILOT_PROVIDER_BEARER_TOKEN",
    "COPILOT_PROVIDER_WIRE_API",
    "COPILOT_PROVIDER_TRANSPORT",
    "COPILOT_PROVIDER_AZURE_API_VERSION",
    "COPILOT_PROVIDER_MODEL_ID",
    "COPILOT_PROVIDER_WIRE_MODEL",
    "COPILOT_PROVIDER_MAX_PROMPT_TOKENS",
    "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS",
    "COPILOT_PROVIDER_VISION",
    "COPILOT_PROVIDER_HEADERS",
)
ISOLATION = (
    "--silent",
    "--no-color",
    "--no-auto-update",
    "--disable-client-telemetry",
    "--disable-builtin-mcps",
    "--no-custom-instructions",
    "--sync-session=off",
    "--no-ask-user",
    "--no-experimental",
    *(f"--deny-tool={kind}" for kind in DENIED_KINDS),
    "--max-ai-credits",
    MIN_AI_CREDITS,
)
SCHEMA_INSTRUCTION = "Answer with one JSON object that matches this JSON schema and nothing else:\n"


def account_selection(source_home: Path | None) -> dict:
    """Read the account-selection fields of the user's Copilot config.

    The file is JSON with full-line `//` comments. Only `ACCOUNT_FIELDS` of each account are kept, so
    no other value can be carried over.

    Args:
        source_home: The user's Copilot home, or None when it cannot be resolved.

    Returns:
        The `ACCOUNT_KEYS` entries found, filtered; empty when the file is missing or unreadable.
    """
    if source_home is None:
        return {}
    try:
        text = (source_home / CONFIG_FILE).read_text()
        data = json.loads("\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//")))
    except OSError, UnicodeDecodeError, ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    selection = {}
    for key in ACCOUNT_KEYS:
        value = data.get(key)
        accounts = value if isinstance(value, list) else [value]
        kept = [
            {field: account[field] for field in ACCOUNT_FIELDS if isinstance(account.get(field), str)}
            for account in accounts
            if isinstance(account, dict)
        ]
        if kept:
            selection[key] = kept if isinstance(value, list) else kept[0]
    return selection


def scratch_home(source_home: Path | None, scratch: Path) -> Path:
    """Create the call's private Copilot home: the account selection and a setting that disables hooks.

    Args:
        source_home: The user's Copilot home, read only for the account selection; None for none.
        scratch: The call's scratch directory.

    Returns:
        The new home, private to its owner.
    """
    home = scratch / HOME_DIR
    home.mkdir(mode=0o700, exist_ok=True)
    (home / SETTINGS_FILE).write_text(json.dumps(SCRATCH_SETTINGS))
    config = home / CONFIG_FILE
    config.write_text(json.dumps(account_selection(source_home)))
    config.chmod(0o600)
    return home


def validate(model: str, effort: str, prompt: str) -> None:
    """Reject a call that could change the argv's meaning or has nothing to run.

    Args:
        model: The model name.
        effort: The reasoning effort.
        prompt: The prompt text.

    Raises:
        ValueError: The model is not a plain model name, the effort is not one the CLI lists, or
            the prompt is empty or holds a NUL character.
    """
    if not MODEL.fullmatch(model):
        msg = f"{model!r} is not a plain model name"
        raise ValueError(msg)
    if effort not in EFFORTS:
        msg = f"{effort!r} is not a Copilot reasoning effort"
        raise ValueError(msg)
    if not prompt.strip() or "\0" in prompt:
        msg = "the prompt must be non-empty text without NUL characters"
        raise ValueError(msg)


def command(
    effort: str,
    source_home: Path | None,
    program: str,
    model: str,
    prompt: str,
    scratch: Path,
    schema: dict | None,
) -> Invocation:
    """Build an isolated, tool-less prompt-mode call that reads the prompt from stdin.

    Args:
        effort: The reasoning effort setting.
        source_home: The user's Copilot home, read only for the account selection.
        program: The resolved `copilot` path.
        model: The model to call.
        prompt: The full prompt text.
        scratch: The scratch directory that holds the call's `COPILOT_HOME` and logs.
        schema: A JSON schema the answer must match, or None for free text. The CLI has no schema
            option, so the schema goes into the prompt and the caller validates the answer.

    Returns:
        The invocation; the prompt is stdin and the child's Copilot home is scratch-only. `validate`
        raises ValueError for a model, effort or prompt it rejects.
    """
    validate(model, effort, prompt)
    home = scratch_home(source_home, scratch)
    text = f"{SCHEMA_INSTRUCTION}{json.dumps(schema)}\n\n{prompt}" if schema else prompt
    argv = [
        ENV_PROGRAM,
        f"COPILOT_HOME={home}",
        *FIXED_ENV,
        program,
        "--model",
        model,
        "--reasoning-effort",
        effort,
        "--log-dir",
        str(scratch / LOG_DIR),
        *ISOLATION,
    ]
    return Invocation(argv, input=text, unset_env=CLEARED_ENV)


def used_tools(scratch: Path) -> bool:
    """Tell whether any session the call recorded ran a tool.

    Args:
        scratch: The call's scratch directory.

    Returns:
        True when native evidence is absent, empty or corrupt, or records a tool start.
    """
    logs = sorted((scratch / HOME_DIR / SESSION_STATE).glob(f"*/{EVENTS_FILE}"))
    if not logs:
        return True
    for log in logs:
        try:
            records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        except OSError, UnicodeDecodeError, ValueError:
            return True
        if not records or any(not isinstance(record, dict) for record in records):
            return True
        if any(record.get("type") == TOOL_EVENT for record in records):
            return True
    return False


def answer(stdout: str, scratch: Path) -> str | None:
    """Read the answer of a run that used no tools.

    Args:
        stdout: The run's `--silent` output, the agent response alone.
        scratch: The call's scratch directory.

    Returns:
        The trimmed response, or None when it is empty or the session ran a tool.
    """
    text = stdout.strip()
    return None if not text or used_tools(scratch) else text


def source_home(home: Path | None, environ: Mapping[str, str]) -> Path | None:
    """Locate the user's Copilot home for the account selection.

    Args:
        home: The user home, or None for the current user's.
        environ: Environment that may set COPILOT_HOME.

    Returns:
        The Copilot home, or None when COPILOT_HOME is relative.
    """
    try:
        return mapping.copilot_home(Path.home() if home is None else home, environ)
    except ValueError:
        return None


def provider(config: dict, home: Path | None = None) -> Provider:
    """Build the Copilot provider at the cheap tier's reasoning effort.

    Args:
        config: The merged configuration.
        home: The user home whose Copilot home supplies the account selection; None for the current user's.

    Returns:
        The provider.
    """
    effort = config["tiers"]["cheap"]["effort"]
    return Provider(EXECUTABLE, partial(command, effort, source_home(home, os.environ)), answer)


def services(home: Path) -> service.Services:
    """Build Copilot's prompt services.

    Args:
        home: The home directory that config, state and handoff paths resolve against.

    Returns:
        The services bound to the Copilot provider and the cheap tier's `model`.
    """
    return service.build(RUNTIME, home, ADAPTER_DIR, partial(provider, home=home), "model")
