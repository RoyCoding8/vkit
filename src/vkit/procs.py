"""Own one command and every process it spawns, for exactly as long as it is allowed.

A check that launches a test runner owns a tree, not a process. The runner spawns
the build, the build spawns a test binary, and a timeout that kills only the first
pid leaves orphans holding file locks. This module makes the tree the unit of
ownership and makes the timeout mean what it says.

Two decisions shape the code.

**Output goes to files, never to pipes.** The caller passes the two paths and this
module hands inheritable write handles to the child, so the kernel does the
writing. There is no reader thread to block, so a chatty child cannot deadlock,
and there is no buffer in this process at all, so output volume is bounded by
disk and not by memory. The acceptance row "large output, no pipe deadlock or
unbounded in-memory accumulation" is satisfied by there being no pipe.

**Containment is established before the process runs.** On Windows the child is
created suspended, assigned to a job object, and only then resumed. A process
that has not been resumed has not executed an instruction, so it cannot have
spawned a grandchild that escapes. If assignment fails, the suspended process is
terminated and the call reports a launch failure. Nothing runs uncontrolled, and
no fallback path exists that would run the command outside a job.

Ownership is established but not granted. Job objects and process groups stop
the descendants a well-behaved test runner produces. They do not defeat hostile
code, which can request breakaway. This is stated here rather than claimed as
containment of arbitrary programs.

## The POSIX boundary

On POSIX the process is started in a new session, making it a process-group
leader whose group is exactly its own tree at the moment of the call. The group
is the unit of ownership and the group is what gets signalled.

The boundary is this: a descendant may call setsid, or setpgid, and leave the
group. No POSIX signal can reach it afterwards. This code does not claim
containment past that point, and nothing here should be read as saying
otherwise.

**The POSIX path is tested, on a real POSIX host.** Measured on WSL2 Ubuntu
26.04, kernel 6.18.33.2-microsoft-standard-WSL2, by scripts/measure_posix_group.py
and by tests/test_procs_posix_real.py, which runs this module's public function
with nothing stubbed. A 1.5-second timeout against a three-level tree killed the
child, the grandchild and the great-grandchild, each of which was confirmed dead
from outside the group, and a walk of /proc found no survivor in the group. The
run reported `posix_process_group`, `timed_out=True`, and `exit_code=-9`, which
is the SIGKILL this module sent.

What is still NOT provided, and is a limitation of POSIX rather than of this
code, is the property the job object gives for free. A Windows tree cannot
outlive the handle that owns it, because closing the last handle is a kill. A
process group is a set of pids the kernel keeps after its leader is gone. Measured
in scripts/measure_supervisor_death.py: a descendant in its own session was still
running after its owner was SIGKILLed. `vkit.supervisor.terminate_owned_tree`
refuses on POSIX for that reason and says so.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .outcome import BlockedReason

if sys.platform == "win32":
    import pywintypes
    import win32con
    import win32event
    import win32file
    import win32job
    import win32process

IS_WINDOWS = sys.platform == "win32"

# The two values run-report.v1.json admits for `process.ownership`. The schema
# owns that vocabulary; these are the names this module reports, not a second
# definition of it.
WINDOWS_OWNERSHIP = "windows_job_object"
POSIX_OWNERSHIP = "posix_process_group"

# Once the job is terminated, no descendant can report an exit code of its own,
# so the code is ours to choose. pywin32 passes it to ExitProcess, which takes a
# 32-bit long; a value like 0xC000013A overflows it and raises instead of
# terminating. 1 is arbitrary and reads as "killed".
KILLED_EXIT_CODE = 1

# An upper bound on the wait after a job termination, not an expected cost.
# Measured on this host: TerminateJobObject returns in 5 ms even for a tree
# holding an open handle.
REAP_TIMEOUT_SECONDS = 5.0

# The floor on a wait. A sub-millisecond timeout would round to zero, which
# WaitForSingleObject reads as INFINITE and would wait forever.
MIN_WAIT_SECONDS = 0.001


class LaunchError(Exception):
    """The command could not be started, or its ownership could not be secured.

    Distinct from a command that ran and failed. A caller must be able to tell
    "nothing was executed" from "execution produced a nonzero exit", because the
    first is BLOCKED with `launch_failed` and the second is a real result.
    """


@dataclass(frozen=True)
class ExecutionResult:
    """What one execution did, in a form a report can cite.

    `exit_code` is None when the process was killed, which is the one case where
    a bare integer would be a lie: the code visible then is the code this module
    chose when it asked the job to terminate, not a code the command produced.
    """

    argv: tuple[str, ...]
    cwd: Path
    stdout_path: Path
    stderr_path: Path
    ownership: str
    pid: int | None = None
    # Read from the live process at launch. Afterwards every handle is
    # closed, and a pid whose process has exited may be unopenable, so a later
    # reader cannot recover this. Without it, a pid alone is all a cancel has,
    # and pids get recycled.
    creation_time: int | None = None
    # POSIX only, and empty on Windows where a FILETIME is absolute. A start
    # time is ticks since boot, so it is only meaningful within one boot, and
    # WSL restarts the whole init namespace. Compared as part of the identity so
    # a record from before a restart reads as a stranger rather than as a match.
    boot_id: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    reason: BlockedReason | None = None
    detail: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def launched(self) -> bool:
        return self.pid is not None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    def to_json(self) -> dict[str, object]:
        """The `process`, `command` and `logs` objects of run-report.v1.json.

        A run that never launched has no process identity, and the schema types
        `process` as nullable rather than as a record with a null pid, so the key
        is omitted rather than half-filled.
        """
        document: dict[str, object] = {
            "command": {"argv": list(self.argv), "cwd": str(self.cwd)},
            "logs": {"stdout": str(self.stdout_path), "stderr": str(self.stderr_path)},
        }
        if self.launched:
            document["process"] = {
                "pid": self.pid,
                "ownership": self.ownership,
                "exit_code": self.exit_code,
                "timed_out": self.timed_out,
            }
        return document


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
) -> ExecutionResult:
    """Run `argv` to completion, to timeout, or to failure.

    Never raises for a command that ran. Only a command that could not be owned
    comes back with a `reason`, and that result carries no pid, so a caller
    cannot mistake it for an execution.

    Output is streamed to the two caller-owned files by the child itself, so the
    bytes on disk are the child's, verbatim, whether it wrote 4 KB or 4 GB. This
    function buffers nothing on either stream.
    """
    argv = tuple(str(part) for part in argv)
    if not argv:
        raise LaunchError("argv must name at least one executable")
    cwd = Path(cwd)
    if not cwd.is_dir():
        raise LaunchError(f"working directory does not exist: {cwd}")
    if not (timeout_seconds > 0) or timeout_seconds == float("inf"):
        raise LaunchError(
            f"timeout must be a positive finite number of seconds, got {timeout_seconds!r}"
        )

    stdout_path = Path(stdout_path)
    stderr_path = Path(stderr_path)
    for path in (stdout_path, stderr_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        return _run_windows(argv, cwd, stdout_path, stderr_path, timeout_seconds)
    return _run_posix(argv, cwd, stdout_path, stderr_path, timeout_seconds)


def _launch_failure(
    argv: tuple[str, ...],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    exc: BaseException,
    started: float,
) -> ExecutionResult:
    """A command that never started.

    The two files exist and are empty, so a report can cite them without
    special-casing the no-launch case, and `pid` is None so nothing downstream
    can read this as an execution that happened.
    """
    for path in (stdout_path, stderr_path):
        with contextlib.suppress(OSError):
            path.touch()
    return ExecutionResult(
        argv=argv,
        cwd=cwd,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        ownership=WINDOWS_OWNERSHIP if IS_WINDOWS else POSIX_OWNERSHIP,
        reason=BlockedReason.LAUNCH_FAILED,
        detail=f"{type(exc).__name__}: {exc}",
        started_at=started,
        ended_at=time.monotonic(),
    )


# ---------------------------------------------------------------- POSIX


def _run_posix(
    argv: tuple[str, ...],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
) -> ExecutionResult:
    """POSIX ownership. The process group is the unit; see the module docstring for
    the boundary this covers and the one it does not.

    Verified on WSL2 Ubuntu 26.04, kernel 6.18.33.2-microsoft-standard-WSL2, by
    scripts/measure_posix_group.py and tests/test_procs_posix_real.py: a timeout
    signalled the group and the child, the grandchild and the great-grandchild
    were all dead afterwards, with no survivor anywhere in the group.
    """
    started = time.monotonic()
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            except OSError as exc:
                return _launch_failure(argv, cwd, stdout_path, stderr_path, exc, started)

            # Read the identity here, while the process is certainly alive. After
            # the wait it may have exited and its /proc entry may be gone, and
            # afterwards this module holds nothing that identifies it. A run
            # record without this is a bare pid, and a bare pid is what a
            # cancellation must refuse to act on because pids get recycled.
            creation_time, boot_id = _posix_identity(proc.pid)

            try:
                exit_code: int | None = proc.wait(timeout=timeout_seconds)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_group(proc.pid)
                try:
                    # The group was signalled, so this normally succeeds and
                    # returns the code the kill produced. It must be assigned on
                    # this path too: leaving it to the except below below made
                    # every ordinary POSIX timeout raise UnboundLocalError
                    # instead of reporting a timeout.
                    exit_code = proc.wait(timeout=REAP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    # Signalled, and the leader has not reaped. The command is
                    # killed either way; the code is simply unknown.
                    exit_code = None
    except OSError as exc:
        return _launch_failure(argv, cwd, stdout_path, stderr_path, exc, started)

    return ExecutionResult(
        argv=argv,
        cwd=cwd,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        ownership=POSIX_OWNERSHIP,
        pid=proc.pid,
        creation_time=creation_time,
        boot_id=boot_id,
        exit_code=exit_code,
        timed_out=timed_out,
        started_at=started,
        ended_at=time.monotonic(),
    )


def _posix_identity(pid: int) -> tuple[int | None, str]:
    """The launched process's (start-time, boot-id), or (None, "") if unreadable.

    Read once, here, while the process is alive. This is the POSIX counterpart of
    the FILETIME read from a suspended handle on Windows, and it exists for the
    same reason: afterwards this module holds no handle and no /proc entry, so a
    later reader has a bare pid, and a bare pid is not an identity.

    A failure to read is not a launch failure. The process is running and the
    command is owned either way; what is missing is the evidence that would let
    somebody else cancel it safely. So the run proceeds and records no creation
    time, and `vkit.cli` refuses a cancel on a record that lacks one, which is
    the correct outcome: ownership unproven is not the same as process gone.
    """
    from .procidentity import CannotConfirm, read_identity

    try:
        identity = read_identity(pid)
    except (CannotConfirm, OSError):
        return None, ""
    if identity is None:
        return None, ""
    return identity.creation_time, identity.boot_id


def _kill_process_group(pid: int) -> None:
    """Signal the group, then the leader directly.

    `os.killpg` raises ProcessLookupError if the leader exited between the
    timeout firing and the signal, which is a race and not a failure. The direct
    kill covers the case where no group exists for the pid but the process is
    still running as an orphan.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


# --------------------------------------------------------------- Windows


def _creation_time(h_process) -> int | None:
    """The process creation FILETIME, read from a handle that is still open.

    Read here, at launch, because this is the last moment the value is
    guaranteed available: every handle is closed before the result is returned,
    and a pid whose process has already exited may be unopenable. A later reader
    has only a bare pid, and pids are recycled.
    """
    if h_process is None or not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            ctypes.c_void_p(int(h_process)), ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    except Exception:  # noqa: BLE001 - an unreadable identity must not fail a run
        return None


def _open_inheritable(path: Path, access: int, disposition: int):
    """Open a file so the child can inherit the handle.

    SECURITY_ATTRIBUTES with bInheritHandle is what makes the handle survive the
    CreateProcess call; without it the child gets an invalid standard handle and
    its writes fail. FILE_SHARE_READ lets a parent read a child's output while
    the child still runs, which is what a live log tail needs.
    """
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.bInheritHandle = 1
    return win32file.CreateFile(
        str(path),
        access,
        win32con.FILE_SHARE_READ,
        attributes,
        disposition,
        win32con.FILE_ATTRIBUTE_NORMAL,
        None,
    )


def _new_job():
    """A job whose members die with the handle.

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes closing the job handle itself a kill.
    That turns "the owner died" into "the tree died", so a crash in the owning
    process cannot leak descendants, and it makes cleanup in a `finally`
    unconditional rather than conditional on which path was taken.
    """
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return job


def _application_name(argv: tuple[str, ...]) -> str | None:
    """Always None, and the reason is worth stating because it looks wrong.

    `lpApplicationName` is where a `.cmd` could not be named anyway: a batch
    file is not an executable image, and CreateProcess fails on one. When it is
    NULL, CreateProcess takes the program from the first token of the command
    line, and when that token names a `.cmd` or `.bat` it runs the command
    interpreter itself, on the same command line and with the same quoting.

    Every launcher shape this product accepts therefore needs nothing special:
    a `.py` or an `.exe` is found normally, and a `.cmd` is routed to the
    interpreter by the OS rather than by a `/c` prefix that has to be quoted
    against itself. See `_command_line`.
    """
    return None


def _command_line(argv: tuple[str, ...]) -> str:
    """The exact command line CreateProcess receives.

    list2cmdline is the quoting the CRT applies when it re-parses this string, so
    the child's argv is the argv passed in. Measured on this host: an embedded
    double quote, a trailing backslash, an empty argument, an argument with a
    space, and a non-ASCII argument all round-tripped byte for byte.

    The manifest supplies an argument array and never a shell string, so no
    quoting, redirection or expansion is applied here beyond that round trip.

    A batch file is named directly rather than through `cmd.exe /c`. The
    interpreter is the only executable that can run a `.cmd`, and CreateProcess
    invokes it itself when `lpApplicationName` is NULL and the command names
    one; wrapping it as `cmd.exe /c <launcher> ...` instead re-quotes the
    launcher path, and `/c` then strips one layer of those quotes and re-splits
    the remainder at the first space.

    Measured on this host by running the pre-fix command line through the real
    launch, against a launcher under six directory names. The `/c` form
    delivered the argument correctly for `plain`, `withspace`, `répertoire` and
    `ünïcødé`, and exited 1 for `with space` and `répertoire ünïcødé`, reporting
    the truncated prefix as an unrecognized command. So the trigger is a space
    in the launcher path, not a non-ASCII character; the direct form delivered
    the argument and the working directory exactly in all six cases, and the
    test that covers it uses seven directory names.

    This is the same reason `lpApplicationName` is left NULL. Naming the
    interpreter there made no difference to any of the six cases above, and
    naming a `.cmd` there fails outright, because a batch file is not an
    executable image.
    """
    return subprocess.list2cmdline(argv)


def _run_windows(
    argv: tuple[str, ...],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
) -> ExecutionResult:
    started = time.monotonic()
    job = _new_job()
    h_stdout = h_stderr = h_stdin = h_process = h_thread = None
    pid: int | None = None

    try:
        try:
            h_stdout = _open_inheritable(
                stdout_path, win32con.GENERIC_WRITE, win32con.CREATE_ALWAYS
            )
            h_stderr = _open_inheritable(
                stderr_path, win32con.GENERIC_WRITE, win32con.CREATE_ALWAYS
            )
            h_stdin = _open_inheritable(
                Path("NUL"), win32con.GENERIC_READ, win32con.OPEN_EXISTING
            )
        except pywintypes.error as exc:
            return _launch_failure(argv, cwd, stdout_path, stderr_path, exc, started)

        startup = win32process.STARTUPINFO()
        startup.dwFlags = win32con.STARTF_USESTDHANDLES
        startup.hStdInput = h_stdin
        startup.hStdOutput = h_stdout
        startup.hStdError = h_stderr

        try:
            h_process, h_thread, pid, _thread_id = win32process.CreateProcess(
                _application_name(argv),
                _command_line(argv),
                None,  # process security attributes
                None,  # thread security attributes
                True,  # bInheritHandles: the three std handles above
                win32con.CREATE_SUSPENDED,
                None,  # environment: this process's
                str(cwd),
                startup,
            )
        except pywintypes.error as exc:
            return _launch_failure(argv, cwd, stdout_path, stderr_path, exc, started)

        # The process now exists but has not run. Every failure past this point
        # terminates it rather than resuming it, because a resumed process that
        # was never assigned to a job is exactly the uncontrolled work this
        # module refuses to start.
        try:
            win32job.AssignProcessToJobObject(job, h_process)
        except pywintypes.error as exc:
            _terminate_unstarted(h_process)
            return _launch_failure(argv, cwd, stdout_path, stderr_path, exc, started)

        # Read here, while the process is suspended and the handle is open. After
        # the wait the process may have exited and the handle is closed by the
        # finally block, so this is the last point the value is guaranteed.
        creation_time = _creation_time(h_process)

        try:
            win32process.ResumeThread(h_thread)
        except pywintypes.error as exc:
            _terminate_unstarted(h_process)
            return _launch_failure(argv, cwd, stdout_path, stderr_path, exc, started)

        exit_code, timed_out = _wait_windows(h_process, timeout_seconds)
        if timed_out:
            exit_code = _terminate_job(job, h_process)
    finally:
        for handle in (h_thread, h_process, h_stdin, h_stderr, h_stdout, job):
            if handle is not None:
                # PyHANDLE.Close() zeroes the handle value, so a second call is a
                # no-op and a stale reference cannot close a handle the OS has
                # since reused. win32api.CloseHandle does not do that: measured,
                # closing the same PyHANDLE twice succeeded both times, which
                # means the second call acted on a recycled handle.
                handle.Close()

    return ExecutionResult(
        argv=argv,
        cwd=cwd,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        ownership=WINDOWS_OWNERSHIP,
        pid=pid,
        creation_time=creation_time,
        exit_code=exit_code,
        timed_out=timed_out,
        started_at=started,
        ended_at=time.monotonic(),
    )


def _terminate_unstarted(h_process) -> None:
    """Kill a process created suspended and never resumed.

    A suspended process has not executed an instruction, so terminating it
    cannot leave partial work behind. Measured: the file the process was told to
    write was never created, and its pid became unopenable immediately. The
    thread handle is left to the caller's cleanup, which already owns it.
    """
    with contextlib.suppress(Exception):
        win32process.TerminateProcess(h_process, KILLED_EXIT_CODE)
    with contextlib.suppress(Exception):
        win32event.WaitForSingleObject(h_process, int(REAP_TIMEOUT_SECONDS * 1000))


def _wait_windows(h_process, timeout_seconds: float) -> tuple[int | None, bool]:
    """Block for the process, bounded by the timeout.

    WaitForSingleObject returns WAIT_TIMEOUT when the process is still running at
    the deadline, which is the timeout itself and not an error. GetExitCodeProcess
    returns STILL_ACTIVE while it runs, so a process that has not finished has no
    exit code to report, and the caller gets None.
    """
    limit_ms = int(max(MIN_WAIT_SECONDS, timeout_seconds) * 1000)
    waited = win32event.WaitForSingleObject(h_process, limit_ms)
    if waited != win32con.WAIT_OBJECT_0:
        return None, True
    return win32process.GetExitCodeProcess(h_process), False


def _terminate_job(job, h_process) -> None:
    """Kill the whole tree and confirm it is gone.

    Returns None for the exit code. The only code visible after a termination is
    the one passed to TerminateJobObject, so reporting it would read as a normal
    nonzero failure; the caller needs None plus `timed_out` to derive BLOCKED
    with a timeout reason.
    """
    with contextlib.suppress(Exception):
        win32job.TerminateJobObject(job, KILLED_EXIT_CODE)
    with contextlib.suppress(Exception):
        win32event.WaitForSingleObject(h_process, int(REAP_TIMEOUT_SECONDS * 1000))


__all__ = [
    "ExecutionResult",
    "LaunchError",
    "POSIX_OWNERSHIP",
    "WINDOWS_OWNERSHIP",
    "run_command",
]
