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
handle to it closed. WinError 5, access denied, is what a pid that IS in use
but out of reach produces -- the System process at pid 4 is the reproducible
example -- and that is cannot-confirm, not gone.

**Only 87 is a death certificate, and that is the whole of the answer.** Measured
on this host by driving `OpenProcess` over 700-odd probes: every live pid under
both `PROCESS_QUERY_LIMITED_INFORMATION` and `PROCESS_QUERY_INFORMATION`, plus
pid 0, pid 1, pid 2, pid 4, three pids above the maximum, a pid whose object
had been reclaimed, a live pid probed under four malformed access masks, and
thread ids passed where a process id belongs. Exactly three outcomes occurred:
the call opened, 5, and 87. WinError 6 and WinError 1168 did not occur once,
for a pid that is alive and not for one that is gone. The run behind that is
`scripts/measure_openprocess_errors.py`, which prints the histogram by category
and is the first thing to re-run if a new code ever appears.

Their absence is the reason they cannot join 87, not a reason to leave them out of
caution. ERROR_INVALID_HANDLE means the handle *you passed* is not a handle, and
`OpenProcess` takes a process id rather than a handle, so that code is not a
statement about the pid at all. ERROR_NOT_FOUND belongs to the lookup-by-name path
-- `OpenJobObject`, `RegOpenKeyEx`, `CreateFile` -- and a numeric process id is
resolved through the process table instead, so that path is not the one running.
Neither can be produced for a pid that is alive, and equally neither proves a pid
is gone. Both are answers about the request rather than about the world, and
reading either as death infers the one fact from a code that does not carry it.

`vkit.recover` decides claims from this same question, so it calls
`openprocess_failure_is_gone` rather than keeping a second table. The guarantee
its callers rest on is that a DEAD verdict is only ever returned on a code that
establishes no process carries the number.

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

## On POSIX

The POSIX equivalent is `/proc/<pid>/stat` field 22, `starttime`: the process's
birth in clock ticks since boot. It is measured, not assumed. See
`scripts/measure_posix_identity.py`, `scripts/measure_posix_reuse.py` and
`scripts/check_stat_fields.py` for the runs behind every claim here.

The same pair is used, with the same rule: the pid alone is never compared. The
second half is a tick count rather than a FILETIME, and the two are not
interchangeable, so a record written on one platform cannot be read on the
other. `boot_id` is part of the comparison for the reason below.

**The pair survives pid reuse, and this was measured rather than reasoned
about.** A pid cannot be reissued while the process holding it is live, so two
live processes never share one. What has to be shown is that a RECYCLED pid
arrives with a different starttime. Reaching a genuine reuse on a live host
needs the counter to wrap past `pid_max`, which is 4194304 here, so
`scripts/measure_posix_reuse.py` produces it from sibling pid namespaces, each
of which restarts its counter near 2. Measured: 800 short-lived processes, 20
distinct pids, every pid held 40 times, and every one of those 800 processes
carried its own starttime. No pid ever came back with the starttime of the
process that held it before.

**starttime is not unique on its own, and that does not break the pair.** A
single run of 64 processes produced 8 distinct starttimes, so up to eight
processes shared one. Those processes had different pids, and the pair is still
injective. starttime has 1/CLK_TCK resolution (10 ms at the measured 100 Hz),
so the residual risk is a pid recycled *and* its new process born inside the
same tick as the old one. That is a narrower window than pid reuse itself by
three orders of magnitude, and it is the same kind of residual the Windows pair
carries. It is stated here rather than claimed away.

**The `boot_id` half exists because WSL resets the pid namespace.** Measured on
this host: after a WSL restart, a long-lived parent's pid was low while its
starttime tick count was larger than any child spawned afterwards, because the
whole init namespace restarted underneath it. Two processes in different boots
can share a tick count, so `boot_id` is compared as well. A run recorded before
a reboot therefore reads as a stranger rather than as a match, which is the
direction that keeps a claim.

**`btime` is not used.** `/proc/stat`'s btime is not a usable epoch on this
host: measured, it reported 30753 while the wall clock was 1790756598, so the
sum is wrong by roughly 56 years. WSL reports boot time relative to its own
epoch. Converting a start time to a wall clock is therefore not portable here,
which is a second reason the identity carries the raw tick count.

**A pid is not readable just because it is signalable.** `kill(0, 0)` addresses
the caller's own process group and succeeds for every caller, so pid 0 is
refused before any probe. `/proc/0/stat` does not exist, which is the check that
behaves.
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
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


# ERROR_INVALID_PARAMETER, the one WinError OpenProcess returns for a pid with no
# process behind it. Measured on this host over 700-odd probes spanning every live
# pid under both query access masks, the sentinels, pids above the maximum, a
# reclaimed pid, malformed access masks and thread ids: the call opened, or it
# failed with 5, or it failed with 87. Nothing else occurred, so this constant is
# the whole of the gone test rather than the best-supported member of a set.
PID_GONE_WINERROR = 87

# A malformed request is still a request, and every code outside the one above
# reads cannot-confirm. This is the module's only place Windows error numbers are
# interpreted, and it is public because a module that decides whether a claim may
# be released has to answer the same question without keeping its own table.
#
# The two codes worth naming, because both were once read as death by
# vkit.recover and neither can carry that fact:
#
#   * 6, ERROR_INVALID_HANDLE -- the handle you passed is not a handle.
#     OpenProcess takes a process id, so the code is a statement about the call
#     and not about the pid. Did not occur in the measurement above.
#   * 1168, ERROR_NOT_FOUND -- the named object does not exist. It belongs to the
#     lookup-by-name path (OpenJobObject, RegOpenKeyEx, CreateFile); a numeric
#     process id is resolved through the process table, which answers 87 instead.
#     Did not occur in the measurement above.
#
# Both are safe here precisely because they are unreachable. A code that cannot
# be produced for a live pid cannot release a live pid's claim, and the run stays
# UNCERTAIN and recoverable rather than lost. Were either to become reachable it
# would need measuring before it could be trusted here, and the measurement is
# the first thing this module would owe: scripts/measure_openprocess_errors.py.

# proc(5) pid_stat, field 22, counting from 1. Splitting the line on the LAST
# ") " leaves field 3 at token 0, so field 22 is token 19. Token 20 is vsize,
# which is a large plausible-looking number for every CPython process, so an
# index off by one produces a confident wrong answer; scripts/check_stat_fields.py
# prints the neighbourhood and checks the value against uptime*CLK_TCK.
STARTTIME_TOKEN = 19

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
PROC = Path("/proc")



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

    `creation_time` is the raw FILETIME on Windows and the raw start-time tick
    count on POSIX. It is deliberately not a datetime. A datetime has no defined
    equality across the out-of-range values Windows reports, and on POSIX the
    conversion needs `btime`, which is not a usable epoch on every host. This
    value is compared, not displayed; a report formats it at the edge.

    `boot_id` is POSIX-only and empty on Windows, where a FILETIME is absolute
    and needs no boot to be meaningful. It is compared because a tick count is
    not: WSL restarts the pid namespace, so two processes in different boots can
    carry the same start time, and a record from before a reboot must read as a
    stranger rather than as a match.
    """

    pid: int
    creation_time: int
    boot_id: str = ""

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {"pid": self.pid, "creation_time": self.creation_time}
        if self.boot_id:
            document["boot_id"] = self.boot_id
        return document

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ProcessIdentity":
        """Rebuild a record written by `to_json`.

        `pid` and `creation_time` are indexed rather than defaulted, so a record
        missing either raises KeyError naming it. That is deliberate: a truncated
        record that silently became `creation_time=0` would compare unequal to
        every real process and read as "the process we launched is gone", which
        is a report about the world made from a corrupt file.

        `boot_id` defaults, because a record written on Windows has none. A pair
        carrying no boot compared against one that has a boot is False, which is
        the direction that keeps a claim rather than releasing it.
        """
        return cls(d["pid"], d["creation_time"], d.get("boot_id", ""))


def read_identity(pid: int) -> ProcessIdentity | None:
    """The identity of `pid` right now, or None if the pid names nothing.

    None means gone, and only that. A pid that exists but cannot be read raises
    `CannotConfirm`, because a caller reading None concludes the process ended
    and that is a different report from the one the evidence supports.
    """
    if not IS_WINDOWS:
        return _read_identity_posix(pid)
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
    # The boot id is part of the comparison, not decoration. A tick count is only
    # meaningful within one boot: WSL restarts the pid namespace, so a record
    # written before a restart can share its start time with a process that is
    # running after it. That pair is a stranger and must not read as a match.
    if current.boot_id != recorded.boot_id:
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

    On POSIX the same question is answered by the process state character in
    /proc/<pid>/stat field 3, which the kernel maintains for a process that has
    exited but not yet been reaped. A zombie reads 'Z', and a zombie is not
    running, so the answer is False for it. Reading 'Z' as alive would report a
    finished worker as running and strand its claim.

    An identity this function cannot open raises `CannotConfirm` rather than
    answering. That is deliberately not the same as `still_the_same_process`,
    which folds the same condition into False. A False from this function would
    be a claim that the process is not running, and a caller that has been told
    "not running" reports it as finished; nothing established that. Raising
    hands the caller the fact instead, and the fact is the one it needs: the
    claim is retained and the run is BLOCKED with reason `ownership_lost`.
    """
    if not IS_WINDOWS:
        return _is_alive_posix(identity)
    handle = _open(identity.pid)
    if handle is None:
        return False
    try:
        return win32process.GetExitCodeProcess(handle) == win32con.STILL_ACTIVE
    finally:
        handle.Close()


def _require_windows() -> None:
    """The single guard, on the path every Windows public function takes.

    Every public entry point dispatches on `IS_WINDOWS` before reaching this, so
    reaching it at all means a Windows-only helper was called off Windows. It is
    kept rather than deleted because removing it would leave the Windows helpers
    callable from a POSIX host with a NameError instead of a named refusal.
    """
    if not IS_WINDOWS:
        raise UnsupportedPlatform(
            "this is a Windows-only helper in vkit.procidentity; the POSIX path "
            "reads /proc/<pid>/stat instead"
        )


def openprocess_failure_is_gone(winerror: int | None) -> bool:
    """Whether a failed OpenProcess establishes that no process carries the pid.

    The single answer to the one question two modules were asking separately.
    False covers every code that does not establish death, and it is the answer
    a caller must be able to act on: a claim stays held, and the run stays
    recoverable by a human. That makes the safe reading available even for a
    code nobody has ever seen, which is the property a release depends on and the
    reason this takes the whole code rather than a set of known-bad ones.

    `None` is what pywintypes raises as when the underlying call never set
    `GetLastError`, and it is not evidence of anything, so it reads False.

    Public because a module that releases another task's resource has to be able
    to ask. The alternative was a second table in vkit.recover, and two tables
    disagreed about 6 and 1168 for as long as both existed.
    """
    return winerror == PID_GONE_WINERROR


def _open(pid: int):
    """A handle for reading, or None if the pid names no process.

    Every Windows public entry point passes through here, so this is the one
    place the gone and cannot-confirm cases are told apart.
    """
    _require_windows()
    try:
        return win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error as exc:
        if openprocess_failure_is_gone(exc.winerror):
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


# ------------------------------------------------------------------ POSIX


def _read_identity_posix(pid: int) -> ProcessIdentity | None:
    """(pid, starttime, boot_id) for `pid`, or None if the pid names nothing.

    Read from `/proc/<pid>/stat`, which the kernel removes when the process
    exits. So the gone case is the same shape as the Windows one: a pid whose
    process has exited has no stat file, and reads as gone rather than as a
    stale record that might match something.

    A pid that exists but whose stat file cannot be read raises CannotConfirm,
    because "cannot open" and "does not exist" are different claims and only one
    of them is a safe basis for releasing a run's claims.
    """
    _require_a_real_pid(pid)
    stat = PROC / str(pid) / "stat"
    try:
        raw = stat.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The kernel reclaims /proc/<pid> at exit, before the pid can be reused.
        return None
    except PermissionError as exc:
        raise CannotConfirm(pid, f"/proc/{pid}/stat is not readable: {exc}") from exc
    except OSError as exc:
        raise CannotConfirm(pid, f"/proc/{pid}/stat could not be read: {exc}") from exc
    return ProcessIdentity(
        pid=pid,
        creation_time=_starttime_from_stat(raw, pid),
        boot_id=boot_id(),
    )


def _starttime_from_stat(raw: str, pid: int) -> int:
    """proc(5) field 22 of a stat line, in clock ticks since boot.

    comm is field 2 and is wrapped in parentheses, and a process may put spaces
    and parentheses inside it. Splitting on whitespace therefore shifts every
    later field, and splitting on the FIRST ") " shifts them the other way, so
    the split is on the LAST one, which is the only one that is right for every
    name the kernel accepts.
    """
    _, separator, tail = raw.rpartition(") ")
    if not separator:
        raise CannotConfirm(pid, f"/proc/{pid}/stat has no comm field, so it cannot be parsed")
    fields = tail.split()
    if len(fields) <= STARTTIME_TOKEN:
        raise CannotConfirm(
            pid,
            f"/proc/{pid}/stat has {len(fields)} fields after comm, too few to "
            f"reach starttime at field 22",
        )
    try:
        return int(fields[STARTTIME_TOKEN])
    except ValueError as exc:
        raise CannotConfirm(
            pid, f"/proc/{pid}/stat field 22 is not an integer: {fields[STARTTIME_TOKEN]!r}"
        ) from exc


def _proc_state(pid: int) -> str | None:
    """proc(5) field 3, the single-character run state, or None if it is gone."""
    try:
        raw = (PROC / str(pid) / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise CannotConfirm(pid, f"/proc/{pid}/stat could not be read: {exc}") from exc
    _, separator, tail = raw.rpartition(") ")
    if not separator:
        raise CannotConfirm(pid, f"/proc/{pid}/stat has no comm field, so it cannot be parsed")
    fields = tail.split()
    if not fields:
        raise CannotConfirm(pid, f"/proc/{pid}/stat has no state field")
    return fields[0]


def _is_alive_posix(identity: ProcessIdentity) -> bool:
    """Whether the process behind `identity` is running right now.

    A zombie has exited and is waiting to be reaped, so it is not running. The
    state character is the discriminator: 'Z' and 'X' are dead, everything else
    is live. Reading 'Z' as alive would report a finished worker as running and
    strand its claim, which is the direction of error that costs a user their
    evidence.
    """
    state = _proc_state(identity.pid)
    if state is None:
        return False
    if state in ("Z", "X", "x"):
        return False
    # A process that is not a zombie is running, whatever else it is doing.
    return True


def _require_a_real_pid(pid: int) -> None:
    """Refuse a pid that does not name one process.

    `kill(0, sig)` addresses the caller's own process group, and it succeeds for
    every caller, so a signal-zero probe on pid 0 proves nothing at all. Measured
    on this host: `os.kill(0, 0)` returned without error, and /proc/0/stat does
    not exist. A reader that took the success as proof of life would have
    reported a process group as a process. Refusing the sentinel is the fix, and
    it is the same refusal `vkit.recover.liveness` makes.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise CannotConfirm(
            pid, f"pid {pid!r} does not name a single process, so it has no identity"
        )


_BOOT_ID: str | None = None


def boot_id() -> str:
    """This boot's identifier, read once and remembered.

    A tick count is only meaningful within one boot. WSL restarts the whole init
    namespace, so a process that outlives a restart can carry a start time that
    a process from after the restart also carries, and a record from before the
    restart would match it. Comparing the boot id makes that pair a stranger.

    Cached because it cannot change while the process runs, and re-reading it on
    every comparison would put a file read on the path of every cancel. A read
    failure is not fatal here: an empty boot id is a weaker identity, and the
    tick count and pid still both have to match, so the pair is still sound
    against reuse and only the cross-boot case is unproven.
    """
    global _BOOT_ID
    if _BOOT_ID is None:
        try:
            _BOOT_ID = Path(BOOT_ID_PATH).read_text(encoding="utf-8").strip()
        except OSError:
            _BOOT_ID = ""
    return _BOOT_ID


__all__ = [
    "CannotConfirm",
    "IS_WINDOWS",
    "PID_GONE_WINERROR",
    "ProcessIdentity",
    "UnsupportedPlatform",
    "boot_id",
    "is_alive",
    "openprocess_failure_is_gone",
    "read_identity",
    "still_the_same_process",
]
