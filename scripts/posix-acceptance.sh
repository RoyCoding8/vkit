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
# One definition of the PATH a POSIX run needs, so no entry point can silently
# lose a directory. See scripts/posix-env.sh for why each is there.
. "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
posix_path
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

# The exit code is the verdict, and it has to agree with what was just printed.
# A script that prints "exit status: 1" and then exits 0 is worse than one that
# crashes, because a reader trusts the exit code. The child's own status is the
# verdict; pass it through rather than deciding a second time.
exit "$status"
