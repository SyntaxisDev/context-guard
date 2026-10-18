"""Optional prompt services: classifier routing, the mechanical runner and calibration.

Off unless `runner.enabled` or `classifier.enabled`. One contract on every runtime: the
adapter's provider plans, mechanical.execute_plan validates the whole plan (failing closed)
and then executes it in a detached worker. A job is staged inside the hook and released by
confirm_staged only after the hook's blocking response is out. The classifier is asked only when tokens reach
the note threshold, inside the hook deadline. Reports reach the chat through the handoff
store's pending/ directory. An empty model setting means the adapter's cheap tier.
"""
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from core import config as config_mod, deadline, handoffs, policy
from core.observation import BLOCK, NOTE, PROMPT, Decision, Observation
from core.runner import calibration, intent_labels, mechanical, prompt_services, provider as provider_mod

SKIP_PREFIXES = ("!", handoffs.BOOKKEEPING_PREFIX)
CALIBRATION_REPLY = re.compile(r"calibration\s+(apply|auto|keep)", re.IGNORECASE)
WORKER, CALIBRATE, DRY_RUN = "--worker", "--calibrate", "--dry-run"
MECHANICAL_NOTE = "This request is mechanical; delegate it to the runner role at the cheap tier."
NEW_TASK_NOTE = "This appears to be a new task; consider a fresh chat after updating the handoff."
SCHEDULE_RESERVE_SECONDS = deadline.POLICY_RESERVE_SECONDS


@dataclass(frozen=True)
class Routing:
    blocked: bool = False
    message: str = ""


def time_to_schedule() -> bool:
    """Stage a runner job only when the hook can still deliver the blocking response."""
    limit = deadline.active()
    return limit is None or limit.remaining() >= SCHEDULE_RESERVE_SECONDS


def confirm_staged(response: object, passed: object) -> None:
    """After the hook printed and flushed `response`, release its staged jobs unless it is the pass
    response (a staged job always comes with a blocking response; a pass means the prompt went on)."""
    if response == passed:
        mechanical.STAGED_JOBS.clear()
    else:
        mechanical.confirm_staged()


def enabled(config: dict) -> bool:
    return bool(config["runner"]["enabled"] or config["classifier"]["enabled"])


def applies(obs: Observation, config: dict) -> bool:
    return obs.event == PROMPT and not obs.is_subagent and enabled(config)


def route(obs: Observation, config: dict, build: Callable[[], "Services"]) -> Routing:
    """Build the services only when they apply, so a disabled runner never starts a process."""
    return build().prompt(obs) if applies(obs, config) else Routing()


def combine(decision: Decision, routing: Routing) -> Decision:
    """Add the routing message to the policy decision; a routing block replaces it."""
    if routing.blocked:
        return Decision(BLOCK, routing.message)
    if not routing.message:
        return decision
    message = "\n\n".join(part for part in (decision.message, routing.message) if part)
    return replace(decision, kind=BLOCK if decision.kind == BLOCK else NOTE, message=message)


class Services:
    """One runtime's classifier cache, calibration and runner, bound to its provider and worker argv."""

    def __init__(self, loader: config_mod.ConfigLoader, home: Path, runtime: str,
                 provider: provider_mod.Provider, cheap_model: str, worker: list[str]):
        self.loader, self.config = loader, loader.load()
        self.directory = config_mod.state_dir(self.config, home, runtime)
        self.provider, self.cheap_model, self.worker_argv = provider, cheap_model, worker
        self.cache = intent_labels.LabelCache(self.directory / "classifier_cache.json",
                                              self.config["classifier"]["cache_entries"])
        self.calibration = calibration.Calibration(loader, self.directory, self.ask, self.cache)
        self.runner = mechanical.Runner(self.directory, self.config["runner"],
                                        config_mod.handoff_dir(self.config, home))

    def ask(self, model: str, prompt: str, timeout: float) -> str | None:
        return provider_mod.complete(self.provider, model or self.cheap_model, prompt, timeout)

    def plan(self, model: str, prompt: str, timeout: float) -> dict | None:
        text = provider_mod.complete(self.provider, model or self.cheap_model, prompt, timeout,
                                     mechanical.PLAN_SCHEMA)
        return provider_mod.json_object(text)

    def prompt(self, obs: Observation) -> Routing:
        text = (obs.prompt or "").strip()
        if text.startswith(SKIP_PREFIXES) or handoffs.HANDOFF_REQUEST.search(text):
            return Routing()
        reply = CALIBRATION_REPLY.fullmatch(text)
        if reply:
            return Routing(True, self.calibration.command(reply[1].lower()))
        self.calibration.maybe_start([*self.worker_argv, CALIBRATE], provider_mod.environment())
        needed = obs.tokens is not None and obs.tokens >= policy.thresholds(self.config, obs.window).note
        found = prompt_services.decide(text, self.config, self.cache, self.ask, needed)
        prompt_services.log_decision(self.directory / "decisions.jsonl",
                                     {"ts": time.time(), "prompt": text, "tokens": obs.tokens,
                                      "model": obs.model, **found}, self.config["calibration"]["window"])
        routing = self.route(found["label"], text, obs)
        note = self.calibration.session_note()
        return replace(routing, message="\n\n".join(part for part in (note, routing.message) if part))

    def route(self, label: str | None, text: str, obs: Observation) -> Routing:
        if label == "MECHANICAL" and self.config["runner"]["enabled"] and time_to_schedule():
            job = self.runner.schedule(text, obs.cwd, self.worker_argv,
                                       provider_mod.environment(),
                                       {"runtime": obs.runtime, "session": obs.session_id})
            if job:
                return Routing(True, f"Mechanical runner scheduled this request. Report: {job}. "
                                     "Resend it starting with ! to handle it here instead.")
        if label == "MECHANICAL":
            return Routing(message=MECHANICAL_NOTE)
        if label == "NEW_TASK":
            return Routing(message=NEW_TASK_NOTE)
        return Routing(message=self.runner.reports(obs.cwd or None))

    def work(self, argv: list[str]) -> int:
        """Detached entry points: `--worker <job>` plans and executes one job, `--calibrate [--dry-run]`."""
        if len(argv) == 2 and argv[0] == WORKER:
            try:
                return self.runner.work(Path(argv[1]), self.plan, self.config["runner"]["model"])
            except ValueError:
                return 2
        if argv and argv[0] == CALIBRATE:
            return self.calibration.run(dry_run=DRY_RUN in argv[1:])
        return 2


def is_worker_call(argv: list[str]) -> bool:
    return bool(argv) and argv[0] in (WORKER, CALIBRATE)
