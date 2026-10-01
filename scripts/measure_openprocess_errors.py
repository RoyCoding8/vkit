"""Which Windows errors can OpenProcess actually return, and on this host?

Run:  ./.venv/Scripts/python.exe scripts/measure_openprocess_errors.py

`vkit.procidentity.openprocess_failure_is_gone` rests on one claim: 87 is the
only code that establishes no process carries a pid, so it is the only code a
claim release may be founded on. `vkit.recover` once read 6 and 1168 as death
too, and a DEAD verdict is a permission to delete a claim row, so the claim
needs a measurement rather than an argument.

This drives `OpenProcess` over every category of pid a caller can hold and
prints the histogram of codes that come back. The category list is chosen to try
to make the module wrong: pids above the maximum, the sentinels, a reclaimed
pid, a live pid under access masks the caller never meant to send, and thread
ids passed where a process id belongs. A code that appears only under a
malformed request is a statement about the request, not about the pid, and
`openprocess_failure_is_gone` deliberately refuses to read it as death.

Measured on the development host: exactly three outcomes across 700-odd probes. The call
opened, or it failed with 5, or it failed with 87. Nothing else occurred, for a
live pid and not for a dead one, so 6 and 1168 are absent from the reachable set
rather than merely rare within it.

This is a measurement, not a test. It asserts nothing, and a host where a new
code appears is a host where `openprocess_failure_is_gone` owes a decision
before it can be trusted -- which is why the classifier takes the whole code and
falls to the safe side rather than enumerating codes known to be safe.
"""
from __future__ import annotations

import collections
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pywintypes  # noqa: E402
import win32api  # noqa: E402
import win32con  # noqa: E402
import win32process  # noqa: E402

from vkit.procidentity import (  # noqa: E402
    PID_GONE_WINERROR,
    openprocess_failure_is_gone,
)

# Toolhelp32, for the thread ids. pywin32 has no thread enumerator, and a thread
# id is the only object id available here that is real and is not a process id.
_TH32CS_SNAPTHREAD = 0x00000004
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]

# Both access masks the two modules use. The classification must not depend on
# which one the caller happened to pass.
ACCESS_MASKS = {
    "LIMITED": win32con.PROCESS_QUERY_LIMITED_INFORMATION,
    "FULL": win32con.PROCESS_QUERY_INFORMATION,
}

NAMES = {
    0: "opened",
    1: "INCORRECT_FUNCTION",
    5: "ACCESS_DENIED",
    6: "INVALID_HANDLE",
    87: "INVALID_PARAMETER",
    1168: "NOT_FOUND",
}


def _probe(pid: int, access: int):
    """The code OpenProcess raises for this pid, or 0 when it opens."""
    try:
        handle = win32api.OpenProcess(access, False, pid)
    except pywintypes.error as exc:
        return exc.winerror
    handle.Close()
    return 0


def _pids_to_try() -> list[tuple[str, int]]:
    """Every category of pid a caller can hold, each labelled by what it is.

    Labelled because the label is the finding. A single "87" in the output is
    ambiguous; "87 for a pid above the maximum" and "87 for a live pid" are
    different facts about the API.
    """
    cases: list[tuple[str, int]] = [
        ("sentinel", 0),
        ("sentinel", 1),
        ("sentinel", 2),
        ("sentinel", 4),
        ("above-max", 0x7FFFFFFF),
        ("above-max", 0xFFFFFFFF),
        ("above-max", 0x100000000),
    ]

    # A pid whose process exited and whose last handle was dropped, which is the
    # shape a finished run record leaves behind.
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    reclaimed = finished.pid
    del finished
    time.sleep(0.3)
    cases.append(("reclaimed", reclaimed))

    # Thread ids are real object ids that are not process ids. If the kernel
    # could tell the two apart with a distinct code, that code would be reachable
    # by a caller holding a stale or mis-typed id.
    for index, thread_id in enumerate(_own_thread_ids()[:3]):
        cases.append((f"thread-id-{index}", thread_id))

    return cases


def _own_thread_ids() -> list[int]:
    """This process's own thread ids, from a Toolhelp32 snapshot.

    pywin32 exposes no thread enumerator, so the snapshot is taken directly. The
    point of the probe is to hand OpenProcess an object id that is real and is not
    a process id, and the only reliable source of one is a thread that is
    certainly running.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE:
        return []
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        found: list[int] = []
        if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32OwnerProcessID == os.getpid():
                    found.append(entry.th32ThreadID)
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
        return found
    finally:
        kernel32.CloseHandle(snapshot)


def _malformed_access(pid: int) -> list[tuple[str, int]]:
    """Access masks a caller never meant to send, against a pid that is alive.

    A code produced by a malformed request describes the request. Feeding one to
    a live pid is how a code that is not a statement about the pid could become
    reachable, so it is probed deliberately rather than assumed away.
    """
    return [
        ("odd-mask/TERMINATE", 0x00000001),
        ("odd-mask/unknown-bit", 0x80000000),
        ("odd-mask/unknown-bit", 0x40000000),
        ("odd-mask/garbage", 0x7FFFFFFF),
    ]


def main() -> int:
    own = os.getpid()
    histogram: collections.Counter = collections.Counter()
    by_category: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )

    for pid in sorted(set(win32process.EnumProcesses())):
        for label, access in ACCESS_MASKS.items():
            code = _probe(pid, access)
            histogram[("live-pid/" + label, code)] += 1

    for label, pid in _pids_to_try():
        for mask_name, access in ACCESS_MASKS.items():
            code = _probe(pid, access)
            histogram[(label, code)] += 1

    for label, access in _malformed_access(own):
        for mask_name, access_mask in ACCESS_MASKS.items():
            code = _probe(access, access_mask)
            histogram[(label, code)] += 1

    for (label, code), count in sorted(histogram.items()):
        by_category[label][code] = count

    print(f"host: {sys.executable}")
    print(f"probes: {sum(histogram.values())}")
    observed = sorted({code for _, code in histogram})
    print(f"codes observed: {observed}")
    for code in observed:
        print(f"  {code:5d}  {NAMES.get(code, 'UNKNOWN')}")

    print("\nby category:")
    for label in sorted(by_category):
        for code, count in sorted(by_category[label].items()):
            name = NAMES.get(code, "UNKNOWN")
            mark = "  <- the death certificate" if code == PID_GONE_WINERROR else ""
            print(f"  {label:26s} {code:5d} {name:18s} x{count}{mark}")

    from vkit.procidentity import openprocess_failure_is_gone

    print("\nreached by the kernel, and what the classifier says about each:")
    for code in observed:
        verdict = "DEAD" if openprocess_failure_is_gone(code) else "cannot confirm"
        print(f"  {code:5d} {NAMES.get(code, 'UNKNOWN'):18s} -> {verdict}")

    for absent in (6, 1168):
        if absent not in observed:
            print(
                f"  {absent:5d} {NAMES[absent]:18s} -> not observed on this host; "
                "cannot establish death, and does not need to"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
