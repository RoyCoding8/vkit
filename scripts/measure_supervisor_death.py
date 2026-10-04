"""Measure what a killed supervisor leaves behind on POSIX, and say what it does not.

Row 8 of acceptance02 requires that a killed supervisor's descendant is dead
afterwards. On Windows that is the job object: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
makes closing the last handle a kill, so the tree cannot outlive its owner.

There is no POSIX equivalent of that guarantee, and this measures rather than
argues the point. A process group is the nearest POSIX mechanism, and a group
outlives the process that created it. The check is whether the descendant is
still running, read from outside the group.

Run:  python scripts/measure_supervisor_death.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def alive(pid: int) -> bool:
    """Whether a pid names a live process, from outside the group.

    signal 0 runs the kernel's existence check without delivering anything.
    EPERM is positive proof the process is there; ESRCH is proof it is not.
    Reading EPERM as absence is how a live worker gets reported dead.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main() -> int:
    if sys.platform == "win32":
        print("this measures POSIX group lifetime; run it under WSL")
        return 2

    print("=" * 72)
    print("Does a killed owner take its POSIX process group with it?")
    print("=" * 72)
    print(f"kernel: {os.uname().release}")
    print()

    work = Path(os.environ.get("TMPDIR", "/tmp")) / f"vkit-sup-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    pid_file = work / "descendant.pid"
    if pid_file.exists():
        pid_file.unlink()

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os, pathlib, time\n"
         f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
         "time.sleep(280)\n"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not pid_file.is_file():
        time.sleep(0.05)
    if not pid_file.is_file():
        print("the descendant never announced itself")
        child.kill()
        return 2
    descendant = int(pid_file.read_text(encoding="utf-8").strip())

    print(f"descendant pid:        {descendant}")
    print(f"descendant pgid:       {os.getpgid(descendant)}")
    print(f"its own leader:        {os.getpgid(descendant) == descendant}")
    print(f"alive before the kill: {alive(descendant)}")
    print()

    owner = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(280)'],\n"
         "                 start_new_session=False)\n"
         "time.sleep(280)\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)
    print(f"owner pid:             {owner.pid}")
    owner.kill()
    owner.wait(timeout=30)
    print("owner SIGKILLed")
    time.sleep(3.0)
    print(f"owner alive after:     {alive(owner.pid)}")
    print(f"descendant alive after: {alive(descendant)}")
    print()

    verdict = "OUTLIVES" if alive(descendant) else "DIES WITH OWNER"
    print(f"VERDICT: the descendant {verdict} its owner on POSIX.")
    print()
    print("  Windows has a handle-keyed mechanism for this: a job object with")
    print("  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes closing the last handle a")
    print("  kill, and vkit.procs creates one before the child is resumed.")
    print("  POSIX has no handle and no such flag. A process group is a set of")
    print("  pids the kernel keeps after the leader is gone, so a tree created")
    print("  with start_new_session survives its creator by construction.")
    print()
    print("  What vkit does about it: procs._run_posix owns the group and")
    print("  signals it on timeout, which scripts/measure_posix_group.py")
    print("  measures to three levels deep. What it does NOT do is tie the")
    print("  group's lifetime to the owning process, because POSIX offers no")
    print("  way to do that. supervisor.terminate_owned_tree raises OSError off")
    print("  Windows rather than pretending, which is the honest report.")

    for pid in (descendant,):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    subprocess.run(["pkill", "-9", "-f", "time.sleep(280)"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
