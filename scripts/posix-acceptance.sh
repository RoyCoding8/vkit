#!/bin/bash
# Run one acceptance script with its output captured, so a silent death is visible.
#
# acceptance02.py drives real child interpreters over a 300-second budget on
# some rows, and a run that dies produces no output at all on this host. Writing
# stdout and stderr to files and echoing the exit status makes that a fact
# rather than an absence.
#
# Run:  bash scripts/posix-acceptance.sh acceptance02.py
#       bash scripts/posix-acceptance.sh acceptance02.py --only 1 2 3
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
SCRIPT="$1"; shift || true

cd "$REPO_ROOT"
OUT="$REPO_ROOT/tmp/$(basename "$SCRIPT" .py).out"
ERR="$REPO_ROOT/tmp/$(basename "$SCRIPT" .py).err"
mkdir -p "$REPO_ROOT/tmp"

echo "== running scripts/$SCRIPT $* =="
export PATH="$VENV/bin:$PATH"
"$VENV/bin/python" -u "scripts/$SCRIPT" "$@" > "$OUT" 2> "$ERR"
status=$?
echo "exit status: $status"
echo
echo "== stdout (last 60 lines of $OUT) =="
tail -60 "$OUT"
echo
echo "== stderr (last 40 lines of $ERR) =="
tail -40 "$ERR"
echo
echo "stdout bytes: $(wc -c < "$OUT")   stderr bytes: $(wc -c < "$ERR")"
