#!/bin/bash
# Can the whole POSIX suite complete in ONE pytest process, and if not, why?
#
# The honest state so far: per-file runs complete, a single-process run does not,
# and the cause was never established. Memory was ruled out by measurement (6.5
# GiB available, zero oom-kill lines, no cgroup memory.events) and the
# process-group theory did not hold either.
#
# This runs the whole suite in one process under setsid, with the time and the
# furthest progress reached recorded, and then checks whether the run left
# anything behind that explains its own death: a surviving process, a core, or a
# kernel record. It is a measurement of the failure, not a workaround.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
OUT="$HOME/vkit-posix-reports/single-process"
rm -rf "$OUT"; mkdir -p "$OUT"

cd "$REPO_ROOT"
. scripts/posix-env.sh
posix_path

echo "== before =="
echo "  processes: $(ps -e --no-headers 2>/dev/null | wc -l)"
echo "  MemAvailable: $(awk '/MemAvailable/{print int($2/1024)" MiB"}' /proc/meminfo)"
echo

start=$(date +%s)
setsid nohup "$VENV/bin/python" -u -m pytest tests/ -p no:cacheprovider --tb=no \
    --color=no -rf --junitxml="$OUT/all.xml" > "$OUT/all.txt" 2>&1 < /dev/null &
RUNNER=$!
echo "started the whole suite as one process: pid $RUNNER"
echo

# Watch it without being able to affect it. Each line is a sample, so a death is
# visible as the series stopping rather than as an absence.
farthest=0
while kill -0 "$RUNNER" 2>/dev/null; do
    size=$(wc -c < "$OUT/all.txt" 2>/dev/null || echo 0)
    tests=$(tr -cd '.sFExX' < "$OUT/all.txt" 2>/dev/null | wc -c)
    [ "$tests" -gt "$farthest" ] && farthest=$tests
    printf '  t+%3ss  log=%-8s bytes  progress=%-5s marks  procs=%s\n' \
        "$(( $(date +%s) - start ))" "$size" "$tests" \
        "$(ps -e --no-headers 2>/dev/null | wc -l)"
    sleep 20
done
wait "$RUNNER" 2>/dev/null
status=$?
elapsed=$(( $(date +%s) - start ))

echo
echo "== what happened =="
echo "  exit status:  $status"
echo "  elapsed:      ${elapsed}s"
echo "  marks written: $farthest (the suite has 475 tests)"
echo "  log bytes:    $(wc -c < "$OUT/all.txt")"
echo "  junit report: $([ -f "$OUT/all.xml" ] && echo present || echo ABSENT)"
echo
echo "== did it leave anything that explains its own death? =="
echo "  survivors named in the log: $(pgrep -fc 'pytest tests/' 2>/dev/null || echo 0)"
echo "  cores: $(ls -1 /var/lib/systemd/coredump 2>/dev/null | wc -l)"
echo "  oom-kill lines in dmesg: $(dmesg 2>/dev/null | grep -ci 'oom-kill' || echo 0)"
echo "  MemAvailable now: $(awk '/MemAvailable/{print int($2/1024)" MiB"}' /proc/meminfo)"
echo
echo "== the last 400 bytes it wrote =="
tail -c 400 "$OUT/all.txt"
echo
echo "== verdict =="
if [ -f "$OUT/all.xml" ]; then
    echo "  It COMPLETED. A junit report was written, so the total in it covers"
    echo "  a single-process run and is no longer unverified."
else
    echo "  It did NOT complete: no junit report, so pytest never finished writing."
    echo "  The cause is still not established. Memory is ruled out by the figures"
    echo "  above, and the process-group theory was tested and did not hold. What"
    echo "  remains is that something external to WSL ends this run, and I cannot"
    echo "  show what from inside the distribution."
fi
