"""Measure whether a vkit timeout kills a whole POSIX process group.

This is the POSIX counterpart of a measurement already taken on Windows, where
the equivalent mechanism is a job object. It is a measurement, not a unit test:
it prints what it observed and asserts only the facts the product's own
docstring claims.

The tree is three deep on purpose. A process group that reaches only the direct
child is not containment, and a group that reaches the child and the grandchild
but not the great-grandchild is still not containment. All three levels must be
gone, and each one's death must be observed from OUTSIDE the group, because a
process that cannot read /proc has not been shown to be gone.

Run:  python scripts/measure_posix_group.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.procs import POSIX_OWNERSHIP, run_command  # noqa: E402

HOLD_SECONDS = 120


def _spawner_source(grandchild_marker: Path, great_marker: Path, ready: Path) -> str:
    """A child that forks a grandchild, which forks a great-grandchild.

    Each level re-parents into init when its parent dies, so an orphaned
    grandchild is exactly what a group signal must still reach.
    """
    return f"""
import os, subprocess, sys, time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", {(_grandchild_source(grandchild_marker, great_marker))!r}])
Path({str(ready)!r}).write_text(str(child.pid), encoding="utf-8")
time.sleep({HOLD_SECONDS})
"""


def _grandchild_source(grandchild_marker: Path, great_marker: Path) -> str:
    return f"""
import os, subprocess, sys, time
from pathlib import Path
great = subprocess.Popen([sys.executable, "-c", "import time; time.sleep({HOLD_SECONDS})"])
Path({str(great_marker)!r}).write_text(str(great.pid), encoding="utf-8")
Path({str(grandchild_marker)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep({HOLD_SECONDS})
"""

def _alive(pid: int) -> bool:
    """Whether a pid names a live process, observed the way a bystander would.

    signal 0 performs the kernel's existence and permission check without
    delivering anything. EPERM is positive proof the process is there, owned by
    another user. ESRCH is proof it is not. Reading it the other way round is how
    a live process gets reported as dead, so both arms are explicit.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _exists_in_proc(pid: int) -> bool:
    """Whether /proc still has an entry for the pid, a second independent witness."""
    return Path(f"/proc/{pid}").exists()


def _read_state(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return "no /proc entry"
    fields = stat.read_text(encoding="utf-8").rsplit(") ", 1)[-1].split()
    return f"state={fields[0]}"


def _wait_gone(pid: int, limit: float = 10.0) -> tuple[bool, float]:
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if not _alive(pid):
            return True, time.monotonic() - started
        time.sleep(0.02)
    return False, time.monotonic() - started


def _wait_for(path: Path, limit: float = 30.0) -> int:
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return int(path.read_text(encoding="utf-8").strip())
        time.sleep(0.02)
    raise RuntimeError(f"the spawner never reported {path}")


def main() -> int:
    if sys.platform == "win32":
        print("this measures POSIX process groups; run it under WSL")
        return 2

    work = Path(os.environ.get("TMPDIR", "/tmp")) / f"vkit-group-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    child_script = work / "child.py"
    grandchild_marker = work / "grand.pid"
    great_marker = work / "great.pid"
    ready = work / "child.pid"
    child_script.write_text(
        _spawner_source(grandchild_marker, great_marker, ready), encoding="utf-8"
    )

    print("=" * 72)
    print("POSIX process-group containment, measured")
    print("=" * 72)
    print(f"kernel:  {os.uname().release}")
    print(f"python:  {sys.version.split()[0]}")
    print(f"clk_tck: {os.sysconf('SC_CLK_TCK')}")
    print(f"work:    {work}")
    print()

    result = run_command(
        (sys.executable, str(child_script)),
        cwd=work,
        stdout_path=work / "out.log",
        stderr_path=work / "err.log",
        timeout_seconds=1.5,
    )

    grandchild = _wait_for(grandchild_marker)
    great = _wait_for(great_marker)
    leader = result.pid

    print("== what the run reported ==")
    print(f"ownership:   {result.ownership}")
    print(f"pid:         {leader}")
    print(f"timed_out:   {result.timed_out}")
    print(f"exit_code:   {result.exit_code}")
    print(f"duration_s:  {result.duration_seconds:.3f}")
    print()

    print("== the tree as the owner sees it (immediately after the timeout) ==")
    for label, pid in (
        ("child (group leader)", leader),
        ("grandchild", grandchild),
        ("great-grandchild", great),
    ):
        print(f"  {label:22} pid={pid:<8} {_read_state(pid)}")
    print()

    print("== is it contained? ==")
    verdicts = []
    for label, pid in (
        ("child", leader),
        ("grandchild", grandchild),
        ("great-grandchild", great),
    ):
        gone, took = _wait_gone(pid)
        proc_gone = not _exists_in_proc(pid)
        verdicts.append(gone and proc_gone)
        print(
            f"  {label:16} pid={pid:<8} killed={str(gone):5} "
            f"proc_gone={str(proc_gone):5} after={took:.3f}s  state={_read_state(pid)}"
        )
    print()

    survivors = _surviving_group(leader)
    print(f"== any process still in group {leader}? ==")
    print(f"  {survivors if survivors else 'none'}")
    print()

    contained = all(verdicts) and not survivors
    print("VERDICT: " + (
        "the timeout killed the whole process group: "
        "child, grandchild and great-grandchild all dead"
        if contained else
        f"NOT CONTAINED: verdicts={verdicts} survivors={survivors}"
    ))
    print(f"ownership value reported by the product: {result.ownership}")
    print(f"schema vocabulary expects:              {POSIX_OWNERSHIP}")
    print()
    return 0 if contained else 1


def _surviving_group(pgid: int) -> list[tuple[int, str]]:
    """Every live pid whose process group is `pgid`, read from /proc.

    Walking /proc is independent of the pids this script remembered, so a
    process it never learned about still shows up.
    """
    found: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        try:
            fields = stat.rsplit(") ", 1)[-1].split()
            if int(fields[2]) == pgid:
                found.append((int(entry.name), fields[0]))
        except (IndexError, ValueError):
            continue
    return found


if __name__ == "__main__":
    raise SystemExit(main())
