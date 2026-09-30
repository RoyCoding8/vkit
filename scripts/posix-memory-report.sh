#!/bin/bash
# Read the memory and OOM evidence for this WSL distribution, without a pipeline
# that PowerShell would mangle.
set -uo pipefail

echo "== memory =="
free -m | head -2

echo
echo "== cgroup limits and current use =="
echo "  max:     $(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo unknown)"
echo "  current: $(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo unknown)"
echo "  peak:    $(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo unknown)"

echo
echo "== cgroup memory.events (oom_kill > 0 means the OOM killer fired) =="
cat /sys/fs/cgroup/memory.events 2>/dev/null || echo "  not readable"

echo
echo "== kernel log: OOM records =="
count=$(dmesg 2>/dev/null | grep -ciE "oom-kill" || true)
echo "  oom-kill lines in dmesg: $count"
if [ "${count:-0}" -gt 0 ]; then
    dmesg 2>/dev/null | grep -iE "oom-kill" | tail -5
fi

echo
echo "== how many processes are alive right now =="
echo "  $(ps -e --no-headers 2>/dev/null | wc -l) processes"
echo "  threads-max: $(cat /proc/sys/kernel/threads-max 2>/dev/null || echo unknown)"
echo "  pid_max:     $(cat /proc/sys/kernel/pid_max)"
