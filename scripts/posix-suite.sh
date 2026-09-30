#!/bin/bash
# Run the suite one file at a time, so one death does not hide the other files.
#
# A whole-suite run was killed twice on this host with no traceback and no
# summary line, so the only trustworthy result is one that reports each file
# separately. A file that dies says so here instead of taking the run with it.
#
# Run:  bash scripts/posix-suite.sh            # every file
#       bash scripts/posix-suite.sh test_cli   # just that one
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
mkdir -p tmp/posix-suite
export PATH="$VENV/bin:$PATH"

if [ "$#" -gt 0 ]; then
    FILES=$(ls tests/test_$*.py 2>/dev/null)
else
    FILES=$(ls tests/test_*.py)
fi

total_pass=0
died=""
for f in $FILES; do
    name=$(basename "$f" .py)
    out="tmp/posix-suite/$name.txt"
    # Each file runs in its own session, so a group signal one of them sends
    # cannot reach this loop, and a file that hangs is bounded rather than
    # taking the run with it.
    setsid timeout --signal=KILL 900 \
        "$VENV/bin/python" -u -m pytest "$f" -q -p no:cacheprovider --tb=line \
        > "$out" 2>&1
    status=$?
    # pytest's -q summary line ends in a count of the slowest test, so the
    # counts are in the middle. Reading the whole line avoids a grep that
    # silently matches nothing and reports "<no summary>" for a file that
    # actually passed.
    summary=$(grep -E "^[0-9]+ (passed|failed)|passed|failed" "$out" | tail -1 | cut -c1-90)
    printf '%-34s exit=%-4s %s\n' "$name" "$status" "${summary:-<no output>}"
    if [ "$status" -ne 0 ] && ! grep -qE "passed|failed|error" "$out"; then
        died="$died $name"
    fi
done

echo
if [ -n "$died" ]; then
    echo "files that died without any pytest summary:$died"
    exit 1
fi
echo "every file produced a pytest summary"
