"""Bound local child process groups and retain subprocess-style results."""
import os
import signal
import subprocess
import time
import threading


def stop_group(process, cleanup_timeout=None) -> None:
    deadline = time.monotonic() + cleanup_timeout if cleanup_timeout is not None else None
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
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


def run(argv, *, cwd, env, timeout, input=None, text=False, capture_output=False,
        stdin=None, stdout=None, stderr=None, shell=False, cleanup_timeout=None):
    if shell:
        raise ValueError('shell execution forbidden')
    if input is not None:
        stdin = subprocess.PIPE
    if capture_output:
        stdout, stderr = subprocess.PIPE, subprocess.PIPE
    process, alarm_handler, deferred = None, None, []
    try:
        if cleanup_timeout is not None and threading.current_thread() is threading.main_thread():
            # Delay an alarm only across process creation. Deliver it once the
            # assigned child is owned by this cleanup scope; child masks stay intact.
            alarm_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, lambda signum, frame: deferred.append((signum, frame)))
        process = subprocess.Popen(argv, cwd=cwd, env=env, text=text, stdin=stdin,
                                   stdout=stdout, stderr=stderr, shell=False,
                                   start_new_session=True)
        if alarm_handler is not None:
            handler, alarm_handler = alarm_handler, None
            signal.signal(signal.SIGALRM, handler)
            if deferred and callable(handler):
                handler(*deferred[0])
            elif deferred and handler == signal.SIG_DFL:
                signal.raise_signal(signal.SIGALRM)
        output, errors = process.communicate(input, timeout=timeout)
        # A direct child can exit while descendants still hold its pipes.
        stop_group(process, cleanup_timeout)
    except BaseException:
        if process is not None:
            try:
                stop_group(process, cleanup_timeout)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Cleanup must not replace the original cancellation or deadline.
        raise
    finally:
        if alarm_handler is not None:
            signal.signal(signal.SIGALRM, alarm_handler)
    return subprocess.CompletedProcess(argv, process.returncode, output, errors)
