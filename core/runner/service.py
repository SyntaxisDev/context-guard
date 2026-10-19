"""Optional prompt services: classifier routing, the mechanical runner and calibration.

Off unless `runner.enabled`, `classifier.enabled` or `calibration.enabled`; each works on its
own, and the runner turns on the classifier routing it needs. One contract on every runtime: the
adapter's provider plans, mechanical.execute_plan validates the whole plan (failing closed)
and then executes it in a detached worker. A job is staged inside the hook and released by
confirm_staged only after the hook's blocking response is out. The classifier is asked only when tokens reach
the note threshold, inside the hook deadline. Reports reach the chat through the handoff
store's pending/ directory. An empty model setting means the adapter's cheap tier.
"""

import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from core import config as config_mod, deadline, events, handoffs, policy
from core.observation import BLOCK, NOTE, PROMPT, Decision, Observation
from core.runner import calibration, intent_labels, mechanical, prompt_services, provider as provider_mod

SKIP_PREFIXES = ("!", handoffs.BOOKKEEPING_PREFIX)
CALIBRATION_REPLY = re.compile(r"calibration\s+(apply|auto|keep)", re.IGNORECASE)
WORKER, CALIBRATE, DRY_RUN = "--worker", "--calibrate", "--dry-run"
WORKER_ARGC = 2
MECHANICAL_NOTE = "This request is mechanical; delegate it to the runner role at the cheap tier."
NEW_TASK_NOTE = "This appears to be a new task; consider a fresh chat after updating the handoff."
SCHEDULE_RESERVE_SECONDS = deadline.POLICY_RESERVE_SECONDS


@dataclass(frozen=True)
class Routing:
    """The prompt services' answer to one prompt.

    Attributes:
        blocked: Whether the prompt is answered here instead of reaching the model.
        message: The block reason or the note added to the policy decision; empty for none.
    """

    blocked: bool = False
    message: str = ""


def time_to_schedule() -> bool:
    """Return whether the hook can still deliver a blocking response after staging a runner job.

    Returns:
        True outside a deadline or when at least `SCHEDULE_RESERVE_SECONDS` remain.
    """
    limit = deadline.active()
    return limit is None or limit.remaining() >= SCHEDULE_RESERVE_SECONDS


def confirm_staged(response: object, passed: object) -> None:
    """Release this process's staged jobs once the hook printed and flushed its response.

    A staged job always comes with a blocking response, so when `response` is the pass
    response the prompt went on and the jobs are dropped instead.

    Args:
        response: The native response the hook printed.
        passed: The runtime's native pass response.
    """
    if response == passed:
        mechanical.STAGED_JOBS.clear()
    else:
        mechanical.confirm_staged()


def routes(config: dict) -> bool:
    """Return whether prompts are routed by the classifier; the runner needs its label, so it enables routing too.

    Args:
        config: The merged configuration.

    Returns:
        True when the runner or the classifier is enabled.
    """
    return bool(config["runner"]["enabled"] or config["classifier"]["enabled"])


def enabled(config: dict) -> bool:
    """Return whether any prompt service is enabled.

    Args:
        config: The merged configuration.

    Returns:
        True when routing or calibration is enabled.
    """
    return routes(config) or bool(config["calibration"]["enabled"])


def applies(obs: Observation, config: dict) -> bool:
    """Return whether the prompt services handle an observation.

    Args:
        obs: The hook observation.
        config: The merged configuration.

    Returns:
        True for a root-chat prompt while any prompt service is enabled.
    """
    return obs.event == PROMPT and not obs.is_subagent and enabled(config)


def route(obs: Observation, config: dict, build: Callable[[], Services]) -> Routing:
    """Route a prompt, building the services only when they apply so a disabled runner never starts a process.

    Args:
        obs: The hook observation.
        config: The merged configuration.
        build: Builds the runtime's services on demand.

    Returns:
        The services' routing, or an empty routing when they do not apply.
    """
    return build().prompt(obs) if applies(obs, config) else Routing()


def combine(decision: Decision, routing: Routing) -> Decision:
    """Add the routing message to the policy decision; a routing block replaces it.

    Args:
        decision: The policy decision for the same observation.
        routing: The prompt services' routing.

    Returns:
        A block with the routing message when routing blocked; otherwise the decision with the
        routing message appended, upgraded to a note unless it already blocks.
    """
    if routing.blocked:
        return Decision(BLOCK, routing.message)
    if not routing.message:
        return decision
    message = "\n\n".join(part for part in (decision.message, routing.message) if part)
    return replace(decision, kind=BLOCK if decision.kind == BLOCK else NOTE, message=message)


class Services:
    """One runtime's classifier cache, calibration and runner, bound to its provider and worker argv.

    Args:
        loader: The runtime's configuration loader.
        home: The home directory that state and handoff paths resolve against.
        runtime: The runtime name used for its state directory.
        provider: The runtime's CLI provider for model calls.
        cheap_model: The model used when a model setting is empty.
        worker: The argv that re-enters the adapter's hook script for detached work.
    """

    def __init__(
        self,
        loader: config_mod.ConfigLoader,
        home: Path,
        runtime: str,
        provider: provider_mod.Provider,
        cheap_model: str,
        worker: list[str],
    ) -> None:
        self.loader, self.config = loader, loader.load()
        self.directory = config_mod.state_dir(self.config, home, runtime)
        self.provider, self.cheap_model, self.worker_argv = provider, cheap_model, worker
        self.cache = intent_labels.LabelCache(
            self.directory / "classifier_cache.json", self.config["classifier"]["cache_entries"]
        )
        self.calibration = calibration.Calibration(loader, self.directory, self.ask, self.cache)
        self.runner = mechanical.Runner(
            self.directory, self.config["runner"], config_mod.handoff_dir(self.config, home)
        )

    def ask(self, model: str, prompt: str, timeout: float) -> str | None:
        """Ask a model for a free-text answer.

        Args:
            model: The model to call; empty means the cheap tier.
            prompt: The full prompt text.
            timeout: The longest the call may take, in seconds.

        Returns:
            The answer text, or None when the call failed.
        """
        return provider_mod.complete(self.provider, model or self.cheap_model, prompt, timeout)

    def plan(self, model: str, prompt: str, timeout: float) -> dict | None:
        """Ask a model for a command plan matching `mechanical.PLAN_SCHEMA`.

        Args:
            model: The model to call; empty means the cheap tier.
            prompt: The full planning prompt.
            timeout: The longest the call may take, in seconds.

        Returns:
            The plan object, or None when the call failed or returned no JSON object.
        """
        text = provider_mod.complete(self.provider, model or self.cheap_model, prompt, timeout, mechanical.PLAN_SCHEMA)
        return provider_mod.json_object(text)

    def prompt(self, obs: Observation) -> Routing:
        """Route one root-chat prompt: calibration replies, classification, runner scheduling and notes.

        Args:
            obs: The prompt observation.

        Returns:
            A block for a handled calibration reply or a scheduled runner job; otherwise the
            classifier note, pending reports and any one-time calibration note.
        """
        text = (obs.prompt or "").strip()
        if text.startswith(SKIP_PREFIXES) or handoffs.HANDOFF_REQUEST.search(text):
            return Routing()
        reply = CALIBRATION_REPLY.fullmatch(text)
        if reply and self.calibration.awaiting_reply():
            return Routing(blocked=True, message=self.calibration.command(reply[1].lower()))
        self.calibration.maybe_start([*self.worker_argv, CALIBRATE], provider_mod.environment())
        needed = obs.tokens is not None and obs.tokens >= policy.thresholds(self.config, obs.window).note
        found = prompt_services.decide(
            text, self.config["classifier"], self.cache, self.ask, ask=needed and routes(self.config)
        )
        if self.config["calibration"]["enabled"]:
            self.log_decision(text, obs, found)
        routing = self.route(found["label"], text, obs)
        note = self.calibration.session_note()
        return replace(routing, message="\n\n".join(part for part in (note, routing.message) if part))

    def log_decision(self, text: str, obs: Observation, found: dict) -> None:
        """Append one classifier decision to the calibration log.

        Args:
            text: The stripped prompt; it is stored truncated to one character over `max_chars`.
            obs: The prompt observation.
            found: The decision record from `prompt_services.decide`.
        """
        record = {
            "ts": time.time(),
            "prompt": text[: self.config["classifier"]["max_chars"] + 1],
            "chars": len(text),
            "tokens": obs.tokens,
            "model": obs.model,
            **found,
        }
        events.append(self.directory / calibration.DECISIONS_FILE, [record], self.config["calibration"]["window"])

    def route(self, label: str | None, text: str, obs: Observation) -> Routing:
        """Turn a classifier label into a routing.

        Args:
            label: The classifier label, or None when there is none.
            text: The stripped prompt.
            obs: The prompt observation.

        Returns:
            A block when a mechanical job was scheduled, a note for a mechanical or new-task
            label, otherwise the pending runner reports for the workspace.
        """
        if label == "MECHANICAL" and self.config["runner"]["enabled"] and time_to_schedule():
            job = self.runner.schedule(
                text,
                obs.cwd,
                self.worker_argv,
                provider_mod.environment(),
                {"runtime": obs.runtime, "session": obs.session_id},
            )
            if job:
                return Routing(
                    blocked=True,
                    message=f"Mechanical runner scheduled this request. Report: {job}. "
                    "Resend it starting with ! to handle it here instead.",
                )
        if label == "MECHANICAL":
            return Routing(message=MECHANICAL_NOTE)
        if label == "NEW_TASK":
            return Routing(message=NEW_TASK_NOTE)
        return Routing(message=self.runner.reports(obs.cwd or None))

    def work(self, argv: list[str]) -> int:
        """Run a detached entry point: `--worker <job>` plans and executes one job, `--calibrate [--dry-run]`.

        Args:
            argv: The hook script's arguments after the program name.

        Returns:
            The entry point's exit status; 2 for unknown arguments or an invalid job path.
        """
        if len(argv) == WORKER_ARGC and argv[0] == WORKER:
            try:
                return self.runner.work(Path(argv[1]), self.plan, self.config["runner"]["model"])
            except ValueError:
                return 2
        if argv and argv[0] == CALIBRATE:
            return self.calibration.run(dry_run=DRY_RUN in argv[1:])
        return 2


def build(
    runtime: str, home: Path, adapter_dir: Path, provider: Callable[[dict], provider_mod.Provider], model_key: str
) -> Services:
    """Build an adapter's services from its config layers, provider, cheap tier and hooks.py worker.

    Args:
        runtime: The runtime name.
        home: The home directory that config, state and handoff paths resolve against.
        adapter_dir: The adapter directory holding `config.json` and `hooks.py`.
        provider: Builds the runtime's provider from the merged configuration.
        model_key: The key of the model name in the cheap tier's profile.

    Returns:
        The runtime's services.
    """
    loader = config_mod.ConfigLoader(config_mod.sources_for(runtime, home, adapter_dir / "config.json"))
    config = loader.load()
    return Services(
        loader,
        home,
        runtime,
        provider(config),
        config["tiers"]["cheap"][model_key],
        [sys.executable, str(adapter_dir / "hooks.py")],
    )


def is_worker_call(argv: list[str]) -> bool:
    """Return whether hook arguments request a detached entry point.

    Args:
        argv: The hook script's arguments after the program name.

    Returns:
        True when the first argument is `--worker` or `--calibrate`.
    """
    return bool(argv) and argv[0] in (WORKER, CALIBRATE)
