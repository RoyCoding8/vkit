"""One short-lived process, reporting its own pid and starttime.

Run inside a fresh pid namespace by scripts/measure_posix_reuse.py. Kept in its
own file so there is no f-string inside a string to get the quoting wrong, which
is exactly the mistake that made the first run of that script read
/proc/{os.getpid()} literally.

Each child is short-lived on purpose. Reuse needs the previous holder to have
exited, so a process that lives for minutes can never expose the collision. The
short-lived grandchild adds pid churn the child alone would not create.

Usage: python report_identity.py <name> <hold-seconds>
Prints: "<pid> <starttime> <name>"

proc(5) field 22 is starttime. Splitting the stat line on the last ") " leaves
field 3 at token 0, so field 22 is token 19. Token 20 is vsize, a large
plausible-looking number for every CPython process, so an index off by one
produces a confident wrong answer. scripts/check_stat_fields.py prints the
neighbourhood so the index is checked rather than trusted.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

STARTTIME_TOKEN = 19


def starttime_ticks() -> int:
    raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
    return int(raw.rsplit(") ", 1)[-1].split()[STARTTIME_TOKEN])


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "?"
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0

    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.02)"])
    print(f"{os.getpid()} {starttime_ticks()} {name}", flush=True)
    time.sleep(hold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
