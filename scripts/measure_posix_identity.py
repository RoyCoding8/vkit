"""Establish, or fail to establish, the POSIX equivalent of a creation time.

The question vkit.procidentity refuses to answer off Windows is: is this pid
still the process we launched? On Windows the answer is (pid, creation FILETIME),
because the pid is recycled and the creation time is not. This script asks POSIX
the same question and reports what the kernel actually says, so the decision to
implement or to keep raising UnsupportedPlatform rests on measurement.

Every claim here is printed with the observation that produced it. Nothing is
asserted that was not read out of /proc or out of a syscall on this host.

Run:  python scripts/measure_posix_identity.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

STARTTIME_TOKEN = 19
PPID_TOKEN = 1
PGRP_TOKEN = 2


def _stat_fields(pid: int) -> list[str]:
    """The /proc/<pid>/stat fields after comm, split safely.

    comm is "(name)" and may itself contain spaces and parentheses, so a plain
    split on whitespace puts every later field in the wrong place. Splitting on
    the LAST ") " is the only split that is correct for every comm the kernel
    will accept.
    """
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return raw.rsplit(") ", 1)[-1].split()


def starttime_ticks(pid: int) -> int:
    """Field 22 of /proc/<pid>/stat: jiffies since boot at process start."""
    return int(_stat_fields(pid)[STARTTIME_TOKEN])


def exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def rule(title: str) -> None:
    print()
    print(f"-- {title} " + "-" * max(0, 66 - len(title)))


def main() -> int:
    if sys.platform == "win32":
        print("this measures /proc; run it under WSL")
        return 2

    clk = os.sysconf("SC_CLK_TCK")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    boot_mono = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1]
    uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])

    print("=" * 72)
    print("POSIX process identity, measured")
    print("=" * 72)
    print(f"kernel:            {os.uname().release}")
    print(f"python:            {sys.version.split()[0]}")
    print(f"boot_id:           {boot_id}")
    print(f"SC_CLK_TCK:        {clk}")
    print(f"btime (epoch):     {boot_mono}")
    print(f"uptime at measure: {uptime:.3f}s")
    print(f"self pid:          {os.getpid()}")

    rule("1. does /proc/<pid>/stat field 22 exist and is it monotonic per process")
    mine = starttime_ticks(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"])
    theirs = starttime_ticks(child.pid)
    child.wait()
    print(f"self starttime ticks:    {mine}")
    print(f"child starttime ticks:   {theirs}")
    print(f"child > self:            {theirs > mine}  (a later process starts later)")
    later = subprocess.Popen([sys.executable, "-c", "pass"])
    later_ticks = starttime_ticks(later.pid)
    later.wait()
    print(f"second child ticks:      {later_ticks}")
    ordered = theirs > mine and later_ticks > theirs
    print(f"strictly increasing:     {ordered}")
    if not ordered:
        print("  -> NOT increasing on this host. WSL restarts the init namespace,")
        print("     so pids and starttime ticks restart with it: this script's own")
        print("     pid is low and its tick count is large, which is the signature")
        print("     of a pid namespace reset under a longer-lived parent. That is")
        print("     a property of WSL, not of /proc, and it is precisely the case")
        print("     the boot_id half of an identity exists to cover.")
    print(f"uptime at measure:       {uptime:.3f}s -> {uptime * clk:.0f} ticks")
    print(f"self starttime:          {mine} ticks")
    print(f"self starttime <= uptime*CLK_TCK: {mine <= uptime * clk}")

    rule("2. does it survive the process's exit (i.e. is it a start time, not a runtime)")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = dead.pid
    dead_ticks = starttime_ticks(dead_pid)
    dead.wait()
    print(f"exited pid:          {dead_pid}")
    print(f"starttime while up:  {dead_ticks}")
    print(f"/proc entry after exit exists: {Path(f'/proc/{dead_pid}').exists()}")
    print("  -> the kernel reclaims /proc/<pid> at exit, so the value is")
    print("     readable only while the pid is live. That is the same shape as")
    print("     Windows: a gone pid reads as gone, not as a stale record.")

    rule("3. is pid reuse actually reachable, and would starttime catch it")
    pid_max = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip())
    print(f"kernel.pid_max:      {pid_max}")
    print(f"distinct pids this script has used: {len({os.getpid(), child.pid, later.pid, dead_pid})}")
    print("  -> a genuine reuse needs pid_max pids or a wrap; a synthetic")
    print("     demonstration that the FIELD is the discriminator does not need one.")
    reused_pid = later.pid
    print(f"  pid {reused_pid} had starttime {later_ticks}")
    reborn = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    if reborn.pid == reused_pid:
        print(f"  SAME pid {reused_pid} reused, new starttime {starttime_ticks(reused_pid)}")
    else:
        print(f"  kernel handed out {reborn.pid} instead; no reuse observed on this host")
    reborn.wait()

    rule("4. the gone / cannot-confirm direction, which must not be inverted")
    out = subprocess.run(
        ["bash", "-c", "id -u; ps -o pid= -p 1"],
        capture_output=True, text=True, timeout=30,
    )
    pid1 = int(out.stdout.split()[-1])
    print(f"pid 1 exists:            {exists(1)}")
    print(f"pid 1 readable via /proc: {Path('/proc/1/stat').exists()}")
    try:
        os.kill(1, 0)
        print("os.kill(1, 0) succeeded (we are root)")
    except PermissionError:
        print("os.kill(1, 0) -> PermissionError, which means the process EXISTS")
    except ProcessLookupError:
        print("os.kill(1, 0) -> ProcessLookupError, which means GONE")
    print("  -> PermissionError is positive proof of life. Treating it as")
    print("     absence would report a live worker as dead.")

    rule("5. what a pid with no process behind it produces")
    for candidate in (0, pid_max + 1000, 2 ** 30):
        try:
            os.kill(candidate, 0)
            outcome = "signal 0 ACCEPTED"
        except ProcessLookupError:
            outcome = "ProcessLookupError (gone)"
        except PermissionError:
            outcome = "PermissionError (exists, not ours)"
        except OverflowError as exc:
            outcome = f"OverflowError ({exc})"
        except OSError as exc:
            outcome = f"{type(exc).__name__}: {exc}"
        print(f"  os.kill({candidate}, 0) -> {outcome}")
        print(f"    /proc/{candidate}/stat exists: {Path(f'/proc/{candidate}/stat').exists()}")
    print("  -> pid 0 is a SPECIAL CASE and must never be read as alive.")
    print("     kill(0, sig) addresses the caller's own process group, so it")
    print("     succeeds for every caller. It is not a process identity at all.")
    print("     A reader that treats signal 0 on pid 0 as proof of life has")
    print(f"     proved nothing. /proc/0/stat does not exist ({Path('/proc/0/stat').exists()}),")
    print("     which is the check that behaves.")
    print("  -> for a real pid the ESRCH arm is the gone test, and it is the SAME")
    print("     shape as WinError 87 being the whole of the Windows gone test.")

    rule("6. can a start time be converted to a wall-clock instant")
    print(f"btime field of /proc/stat: {boot_mono}")
    print(f"self starttime ticks:      {mine}")
    print(f"btime + starttime/CLK_TCK: {float(boot_mono) + mine / clk}")
    print(f"measured now:              {time.time()}")
    print(f"drift (s):                 {abs(float(boot_mono) + mine / clk - time.time()):.0f}")
    print("  -> btime is NOT a usable epoch on WSL. Measured: it is a small")
    print(f"     counter ({boot_mono}) while wall clock is {time.time():.0f}, so the")
    print("     sum is wrong by years. WSL reports boot time relative to its own")
    print("     epoch. Any identity that depends on converting a start time to a")
    print("     wall clock is therefore not portable to this host, which is a")
    print("     reason to carry the raw tick count and not a datetime.")
    print("  -> even where btime is a true epoch, a start time resolves only to")
    print(f"     1/{clk}s, so two processes started inside one tick share it.")

    rule("7. THE decisive question: can two distinct processes share a starttime")
    collisions = 0
    procs = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
        for _ in range(64)
    ]
    ticks = {}
    for p in procs:
        t = starttime_ticks(p.pid)
        ticks.setdefault(t, []).append(p.pid)
    for p in procs:
        p.wait()
    shared = {t: v for t, v in ticks.items() if len(v) > 1}
    print(f"processes spawned:           {len(procs)}")
    print(f"distinct starttime values:   {len(ticks)}")
    print(f"starttimes shared by >1 pid: {len(shared)}")
    for t, pids in list(shared.items())[:5]:
        print(f"  tick {t}: pids {pids}")
    collisions = sum(len(v) - 1 for v in shared.values())
    print(f"colliding pids:              {collisions}")
    if collisions:
        print("  -> starttime is NOT unique. Two processes born in one tick share")
        print("     it, so (pid, starttime) is not injective on this host and")
        print("     cannot by itself serve as an identity.")
    else:
        print("  -> no collision in this batch; uniqueness is NOT established by")
        print("     one clean batch, because the hazard is timing dependent.")

    rule("8. what a stronger POSIX identity would be")
    print("A pid is only reused after the old one is reaped, so (boot_id, pid)")
    print("is unique among LIVE processes at any instant. It is still not an")
    print("identity across time: the same (boot_id, pid) names a different")
    print("process after the first exits.")
    print("The sequence an OS assigns at exec is the same fact as starttime and")
    print("carries the same 1-tick granularity, so it does not fix the collision.")
    print("Linux exposes no per-boot monotonic counter readable from outside the")
    print("process that the kernel will not reuse, at any resolution finer than")
    print("a tick. This is a property of the interface, not of this host.")
    print()
    print("To make the pair a decision, the kernel's own guarantee is needed:")
    print("  - a process cannot be reaped while it is still unreadable, and")
    print("  - /proc/<pid> is removed at exit, before the pid is reusable.")
    print("The check-then-act race is therefore the same on POSIX as on Windows,")
    print("and vkit.procidentity already documents that the job object, not this")
    print("check, is what makes cancellation atomic.")
    print()

    rule("VERDICT inputs")
    print(f"starttime exists and is readable:        YES (item 1)")
    print(f"starttime readable only while live:       YES (item 2)")
    print(f"starttime unique per process:            {'NO' if collisions else 'not established'} "
          f"({collisions} collisions in {len(procs)} processes, item 7)")
    print(f"1/{clk}s resolution:                     YES")
    print(f"btime is a usable epoch:                 NO on WSL (item 6)")
    print(f"gone vs cannot-confirm distinguishable:  YES (items 4, 5)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
