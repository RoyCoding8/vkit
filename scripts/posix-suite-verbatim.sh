#!/bin/bash
# Run the whole suite one file at a time and print pytest's own count line.
#
# The reason this script exists at all: pytest's count summary goes to a terminal
# writer rather than stdout, so a redirected run keeps only the progress dots and
# any grep for "passed" finds nothing on a file that passed. Two things were
# measured while working this out.
#
# First, `script` supplies a pseudo-terminal, and that alone does not help:
# under `script` with `-q`, pytest still printed only the dots. Second, `-q`
# itself is what suppresses the count line. Run WITHOUT `-q`, with or without a
# pty, pytest writes "6 passed in 3.39s" where a redirect can see it.
#
# So the pty is dropped and the count comes from the default reporter. The
# per-file loop is kept because a whole-suite run in one process was killed
# repeatedly part-way through on this host, and running each file in its own
# session under setsid means a file that signals a process group cannot take the
# loop with it.
#
# Run:  bash scripts/posix-suite-verbatim.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
OUT="$HOME/vkit-posix-reports/verbatim"

cd "$REPO_ROOT"
# One definition of the PATH a POSIX run needs, so no entry point can
# silently lose a directory. See scripts/posix-env.sh for why each is there.
. "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
posix_path
rm -rf "$OUT"
mkdir -p "$OUT"

for f in tests/test_*.py; do
    name=$(basename "$f" .py)
    setsid timeout --signal=KILL 900 \
        "$VENV/bin/python" -m pytest "$f" -p no:cacheprovider --tb=no --color=no \
        > "$OUT/$name.txt" 2>&1
done

echo "== pytest's own count line, per file =="
for f in tests/test_*.py; do
    name=$(basename "$f" .py | sed 's/^test_//')
    line=$(grep -E "[0-9]+ (passed|failed|skipped|error)|no tests ran" "$OUT/$name.txt" | tail -1)
    printf '  %-30s %s\n' "$name" "${line:-<no count line>}"
done | tee "$OUT/summary.txt"

echo
echo "== totals =="
"$VENV/bin/python" - "$OUT" <<'PY'
import glob
import os
import re
import sys

pattern = re.compile(r"(\d+) (passed|failed|skipped|error|errors)")
tests = failed = skipped = 0
missing = []
reports = sorted(glob.glob(os.path.join(sys.argv[1], "test_*.txt")))
for path in reports:
    text = open(path, encoding="utf-8", errors="replace").read()
    counts = {kind: int(n) for n, kind in pattern.findall(text)}
    if not counts:
        missing.append(os.path.basename(path))
        continue
    tests += sum(counts.values())
    failed += counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    skipped += counts.get("skipped", 0)

print(f"TOTAL  {tests} tests: {failed} failed or errored, {skipped} skipped")
print(f"PASSED {tests - failed - skipped}")
if missing:
    print(f"no count parsed for: {', '.join(missing)}")

# The exit code is the verdict, and it has to agree with the numbers above.
# A script that prints a failure count and exits 0 is worse than one that
# crashes, because a reader trusts the exit code. Two things count as a
# failure beyond the failure count itself: a report whose count line could not
# be parsed, which contributes nothing to TOTAL and would otherwise read as
# neither failed nor run; and no reports at all, which is the same false green
# one layer up.
sys.exit(1 if (failed or missing or not reports) else 0)
PY
