"""Run one command and everything it spawns, and stop all of it on timeout or cancel."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from .nowindow import hidden_window, is_windows

POLL_SECONDS = 0.1
CREATE_SUSPENDED = 0x00000004


@dataclass(frozen=True)
class Exit:
    code: int | None
    timed_out: bool = False
    cancelled: bool = False
    launch_error: str | None = None
    output_limited: bool = False


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

    def cancel(self, *, graceful: bool) -> None:
        if graceful and not is_windows:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self.kill()


def run(argv: Sequence[str], *, cwd: Path, stdout_path: Path, stderr_path: Path,
        timeout_seconds: float, env: Mapping[str, str] | None = None,
        should_cancel: Callable[[], bool] = lambda: False,
        max_output_bytes: int | None = None, stdin_path: Path | None = None,
        graceful_cancel: bool = False) -> Exit:
    if max_output_bytes is not None and max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    options = hidden_window()
    if is_windows:
        options["creationflags"] = options.get("creationflags", 0) | CREATE_SUSPENDED
    else:
        options["start_new_session"] = True
    with (open(stdout_path, "wb") as out, open(stderr_path, "wb") as err,
          open(stdin_path, "rb") if stdin_path is not None else nullcontext(subprocess.DEVNULL) as source):
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdin=source,
                stdout=subprocess.PIPE if max_output_bytes is not None else out,
                stderr=subprocess.PIPE if max_output_bytes is not None else err,
                env=None if env is None else dict(env),
                **options,
            )
        except OSError as exc:
            return Exit(None, launch_error=f"{argv[0]}: {exc}")
        tree = _Tree(proc)
        deadline = time.monotonic() + timeout_seconds
        output_limited = threading.Event()
        output_lock = threading.Lock()
        captured = 0
        readers: list[threading.Thread] = []

        def drain(pipe, destination) -> None:
            nonlocal captured
            try:
                while chunk := pipe.read(64 * 1024):
                    with output_lock:
                        room = max_output_bytes - captured
                        if room > 0:
                            destination.write(chunk[:room])
                            captured += min(len(chunk), room)
                        if len(chunk) > room:
                            output_limited.set()
            except OSError:
                output_limited.set()
            finally:
                pipe.close()

        if max_output_bytes is not None:
            for pipe, destination in ((proc.stdout, out), (proc.stderr, err)):
                reader = threading.Thread(target=drain, args=(pipe, destination), daemon=True)
                reader.start()
                readers.append(reader)
        try:
            while True:
                try:
                    code = proc.wait(POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if output_limited.is_set():
                    tree.kill()
                    code = proc.wait()
                    break
                if should_cancel():
                    tree.cancel(graceful=graceful_cancel)
                    proc.wait()
                    return Exit(None, cancelled=True)
                if time.monotonic() >= deadline:
                    tree.kill()
                    proc.wait()
                    return Exit(None, timed_out=True)
        finally:
            tree.close()
            for reader in readers:
                reader.join()
        if time.monotonic() >= deadline:
            return Exit(None, timed_out=True)
        return Exit(code, output_limited=output_limited.is_set())
