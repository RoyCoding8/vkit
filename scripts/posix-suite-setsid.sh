#!/bin/bash
# Test whether a long suite run is dying because something signals its group.
#
# Memory is ruled out already: 6.5 GiB available, zero oom-kill lines in dmesg.
# That leaves the group signal as the explanation that fits: a run of these
# tests spawns real processes, and a kill aimed at a process group reaches every
# member of it, including the shell that launched the run.
#
# This starts the run in its OWN session with setsid, so the run and everything it
# spawns share a group that this shell is not in, and then reports whether the
# shell survived and how far the run got. If the run completes under setsid and
# dies without it, the group signal was the cause.
#
# Run:  bash scripts/posix-suite-setsid.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
export PATH="$VENV/bin:$PATH"
OUTDIR="$HOME/vkit-posix-reports/setsid-run"
rm -rf "$OUTDIR"; mkdir -p "$OUTDIR"

echo "this shell's pid: $$   its process group: $(ps -o pgid= -p $$ | tr -d ' ')"
echo "reports: $OUTDIR"
echo

# The whole suite in ONE pytest process, in its own session, so every process it
# spawns is inside that session rather than in this shell's group.
setsid "$VENV/bin/python" -u -m pytest tests/ -q -p no:cacheprovider --tb=line \
    --junitxml="$OUTDIR/all.xml" > "$OUTDIR/all.txt" 2>&1 < /dev/null &
RUNNER=$!
echo "started the suite as pid $RUNNER in its own session"

wait "$RUNNER"
status=$?

echo
echo "== the shell that launched it survived: yes (you are reading this) =="
echo "suite exit status: $status"
echo "output bytes:     $(wc -c < "$OUTDIR/all.txt")"
echo
echo "== first and last of the run =="
head -c 400 "$OUTDIR/all.txt"
echo
echo "..."
tail -c 1200 "$OUTDIR/all.txt"
echo
if [ -f "$OUTDIR/all.xml" ]; then
    echo
    echo "== totals from the junit report =="
    "$VENV/bin/python" - "$OUTDIR/all.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suite = root.find("testsuite") if root.tag == "testsuites" else root
print(f"  tests={suite.get('tests')} failures={suite.get('failures')} "
      f"errors={suite.get('errors')} skipped={suite.get('skipped')}")
PY
else
    echo
    echo "  no junit report: the run was killed before it could write one"
fi
