"""Bound local child process groups and retain subprocess-style results."""

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any


def stop_group(process: subprocess.Popen, cleanup_timeout: float | None = None) -> None:
    """Kill a child's process group and reap the child within a bounded time.

    Args:
        process: A child started in its own session, so its pid is its group id.
        cleanup_timeout: Seconds allowed for the whole cleanup; None allows 5 seconds to
            collect output and 1 more to reap.

    Raises:
        subprocess.TimeoutExpired: Without `cleanup_timeout`, the child could not be reaped in time.
    """
    deadline = time.monotonic() + cleanup_timeout if cleanup_timeout is not None else None
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.communicate(timeout=5 if deadline is None else max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # A trusted program may detach a descendant from the process group.
        # Close inherited pipes so cleanup itself cannot wait without a bound.
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
        try:
            process.wait(timeout=1 if deadline is None else max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            if deadline is None:
                raise


@contextlib.contextmanager
def alarms_deferred(*, enabled: bool) -> Iterator[None]:
    """Hold SIGALRM across the block and deliver the first one to the previous handler after it.

    A block that raises restores the handler without delivering.

    Args:
        enabled: Whether to defer; when False the block runs with the current handler.

    Yields:
        None, while alarms are deferred.
    """
    if not enabled:
        yield
        return
    handler, deferred = signal.getsignal(signal.SIGALRM), []
    signal.signal(signal.SIGALRM, lambda signum, frame: deferred.append((signum, frame)))
    try:
        yield
    finally:
        signal.signal(signal.SIGALRM, handler)
    if deferred and callable(handler):
        handler(*deferred[0])
    elif deferred and handler == signal.SIG_DFL:
        signal.raise_signal(signal.SIGALRM)


def run(
    argv: Sequence[str | Path],
    *,
    cwd: str | Path | None,
    env: Mapping[str, str] | None,
    timeout: float | None,
    input: str | bytes | None = None,  # noqa: A002
    text: bool = False,
    capture_output: bool = False,
    stdin: int | IO[Any] | None = None,
    stdout: int | IO[Any] | None = None,
    stderr: int | IO[Any] | None = None,
    shell: bool = False,
    cleanup_timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run a command like `subprocess.run`, in its own session whose process group is always killed.

    The keywords mirror `subprocess.run`. With `cleanup_timeout`, a SIGALRM that arrives while the
    child is being created is delivered once the child is owned, so a deadline never leaks it.
    A child that outlives `timeout` has its group killed and `subprocess.TimeoutExpired` propagates.

    Args:
        argv: The program and its arguments, already validated by the caller.
        cwd: Working directory for the child, or None for the current one.
        env: Complete child environment, or None to inherit.
        timeout: Seconds to wait for the child, or None to wait without a bound.
        input: Data sent to the child's stdin; sets stdin to a pipe.
        text: Whether the streams are text rather than bytes.
        capture_output: Whether to capture stdout and stderr, overriding `stdout` and `stderr`.
        stdin: The child's stdin, as for `subprocess.Popen`.
        stdout: The child's stdout, as for `subprocess.Popen`.
        stderr: The child's stderr, as for `subprocess.Popen`.
        shell: Must be False; present so a shell request fails loudly.
        cleanup_timeout: Seconds allowed for killing and reaping the group; see `stop_group`.

    Returns:
        The completed process with its return code and any captured output.

    Raises:
        ValueError: `shell` was requested.
    """
    if shell:
        msg = "shell execution forbidden"
        raise ValueError(msg)
    if input is not None:
        stdin = subprocess.PIPE
    if capture_output:
        stdout, stderr = subprocess.PIPE, subprocess.PIPE
    process = None
    try:
        with alarms_deferred(
            enabled=cleanup_timeout is not None and threading.current_thread() is threading.main_thread()
        ):
            process = subprocess.Popen(  # noqa: S603
                argv,
                cwd=cwd,
                env=env,
                text=text,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                start_new_session=True,
            )
        output, errors = process.communicate(input, timeout=timeout)
        # A direct child can exit while descendants still hold its pipes.
        stop_group(process, cleanup_timeout)
    except BaseException:
        if process is not None:
            # Cleanup must not replace the original cancellation or deadline.
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                stop_group(process, cleanup_timeout)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, output, errors)
