#!/bin/bash
# Run every test file and print one total, from a junitxml report.
#
# pytest's count summary goes to a terminal writer that is not stdout, so a
# redirected run shows only the progress dots and the counts are lost. junitxml
# is written as a file, so the numbers survive a redirect and can be summed
# across per-file runs.
#
# Run:  bash scripts/posix-suite-total.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
# The reports go on the WSL-native filesystem, not under /mnt/d. A whole-suite
# run was killed repeatedly part-way through on this host with no traceback and
# no summary, and the venv had already been moved off /mnt/d for the same reason:
# every file written across the DrvFs boundary is slow enough to matter and the
# long run is the thing that dies. The cause was not established, so this is
# recorded as a mitigation rather than a fix.
OUTDIR="${VKIT_POSIX_REPORTS:-$HOME/vkit-posix-reports/posix-suite}"
mkdir -p "$OUTDIR"
export PATH="$VENV/bin:$PATH"

echo "reports: $OUTDIR"
echo

for f in tests/test_*.py; do
    name=$(basename "$f" .py)
    out="$OUTDIR/$name.txt"
    xml="$OUTDIR/$name.xml"
    setsid timeout --signal=KILL 900 \
        "$VENV/bin/python" -u -m pytest "$f" -q -p no:cacheprovider --tb=line \
        --junitxml="$xml" > "$out" 2>&1
    status=$?
    if [ ! -f "$xml" ]; then
        # exit 5 is "no tests collected", which is a fact about the file rather
        # than a death. Anything else with no report is a real problem.
        if [ "$status" -eq 5 ]; then
            printf '%-32s no tests collected\n' "$name"
        else
            printf '%-32s NO REPORT (exit %s)\n' "$name" "$status"
        fi
        continue
    fi
    read -r tests failures errors skipped < <(
        "$VENV/bin/python" - "$xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suite = root.find("testsuite") if root.tag == "testsuites" else root
if suite is None:
    print("0 0 0 0")
else:
    print(suite.get("tests", 0), suite.get("failures", 0),
          suite.get("errors", 0), suite.get("skipped", 0))
PY
    )
    printf '%-32s tests=%-4s fail=%-3s err=%-3s skip=%s\n' \
        "$name" "$tests" "$failures" "$errors" "$skipped"
done

echo
"$VENV/bin/python" - "$OUTDIR" <<'PY'
import glob
import os
import sys
import xml.etree.ElementTree as ET

pattern = os.path.join(sys.argv[1], "*.xml")
tests = failures = errors = skipped = 0
for path in sorted(glob.glob(pattern)):
    root = ET.parse(path).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None:
        continue
    tests += int(suite.get("tests", 0))
    failures += int(suite.get("failures", 0))
    errors += int(suite.get("errors", 0))
    skipped += int(suite.get("skipped", 0))

print("=" * 60)
print(f"TOTAL  {tests} tests: {failures} failed, {errors} errors, {skipped} skipped")
print(f"PASSED {tests - failures - errors - skipped}")
print("=" * 60)
PY
