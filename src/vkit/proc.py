"""Run one command and everything it spawns, and stop all of it on timeout or cancel."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .nowindow import hidden_window, is_windows

POLL_SECONDS = 0.1
CREATE_SUSPENDED = 0x00000004


@dataclass(frozen=True)
class Exit:
    code: int | None
    timed_out: bool = False
    cancelled: bool = False
    launch_error: str | None = None


class _Tree:
    """The OS handle that owns a process and its descendants."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.job = None
        if is_windows:
            import ctypes
            import win32job

            self.job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, info)
            win32job.AssignProcessToJobObject(self.job, int(proc._handle))
            ctypes.windll.ntdll.NtResumeProcess(int(proc._handle))

    def kill(self) -> None:
        """Kill every process in the tree. macOS answers EPERM, not ESRCH, when the group holds only zombies."""
        if is_windows:
            import win32job

            win32job.TerminateJobObject(self.job, 1)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def close(self) -> None:
        if is_windows:
            self.job.Close()
        else:
            self.kill()


def run(argv: Sequence[str], *, cwd: Path, stdout_path: Path, stderr_path: Path,
        timeout_seconds: float, env: Mapping[str, str] | None = None,
        should_cancel: Callable[[], bool] = lambda: False) -> Exit:
    options = hidden_window()
    if is_windows:
        options["creationflags"] = options.get("creationflags", 0) | CREATE_SUSPENDED
    else:
        options["start_new_session"] = True
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        try:
            proc = subprocess.Popen(list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, env=None if env is None else dict(env), **options)
        except OSError as exc:
            return Exit(None, launch_error=f"{argv[0]}: {exc}")
        tree = _Tree(proc)
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                try:
                    code = proc.wait(POLL_SECONDS)
                    return Exit(code)
                except subprocess.TimeoutExpired:
                    pass
                if should_cancel():
                    tree.kill()
                    proc.wait()
                    return Exit(None, cancelled=True)
                if time.monotonic() >= deadline:
                    tree.kill()
                    proc.wait()
                    return Exit(None, timed_out=True)
        finally:
            tree.close()
