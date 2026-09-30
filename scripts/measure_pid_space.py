"""What pid range does a fresh pid namespace actually allocate from?

scripts/measure_posix_reuse.py expects `unshare --pid` to give a counter that
starts near 1 and wraps quickly. It did not wrap: 200 processes drew 200
distinct pids. This measures the real allocation behaviour instead of assuming
it, because a pid-reuse experiment that cannot produce a reuse proves nothing
either way.

Run:  bash scripts/posix-run.sh scripts/measure_pid_space.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROBE = "import os; print(os.getpid())"


def main() -> int:
    if sys.platform == "win32":
        print("this probes pid allocation; run it under WSL")
        return 2

    print("=" * 72)
    print("pid allocation, as this kernel actually behaves")
    print("=" * 72)
    print(f"pid_max:  {Path('/proc/sys/kernel/pid_max').read_text().strip()}")
    print(f"host pid: {os.getpid()}")
    print()

    # Inside a pid namespace, /proc/sys/kernel/pid_max is per-namespace. Read it
    # from within, which is the value that actually bounds the counter.
    inside = subprocess.run(
        ["unshare", "--fork", "--pid", "--mount-proc", "cat", "/proc/sys/kernel/pid_max"],
        capture_output=True, text=True, timeout=120,
    )
    print(f"pid_max visible inside a fresh pid namespace: {inside.stdout.strip() or inside.stderr.strip()}")
    print()

    # Draw a batch of pids inside one namespace and look at the shape.
    code = (
        "import os, subprocess, sys\n"
        "pids = []\n"
        "for i in range(300):\n"
        "    pids.append(int(subprocess.run([sys.executable, '-c', "
        "\"import os;print(os.getpid())\"], capture_output=True, text=True).stdout))\n"
        "print('min', min(pids), 'max', max(pids), 'distinct', len(set(pids)))\n"
        "print('first10', pids[:10])\n"
        "print('last10', pids[-10:])\n"
    )
    done = subprocess.run(
        ["unshare", "--fork", "--pid", "--mount-proc", sys.executable, "-c", code],
        capture_output=True, text=True, timeout=900,
    )
    print("300 pids drawn inside one fresh pid namespace:")
    print(done.stdout or done.stderr[-2000:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
