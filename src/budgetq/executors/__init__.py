"""Executors run one task and report what it cost."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable

from ..quota import QuotaState


@dataclass
class RunResult:
    exit_code: int | None
    outcome: str  # success | error | timeout | aborted_quota | not_found
    summary: str = ""
    cost_usd_est: float | None = None
    tokens: dict = field(default_factory=dict)
    quota_after: QuotaState | None = None
    requeue: bool = False  # put the task back in the queue (quota abort)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.kill()

def _reap_children() -> None:
    # Reap zombie processes to prevent accumulation
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except ChildProcessError:
        pass
    except OSError:
        pass


def popen(cmd: list[str], cwd: str, env: dict | None = None) -> subprocess.Popen:
    kwargs: dict = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def start_timer(proc: subprocess.Popen, seconds: float, flag: Callable[[], None]) -> threading.Timer:
    def fire():
        flag()
        kill_tree(proc)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    return timer


def drain(stream, sink) -> threading.Thread:
    def pump():
        for line in stream:
            sink.write(line)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return thread


def _setup_sigchld_handler() -> None:
    # Set up SIGCHLD handler to reap zombie processes on Unix systems
    if sys.platform != "win32":
        signal.signal(signal.SIGCHLD, lambda signum, frame: _reap_children())


# Initialize the SIGCHLD handler for zombie process reaping
_setup_sigchld_handler()
