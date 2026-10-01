#!/bin/bash
# Re-run one acceptance02 row with its condition printed field by field.
#
# Rows 4 and 5 print a narrative describing what happened and then record FAIL.
# The narrative is written unconditionally, so it reads like a pass while the
# condition that decided the verdict was false. This prints the fields the
# condition is built from, so the failing term is named rather than guessed.
#
# Run:  bash scripts/posix-row-detail.sh 5
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
# One definition of the PATH a POSIX run needs, so no entry point can silently
# lose a directory. See scripts/posix-env.sh for why each is there.
. "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
posix_path
mkdir -p tmp

row="$1"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

VKIT_DBG="$tmp" "$VENV/bin/python" -u scripts/acceptance02.py --only "$row" > "tmp/acc02-row${row}.out" 2>&1
status=$?
cat "tmp/acc02-row${row}.out"
echo
echo "== what the row saw, read from its own child output =="
grep -oE "ACCEPTANCE02 \{.*\}" "tmp/acc02-row${row}.out" | head -20 || echo "(no ACCEPTANCE02 line captured in the row output)"

# The exit code is the verdict, and it has to agree with the output above. A
# script that shows a row recording FAIL and then exits 0 is worse than one that
# crashes, because a reader trusts the exit code. The row's own status is the
# verdict; pass it through rather than deciding a second time.
exit "$status"
