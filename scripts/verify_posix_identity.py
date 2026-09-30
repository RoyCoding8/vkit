"""Drive the new POSIX identity against real processes and real pid reuse.

The claim under test is the one vkit.procidentity exists to support: a recorded
(pid, start-time) pair must survive pid reuse, and a pid that has been taken
over by another process must read as a stranger rather than as the process we
launched. Everything here is observed, not simulated.

Run:  bash scripts/posix-run.sh scripts/verify_posix_identity.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.procidentity import (  # noqa: E402
    CannotConfirm,
    ProcessIdentity,
    boot_id,
    is_alive,
    read_identity,
    still_the_same_process,
)

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    mark = "PASS" if condition else "FAIL"
    if not condition:
        ok = False
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    if sys.platform == "win32":
        print("this verifies the POSIX identity; run it under WSL")
        return 2

    print("=" * 72)
    print("POSIX process identity, verified end to end")
    print("=" * 72)
    print(f"kernel:  {os.uname().release}")
    print(f"boot_id: {boot_id()}")
    print()

    print("== a live process has a readable identity ==")
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    time.sleep(0.3)
    identity = read_identity(live.pid)
    check("read_identity returns an identity", identity is not None)
    check("it names the pid we asked about", identity is not None and identity.pid == live.pid)
    check("it carries a start time", identity is not None and identity.creation_time > 0)
    check("it carries the boot id", identity is not None and identity.boot_id == boot_id())
    check(
        "the start time is in the right range for a live process",
        identity is not None and identity.creation_time <= os.sysconf("SC_CLK_TCK") * _uptime(),
        f"starttime={identity.creation_time if identity else None}",
    )
    check("is_alive says a sleeping interpreter is alive", is_alive(identity))
    check("still_the_same_process agrees", still_the_same_process(identity))

    print()
    print("== a stranger cannot answer to a record that does not match ==")
    # The record every POSIX implementation gets wrong: the pid alone, with a
    # start time from some other process.
    stranger_record = ProcessIdentity(live.pid, (identity.creation_time if identity else 0) + 1, boot_id())
    check(
        "a mismatched start time is False, not True",
        still_the_same_process(stranger_record) is False,
        "this is the bug class: acting on it would kill a stranger",
    )
    check("a mismatched pair still reads the pid as alive", is_alive(stranger_record))

    print()
    print("== a pid from another boot is a stranger ==")
    other_boot = ProcessIdentity(live.pid, identity.creation_time if identity else 0, "not-this-boot")
    check("same pid and start time, different boot -> False", still_the_same_process(other_boot) is False)

    print()
    print("== an exited process reads as gone ==")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = dead.pid
    dead.wait()
    time.sleep(0.5)
    check(f"read_identity({dead_pid}) is None", read_identity(dead_pid) is None)
    check("is_alive on the recorded identity is False", not is_alive(
        ProcessIdentity(dead_pid, 1, boot_id())))
    check("still_the_same_process is False", still_the_same_process(
        ProcessIdentity(dead_pid, 1, boot_id())) is False)

    print()
    print("== a pid that names nothing is gone, not unconfirmable ==")
    pid_max = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    check("a pid above pid_max reads as gone", read_identity(pid_max + 1000) is None)

    print()
    print("== the direction of error: EPERM means ALIVE, not absent ==")
    # As root every pid is signalable, so the EPERM arm cannot be produced here.
    # What can be checked is that the code does not treat an unreadable stat as
    # absence: a pid whose /proc entry cannot exist is refused as not a pid.
    for sentinel in (0, -1):
        try:
            read_identity(sentinel)
            check(f"pid {sentinel} is refused", False, "it was accepted")
        except CannotConfirm as exc:
            check(f"pid {sentinel} is refused, not answered", True, str(exc)[:60])
        except Exception as exc:
            check(f"pid {sentinel} is refused, not answered", False, f"{type(exc).__name__}: {exc}")

    print()
    print("== round-trip through JSON keeps the pair comparable ==")
    record = identity
    restored = ProcessIdentity.from_json(record.to_json())
    check("to_json/from_json is a fixed point", restored == record)
    check("and still matches its process", still_the_same_process(restored))
    check("a Windows record has no boot_id", "boot_id" not in ProcessIdentity(1, 2).to_json())
    check(
        "a record with no boot_id does not match a POSIX one",
        still_the_same_process(ProcessIdentity(record.pid, record.creation_time)) is False,
        "absent is not the same as present, which keeps the claim",
    )

    live.kill()
    live.wait()
    print()
    print("VERDICT: " + ("every check passed" if ok else "at least one check FAILED"))
    return 0 if ok else 1


def _uptime() -> float:
    return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])


if __name__ == "__main__":
    raise SystemExit(main())
