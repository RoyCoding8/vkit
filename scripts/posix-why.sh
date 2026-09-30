#!/bin/bash
# Run one test file and show where each failure came from.
#
# A dot line like "FFEEEE" says how many tests failed and how many errored but
# not why, and the reason is the part worth reading. This prints the assertion
# line and the first error line for each, which is enough to classify without
# dumping a full traceback per test.
#
# Run:  bash scripts/posix-why.sh test_mcp_stdio
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
export PATH="$VENV/bin:$PATH"

stem="$1"
shift || true

out="tmp/why-$stem.txt"
setsid timeout --signal=KILL 900 \
    "$VENV/bin/python" -u -m pytest "tests/test_${stem}.py" -q -p no:cacheprovider \
    --tb=line -rf "$@" > "$out" 2>&1
status=$?

echo "=== tests/test_${stem}.py  exit=$status ==="
head -1 "$out"
echo
echo "--- where each failure came from ---"
grep -E "^/mnt.*:[0-9]+: " "$out" | head -20
echo
echo "--- the named failures ---"
grep -E "^FAILED" "$out" | sed 's/ - .*//' | head -20
