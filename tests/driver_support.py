"""Test-only semantic driver protocol and independently validated process execution."""

import importlib
import json
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from unittest import mock

from core.runner import mechanical, service
from tests import harness

DEADLINE_LIMIT_SECONDS = 2


@dataclass(frozen=True)
class Outcome:
    """Native output with separate model-facing text and user-facing refusal text.

    Attributes:
        raw: Independently validated native response.
        text: Text delivered to the model, excluding user-only notifications.
        blocked: Whether the submitted prompt was refused.
        channel: Native delivery channel carrying the model text or refusal.
        user_text: User-facing refusal text that never counts as model delivery.
    """

    raw: dict
    text: str
    blocked: bool
    channel: str
    user_text: str = ""


class DriverProtocol(Protocol):
    """The native translations consumed by shared semantic scenarios."""

    runtime: str
    home: Path
    project: Path
    session: str
    window: int

    def payload(self, action: str, **overrides: object) -> dict:
        """Materialize a recorded native payload for an action.

        Args:
            action: Semantic action to translate.
            **overrides: Explicit native payload overrides.
        """
        ...

    def decode(self, raw: dict) -> Outcome:
        """Decode validated native output independently of production renderers.

        Args:
            raw: Schema-validated native output.
        """
        ...

    def tokens(self, count: int, *, leaf: bool = False) -> None:
        """Write native telemetry for the root or child.

        Args:
            count: Context tokens to record.
            leaf: Whether to write child telemetry.
        """
        ...

    def response_problems(self, event: str, raw: object) -> list[str]:
        """Validate parsed output against independent native response schemas."""
        ...

    def fire(self, action: str, **overrides: object) -> Outcome:
        """Execute a semantic action through a validated native subprocess."""
        ...

    def deliver_prompt(self) -> Outcome:
        """Return direct delivery or advance the native deferred-delivery recipe."""
        ...

    def events(self) -> list[dict]:
        """Read observed native hook events."""
        ...

    def handoff(self) -> Path:
        """Seed an open handoff in the supplied temporary home."""
        ...

    def arm_recorded(self, data: dict, *, subagent: bool) -> None:
        """Write telemetry for an exhaustive recorded native payload case."""
        ...

    def registered_commands(self) -> dict[str, str]:
        """Inspect exact installed native commands."""
        ...

    def install(self) -> subprocess.CompletedProcess[str]:
        """Install through the public CLI and select registered execution."""
        ...

    def configure_access(self) -> None:
        """Configure explicit workspace access after installation."""
        ...

    def uninstall(self) -> subprocess.CompletedProcess[str]:
        """Uninstall through the public CLI."""
        ...

    def doctor(self) -> subprocess.CompletedProcess[str]:
        """Run public installation health checks."""
        ...

    def dry_run(self) -> subprocess.CompletedProcess[str]:
        """Plan an install without mutating native files."""
        ...

    def installed_roles(self) -> list[Path]:
        """Inspect installed native role paths."""
        ...

    def installed_skills(self) -> list[Path]:
        """Inspect installed skill bodies."""
        ...

    def access_config(self) -> dict:
        """Parse native access configuration independently."""
        ...

    def access_grants(self) -> set[str]:
        """Extract effective native writable directory grants."""
        ...

    def role_models(self) -> dict[str, str]:
        """Parse each installed role's configured model."""
        ...

    def policy_text(self) -> str:
        """Read native model-facing startup or installed policy."""
        ...

    def silence(self) -> Outcome:
        """Exercise controlled native background activity and silence delivery."""
        ...

    def wake(self) -> dict:
        """Return observed wake evidence and explicit lifecycle limitations."""
        ...

    def runner_contract(self) -> dict[str, bool]:
        """Exercise native runner model pinning and service validation."""
        ...

    def malformed(self) -> Outcome:
        """Exercise the real process entry with malformed stdin."""
        ...

    def corrupt_telemetry(self) -> None:
        """Corrupt native telemetry without editing session policy state."""
        ...

    def deadline_passes(self) -> bool:
        """Observe a stalled-stdin pass within the bounded deadline."""
        ...

    def exception_passes(self) -> bool:
        """Observe native pass output after an injected handler error."""
        ...

    def seed_user_files(self) -> dict[Path, bytes]:
        """Seed user content and return exact preservation snapshots."""
        ...

    def introduce_drift(self) -> None:
        """Change installed kit content for doctor drift checks."""
        ...


def load_driver(runtime: str, home: Path, project: Path, session: str) -> DriverProtocol:
    """Load an adapter-owned driver with explicit temporary paths.

    Args:
        runtime: Registered adapter identity.
        home: Explicit temporary home.
        project: Explicit temporary project.
        session: Isolated session identity.

    Returns:
        The observed native result.
    """
    module = importlib.import_module(f"adapters.{runtime}.tests.driver")
    return module.Driver(home, project, session)


class Operations:
    """Common test operations composed with adapter-owned native translation.

    Args:
        home: Explicit temporary home; no live runtime homes are read.
        project: Temporary project directory used as native workspace input.
        session: Isolated native session identifier.
    """

    runtime: str
    window: int
    fixtures: dict[str, str]
    native_file: str
    response_runtime: str
    grouped_hooks = True
    command_key = "command"
    deferred_prompt = False
    runtime_cwd = False
    toml_config = False

    def __init__(self, home: Path, project: Path, session: str) -> None:
        self.home, self.project, self.session = home, project, session
        self.agent = "agent-conformance"
        self.call = "call-conformance"
        self.transcript = home / f".{self.runtime}/sessions/rollout-2026-10-18T09-00-00-{session}.jsonl"
        self.last_prompt: Outcome | None = None
        self.commands: dict[str, str] | None = None
        self.now: float | None = None

    def payload(self, action: str, **overrides: object) -> dict:
        """Materialize native data in each adapter.

        Args:
            action: Semantic action to translate.
            **overrides: Explicit native payload overrides.
        """
        raise NotImplementedError

    def decode(self, raw: dict) -> Outcome:
        """Decode native output in each adapter.

        Args:
            raw: Schema-validated native output.
        """
        raise NotImplementedError

    def tokens(self, count: int, *, leaf: bool = False) -> None:
        """Write telemetry in each adapter.

        Args:
            count: Context tokens to record.
            leaf: Whether to write child telemetry.
        """
        raise NotImplementedError

    def response_problems(self, event: str, raw: object) -> list[str]:
        """Validate native output using the adapter's independently recorded schema family.

        Args:
            event: Native hook event being answered.
            raw: Parsed native output to validate.

        Returns:
            Native schema violations; empty when output is valid. A new schema family
            overrides this operation in its adapter-owned driver.
        """
        return harness.response_problems(self.response_runtime, event, raw)

    def event_name(self, _action: str, data: dict) -> str:
        """Read the native event identity for an action.

        Args:
            _action: Semantic action being invoked.
            data: Materialized native payload.

        Returns:
            The event embedded in the native payload by default.
        """
        return data["hook_event_name"]

    def fire(self, action: str, **overrides: object) -> Outcome:
        """Run the real hook process, validating status, JSON count and native schema.

        Args:
            action: Semantic action to translate.
            **overrides: Explicit native payload overrides.

        Returns:
            The observed native result.

        Raises:
            AssertionError: When the native operation violates its contract.
        """
        data = self.payload(action, **overrides)
        event = self.event_name(action, data)
        command = self.commands[event] if self.commands is not None else harness.source_hook(self.runtime, event)
        if self.now is not None:
            command = self.timed_command(event)
        run = harness.run_hook(command, data, self.home, self.invocation_cwd())
        if run.returncode != 0 or run.stderr:
            msg = f"{self.runtime}/{event}: exit={run.returncode}, stderr={run.stderr!r}"
            raise AssertionError(msg)
        if len(run.stdout.splitlines()) != 1:
            msg = f"expected one native response, got {run.stdout!r}"
            raise AssertionError(msg)
        raw = run.response()
        problems = self.response_problems(event, raw)
        if problems:
            raise AssertionError(problems)
        outcome = self.decode(raw)
        if action == "prompt":
            self.last_prompt = outcome
        return outcome

    def timed_command(self, event: str) -> list[str]:
        """Run a real hook with only its native activity clock controlled.

        Args:
            event: Native hook event name.

        Returns:
            The observed native result.
        """
        script = (
            "import runpy,sys; from unittest.mock import patch; "
            f"sys.path.insert(0,{str(harness.ROOT)!r}); "
            f"sys.argv=['hooks.py',{event!r}]; "
            f"p=patch('adapters.{self.runtime}.subagents.clock',return_value={self.now!r}); "
            "p.start(); "
            f"runpy.run_path({str(harness.ROOT / 'adapters' / self.runtime / 'hooks.py')!r},run_name='__main__')"
        )
        return [sys.executable, "-c", script]

    def invocation_cwd(self) -> Path:
        """Return the native invocation cwd, including Cursor's runtime directory.

        Returns:
            The observed native result.
        """
        if self.runtime_cwd:
            directory = self.home / ".cursor"
            directory.mkdir(exist_ok=True)
            return directory
        return self.project

    def deliver_prompt(self) -> Outcome:
        """Advance Cursor to its declared delivery opportunity; preserve direct output.

        Returns:
            The observed native result.

        Raises:
            AssertionError: When the native operation violates its contract.
        """
        if self.deferred_prompt:
            return self.fire("tool")
        if self.last_prompt is None:
            msg = "deliver_prompt requires a preceding prompt action"
            raise AssertionError(msg)
        return self.last_prompt

    def events(self) -> list[dict]:
        """Read the adapter's observed event log without changing policy state.

        Returns:
            The observed native result.
        """
        return harness.events(self.home, self.runtime)

    def handoff(self) -> Path:
        """Seed a real open handoff for the current project.

        Returns:
            The observed native result.
        """
        return harness.seed_handoff(self.home, self.project)

    def fixture_events(self) -> set[str]:
        """Derive the native event inventory from recorded payloads.

        Returns:
            The observed native result.
        """
        return {item.event for item in harness.recorded(self.runtime)}

    def registered_commands(self) -> dict[str, str]:
        """Read exact installed commands independently of production mappings.

        Returns:
            The observed native result.
        """
        hooks = json.loads((self.home / self.native_file).read_text())["hooks"]
        kit = str(self.home / ".context-guard/kit")
        commands = {}
        for event, groups in hooks.items():
            entries = groups if not self.grouped_hooks else [hook for group in groups for hook in group["hooks"]]
            for entry in entries:
                command = entry.get(self.command_key, "")
                if kit in command:
                    commands[event] = command
        return commands

    def cli(self, operation: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Execute a CLI lifecycle operation against the supplied temporary home.

        Args:
            operation: Explicit operation input.
            *args: Explicit operation input.

        Returns:
            The observed native result.
        """
        return subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(harness.ROOT / "cli.py"),
                operation,
                "--runtime",
                self.runtime,
                "--home",
                str(self.home),
                *args,
            ],
            capture_output=True,
            text=True,
            env=harness.hook_env(self.home),
            timeout=300,
            check=False,
        )

    def install(self) -> subprocess.CompletedProcess[str]:
        """Install and select the exact native registered commands for later actions.

        Returns:
            The observed native result.
        """
        result = self.cli("install")
        if result.returncode == 0:
            self.commands = self.registered_commands()
        return result

    def configure_access(self) -> None:
        """Use the access configured by the normal installation."""

    def uninstall(self) -> subprocess.CompletedProcess[str]:
        """Uninstall through the public CLI.

        Returns:
            The observed native result.
        """
        return self.cli("uninstall")

    def doctor(self) -> subprocess.CompletedProcess[str]:
        """Inspect installation health through the public CLI.

        Returns:
            The observed native result.
        """
        return self.cli("doctor")

    def dry_run(self) -> subprocess.CompletedProcess[str]:
        """Plan an install without changing native files.

        Returns:
            The observed native result.
        """
        return self.cli("install", "--dry-run")

    def installed_roles(self) -> list[Path]:
        """Find installed native role files.

        Returns:
            The observed native result.
        """
        return sorted((self.home / f".{self.runtime}/agents").glob("*"))

    def installed_skills(self) -> list[Path]:
        """Find installed shared skill bodies.

        Returns:
            The observed native result.
        """
        return sorted((self.home / f".{self.runtime}/skills").glob("*/SKILL.md"))

    def access_config(self) -> dict:
        """Parse native access settings without using the production mapping.

        Returns:
            The observed native result.
        """
        if self.toml_config:
            return tomllib.loads((self.home / f".{self.runtime}/config.toml").read_text())
        return json.loads((self.home / self.native_file).read_text())

    def seed_user_files(self) -> dict[Path, bytes]:
        """Seed representative native user content and return preservation snapshots.

        Returns:
            The observed native result.
        """
        data = {"hooks": {"Stop" if self.grouped_hooks else "stop": []}, "user_setting": "preserve me"}
        target = self.home / self.native_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=3) + "\n")
        files = {target: target.read_bytes()}
        if self.toml_config:
            name = f".{self.runtime}/config.toml"
            path = self.home / name
            path.write_text('model = "user-model"\n')
            files[path] = path.read_bytes()
        return files

    def malformed(self) -> Outcome:
        """Send deliberately malformed stdin through the real process boundary.

        Returns:
            The observed native result.

        Raises:
            AssertionError: When the native operation violates its contract.
        """
        event = self.event_name("prompt", self.payload("prompt"))
        run = harness.run_hook(harness.source_hook(self.runtime, event), "{", self.home, self.project)
        if (run.returncode, run.stderr) != (0, "") or len(run.stdout.splitlines()) != 1:
            raise AssertionError(run)
        raw = run.response()
        if problems := self.response_problems(event, raw):
            raise AssertionError(problems)
        return self.decode(raw)

    def corrupt_telemetry(self) -> None:
        """Replace the native telemetry bytes with invalid data."""
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text('{"type": invalid telemetry\n')

    def deadline_passes(self) -> bool:
        """Exercise stalled stdin inside a shortened real process deadline.

        Returns:
            The observed native result.

        Raises:
            AssertionError: When the native operation violates its contract.
        """
        event = self.event_name("prompt", self.payload("prompt"))
        script = (
            "import functools,runpy,sys,time; from unittest.mock import Mock; "
            f"sys.path.insert(0,{str(harness.ROOT)!r}); "
            "from core import deadline; "
            "deadline.run_within=functools.partial(deadline.run_within,seconds=0.1); "
            "sys.stdin=Mock(buffer=Mock(read=lambda *_: time.sleep(5) or b'{}')); "
            f"sys.argv=['hooks.py',{event!r}]; "
            f"runpy.run_path({str(harness.ROOT / 'adapters' / self.runtime / 'hooks.py')!r},run_name='__main__')"
        )
        started = time.monotonic()
        try:
            run = harness.run_hook(
                [sys.executable, "-c", script], {}, self.home, self.project, timeout=DEADLINE_LIMIT_SECONDS
            )
        except subprocess.TimeoutExpired as error:
            msg = "stalled stdin exceeded the subprocess deadline"
            raise AssertionError(msg) from error
        if time.monotonic() - started >= DEADLINE_LIMIT_SECONDS:
            msg = "stalled stdin exceeded the shortened deadline"
            raise AssertionError(msg)
        if (run.returncode, run.stderr) != (0, "") or len(run.stdout.splitlines()) != 1:
            raise AssertionError(run)
        raw = run.response()
        if problems := self.response_problems(event, raw):
            raise AssertionError(problems)
        outcome = self.decode(raw)
        return not outcome.blocked and not outcome.text

    def introduce_drift(self) -> None:
        """Change an installed kit file so doctor must report drift."""
        path = self.installed_roles()[0]
        path.write_text(path.read_text() + "\nUnmanaged drift.\n")

    def role_models(self) -> dict[str, str]:
        """Parse each installed role's native model declaration.

        Returns:
            The observed native result.
        """
        found = {}
        for path in self.installed_roles():
            if path.suffix == ".toml":
                found[path.stem] = tomllib.loads(path.read_text())["model"]
            else:
                for line in path.read_text().splitlines():
                    if line.startswith("model:"):
                        found[path.stem] = line.partition(":")[2].strip().strip('"')
        return found

    def policy_text(self) -> str:
        """Return model-facing policy from startup or the installed native rule.

        Returns:
            The observed native result.
        """
        return self.fire("start").text

    def exception_passes(self) -> bool:
        """Inject a handler error while preserving the real native process entry.

        Returns:
            The observed native result.

        Raises:
            AssertionError: When the native operation violates its contract.
        """
        event = self.event_name("prompt", self.payload("prompt"))
        script = (
            "import runpy,sys; from unittest.mock import patch; "
            f"sys.path.insert(0,{str(harness.ROOT)!r}); "
            f"sys.argv=['hooks.py',{event!r}]; "
            f"m=runpy.run_path({str(harness.ROOT / 'adapters' / self.runtime / 'hooks.py')!r}); "
            "f=m['main']; f.__globals__['handle']=lambda *_: (_ for _ in ()).throw(AttributeError('boom')); f()"
        )
        run = harness.run_hook([sys.executable, "-c", script], {}, self.home, self.project)
        if run.returncode or len(run.stdout.splitlines()) != 1 or run.stderr.count("\n") != 1:
            raise AssertionError(run)
        raw = run.response()
        if problems := self.response_problems(event, raw):
            raise AssertionError(problems)
        outcome = self.decode(raw)
        return not outcome.blocked and not outcome.text and "AttributeError: boom" in run.stderr

    def runner_roundtrip(self, hooks: object, runner: object, staged: list[Path], index: int) -> tuple:
        """Send a native prompt, confirm its staged job and run its worker.

        Args:
            hooks: This adapter's real hook module.
            runner: This adapter's real runner module.
            staged: Worker paths captured by the fake process launcher.
            index: The number of jobs already sent.

        Returns:
            Staged and confirmed statuses, worker exit code and job record.

        Raises:
            AssertionError: When the native response or staging is incorrect.
        """
        data = self.payload("prompt", prompt=f"git status and commit task {index}")
        event = self.event_name("prompt", data)
        raw = hooks.run(event, data, self.home)
        if problems := self.response_problems(event, raw):
            raise AssertionError(problems)
        outcome = self.decode(raw)
        if not outcome.blocked or "Mechanical runner scheduled" not in str(raw):
            raise AssertionError(raw)
        if len(staged) != index + 1:
            msg = "runner did not stage a worker"
            raise AssertionError(msg)
        path = staged[-1]
        staged_status = json.loads(path.read_text())["status"]
        service.confirm_staged(raw, hooks.passing(event) if self.runtime == "cursor" else {})
        confirmed_status = json.loads(path.read_text())["status"]
        code = runner.services(self.home).work(["--worker", str(path)])
        return staged_status, confirmed_status, code, json.loads(path.read_text())

    def runner_contract(self) -> dict[str, bool]:
        """Drive native prompt routing and the confirmed worker with fake transports.

        Returns:
            Observed routing, model pinning, staging, execution and rejection.
        """
        hooks = importlib.import_module(f"adapters.{self.runtime}.hooks")
        runner = importlib.import_module(f"adapters.{self.runtime}.runner")
        shipped = json.loads((harness.ROOT / "adapters" / self.runtime / "config.json").read_text())
        expected_model = shipped["tiers"]["cheap"].get("model") or shipped["tiers"]["cheap"]["call"]
        calls: list[list[str]] = []
        executions: list[list[str]] = []
        staged: list[Path] = []
        plan = {"commands": [["git", "status"]]}

        def transport(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "stdout" in kwargs:
                executions.append(argv)
                return subprocess.CompletedProcess(argv, 0)
            calls.append(argv)
            text = json.dumps(plan) if len(calls) % 2 == 0 else "MECHANICAL"
            scratch = Path(kwargs["cwd"])
            if self.runtime == "codex":
                (scratch / "answer.txt").write_text(text)
            if self.runtime == "copilot":
                log = scratch / "copilot-home/session-state/fake/events.jsonl"
                log.parent.mkdir(parents=True)
                log.write_text('{"type":"session.start","data":{}}\n')
            stdout = json.dumps({"result": text}) if self.runtime in {"claude", "cursor"} else text
            if self.runtime == "codex":
                stdout = '{"item":{"type":"agent_message"}}'
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        def spawn(argv: list[str], **_kwargs: object) -> None:
            staged.append(Path(argv[-1]))

        environment = {
            "HOME": str(self.home),
            "CONTEXT_GUARD_HOME": str(self.home),
            "COPILOT_HOME": str(self.home / ".copilot"),
            "CONTEXT_GUARD_INTERNAL": "0",
            "CONTEXT_GUARD_RUNNER_ENABLED": "true",
            "CONTEXT_GUARD_RUNNER_COOLDOWN_SECONDS": "0",
        }
        self.tokens(int(self.window * 0.7))
        observations = []
        try:
            with (
                mock.patch.dict(os.environ, environment),
                mock.patch("core.runner.provider.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
                mock.patch("core.runner.provider.processes.run", side_effect=transport),
                mock.patch("core.runner.mechanical.subprocess.Popen", side_effect=spawn),
            ):
                for index, proposed in enumerate(
                    (
                        {"commands": [["git", "status"]]},
                        {"commands": [["git", "status"], ["sh", "-c", "false"]]},
                    )
                ):
                    plan = proposed
                    observations.append(self.runner_roundtrip(hooks, runner, staged, index))
        finally:
            mechanical.STAGED_JOBS.clear()
        valid, invalid = observations
        return {
            "model_pinned": len(calls) == 2 * len(observations)
            and all("--model" in argv and argv[argv.index("--model") + 1] == expected_model for argv in calls),
            "routed": len(staged) == len(observations) and valid[0] == invalid[0] == "staged",
            "confirmed": valid[1] == invalid[1] == "scheduled",
            "executed": valid[2] == 0 and valid[3]["status"] == "done" and len(executions) == 1,
            "invalid_command_rejected": invalid[2] == 1
            and invalid[3]["status"] == "failed"
            and len(invalid[3].get("commands", [])) == 0,
        }

    def silence(self) -> Outcome:
        """Reject invocation when the runtime declares no silence-note implementation.

        Raises:
            NotImplementedError: When a gap scenario is incorrectly executed.
        """
        msg = "silence notes have no implementation for this declared capability gap"
        raise NotImplementedError(msg)

    def wake(self) -> dict:
        """Reject invocation when the runtime has no idle-parent wake mechanism.

        Raises:
            NotImplementedError: When a gap scenario is incorrectly executed.
        """
        msg = "idle-parent wake has no mechanism for this declared capability gap"
        raise NotImplementedError(msg)

    def access_grants(self) -> set[str]:
        """Read effective native writable-root grants independently of install rendering.

        Returns:
            Native writable roots normalized to absolute directory paths.
        """
        data = self.access_config()
        if self.toml_config:
            return set(data.get("sandbox_workspace_write", {}).get("writable_roots", []))
        grants = set()
        for rule in data.get("permissions", {}).get("allow", []):
            if rule.startswith("Edit(/") and rule.endswith("/**)"):
                grants.add(rule.removeprefix("Edit(/").removesuffix("/**)"))
        return grants
