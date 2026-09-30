#!/bin/bash
# Run one acceptance02 row under `setsid` in its own process group, and report
# what happened without letting one row's death take the caller with it.
#
# Row 7 kills this shell on the host: it starts a 300-second bystander, and the
# POSIX branch of `supervisor.terminate_owned_tree` raises OSError by design, so
# the row's own child dies with it and the loop that invoked it never returns.
# Isolating the row in its own session makes the death a fact this script
# reports rather than a fact the caller disappears into.
#
# Run:  bash scripts/posix-one-row.sh 7
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
mkdir -p tmp

row="$1"; shift || true
out="tmp/acc02-row${row}.out"
err="tmp/acc02-row${row}.err"

# setsid detaches the row into a new session, so a group signal the row itself
# performs cannot reach this shell. `timeout` bounds a row that hangs.
setsid timeout --signal=KILL 420 \
    "$VENV/bin/python" -u scripts/acceptance02.py --only "$row" "$@" \
    > "$out" 2> "$err"
status=$?

echo "row $row exit=$status"
echo "--- stdout ---"
cat "$out"
echo "--- stderr ---"
head -30 "$err"
echo "--- bytes: out=$(wc -c < "$out") err=$(wc -c < "$err") ---"
