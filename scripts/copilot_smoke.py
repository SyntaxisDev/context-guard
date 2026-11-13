"""Exercise an installed Copilot hook offline or through an explicitly requested native run."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def invoke(command: list[str], *, env: dict[str, str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a bounded process and capture its output.

    Args:
        command: Executable and arguments.
        env: Isolated process environment.
        cwd: Process working directory.
        timeout: Maximum duration in seconds.

    Returns:
        Captured process result.
    """
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)  # noqa: S603


def require(condition: object, message: str) -> None:
    """Raise when an expected piece of smoke evidence is absent.

    Args:
        condition: Evidence assertion.
        message: Failure description.

    Raises:
        RuntimeError: The assertion is false.
    """
    if not condition:
        msg = message
        raise RuntimeError(msg)


def records(path: Path) -> list[dict]:
    """Read valid JSONL objects from a retained evidence log.

    Args:
        path: JSONL evidence file.

    Returns:
        Parsed object records; malformed lines are ignored.
    """
    if not path.is_file():
        return []
    result = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            result.append(record)
    return result


def evidence(runtime: Path, home: Path, session: str, event: str) -> tuple[Path, list[dict], list[dict]]:
    """Load native hook records and shared context-guard events for a session.

    Args:
        runtime: Temporary Copilot root.
        home: Temporary context-guard home.
        session: Native session ID.
        event: Shared event name to confirm.

    Returns:
        Native journal, matching hook records and matching shared events.
    """
    native = runtime / "session-state" / session / "events.jsonl"
    shared = home / ".context-guard" / "state" / "copilot" / "events.jsonl"
    hooks = [item for item in records(native) if item.get("type") in {"hook.start", "hook.end"}]
    context = [item for item in records(shared) if item.get("event") == event and item.get("session") == session]
    return native, hooks, context


def installed_hook(runtime: Path, event: str, payload: dict, home: Path, cwd: Path) -> dict:
    """Invoke the exact command rendered in the installed hook file.

    Args:
        runtime: Temporary Copilot root.
        event: Native event key.
        payload: Native event input.
        home: Temporary context-guard home.
        cwd: Temporary project directory.

    Returns:
        Decoded response from the installed command.
    """
    config = json.loads((runtime / "hooks/context-guard.json").read_text())
    spec = config["hooks"][event][0]
    env = {**os.environ, "COPILOT_HOME": str(runtime), "CONTEXT_GUARD_HOME": str(home)}
    shell = shutil.which("bash")
    require(shell is not None, "bash is required to invoke installed hooks")
    result = subprocess.run(  # noqa: S603
        [shell, "-lc", spec["bash"]],
        input=json.dumps(payload),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=spec["timeoutSec"] + 2,
        check=False,
    )
    require(result.returncode == 0 and not result.stderr, f"installed {event} command failed ({result.returncode})")
    return json.loads(result.stdout)


def native_command(cli: str, *, resume: str | None, prompt: str, output: Path) -> list[str]:
    """Build a bounded cheap-model Copilot invocation.

    Args:
        cli: Native Copilot executable or JS entry point.
        resume: Existing session ID, or None for a new session.
        prompt: User prompt.
        output: Native log directory.

    Returns:
        Native CLI command arguments.
    """
    prefix = [cli]
    if cli.endswith(".js"):
        node = shutil.which("node")
        require(node is not None, "Node is required for a .js CLI entry point")
        prefix = [node, cli]
    usage_file = output / f"usage-{time.time_ns()}.json"
    command = [
        *prefix,
        "--no-auto-update",
        "--disable-builtin-mcps",
        "--sync-session=off",
        "--no-ask-user",
        "--model",
        "gpt-6-luna",
        "--max-ai-credits",
        "30",
        "--log-dir",
        str(output / "logs"),
        "--usage-output-file",
        str(usage_file),
    ]
    if resume:
        command.append(f"--resume={resume}")
    return [*command, "-p", prompt]


def configure(home: Path, handoff: Path, *, block: bool = False) -> None:
    """Write isolated thresholds and handoff settings to the temporary user config.

    Args:
        home: Temporary context-guard home.
        handoff: Temporary handoff directory.
        block: Whether the block threshold should be lowered for the next prompt.
    """
    values = {
        "thresholds": {"note_cap": 1, "firm_cap": 999999, "block_cap": 1 if block else 999999},
        "paths": {"handoff_dir": str(handoff)},
        "handoff_refresh": {"enabled": False},
    }
    (home / ".context-guard").mkdir(parents=True, exist_ok=True)
    (home / ".context-guard" / "config.json").write_text(json.dumps(values))


def copy_account_selection(runtime: Path) -> None:
    """Copy only non-secret account identifiers needed for existing Keychain auth.

    Args:
        runtime: Temporary Copilot configuration root.

    Raises:
        RuntimeError: Existing account selection cannot be read or parsed.
    """
    source = Path.home() / ".copilot" / "config.json"
    try:
        raw = source.read_text()
        parsed = json.loads("\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("//")))
    except (OSError, ValueError) as exc:
        msg = "unable to read existing Copilot account selection metadata"
        raise RuntimeError(msg) from exc
    require(isinstance(parsed, dict), "existing Copilot account metadata must be an object")
    selected = {}
    for key in ("loggedInUsers", "lastLoggedInUser"):
        value = parsed.get(key)
        accounts = value if isinstance(value, list) else [value]
        kept = [
            {field: account[field] for field in ("host", "login", "kind") if isinstance(account.get(field), str)}
            for account in accounts
            if isinstance(account, dict)
        ]
        if kept:
            selected[key] = kept if isinstance(value, list) else kept[0]
    require(bool(selected), "existing Copilot config has no account selection metadata")
    config = runtime / "config.json"
    current = (
        json.loads("\n".join(line for line in config.read_text().splitlines() if not line.lstrip().startswith("//")))
        if config.exists()
        else {}
    )
    current.update(selected)
    config.write_text(json.dumps(current))
    config.chmod(0o600)


def main() -> int:
    """Run offline hooks, or prove blind native handoff, note and block/resend delivery.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="offline temporary-home check")
    mode.add_argument("--run", action="store_true", help="authorized authenticated native check")
    parser.add_argument("--output", type=Path, default=Path(tempfile.gettempdir()) / "copilot-installed-smoke")
    parser.add_argument("--copilot-cli", default=shutil.which("copilot"), help="CLI executable or measured cli-main.js")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    record = {"mode": "native" if args.run else "dry-run", "started": time.time(), "output": str(args.output)}
    base = args.output / "sandbox"
    home, runtime, work = base / "home", base / "runtime", base / "work"
    try:
        require(not base.exists(), "smoke sandbox already exists; choose a fresh --output directory")
        for path in (home, runtime, work / ".git", base / "cache", base / "xdg", base / "uv-cache"):
            path.mkdir(parents=True, exist_ok=True)
        git_init = invoke(["git", "init", "-q"], env=dict(os.environ), cwd=work, timeout=15)
        require(git_init.returncode == 0, "unable to initialize isolated smoke project")
        env = {
            **os.environ,
            "COPILOT_HOME": str(runtime),
            "CONTEXT_GUARD_HOME": str(home),
            "COPILOT_AUTO_UPDATE": "false",
            "COPILOT_TELEMETRY_DISABLED": "true",
            "COPILOT_CACHE_HOME": str(base / "cache"),
            "XDG_CACHE_HOME": str(base / "xdg"),
            "UV_CACHE_DIR": str(base / "uv-cache"),
        }
        title = f"blind-handoff-{uuid.uuid4().hex[:8]}"
        handoffs = home / ".context-guard" / "handoffs"
        handoffs.mkdir(parents=True, exist_ok=True)
        handoff = handoffs / f"{title}.md"
        handoff.write_text(f"# {title}\n\nWorkspace: {work}\nStatus: open\n\n## Goal\nReport the title exactly.\n")
        configure(home, handoffs)
        install = invoke(
            [sys.executable, str(ROOT / "cli.py"), "install", "--runtime", "copilot", "--home", str(home)],
            env=env,
            cwd=ROOT,
            timeout=180,
        )
        require(install.returncode == 0, f"temporary install failed: {install.stderr[-500:]}")
        record["install_exit"] = install.returncode
        if args.dry_run:
            record["offline"] = offline_check(home, runtime, work, handoffs)
        else:
            record.update(native_check(args, env, runtime, home, work, handoffs, title))
        record["status"] = "pass"
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        record["status"] = "fail"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["retained_sandbox"] = str(base)
    record["finished"] = time.time()
    (args.output / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps(
            {key: record[key] for key in ("mode", "status", "error", "output", "retained_sandbox") if key in record}
        )
    )
    return 0 if record["status"] == "pass" else 1


def offline_check(home: Path, runtime: Path, work: Path, handoffs: Path) -> dict:
    """Verify exact installed commands against temporary native telemetry.

    Args:
        home: Context-guard sandbox home.
        runtime: Copilot configuration root.
        work: Isolated project directory.
        handoffs: Isolated handoff store.

    Returns:
        Successful session-context and prompt-refusal observations.
    """
    native_events = runtime / "session-state" / "offline" / "events.jsonl"
    native_events.parent.mkdir(parents=True)
    native_events.write_text(
        json.dumps(
            {
                "type": "session.usage_record",
                "data": {
                    "usage": {
                        "interactionType": "conversation-agent",
                        "inputTokens": 220000,
                        "maxPromptTokens": 272000,
                        "model": "gpt-6-luna",
                    }
                },
            }
        )
        + "\n"
    )
    start = installed_hook(
        runtime, "sessionStart", {"sessionId": "offline", "cwd": str(work), "source": "new"}, home, work
    )
    configure(home, handoffs, block=True)
    block = installed_hook(
        runtime,
        "userPromptSubmitted",
        {"sessionId": "offline", "cwd": str(work), "prompt": "continue"},
        home,
        work,
    )
    require(bool(start.get("additionalContext")), "installed sessionStart did not return context")
    require(block.get("decision") == "block", "installed prompt hook did not block at configured threshold")
    return {"session_context": True, "block": True}


def native_check(
    args: argparse.Namespace,
    env: dict[str, str],
    runtime: Path,
    home: Path,
    work: Path,
    handoffs: Path,
    title: str,
) -> dict:
    """Verify blind context delivery and native warning, refusal and resend.

    Args:
        args: Explicit native-run options.
        env: Sandbox runtime environment.
        runtime: Copilot configuration root.
        home: Context-guard sandbox home.
        work: Isolated project directory.
        handoffs: Isolated handoff store.
        title: Blind handoff title withheld from the user prompt.

    Returns:
        Native version, session and independently asserted delivery observations.
    """
    record: dict = {}
    require(args.copilot_cli is not None, "Copilot executable unavailable; supply --copilot-cli")
    copy_account_selection(runtime)
    version_command = [args.copilot_cli]
    if args.copilot_cli.endswith(".js"):
        version_command = [shutil.which("node") or "node", args.copilot_cli]
    version = invoke([*version_command, "--version"], env=env, cwd=work, timeout=15)
    require(version.returncode == 0, "unable to verify selected Copilot CLI version")
    record["cli_version"] = version.stdout.strip()
    session, transcript = native_turn(args, env, runtime, work, title, None, args.output, blind_title=title)
    record["session"] = session
    record["handoff_transcript"] = transcript
    configure(home, handoffs)
    _, note = native_turn(
        args,
        env,
        runtime,
        work,
        "Quote the context warning you received. Do not use tools.",
        session,
        args.output,
    )
    assert_native_delivery(runtime, home, session, "note_delivered", "userPromptSubmitted", note, "Context check")
    configure(home, handoffs, block=True)
    blocked, before, blocked_prompt = native_block(args, env, runtime, work, session, args.output)
    assert_block(runtime, home, session, blocked, before)
    _, resent = native_turn(args, env, runtime, work, blocked_prompt, session, args.output)
    require("4" in resent, "same-text resend did not produce a final model response")
    require(
        any(row.get("event") == "resend" for row in records(home / ".context-guard/state/copilot/events.jsonl")),
        "shared same-text resend event missing",
    )
    record["native"] = {"handoff": True, "note": True, "block": True, "resend": True}
    record["usage"] = native_usage(runtime)
    return record


def native_usage(runtime: Path) -> dict:
    """Count unique native requests without summing cumulative resume exports.

    Args:
        runtime: Isolated Copilot root containing native session journals.

    Returns:
        Unique request count and observed credits, or None for unavailable credit data.
    """
    requests = {
        row["id"]: row.get("data", {}).get("usage", {}).get("copilotUsage", {}).get("totalNanoAiu")
        for path in (runtime / "session-state").glob("*/events.jsonl")
        for row in records(path)
        if row.get("type") == "session.usage_record" and isinstance(row.get("id"), str)
    }
    ai_credits = sum(requests.values()) / 1_000_000_000 if all(type(v) is int for v in requests.values()) else None
    return {"requests": len(requests), "ai_credits": ai_credits}


def native_turn(
    args: argparse.Namespace,
    env: dict[str, str],
    runtime: Path,
    work: Path,
    prompt: str,
    resume: str | None,
    output: Path,
    *,
    blind_title: str | None = None,
) -> tuple[str, str]:
    """Run a native prompt and assert blind handoff delivery or return its response.

    Args:
        args: Parsed CLI options.
        env: Isolated native environment.
        runtime: Temporary Copilot root.
        work: Temporary git project.
        prompt: Native user prompt.
        resume: Existing session to resume.
        output: Retained evidence directory.
        blind_title: Expected secret handoff title for the first native turn.

    Returns:
        Session ID and final model output.
    """
    command_prompt = (
        "Report the most relevant open handoff title visible in your injected context. No tools."
        if blind_title
        else prompt
    )
    before = set((runtime / "session-state").glob("*/events.jsonl")) if (runtime / "session-state").exists() else set()
    result = invoke(
        native_command(args.copilot_cli, resume=resume, prompt=command_prompt, output=output),
        env=env,
        cwd=work,
        timeout=120,
    )
    (output / f"turn-{time.time_ns()}.stdout").write_text(result.stdout)
    (output / f"turn-{time.time_ns()}.stderr").write_text(result.stderr)
    require(result.returncode == 0, f"native prompt exited {result.returncode}")
    if resume:
        session = resume
    else:
        paths = set((runtime / "session-state").glob("*/events.jsonl")) - before
        require(bool(paths), "new native turn created no session journal")
        session = max(paths, key=lambda path: path.stat().st_mtime_ns).parent.name
        native_records = records(next(iter(paths)))
        session_ends = [
            record
            for record in native_records
            if record.get("type") == "hook.end" and record.get("data", {}).get("hookType") == "sessionStart"
        ]
        require(bool(session_ends), "sessionStart hook did not end in native journal")
        require(blind_title is not None, "the blind handoff assertion title is missing")
        end_output = session_ends[-1].get("data", {}).get("output", {})
        require(blind_title in str(end_output), "native sessionStart hook output omitted the blind handoff title")
        require(
            blind_title is not None and blind_title in result.stdout, "model did not return the blind handoff title"
        )
    return session, result.stdout


def native_block(
    args: argparse.Namespace, env: dict[str, str], runtime: Path, work: Path, session: str, output: Path
) -> tuple[subprocess.CompletedProcess[str], int, str]:
    """Submit a high-context prompt expected to be refused before model execution.

    Args:
        args: Parsed CLI options.
        env: Isolated process environment.
        runtime: Temporary Copilot root.
        work: Temporary project.
        session: Existing native session ID.
        output: Retained evidence directory.

    Returns:
        Native result, pre-call usage count and the exact refused prompt.
    """
    path = runtime / "session-state" / session / "events.jsonl"
    before = sum(record.get("type") == "session.usage_record" for record in records(path))
    prompt = f"What is 2+2? Reply with only the number. Reference {uuid.uuid4().hex[:10]}."
    result = invoke(
        native_command(args.copilot_cli, resume=session, prompt=prompt, output=output),
        env=env,
        cwd=work,
        timeout=120,
    )
    (output / "block.stdout").write_text(result.stdout)
    (output / "block.stderr").write_text(result.stderr)
    return result, before, prompt


def assert_native_delivery(
    runtime: Path, home: Path, session: str, event: str, hook_event: str, response: str, required_text: str
) -> None:
    """Require native hook output and shared model-delivery telemetry.

    Args:
        runtime: Temporary Copilot root.
        home: Temporary context-guard home.
        session: Native session ID.
        event: Shared delivery event.
        hook_event: Native hook event name.
        response: Model response.
        required_text: Model-visible text expected in its final response.
    """
    _, hooks, shared = evidence(runtime, home, session, event)
    require(
        any(row.get("data", {}).get("hookType") == hook_event for row in hooks),
        f"missing native {hook_event} hook record",
    )
    require(bool(shared), f"missing shared {event} event")
    require(required_text.casefold() in response.casefold(), f"native final response did not show {event} delivery")


def assert_block(
    runtime: Path, home: Path, session: str, result: subprocess.CompletedProcess[str], before: int
) -> None:
    """Require native refusal, shared block evidence and no additional model request.

    Args:
        runtime: Temporary Copilot root.
        home: Temporary context-guard home.
        session: Native session ID.
        result: Blocked CLI result.
        before: Usage count before the refusal.
    """
    path, hooks, shared = evidence(runtime, home, session, "block")
    after = sum(row.get("type") == "session.usage_record" for row in records(path))
    require(result.returncode == 1, f"native block expected exit 1, received {result.returncode}")
    require(
        any(row.get("data", {}).get("hookType") == "userPromptSubmitted" for row in hooks),
        "native prompt-block hook record missing",
    )
    require(bool(shared), "shared block event missing")
    require(after == before, "blocked prompt generated a model usage record")


if __name__ == "__main__":
    raise SystemExit(main())
