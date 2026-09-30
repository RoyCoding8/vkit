#!/bin/bash
# Find out why a long POSIX run of this suite dies part-way with no traceback.
#
# The whole-suite run was killed repeatedly on this host, always after roughly
# three to nine files, with no traceback, no summary line and no non-zero exit
# from the shell that launched it. Three explanations fit that shape and they
# have different fixes, so it is worth telling them apart rather than guessing:
#
#   * the OOM killer, which would appear in dmesg and in the memory cgroup's
#     events;
#   * the WSL VM being reclaimed under memory pressure, which shows as the
#     distribution stopping;
#   * something in the tests signalling its own process group, which is what
#     scripts/acceptance02.py's kill_tree did before it was fixed.
#
# This watches memory, the cgroup limits and the distribution's liveness across a
# run, and records the peak. If memory is flat and the cgroup never approaches its
# limit, the kills are not the OOM killer and the group-signal theory is the one
# left.
#
# Run:  bash scripts/posix-suite-doctor.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
export PATH="$VENV/bin:$PATH"
mkdir -p tmp

echo "== host facts =="
free -m 2>/dev/null | head -2
echo "cgroup max:  $(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo unknown)"
echo "cgroup peak: $(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo unknown)"
echo "swap:        $(free -m 2>/dev/null | awk '/Swap/{print $2" MiB total, "$3" used"}')"
echo "pid_max:     $(cat /proc/sys/kernel/pid_max)"
echo "threads-max: $(cat /proc/sys/kernel/threads-max 2>/dev/null || echo unknown)"
echo "nproc:       $(nproc)"
echo

# Watch in the background so the numbers exist even if the run dies.
(
    peak_rss=0
    peak_used=0
    while [ -f /tmp/vkit-doctor-run ]; do
        used=$(awk '/MemAvailable/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
        rss=$(ps -eo rss= 2>/dev/null | sort -rn | head -1)
        rss=${rss:-0}
        [ "$used" -gt "$peak_used" ] 2>/dev/null && peak_used=$used
        [ "$rss" -gt "$peak_rss" ] 2>/dev/null && peak_rss=$rss
        echo "$peak_used $peak_rss" > /tmp/vkit-doctor-peak
        sleep 2
    done
) &
WATCHER=$!

touch /tmp/vkit-doctor-run
trap 'rm -f /tmp/vkit-doctor-run; kill $WATCHER 2>/dev/null' EXIT

killed_at=""
for f in tests/test_*.py; do
    name=$(basename "$f" .py)
    setsid timeout --signal=KILL 900 \
        "$VENV/bin/python" -u -m pytest "$f" -q -p no:cacheprovider --tb=no \
        > "tmp/doctor-$name.txt" 2>&1
    status=$?
    if [ ! -s "tmp/doctor-$name.txt" ] && [ "$status" -ne 5 ]; then
        killed_at="$name (exit $status, produced no output at all)"
        echo "DIED on $name with exit $status and no output"
        break
    fi
    printf '%-32s exit=%s\n' "$name" "$status"
done

rm -f /tmp/vkit-doctor-run
sleep 3

echo
echo "== what the watcher saw =="
if [ -f /tmp/vkit-doctor-peak ]; then
    read -r peak_avail peak_rss < /tmp/vkit-doctor-peak
    echo "  lowest MemAvailable seen: $((peak_avail / 1024)) MiB"
    echo "  highest single-process RSS: $((peak_rss / 1024)) MiB"
fi
echo "  cgroup peak: $(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo unknown)"
echo "  cgroup max:  $(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo unknown)"
echo
echo "== did the OOM killer fire? =="
if dmesg 2>/dev/null | grep -qiE "out of memory|oom-kill|killed process"; then
    echo "  YES, the kernel log says so:"
    dmesg 2>/dev/null | grep -iE "out of memory|oom-kill|killed process" | tail -5
else
    echo "  No OOM record in dmesg."
fi
if [ -f /sys/fs/cgroup/memory.events ]; then
    echo "  cgroup memory.events:"
    sed 's/^/    /' /sys/fs/cgroup/memory.events
fi
echo
echo "== verdict =="
if [ -n "$killed_at" ]; then
    echo "  The run died at: $killed_at"
else
    echo "  The run completed every file."
fi
if dmesg 2>/dev/null | grep -qiE "oom-kill|killed process"; then
    echo "  The OOM killer fired, so memory pressure is the cause."
elif [ -f /sys/fs/cgroup/memory.events ] && grep -qE "^[a-z_]*oom_kill [1-9]" /sys/fs/cgroup/memory.events; then
    echo "  The cgroup counted an oom_kill, so memory pressure is the cause."
else
    echo "  No OOM record. Memory was not the cause, so the remaining explanation"
    echo "  is that something signsalled this process group, or the WSL VM was"
    echo "  reclaimed without a kernel record. Not established either way."
fi
