"""POSIX lifecycle hook budget; manual commands and workers opt out explicitly.

Once a hook persists the state behind its response it commits: the deadline stops
interrupting so the response that matches the saved state is the one delivered.
"""

import contextvars
import signal
import sys
import time
from collections.abc import Callable
from types import FrameType, TracebackType
from typing import Self

HOOK_TIMEOUT_SECONDS = 5
HOOK_WORK_SECONDS = 4
POLICY_RESERVE_SECONDS = 1
COMMIT_RESERVE_SECONDS = 0.5
MODEL_CLEANUP_SECONDS = 0.25
ACTIVE = contextvars.ContextVar("hook_deadline", default=None)


class DeadlineExceeded(BaseException):
    """A lifecycle deadline, distinct from optional-service I/O failures."""


class HookDeadline:
    """Own an absolute monotonic deadline while preserving an existing alarm.

    Used as a context manager: entering arms SIGALRM for the earlier of the deadline and any
    alarm already pending, and exiting restores the previous handler and timer.

    Args:
        expires_at: Absolute `time.monotonic()` value at which the deadline expires.

    Attributes:
        expires_at: Absolute `time.monotonic()` value at which the deadline expires.
        previous_handler: SIGALRM handler installed before entering, restored on exit.
        previous_due: Monotonic time the pre-existing alarm is due, or None without one.
        previous_interval: Repeat interval of the pre-existing alarm, 0 when it fires once.
        committed: Whether the deadline has stopped interrupting.
    """

    def __init__(self, expires_at: float) -> None:
        self.expires_at = expires_at
        self.previous_handler: Callable[[int, FrameType | None], object] | int | None = None
        self.previous_due: float | None = None
        self.previous_interval: float = 0
        self.committed = False

    def remaining(self) -> float:
        """Return the seconds left before the deadline.

        Returns:
            The remaining seconds, never negative.
        """
        return max(0, self.expires_at - time.monotonic())

    def check(self) -> None:
        """Interrupt the hook when the deadline has passed and is not committed.

        Raises:
            DeadlineExceeded: The deadline has expired and has not been committed.
        """
        if not self.committed and self.remaining() <= 0:
            raise DeadlineExceeded

    def commit(self) -> None:
        """Stop interrupting from here on, unless too little time is left to finish the response.

        Committing twice is a no-op.

        Raises:
            DeadlineExceeded: Less than COMMIT_RESERVE_SECONDS remain.
        """
        if self.committed:
            return
        if self.remaining() < COMMIT_RESERVE_SECONDS:
            raise DeadlineExceeded
        self.committed = True
        self._arm()

    def _arm(self) -> None:
        """Set the real-time timer for the next due alarm, or clear it when none is due."""
        dues = [due for due in (None if self.committed else self.expires_at, self.previous_due) if due is not None]
        delay = max(0.000001, min(dues) - time.monotonic()) if dues else 0
        signal.setitimer(signal.ITIMER_REAL, delay)

    def _alarm(self, signum: int, frame: FrameType | None) -> None:
        """Handle SIGALRM: forward a due pre-existing alarm, then enforce the deadline.

        `DeadlineExceeded` propagates into the interrupted code once the deadline has expired.

        Args:
            signum: The signal number delivered.
            frame: The interrupted stack frame, forwarded to the previous handler.
        """
        now = time.monotonic()
        if self.previous_due is not None and now >= self.previous_due:
            if self.previous_interval:
                skipped = int((now - self.previous_due) // self.previous_interval) + 1
                self.previous_due += skipped * self.previous_interval
            else:
                self.previous_due = None
            if callable(self.previous_handler):
                self.previous_handler(signum, frame)
            elif self.previous_handler == signal.SIG_DFL:
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
                signal.raise_signal(signal.SIGALRM)
        self.check()
        self._arm()

    def __enter__(self) -> Self:
        """Install the deadline alarm, remembering the alarm already in place.

        An already expired deadline raises `DeadlineExceeded` before anything is installed.

        Returns:
            This deadline.
        """
        self.check()
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        delay, self.previous_interval = signal.getitimer(signal.ITIMER_REAL)
        self.previous_due = time.monotonic() + delay if delay else None
        signal.signal(signal.SIGALRM, self._alarm)
        self._arm()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Restore the previous SIGALRM handler and its remaining timer.

        Args:
            kind: The exception type leaving the block, or None.
            value: The exception leaving the block, or None.
            traceback: The traceback of that exception, or None.
        """
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous_handler)
        if self.previous_due is not None:
            signal.setitimer(
                signal.ITIMER_REAL, max(0.000001, self.previous_due - time.monotonic()), self.previous_interval
            )


def active() -> HookDeadline | None:
    """Return the deadline of the hook currently running in this context.

    Returns:
        The active deadline, or None outside `run_within`.
    """
    return ACTIVE.get()


def commit() -> None:
    """Commit the active hook deadline; a no-op outside `run_within`.

    `DeadlineExceeded` propagates when too little time is left to save state and respond.
    """
    limit = active()
    if limit is not None:
        limit.commit()


def boundary[T](work: Callable[[], T], fallback: T, label: str) -> T:
    """Run the hook process boundary: any unexpected error prints one stderr line and passes.

    Args:
        work: The hook body to run.
        fallback: The response returned when `work` raises an `Exception`.
        label: Hook name used in the stderr line.

    Returns:
        The result of `work`, or `fallback` when it raised.
    """
    try:
        return work()
    except Exception as exc:  # noqa: BLE001
        print(f"context-guard: {label} hook failed open: {type(exc).__name__}: {exc}", file=sys.stderr)
        return fallback


def run_within[T](work: Callable[[], T], fallback: T, seconds: float = HOOK_WORK_SECONDS) -> T:
    """Run `work` under a hook deadline that is active for its duration.

    Args:
        work: The hook body to run.
        fallback: The response returned when the deadline expires (fail open).
        seconds: The budget in seconds from now.

    Returns:
        The result of `work`, or `fallback` when the deadline expired.
    """
    try:
        with HookDeadline(time.monotonic() + seconds) as limit:
            token = ACTIVE.set(limit)
            try:
                return work()
            finally:
                ACTIVE.reset(token)
    except DeadlineExceeded:
        return fallback


def model_timeout(configured: float, deadline: HookDeadline | None = None) -> float:
    """Bound a model call so time is left for process cleanup, policy decisions and state persistence.

    An expired `deadline` raises `DeadlineExceeded` instead of returning a zero timeout.

    Args:
        configured: The configured model timeout in seconds.
        deadline: The hook deadline to fit within, or None for no bound.

    Returns:
        The timeout in seconds to give the model call, never negative.
    """
    if deadline is None:
        return configured
    deadline.check()
    return min(configured, max(0, deadline.remaining() - POLICY_RESERVE_SECONDS))
