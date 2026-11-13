"""The hook sequence every adapter shares: process entry, fail-open guard and evaluation.

Adapters pass their native pieces as callables: payload parsing happens before `evaluate`,
`respond` renders a Decision as the native response, and `delivered` says whether that
response handed the message to the model.
"""

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO

from core import config as config_mod, deadline, policy, state as state_mod
from core.observation import COMPACT, SESSION, Decision, Observation
from core.runner import service
from core.runner.provider import INTERNAL_ENV


def read_payload(stream: BinaryIO) -> object:
    """Read one JSON hook payload, failing open.

    Args:
        stream: The hook's standard input.

    Returns:
        The decoded payload, or None when it cannot be read or decoded.
    """
    try:
        return json.load(stream)
    except OSError, ValueError, UnicodeError:
        return None


def guarded(kit: Path, work: Callable[[], dict], fallback: dict, environ: Mapping[str, str] | None = None) -> dict:
    """Run a hook's work unless it must stand aside, failing open.

    Internal runs and foreign homes pass before configuration or state is read; unreadable
    configuration, state or telemetry fails open.

    Args:
        kit: Directory the running hook code lives in.
        work: Produces the native response.
        fallback: The native pass response.
        environ: Environment to read; None reads the process environment.

    Returns:
        The response from `work`, or `fallback` for internal runs, foreign homes, ValueError and OSError.
    """
    environment = os.environ if environ is None else environ
    if environment.get(INTERNAL_ENV) == "1" or config_mod.foreign_home(kit, environment):
        return fallback
    try:
        return work()
    except ValueError, OSError:
        return fallback


def evaluate(
    obs: Observation,
    config: dict,
    home: Path,
    services: Callable[[], service.Services],
    respond: Callable[[Decision], dict],
    delivered: Callable[[dict], bool],
    session_text: Callable[[], str] = str,
    carry: Callable[[Decision], Decision] | None = None,
    adjust: Callable[[Decision, dict], Decision] | None = None,
) -> dict:
    """Prune on session start, route, evaluate policy, render, and log a note the response delivered.

    Args:
        obs: The observation from the adapter's payload.
        config: Loaded configuration.
        home: Home directory the hook acts on.
        services: Builds the runner services; called only when routing applies.
        respond: Renders a decision as the native response.
        delivered: Tells whether a native response hands the decision's message to the model.
        session_text: Produces the policy text for session and compaction context in main sessions.
        carry: Adjusts the decision before it is rendered; None renders it unchanged.
        adjust: Adjusts decision and state together before the policy transaction commits.

    Returns:
        The native response.
    """
    text = ""
    if obs.event in (SESSION, COMPACT) and not obs.is_subagent:
        state_mod.prune(config_mod.state_dir(config, home, obs.runtime), config["paths"]["prune_days"])
        text = session_text()
    routing = service.route(obs, config, services)
    if routing.blocked:
        return respond(service.combine(Decision(), routing))
    decision = service.combine(policy.evaluate(obs, config, home, text, adjust), routing)
    response = respond(carry(decision) if carry else decision)
    if delivered(response):
        policy.log_delivered(obs, decision, config, home)
    return response


def main(
    kit: Path,
    argv: list[str],
    services: Callable[[Path], service.Services],
    run: Callable[[str, object, Path], dict],
    passing: Callable[[str], dict],
) -> None:
    """The process boundary: worker dispatch, stdin read inside the deadline, one printed response.

    Args:
        kit: Directory the running hook code lives in.
        argv: Command-line arguments after the program name; the first names the hook event.
        services: Builds the runner services for a home.
        run: Answers an event name, its payload and the home with the native response.
        passing: Returns the native pass response for an event name.

    Raises:
        SystemExit: After a runner worker or calibration call, with its exit status.
    """
    home = config_mod.resolve_home(kit)
    if service.is_worker_call(argv):
        raise SystemExit(services(home).work(argv))
    event = argv[0] if argv else ""
    fallback = passing(event)
    response = deadline.boundary(
        lambda: deadline.run_within(lambda: run(event, read_payload(sys.stdin.buffer), home), fallback), fallback, event
    )
    print(json.dumps(response), flush=True)
    service.confirm_staged(response, fallback)
