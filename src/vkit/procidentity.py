"""Answer one question: is this pid still the process we launched?

A pid alone cannot answer it. Windows recycles process identifiers, so a
recorded pid eventually names whatever process took the number after ours
exited, and acting on that is how a cancellation kills a stranger. The recorded
identity is therefore the pair `(pid, creation_time)`, where `creation_time` is
the FILETIME from `GetProcessTimes` as an integer count of 100ns ticks since
1601. The pairing is what makes the number meaningful: the pid is reused, the
creation time is not.

**The pair is verified or it is not verified. There is no third answer and no
fallback.** If the pid is gone, or its creation time cannot be read, the answer
is False. Comparing the pid alone is the one implementation that is always
wrong, because it is the one that succeeds in exactly the case the identity
exists to catch.

## Gone, and cannot-confirm, are different

`read_identity` returns None for exactly one condition: the pid no longer names
a process. Every other failure raises `CannotConfirm`, and the caller must keep
its claim rather than conclude the process is gone. A supervisor that cannot
confirm ownership has not been told the process ended, only that it cannot
prove the process is its own, and those lead to different reports.

The line is WinError 87 from `OpenProcess`, and it was measured on this host
rather than assumed. 87 is what a pid with no process behind it produces: pid 0,
a pid above the configured maximum, and a pid whose process exited after every
handle to it was closed. WinError 5, access denied, is what a pid that IS in use
but out of reach produces -- the System process at pid 4 is the reproducible
example -- and that is cannot-confirm, not gone.

An earlier reading of this problem held that an exited process keeps a readable
creation time, so the exit case always resolves to "readable, and different".
That holds only while some handle keeps the process object alive. The moment
the last handle closes, the kernel reclaims the pid and the object with it, and
`OpenProcess` reports 87. A process that exited under this package's own
`vkit.procs` has no handle left anywhere, so its pid reads as gone. Both paths
are exercised in tests/test_procidentity.py rather than one being assumed.

`QueryFullProcessImageNameW` is deliberately not used. It fails with WinError 31
on a process that has exited, and its failure is a fact about the image, not
about the identity, so a reader that treated it as evidence would report
"different process" for a process that is merely finished. The image name buys
nothing here: the creation time alone decides.

## What this does not close

A supervisor that reattaches has no handle from the launch moment, so it reads
the pair from the run record and asks this module. Checking narrows the window
to the race between the check and the caller's next act; it does not close that
race, because the pid can be recycled in between. Cancellation that must be
atomic is a job object, which `vkit.procs` already creates and which is keyed to
a handle rather than a pid. This module is the check that makes a reattach safe
to *report*, not a substitute for the containment mechanism.

## The POSIX boundary

Every number this module relies on is a Windows number. The POSIX equivalent --
what pins a process against pid reuse there -- is not established, so this module
raises `UnsupportedPlatform` off Windows rather than answering with something
that reads as a verification and is not one. It is importable everywhere, so the
failure is a clear exception at the call and not an ImportError at the import.
"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Any

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import pywintypes
    import win32api
    import win32con
    import win32process
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _FILETIME = ctypes.POINTER(wintypes.FILETIME)
    # Without explicit argtypes the pointer arguments are guessed, and a PyHANDLE
    # raises "Don't know how to convert parameter 1" instead of being passed.
    _KERNEL32.GetProcessTimes.argtypes = [
        ctypes.c_void_p, _FILETIME, _FILETIME, _FILETIME, _FILETIME
    ]
    _KERNEL32.GetProcessTimes.restype = wintypes.BOOL


# The one WinError OpenProcess returns for a pid with no process behind it.
# Measured on this host: pid 0, a pid above the configured maximum, and a pid
# whose process exited after every handle to it closed all produce 87. Nothing
# else does; a pid in use but out of reach produces 5, and that is a different
# answer, so this constant is the whole of the gone test.
PID_GONE_WINERROR = 87


class UnsupportedPlatform(RuntimeError):
    """The process identity is defined only on Windows.

    Raised instead of an answer, because a caller handed a wrong answer here
    cancels a process it does not own.
    """


class CannotConfirm(RuntimeError):
    """The pid may be in use, but nothing readable establishes what it is.

    Distinct from a pid that is gone. A caller that catches this has not been
    told the process ended, only that ownership cannot be proven, so it must
    keep its claim. `still_the_same_process` folds both into False for the same
    reason: the report is the same, and a caller that wants to tell them apart
    calls `read_identity` directly.
    """

    def __init__(self, pid: int, detail: str) -> None:
        super().__init__(f"cannot read the identity of pid {pid}: {detail}")
        self.pid = pid
        self.detail = detail


@dataclass(frozen=True)
class ProcessIdentity:
    """What one process was, recorded once and compared forever after.

    Frozen because the pair is only an identity while neither half has been
    edited. A mutable copy of a recorded identity is a way to record something
    false, and nothing in this module has a reason to change one.

    `creation_time` is the raw FILETIME, not a datetime. A datetime has no
    defined equality across the out-of-range values Windows reports, and this
    value is compared, not displayed; a report formats it at the edge.
    """

    pid: int
    creation_time: int

    def to_json(self) -> dict[str, Any]:
        return {"pid": self.pid, "creation_time": self.creation_time}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ProcessIdentity":
        """Rebuild a record written by `to_json`.

        The two keys are indexed rather than defaulted, so a record missing
        either raises KeyError naming it. That is deliberate: a truncated record
        that silently became `creation_time=0` would compare unequal to every
        real process and read as "the process we launched is gone", which is a
        report about the world made from a corrupt file.
        """
        return cls(d["pid"], d["creation_time"])


def read_identity(pid: int) -> ProcessIdentity | None:
    """The identity of `pid` right now, or None if the pid names nothing.

    None means gone, and only that. A pid that exists but cannot be read raises
    `CannotConfirm`, because a caller reading None concludes the process ended
    and that is a different report from the one the evidence supports.
    """
    handle = _open(pid)
    if handle is None:
        return None
    try:
        return ProcessIdentity(pid=pid, creation_time=_creation_time(handle, pid))
    finally:
        # handle.Close(), not win32api.CloseHandle: PyHANDLE.Close zeroes the
        # value, so a stale reference cannot close a handle the OS has since
        # reused. Measured in vkit/procs.py: closing the same PyHANDLE twice
        # through win32api succeeds both times, which means the second call
        # acted on a recycled handle.
        handle.Close()


def still_the_same_process(recorded: ProcessIdentity) -> bool:
    """Whether the process behind `recorded` is running right now.

    True only when the pid exists AND its creation time equals the recorded one.
    A pid that is gone, or one whose creation time cannot be read, is False.

    False is deliberately one answer for both of those. They are different facts
    and a caller that wants to tell them apart calls `read_identity`, which
    distinguishes a gone pid from a CannotConfirm. What must not happen is the
    report drifting toward "the process finished" on evidence that only ever
    established "ownership unproven", so a caller is expected to read False as
    BLOCKED with reason `ownership_lost` and the claim retained -- never as
    permission to drop the claim.
    """
    try:
        current = read_identity(recorded.pid)
    except CannotConfirm:
        return False
    if current is None:
        return False
    return current.creation_time == recorded.creation_time


def is_alive(identity: ProcessIdentity) -> bool:
    """Whether the process behind `identity` is still running.

    An exited process whose object is still held open by some other handle
    remains readable, and its creation time still matches, so the identity alone
    cannot answer this. The exit code can: Windows reports STILL_ACTIVE only for
    a process that has not exited, and GetExitCodeProcess needs no access beyond
    the one used to read the creation time. Measured on this host: a running
    process reports 259 and a finished one reports the code it exited with, so
    this compares equal to STILL_ACTIVE for alive. The negation is the reading
    that is easy to write and reports every process backwards.

    An identity this function cannot open raises `CannotConfirm` rather than
    answering. That is deliberately not the same as `still_the_same_process`,
    which folds the same condition into False. A False from this function would
    be a claim that the process is not running, and a caller that has been told
    "not running" reports it as finished; nothing established that. Raising
    hands the caller the fact instead, and the fact is the one it needs: the
    claim is retained and the run is BLOCKED with reason `ownership_lost`.
    """
    handle = _open(identity.pid)
    if handle is None:
        return False
    try:
        return win32process.GetExitCodeProcess(handle) == win32con.STILL_ACTIVE
    finally:
        handle.Close()


def _require_windows() -> None:
    """The single guard, on the path every public function takes."""
    if not IS_WINDOWS:
        raise UnsupportedPlatform(
            "vkit.procidentity verifies a process identity using the Windows "
            "creation time, and the POSIX equivalent is not established. It is "
            "raised rather than answered because an unverified process must "
            "never be reported as verified."
        )


def _open(pid: int):
    """A handle for reading, or None if the pid names no process.

    Every public entry point passes through here, so this is the one place the
    gone and cannot-confirm cases are told apart, and the one place a
    non-Windows host is refused.
    """
    _require_windows()
    try:
        return win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error as exc:
        if exc.winerror == PID_GONE_WINERROR:
            return None
        raise CannotConfirm(pid, f"OpenProcess failed: {exc.strerror}") from exc


def _creation_time(handle, pid: int) -> int:
    """The process's creation FILETIME, as 100ns ticks since 1601.

    Read through an open handle rather than by pid. Holding the handle is what
    makes this a reading of one process: the object cannot be torn down and its
    pid reused while this handle is open, so there is no window between finding
    the process and measuring it.
    """
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    ok = _KERNEL32.GetProcessTimes(
        ctypes.c_void_p(int(handle)),
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    if not ok:
        code = ctypes.get_last_error()
        raise CannotConfirm(pid, f"GetProcessTimes failed: {ctypes.FormatError(code)}")
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


__all__ = [
    "CannotConfirm",
    "IS_WINDOWS",
    "PID_GONE_WINERROR",
    "ProcessIdentity",
    "UnsupportedPlatform",
    "is_alive",
    "read_identity",
    "still_the_same_process",
]
