"""Measure what a vkit timeout does to a descendant that left the process group.

`scripts/measure_posix_group.py` measured the other half of this question and
found the product contained: a timeout signalled the group and the child, the
grandchild and the great-grandchild were all dead, with no survivor in the group.
That is a real result and it is not in dispute. Its tree was well-behaved,
though, because every level was an ordinary child.

`procs.py` states the boundary in one sentence -- a descendant may call setsid
or setpgid and leave the group, and no POSIX signal reaches it afterwards -- and
until now that sentence had no measurement behind it while every claim beside it
did. This is that measurement. It is a measurement, not a unit test: it prints
what it observed and asserts only what the docstring claims.

The escape is not hypothetical. Anything that daemonises calls setsid, and so
does any program that hands work to a library which starts a fresh session. A
test runner that starts a server, or a build tool that detaches a watcher, is
enough. The tree here is deliberately split: one grandchild stays in the group
and one calls setsid, so the two outcomes are observed in the same run under the
same signal. A run that only tested the escaper could not tell a setsid that
worked from a signal that killed everything by accident.

Every death is observed from OUTSIDE the group, and the escaper's group is
confirmed to differ from the victim's before the signal, because a survivor that
was never really outside proves nothing.

Run:  bash scripts/posix-run.sh scripts/measure_posix_escape.py
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.procs import POSIX_OWNERSHIP, _kill_process_group  # noqa: E402

HOLD_SECONDS = 120


def _spawner_source(ordinary: Path, escaper: Path, ready: Path) -> str:
    """A child that forks one ordinary grandchild and one that escapes.

    The escaper calls setsid before it forks, so the grandchild it creates
    inherits a brand-new session and is never in the victim's group at any
    point. Forking after the setsid is what makes the escape structural rather
    than a race the signal happened to win.
    """
    return f"""
import os, subprocess, sys, time
from pathlib import Path

stay = subprocess.Popen([sys.executable, "-c", "import time; time.sleep({HOLD_SECONDS})"])
Path({str(ordinary)!r}).write_text(str(stay.pid), encoding="utf-8")

left = subprocess.Popen(
    [sys.executable, "-c", "import os, time; os.setsid(); time.sleep({HOLD_SECONDS})"]
)
Path({str(escaper)!r}).write_text(str(left.pid), encoding="utf-8")
Path({str(ready)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep({HOLD_SECONDS})
"""


def _state(pid: int) -> str | None:
    """A pid's process state, from /proc, or None if there is no entry."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    try:
        return stat.rsplit(") ", 1)[-1].split()[0]
    except (IndexError, ValueError):
        return None


def _alive(pid: int) -> bool:
    """Whether a pid names a process that is still running.

    signal 0 performs the kernel's existence and permission check without
    delivering anything. EPERM is positive proof the process is there, owned by
    another user. ESRCH is proof it is not. Reading it the other way round is how
    a live process gets reported as dead, so both arms are explicit.

    A zombie counts as dead, and that is the second half of the answer. A child
    that has exited but has not been reaped keeps a /proc entry and still answers
    signal 0, so existence alone reports a terminated process as running. Z is
    the state the kernel gives a process that has already stopped, so it is dead
    by the only definition this measurement cares about. The leader of the victim
    tree is a direct child of this script, so unless it is reaped it stays a
    zombie and every containment verdict below would be wrong.
    """
    if _state(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pgid(pid: int) -> int | None:
    """A pid's process group, from /proc, as an independent witness."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    try:
        return int(stat.rsplit(") ", 1)[-1].split()[2])  # field 5 overall: pgrp
    except (IndexError, ValueError):
        return None


def _session(pid: int) -> int | None:
    """A pid's session id, from /proc.

    This is the value setsid actually changes, and it is read separately from the
    group because a process can share a group with its parent and still be in its
    own session, or the reverse.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    try:
        return int(stat.rsplit(") ", 1)[-1].split()[3])  # field 6 overall: session
    except (IndexError, ValueError):
        return None


def _wait_escaped(pid: int, leader_sid: int | None, limit: float = 15.0) -> bool:
    """Wait for the descendant to actually be in a different session.

    `Popen` returns once the child is exec'd, not once it has run its first
    statement, so a measurement that reads /proc the moment the pid is known can
    observe the child before it has called setsid. It would then report the
    escaper in the leader's session, conclude the escape had not happened, and
    prove nothing about anything.

    This waits for the property the measurement depends on instead of trusting
    the source to have produced it by the time the pid is written down.
    """
    started = time.monotonic()
    while time.monotonic() - started < limit:
        sid = _session(pid)
        if sid is not None and sid != leader_sid:
            return True
        time.sleep(0.02)
    return False


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

    work = Path(os.environ.get("TMPDIR", "/tmp")) / f"vkit-escape-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    child_script = work / "child.py"
    ordinary_marker = work / "ordinary.pid"
    escaper_marker = work / "escaper.pid"
    ready = work / "child.pid"
    child_script.write_text(
        _spawner_source(ordinary_marker, escaper_marker, ready), encoding="utf-8"
    )

    print("=" * 72)
    print("POSIX process-group containment vs a descendant that escaped", "=" * 72)
    print(f"kernel:  {os.uname().release}")
    print(f"python:  {sys.version.split()[0]}")
    print(f"work:    {work}")
    print()

    # The product's own kill path, not a re-implementation of it. A probe that
    # signalled the group itself would measure the probe, and the two could
    # disagree in a way that says nothing about the product.
    #
    # `start_new_session=True` is the product's own setting in `_run_posix`, so
    # the leader is a session and group leader exactly as it is under a real
    # timeout, and the group this signals is the group the product would signal.
    with (work / "out.log").open("wb") as out, (work / "err.log").open("wb") as err:
        child = subprocess.Popen(
            [sys.executable, str(child_script)],
            cwd=str(work),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
    try:
        leader = _wait_for(ready)
        ordinary = _wait_for(ordinary_marker)
        escaper = _wait_for(escaper_marker)

        # Established before anything is printed, because the readouts below are
        # only meaningful once the escape has actually happened.
        leader_sid = _session(leader)
        escaped = _wait_escaped(escaper, leader_sid)

        print("== the tree before the signal ==")
        for label, pid in (
            ("child (group leader)", leader),
            ("ordinary grandchild", ordinary),
            ("setsid grandchild", escaper),
        ):
            print(
                f"  {label:22} pid={pid:<8} pgid={_pgid(pid)} sid={_session(pid)}"
            )
        print()

        leader_pgid = _pgid(leader)
        ordinary_pgid = _pgid(ordinary)
        escaper_pgid = _pgid(escaper)
        leader_sid = _session(leader)

        # A survivor that was never really outside proves nothing, so the escape
        # is established before the signal rather than assumed from the source.
        escape_real = (
            escaped
            and escaper_pgid is not None
            and leader_pgid is not None
            and escaper_pgid != leader_pgid
            and ordinary_pgid == leader_pgid
        )
        print("== was the tree shaped as this measurement requires? ==")
        print(f"  leader and ordinary grandchild share group {leader_pgid}: "
              f"{ordinary_pgid == leader_pgid}")
        print(f"  setsid grandchild is in group {escaper_pgid}, not {leader_pgid}: "
              f"{escaper_pgid != leader_pgid}")
        print(f"  setsid grandchild is in its own session {_session(escaper)}, "
              f"not the leader's {leader_sid}: {_session(escaper) != leader_sid}")
        print()

        if not escape_real:
            print("VERDICT: SETUP DID NOT PRODUCE AN ESCAPE -- this run proves nothing")
            return 2

        # Exactly what a timeout does, so the finding is about the product and
        # not about a signal the product never sends.
        _kill_process_group(leader)

        print("== the same signal the product's timeout sends ==")
        leader_gone, leader_took = _wait_gone(leader)
        ordinary_gone, ordinary_took = _wait_gone(ordinary)
        escaper_gone, escaper_took = _wait_gone(escaper, limit=3.0)
        for label, pid, gone, took in (
            ("child (leader)", leader, leader_gone, leader_took),
            ("ordinary grandchild", ordinary, ordinary_gone, ordinary_took),
            ("setsid grandchild", escaper, escaper_gone, escaper_took),
        ):
            print(
                f"  {label:22} pid={pid:<8} dead={str(gone):5} after={took:.3f}s"
            )
        print()

        print("== census: is anything still in the victim's group? ==")
        survivors = _surviving_group(leader_pgid)
        print(f"  {survivors if survivors else 'none'}")
        print(f"== census: is the escaped process still alive? ==")
        print(f"  pid {escaper}: {'ALIVE' if _alive(escaper) else 'dead'}")
        print()

        if not leader_gone or not ordinary_gone:
            print("VERDICT: INCONCLUSIVE -- the group signal did not even kill a "
                  "member, so the escaper's survival proves nothing")
            return 1
        if not escaper_gone and _alive(escaper):
            print("VERDICT: THE GAP IS REAL")
            print("  A signal the product's own timeout sends killed the child and the")
            print("  ordinary grandchild, and left the setsid descendant running. The")
            print("  boundary in procs.py is therefore measured, not asserted: group")
            print("  containment holds for descendants that stay in the group and does")
            print("  not reach a descendant that leaves it.")
            print(f"ownership value the product reports: {POSIX_OWNERSHIP}")
            return 0

        print("VERDICT: NOT REPRODUCED -- the setsid descendant died too, so this")
        print("  run does not support the boundary claim in procs.py")
        return 1
    finally:
        # The escaped process is the whole point of the measurement and is not
        # in the group, so nothing else in this script will ever stop it. Left
        # behind, it would survive this script and hold a pid for HOLD_SECONDS.
        for pid in (leader, ordinary, escaper):
            if pid and _alive(pid):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(pid, signal.SIGKILL)
        # Reaped so the leader cannot linger as a zombie and appear in the census
        # of the group it was supposed to have been killed out of.
        with contextlib.suppress(OSError, ValueError):
            child.wait(timeout=5)


def _surviving_group(pgid: int | None) -> list[tuple[int, str]]:
    """Every live pid whose process group is `pgid`, read from /proc.

    Walking /proc is independent of the pids this script remembered, so a
    process it never learned about still shows up.

    A zombie is excluded. It is already dead and keeps only a /proc entry until
    it is reaped, and a leader killed but not yet reaped is a real entry in its
    own group. Reporting it as a survivor would put a dead pid in a containment
    verdict and make the number a measurement of the reap timing rather than of
    containment. `X` (dead) and `x` are excluded for the same reason.
    """
    running = {"R", "S", "D", "T", "t", "W", "I", "P"}
    found: list[tuple[int, str]] = []
    if pgid is None:
        return found
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        try:
            fields = stat.rsplit(") ", 1)[-1].split()
            if int(fields[2]) == pgid and fields[0] in running:  # field 5: pgrp
                found.append((int(entry.name), fields[0]))
        except (IndexError, ValueError):
            continue
    return found


if __name__ == "__main__":
    raise SystemExit(main())
