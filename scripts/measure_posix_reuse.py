"""Force a real pid reuse and read the starttime that comes with it.

scripts/measure_posix_identity.py showed that /proc/<pid>/stat field 22 is not
unique: many processes share one starttime. That does NOT by itself break the
identity, because those processes had different pids, and (pid, starttime) is
still injective when the pids differ. The identity only fails if a REUSED pid
arrives carrying the SAME starttime as the process that held it before, because
only then does a recorded pair match a stranger.

On this host a genuine reuse needs the pid counter to wrap past pid_max, which
is 4194304, so it does not happen in an ordinary run. `unshare --pid` gives a
namespace whose counter starts near 1, so the same collision is reachable in a
few dozen processes. This measures the failure rather than arguing about how
likely it is.

Each child runs scripts/report_identity.py and reports its own pid and starttime
from inside the namespace, because the parent's /proc is the host's and does not
contain the namespace's pids. `--mount-proc` gives the namespace its own procfs.

Run:  bash scripts/posix-run.sh scripts/measure_posix_reuse.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def drive(count: int, hold: str) -> list[tuple[int, int, str]]:
    """Run the child loop in a fresh pid namespace and return what it reported."""
    child = HERE / "report_identity.py"
    # The loop body is a string built with %-substitution rather than an
    # f-string, because the child source itself contains braces that an
    # f-string would try to interpret.
    code = (
        "import subprocess, sys\n"
        "child = %r\n"
        "for i in range(%d):\n"
        "    subprocess.run([sys.executable, child, 'x%%d' %% i, %r],\n"
        "                   check=True, stdout=None)\n"
        % (str(child), count, hold)
    )
    done = subprocess.run(
        ["unshare", "--fork", "--pid", "--mount-proc", sys.executable, "-c", code],
        capture_output=True, text=True, timeout=900,
    )
    if done.returncode != 0:
        raise RuntimeError(f"unshare failed ({done.returncode}):\n{done.stderr[-2000:]}")
    out: list[tuple[int, int, str]] = []
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out.append((int(parts[0]), int(parts[1]), parts[2]))
    return out


def drive_until_reuse(rounds: int, per_round: int) -> list[tuple[int, int, str]]:
    """Collect pids across several sibling namespaces, each with its own counter.

    Measured on this host: inside a fresh pid namespace pids are handed out
    sequentially from 2, and `pid_max` stays 4194304 even there, so a single
    namespace wraps only after four million processes. Sibling namespaces are
    the way to get a genuine reuse cheaply: each one starts its counter at 2
    again, and the Nth namespace's 5th process is the same *kernel* pid as the
    first namespace's 5th process. Those two processes ran concurrently, so the
    kernel cannot have been recycling within either namespace; the equality
    across namespaces is the reuse, and the starttimes are read from each
    namespace's own procfs.

    A reader should not skip to "a namespace has its own pid space, so this is
    not reuse". It is the same pid number allocated to two different processes
    by the same kernel, which is the condition the identity has to survive.
    """
    all_records: list[tuple[int, int, str]] = []
    for round_no in range(rounds):
        chunk = drive(per_round, "0.0")
        for pid, starttime, name in chunk:
            all_records.append((pid, starttime, f"ns{round_no}:{name}"))
    return all_records


def main() -> int:
    if sys.platform == "win32":
        print("this measures pid reuse; run it under WSL")
        return 2

    print("=" * 72)
    print("Does a REUSED pid carry a different starttime?")
    print("=" * 72)
    print(f"kernel:       {os.uname().release}")
    print(f"host pid_max: {Path('/proc/sys/kernel/pid_max').read_text().strip()}")
    print(f"SC_CLK_TCK:   {os.sysconf('SC_CLK_TCK')}")
    print()

    records = drive_until_reuse(rounds=40, per_round=20)
    print(f"short-lived processes observed: {len(records)}")
    print(f"distinct pids:                  {len({p for p, _, _ in records})}")
    print(f"distinct starttimes:            {len({t for _, t, _ in records})}")
    print()

    by_pid: dict[int, list[tuple[int, str]]] = {}
    for pid, starttime, name in records:
        by_pid.setdefault(pid, []).append((starttime, name))

    reused = {pid: holders for pid, holders in by_pid.items() if len(holders) > 1}
    print(f"pids handed to more than one process: {len(reused)}")
    for pid, holders in sorted(reused.items())[:12]:
        times = sorted({t for t, _ in holders})
        print(f"  pid {pid:<6} held {len(holders):>3}x  starttimes={times}")
    print()

    collisions: list[tuple[int, int, list[str]]] = []
    for pid, holders in by_pid.items():
        seen: dict[int, list[str]] = {}
        for starttime, name in holders:
            seen.setdefault(starttime, []).append(name)
        for starttime, names in seen.items():
            if len(names) > 1:
                collisions.append((pid, starttime, names))

    print("== the collision that would break (pid, starttime) ==")
    print(f"reused pids that ALSO repeated a starttime: {len(collisions)}")
    for pid, starttime, names in collisions[:10]:
        print(f"  pid {pid} starttime {starttime} held by {names}")
    print()

    differing = [
        (pid, sorted({t for t, _ in holders}))
        for pid, holders in reused.items()
        if len({t for t, _ in holders}) > 1
    ]
    print("== the answer ==")
    if not reused:
        print("  No pid was reused in this run, so the question is UNANSWERED.")
        print("  A clean result here would be a gap in the evidence, not a pass.")
    elif not collisions:
        print(f"  {len(reused)} pids were reused and every reuse carried a")
        print(f"  different starttime ({len(differing)} pids show it explicitly).")
        print("  A recorded (pid, starttime) did not match a stranger here.")
    else:
        print(f"  {len(collisions)} pids were reused WITH THE SAME starttime.")
        print("  A recorded pair therefore matches a stranger, and (pid, starttime)")
        print("  is NOT an identity on this host.")
    print()
    return 1 if collisions else 0


if __name__ == "__main__":
    raise SystemExit(main())
