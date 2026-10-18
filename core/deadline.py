"""POSIX lifecycle hook budget; manual commands and workers opt out explicitly.

Once a hook persists the state behind its response it commits: the deadline stops
interrupting so the response that matches the saved state is the one delivered.
"""

import contextvars
import signal
import sys
import time

HOOK_TIMEOUT_SECONDS = 5
HOOK_WORK_SECONDS = 4
POLICY_RESERVE_SECONDS = 1
COMMIT_RESERVE_SECONDS = 0.5
MODEL_CLEANUP_SECONDS = 0.25
ACTIVE = contextvars.ContextVar("hook_deadline", default=None)


class DeadlineExceeded(BaseException):
    """A lifecycle deadline, distinct from optional-service I/O failures."""


class HookDeadline:
    """Own an absolute monotonic deadline while preserving an existing alarm."""
    def __init__(self, expires_at):
        self.expires_at = expires_at
        self.previous_due = None
        self.previous_interval = 0
        self.committed = False

    def remaining(self):
        return max(0, self.expires_at - time.monotonic())

    def check(self):
        if not self.committed and self.remaining() <= 0:
            raise DeadlineExceeded()

    def commit(self):
        """Stop interrupting from here on, unless too little time is left to finish the response."""
        if self.committed:
            return
        if self.remaining() < COMMIT_RESERVE_SECONDS:
            raise DeadlineExceeded()
        self.committed = True
        self._arm()

    def _arm(self):
        dues = [due for due in (None if self.committed else self.expires_at, self.previous_due) if due is not None]
        delay = max(0.000001, min(dues) - time.monotonic()) if dues else 0
        signal.setitimer(signal.ITIMER_REAL, delay)

    def _alarm(self, signum, frame):
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

    def __enter__(self):
        self.check()
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        delay, self.previous_interval = signal.getitimer(signal.ITIMER_REAL)
        self.previous_due = time.monotonic() + delay if delay else None
        signal.signal(signal.SIGALRM, self._alarm)
        self._arm()
        return self

    def __exit__(self, kind, value, traceback):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous_handler)
        if self.previous_due is not None:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, self.previous_due - time.monotonic()),
                             self.previous_interval)


def active():
    """The deadline of the hook currently running in this context, or None outside run_within."""
    return ACTIVE.get()


def commit():
    """Commit the active hook deadline (no-op outside run_within); raises when too late to save."""
    limit = active()
    if limit is not None:
        limit.commit()


def boundary(work, fallback, label):
    """The hook process boundary: any unexpected error prints one stderr line and passes."""
    try:
        return work()
    except Exception as exc:
        print(f"context-guard: {label} hook failed open: {type(exc).__name__}: {exc}", file=sys.stderr)
        return fallback


def run_within(work, fallback, seconds=HOOK_WORK_SECONDS):
    """Run work() under a hook deadline; an expired deadline returns fallback (fail open)."""
    try:
        with HookDeadline(time.monotonic() + seconds) as limit:
            token = ACTIVE.set(limit)
            try:
                return work()
            finally:
                ACTIVE.reset(token)
    except DeadlineExceeded:
        return fallback


def model_timeout(configured, deadline=None):
    """Leave time for process cleanup, policy decisions and state persistence."""
    if deadline is None:
        return configured
    deadline.check()
    return min(configured, max(0, deadline.remaining() - POLICY_RESERVE_SECONDS))
