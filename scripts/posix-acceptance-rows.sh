#!/bin/bash
# Run each acceptance02 row in its own process and report each verdict separately.
#
# The full run on this host dies with no output at all, which is a fact about
# the harness rather than about any one row. Running each row in isolation
# turns that silence into a per-row verdict, and a row that kills its own
# process is then visible instead of taking the rest of the table with it.
#
# Run:  bash scripts/posix-acceptance-rows.sh
#       bash scripts/posix-acceptance-rows.sh 3 7 9
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
mkdir -p tmp

# The example manifests name the interpreter `python`; the venv provides it.
# See scripts/posix-run.sh for the measurement behind this.
export PATH="$VENV/bin:$PATH"

if [ "$#" -gt 0 ]; then
    ROWS="$*"
else
    ROWS=$(seq 1 14)
fi

overall=0
for row in $ROWS; do
    out="tmp/acc02-row${row}.out"
    err="tmp/acc02-row${row}.err"
    "$VENV/bin/python" -u scripts/acceptance02.py --only "$row" > "$out" 2> "$err"
    status=$?
    verdict=$(grep -oE "\[ (PASS|FAIL|SKIP) \] row [0-9]+" "$out" | head -1)
    summary=$(grep -E "^[0-9]+/[0-9]+ acceptance rows pass" "$out" | tail -1)
    printf 'row %-3s exit=%-3s %s\n' "$row" "$status" "${verdict:-<no verdict>}"
    if [ -n "$summary" ]; then
        printf '        %s\n' "$summary"
    fi
    if [ -s "$err" ]; then
        printf '        stderr: %s\n' "$(head -3 "$err" | tr '\n' ' ')"
    fi
    if [ "$status" -ne 0 ] || [ -z "$verdict" ]; then
        overall=1
    fi
done
exit "$overall"
