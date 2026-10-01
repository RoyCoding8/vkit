#!/bin/bash
# Print the per-file totals from the junit reports of a completed run.
#
# The reports are the record, not the console output: pytest -q writes its count
# summary to a terminal writer that is not stdout, so a redirected run keeps only
# the progress dots. junitxml is a file, so it survives both the redirect and a
# killed run, and summing it is the only way to get a total that can be trusted.
#
# Run:  bash scripts/posix-totals.sh [report-directory]
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
cd "$REPO_ROOT"
# One definition of the PATH a POSIX run needs, so no entry point can silently
# lose a directory. See scripts/posix-env.sh for why each is there.
. "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
posix_path

DIR="${1:-$HOME/vkit-posix-reports/posix-suite}"
echo "reading $DIR"
echo

"$VENV/bin/python" - "$DIR" <<'PY'
import glob
import os
import sys
import xml.etree.ElementTree as ET

directory = sys.argv[1]
rows = []
for path in sorted(glob.glob(os.path.join(directory, "*.xml"))):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        rows.append((os.path.basename(path)[:-4], "UNREADABLE", str(exc)[:40], 0, 0, 0))
        continue
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None:
        rows.append((os.path.basename(path)[:-4], "NO SUITE", "", 0, 0, 0))
        continue
    rows.append((
        os.path.basename(path)[:-4],
        "",
        "",
        int(suite.get("tests", 0)),
        int(suite.get("failures", 0)) + int(suite.get("errors", 0)),
        int(suite.get("skipped", 0)),
    ))

print(f"{'file':<34}{'tests':>7}{'fail+err':>10}{'skip':>7}  note")
for name, note, detail, tests, bad, skipped in rows:
    label = name.replace("test_", "")
    print(f"{label:<34}{tests:>7}{bad:>10}{skipped:>7}  {note}{detail}")

tests = sum(r[3] for r in rows)
bad = sum(r[4] for r in rows)
skipped = sum(r[5] for r in rows)
# A report that could not be parsed contributes nothing to TOTAL, so it would
# otherwise read as neither failed nor run.
unusable = [name for name, note, _detail, _t, _b, _s in rows if note]
print()
print("=" * 62)
print(f"files with a report: {len(rows)}")
if unusable:
    print(f"unusable report: {', '.join(unusable)}")
print(f"TOTAL  {tests} tests: {bad} failed or errored, {skipped} skipped")
print(f"PASSED {tests - bad - skipped}")
print("=" * 62)

# The exit code is the verdict, and it has to agree with the numbers above. A
# script that prints a failure count and exits 0 is worse than one that crashes,
# because a reader trusts the exit code. So a failure or an error is a failure,
# an unusable report is a failure, and no reports at all is a failure rather
# than a clean bill of health for a directory that was never written to.
sys.exit(1 if (bad or unusable or not rows) else 0)
PY
