"""Check the field index of starttime in /proc/<pid>/stat.

A reader that splits the stat line on the last ") " gets fields 3.. onward as
tokens 0.. upward, so field N is token N-3. Getting that wrong by one silently
reads a neighbouring field, and every conclusion drawn from it is about the
wrong number. This prints the neighbourhood so the index is checked by eye
against proc(5) rather than trusted.

Run:  python scripts/check_stat_fields.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

NAMES = {
    1: "pid", 2: "comm", 3: "state", 4: "ppid", 5: "pgrp", 6: "session",
    7: "tty_nr", 8: "tpgid", 9: "flags", 10: "minflt", 11: "cminflt",
    12: "majflt", 13: "cmajflt", 14: "utime", 15: "stime", 16: "cutime",
    17: "cstime", 18: "priority", 19: "nice", 20: "num_threads", 21: "itrealvalue",
    22: "starttime", 23: "vsize", 24: "rss",
}


def main() -> int:
    if sys.platform == "win32":
        print("this reads /proc; run it under WSL")
        return 2

    raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
    head, _, tail = raw.rpartition(") ")
    tokens = tail.split()
    clk = os.sysconf("SC_CLK_TCK")
    uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])

    print(f"raw comm: {head.split('(', 1)[1].rsplit(')', 1)[0]!r}")
    print(f"tokens after the last ') ': {len(tokens)}")
    print()
    print("field  name         token index  value        sanity")
    for field in range(3, 25):
        index = field - 3
        value = tokens[index] if index < len(tokens) else "-"
        note = ""
        if field == 1:
            note = f"should equal {os.getpid()}"
        elif field == 2:
            note = "the comm field, excluded by the split"
        elif field == 22:
            note = f"MUST be <= uptime*{clk} = {uptime * clk:.0f}"
        elif field == 23:
            note = "vsize in bytes, looks like any large number"
        print(f"{field:<6}  {NAMES[field]:<11}  {index:<11}  {value:<12}  {note}")

    print()
    correct = tokens[19]
    off_by_one = tokens[20]
    print(f"starttime at index 19 (field 22): {correct}")
    print(f"  <= uptime*CLK_TCK ({uptime * clk:.0f}): {int(correct) <= uptime * clk}")
    print(f"value at index 20 (field 23, vsize): {off_by_one}")
    print(f"  <= uptime*CLK_TCK: {int(off_by_one) <= uptime * clk}")
    print()
    print("An index off by one reads vsize, which is ~20 MB for every CPython")
    print("process, so it looks like a plausible tick count to anyone who does")
    print("not check it against uptime. That is how a wrong field becomes a")
    print("confident wrong conclusion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
