"""Bounded command-plan execution; provider models never receive execution tools.

Configured exact commands are trusted project programs, not an OS sandbox.
All other commands must satisfy the narrow Git grammar below. `git commit` and `git push`
run the repository's own hooks (pre-commit, pre-push), which are repository code.
"""

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from core import handoffs, processes
from core.state import file_lock, read_json, write_json

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "commands": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        }
    },
    "required": ["commands"],
    "additionalProperties": False,
}
SHELL_CHARS = re.compile(r"[\x00\n\r`$|;&<>]")
MAX_ARGV = 40
MAX_ARG_CHARS = 4096
COMMIT_ARGS = 2
READ_GIT = {
    "status": {"--short", "--branch", "--porcelain", "--porcelain=v1"},
    "diff": {"--stat", "--name-only", "--cached", "--staged", "--check"},
    "log": {"--oneline", "--decorate", "-1", "-5", "-10"},
}


def valid_path(value: str, workspace: Path) -> bool:
    """Return whether a pathspec names a relative path inside the workspace.

    Args:
        value: The pathspec argument.
        workspace: The resolved workspace directory.

    Returns:
        False for an empty, option-like, magic (`:`), absolute or escaping path.
    """
    if not value or value.startswith(("-", ":")) or Path(value).is_absolute():
        return False
    try:
        (workspace / value).resolve().relative_to(workspace)
    except ValueError, OSError:
        return False
    return True


def authorized_action(task: str, action: str) -> bool:
    """Return whether the user's task explicitly asks for a mutating git action.

    Args:
        task: The user's prompt.
        action: One of `add`, `commit` or `push`.

    Returns:
        True only when the task has no negation, every clause starts with a stage/add/commit/push
        verb (optionally polite), and one of them authorizes `action`.
    """
    if re.search(r"\b(not|never|don't|do not|no)\b", task, re.IGNORECASE):
        return False
    # Quoted descriptions cannot introduce another mutation clause.
    plain = re.sub(r"\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`", "", task).strip()
    polite = r"(?:(?:please|can you|could you|would you)\s+)*"
    clauses = re.split(r"\b(?:and\s+then|and|then)\b", plain, flags=re.IGNORECASE)
    verbs = []
    for clause in clauses:
        match = re.match(r"^\s*" + polite + r"(stage|add|commit|push)\b", clause, re.IGNORECASE)
        if match is None:
            return False
        verbs.append(match[1].lower())
    accepted = {"add": {"stage", "add", "commit"}, "commit": {"commit"}, "push": {"push"}}[action]
    return bool(accepted.intersection(verbs))


def execution_environment() -> dict[str, str]:
    """Return the environment plan commands run in.

    Returns:
        The process environment without variables that redirect git's repository, config,
        diff or exec paths, marked internal, with terminal prompts off and literal pathspecs.
    """
    redirects = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
        "GIT_EXEC_PATH",
    }
    env = {key: value for key, value in os.environ.items() if key not in redirects and not key.startswith("GIT_CONFIG")}
    env.update(CONTEXT_GUARD_INTERNAL="1", GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1")
    return env


def git_allowed(args: list[str], task: str, workspace: Path) -> bool:
    """Return whether git arguments fit the narrow read and authorized-mutation grammar.

    Args:
        args: The arguments after `git`.
        task: The user's prompt, which must authorize add, commit and push.
        workspace: The resolved workspace directory that added paths must stay inside.

    Returns:
        True for allow-listed read flags, `branch --show-current`, authorized `add` of workspace
        paths, `commit -m <message>` and a bare `push`; False otherwise.
    """
    if not args:
        return False
    action, rest = args[0], args[1:]
    if action in READ_GIT:
        return all(arg in READ_GIT[action] for arg in rest)
    if action == "branch":
        return rest == ["--show-current"]
    if action == "add" and authorized_action(task, "add"):
        paths = rest[1:] if rest and rest[0] == "--" else rest
        return bool(paths) and all(valid_path(path, workspace) for path in paths)
    if action == "commit" and authorized_action(task, "commit"):
        return len(rest) == COMMIT_ARGS and rest[0] == "-m" and bool(rest[1]) and not rest[1].startswith("-")
    if action == "push" and authorized_action(task, "push"):
        return not rest  # Never permit URLs, force, transport helpers or arbitrary options.
    return False


def check_argv(argv: object) -> None:
    """Reject argv that is malformed, carries shell syntax, runs a shell or passes inline code.

    Args:
        argv: The planned command as the model returned it.

    Raises:
        ValueError: When argv is not a non-empty list of at most `MAX_ARGV` non-empty strings of at
            most `MAX_ARG_CHARS` characters without shell characters, names a shell or `env`, or
            passes an inline-code flag.
    """
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV:
        msg = "invalid argv"
        raise ValueError(msg)
    if any(not isinstance(arg, str) or not arg or len(arg) > MAX_ARG_CHARS or SHELL_CHARS.search(arg) for arg in argv):
        msg = "invalid command argument"
        raise ValueError(msg)
    if Path(argv[0]).name in {"sh", "bash", "zsh", "fish", "dash", "env"}:
        msg = "shell execution forbidden"
        raise ValueError(msg)
    if any(arg in {"-c", "-e", "--eval", "--command"} for arg in argv[1:]):
        msg = "inline code forbidden"
        raise ValueError(msg)


def resolve_program(argv: list[str], task: str, workspace: Path, commands: list[list[str]]) -> str:
    """Resolve the executable of an allowed command.

    Args:
        argv: The planned command, already shape-checked by `check_argv`.
        task: The user's prompt, which must authorize git mutations.
        workspace: The resolved workspace directory.
        commands: The exact command templates from the runner config.

    Returns:
        The executable's path on PATH.

    Raises:
        ValueError: When a git command is not authorized, the command is neither git nor an exact
            configured template, a configured template names a path, or the executable is missing.
    """
    if argv[0] == "git":
        if not git_allowed(argv[1:], task, workspace):
            msg = "git command not authorized"
            raise ValueError(msg)
        executable = shutil.which("git")
    elif argv in commands:
        # Exact command templates are explicit user configuration, never model additions.
        if "/" in argv[0]:
            msg = "configured commands must name a program on PATH, not a path"
            raise ValueError(msg)
        executable = shutil.which(argv[0])
    else:
        msg = "command not allowed"
        raise ValueError(msg)
    if not executable:
        msg = "executable unavailable"
        raise ValueError(msg)
    return executable


def validate_command(argv: list, task: str, workspace: Path, commands: list[list[str]]) -> list[str]:
    """Validate one planned command, failing closed, and return the argv to execute.

    A rejected command propagates the ValueError from `check_argv` or `resolve_program`.

    Args:
        argv: The planned command as the model returned it.
        task: The user's prompt, which must authorize git mutations.
        workspace: The resolved workspace directory.
        commands: The exact command templates from the runner config.

    Returns:
        The argv with the resolved executable; git runs without a pager, fsmonitor or, for
        diffs, external diff and textconv drivers.
    """
    check_argv(argv)
    executable = resolve_program(argv, task, workspace, commands)
    if argv[0] == "git":
        extras = ["--no-ext-diff", "--no-textconv"] if argv[1] == "diff" else []
        return [executable, "--no-pager", "-c", "core.fsmonitor=false", argv[1], *extras, *argv[2:]]
    return [executable, *argv[1:]]


def execute_plan(plan: dict | None, job: dict, config: dict, log: Path) -> dict:
    """Validate a whole command plan, then run its commands in order inside the job's workspace.

    Args:
        plan: The model's plan object, or None when planning failed.
        job: The job record; `prompt` and `workspace` are used.
        config: The `runner` config section; `max_commands`, `commands` and `timeout_seconds` are used.
        log: The job log that receives each argv and the commands' output.

    Returns:
        `{"status", "commands"}` with status `done`, `failed` (a command exited non-zero) or
        `timeout`, and the argv and exit code of each command that finished.

    Raises:
        ValueError: When the plan is empty, too long or malformed, the workspace is not a
            directory, or any command fails validation; nothing runs then.
    """
    commands = plan.get("commands") if isinstance(plan, dict) else None
    limit = min(config.get("max_commands", 8), 8)
    if not isinstance(commands, list) or not commands or len(commands) > limit:
        msg = "invalid command plan"
        raise ValueError(msg)
    workspace = Path(job["workspace"]).resolve(strict=True)
    if not workspace.is_dir():
        msg = "workspace unavailable"
        raise ValueError(msg)
    # Validate the entire plan before executing even its first command.
    approved = [validate_command(argv, job["prompt"], workspace, config.get("commands", [])) for argv in commands]
    results, started = [], time.monotonic()
    total = min(config.get("timeout_seconds", 300), 1800)
    environment = execution_environment()
    with log.open("a") as output:
        for original, argv in zip(commands, approved, strict=True):
            remaining = total - (time.monotonic() - started)
            if remaining <= 0:
                return {"status": "timeout", "commands": results}
            output.write(json.dumps({"argv": original}) + "\n")
            output.flush()
            try:
                completed = processes.run(
                    argv,
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    timeout=remaining,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                return {"status": "timeout", "commands": results}
            results.append({"argv": original, "exit_code": completed.returncode})
            if completed.returncode:
                return {"status": "failed", "commands": results}
    return {"status": "done", "commands": results}


STAGED, SCHEDULED, CANCELLED = "staged", "scheduled", "cancelled"
CONFIRM_SECONDS = 15
CONFIRM_POLL_SECONDS = 0.1
STALE_JOB_SECONDS = 3600
# Jobs this hook process staged; they run only once its blocking response is out (confirm_staged).
STAGED_JOBS: list[Path] = []


def confirm_staged() -> None:
    """Release this process's staged jobs to their workers; call only after the response is flushed.

    A job whose record cannot be locked or read stays staged, so its worker cancels it.
    """
    while STAGED_JOBS:
        path = STAGED_JOBS.pop()
        try:
            with file_lock(path.with_suffix(".lock")):
                job = read_json(path)
                if job.get("status") == STAGED:
                    write_json(path, dict(job, status=SCHEDULED))
        except OSError:
            continue


def check_job(job: dict) -> None:
    """Reject a job record whose prompt or workspace is not usable text.

    Args:
        job: The claimed job record.

    Raises:
        ValueError: When `prompt` or `workspace` is missing, empty, not a string, contains NUL or
            is not encodable as UTF-8.
    """
    for field in ("prompt", "workspace"):
        if not isinstance(job.get(field), str) or not job[field] or "\0" in job[field]:
            msg = "invalid job " + field
            raise ValueError(msg)
        try:
            job[field].encode("utf-8")
        except UnicodeEncodeError as exc:
            msg = "invalid job " + field
            raise ValueError(msg) from exc


class Runner:
    """Persist independent job reports and own detached worker scheduling.

    Scheduling is two-phase so a job never runs beside a prompt that went through anyway: the
    hook stages the job, and only after printing its blocking response marks it scheduled. A
    worker whose job stays staged for `confirm_seconds` cancels it.

    Args:
        directory: The runtime's state directory; jobs live in its `runner/` subdirectory.
        config: The `runner` config section.
        report_dir: The handoff store that finished jobs are published to, or None to keep them local.
        confirm_seconds: How long a worker waits for its job to be confirmed before cancelling it.
    """

    def __init__(
        self, directory: Path, config: dict, report_dir: Path | None = None, confirm_seconds: float = CONFIRM_SECONDS
    ) -> None:
        self.directory, self.config = Path(directory) / "runner", config
        self.report_dir, self.confirm_seconds = report_dir, confirm_seconds

    def schedule(
        self,
        prompt: str,
        workspace: str,
        command: list[str],
        env: dict[str, str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Stage a job and start its detached worker, at most once per cooldown.

        Args:
            prompt: The user's request the job carries out.
            workspace: The directory the job's commands run in.
            command: The fixed worker argv; `--worker <job>` is appended.
            env: The worker's environment, or None to inherit this process's.
            metadata: Extra fields stored in the job record.

        Returns:
            The staged job's path, or "" when the runner is disabled, the workspace is not a
            directory, the cooldown has not passed or the worker could not start.
        """
        if not self.config.get("enabled") or not workspace or not Path(workspace).is_dir():
            return ""
        self.directory.mkdir(parents=True, exist_ok=True)
        with file_lock(self.directory / "schedule.lock"):
            stamp = read_json(self.directory / "last.json")
            when = stamp.get("ts", 0)
            when = when if type(when) in (int, float) else 0
            if time.time() - when < self.config.get("cooldown_seconds", 60):
                return ""
            key = uuid.uuid4().hex
            path = self.directory / (key + ".json")
            write_json(
                path,
                {
                    "status": STAGED,
                    "prompt": prompt,
                    "workspace": str(Path(workspace).resolve()),
                    "created_at": time.time(),
                    "log": str(path.with_suffix(".log")),
                    **(metadata or {}),
                },
            )
            try:
                with path.with_suffix(".log").open("a") as output:
                    subprocess.Popen(  # noqa: S603  # fixed worker argv from the adapter, never model output
                        [*command, "--worker", str(path)],
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=output,
                        start_new_session=True,
                    )
            except OSError:
                path.unlink(missing_ok=True)
                return ""
            write_json(self.directory / "last.json", {"ts": time.time()})
            STAGED_JOBS.append(path)
            return str(path)

    def claim(self, path: Path) -> dict | None:
        """Wait for the hook to confirm a staged job, then mark it running; cancel it on timeout.

        Args:
            path: The job record.

        Returns:
            The running job, or None when it was cancelled or is not staged or scheduled.
        """
        expires = time.monotonic() + self.confirm_seconds
        while True:
            with file_lock(path.with_suffix(".lock")):
                job = read_json(path)
                status = job.get("status")
                if status == SCHEDULED:
                    job.update(status="running", started_at=time.time())
                    write_json(path, job)
                    return job
                if status != STAGED:
                    return None
                if time.monotonic() >= expires:
                    write_json(
                        path,
                        dict(
                            job, status=CANCELLED, finished_at=time.time(), error="the hook response was not delivered"
                        ),
                    )
                    return None
            time.sleep(CONFIRM_POLL_SECONDS)

    def work(self, path: Path, complete_plan: Callable[[str, str, float], dict | None], model: str) -> int:
        """Claim a staged job, plan it with a model, execute the validated plan and publish the result.

        Args:
            path: The job record inside this runner's directory.
            complete_plan: Called as `complete_plan(model, prompt, timeout)`; returns the plan or None.
            model: The planning model; empty means the provider's cheap tier.

        Returns:
            0 when every command succeeded; 1 when the job was not claimed, failed or timed out.

        Raises:
            ValueError: When `path` is not a job record of this runner.
        """
        path = Path(path).resolve()
        if path.parent != self.directory.resolve() or not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
            msg = "invalid job path"
            raise ValueError(msg)
        job = self.claim(path)
        if job is None:
            return 1
        with path.with_suffix(".log").open("a") as output:
            output.write(json.dumps({"status": "running"}) + "\n")
        try:
            check_job(job)
            prompt = (
                "Return a JSON command plan for this explicitly requested mechanical task. "
                "No tools, no scripts, no shell. Only narrow git status/diff/log/add/commit -m/push "
                "or exact configured commands: "
                + json.dumps(self.config.get("commands", []))
                + '. If insufficient context or unsupported task, return {"commands": []}. '
                "Workspace: " + job["workspace"] + "\nTask: " + job["prompt"]
            )
            plan = complete_plan(model, prompt, self.config.get("plan_timeout_seconds", 60))
            result = execute_plan(plan, job, self.config, path.with_suffix(".log"))
        except (ValueError, OSError) as exc:
            result = {"status": "failed", "error": str(exc)}
        job.update(result, finished_at=time.time())
        with file_lock(path.with_suffix(".lock")):
            latest = read_json(path)
            if latest.get("published"):
                job["published"] = True
            write_json(path, job)
            self.publish(path, job)
        return 0 if job["status"] == "done" else 1

    def publish(self, path: Path, job: dict) -> None:
        """Publish a finished job to the handoff store's pending reports once, and mark it published.

        Args:
            path: The job record.
            job: The finished job; marked published in place and on disk.
        """
        if self.report_dir is None or job.get("published"):
            return
        report = dict(
            job,
            id="native-" + path.stem,
            status="timed out" if job["status"] == "timeout" else job["status"],
            report=job.get("error") or json.dumps(job.get("commands", [])),
        )
        handoffs.publish_pending(report, self.report_dir, {"job": str(path), "output": str(path.with_suffix(".log"))})
        job["published"] = True
        write_json(path, job)

    def reports(self, workspace: str | None = None) -> str:
        """Publish finished jobs to the handoff store's pending/ and return the pending context.

        Args:
            workspace: Limit publishing and the context to this workspace, or None for all.

        Returns:
            The pending-report context for the chat, or "" without a handoff store.
        """
        self.publish_finished(workspace)
        if self.report_dir is None:
            return ""
        return handoffs.pending_context([workspace] if workspace else [], self.report_dir)

    def publish_finished(self, workspace: str | None = None) -> None:
        """Publish finished jobs, failing running or scheduled jobs older than `STALE_JOB_SECONDS` first.

        Args:
            workspace: Limit to jobs of this workspace, or None for all.
        """
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.json")):
            if path.name == "last.json":
                continue
            with file_lock(path.with_suffix(".lock")):
                job = read_json(path)
                if workspace and job.get("workspace") != str(Path(workspace).resolve()):
                    continue
                status = job.get("status")
                created = job.get("started_at", job.get("created_at", 0))
                created = created if type(created) in (int, float) else 0
                if status in ("running", SCHEDULED) and time.time() - created > STALE_JOB_SECONDS:
                    job.update(status="failed", error="worker did not finish before its deadline")
                    status = "failed"
                    write_json(path, job)
                if status in ("done", "failed", "timeout"):
                    self.publish(path, job)
