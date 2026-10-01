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
# One definition of the PATH a POSIX run needs, so no entry point can silently
# lose a directory. See scripts/posix-env.sh for why each is there.
. "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
posix_path

if [ "$#" -gt 0 ]; then
    # Arguments are file stems, matched as tests/test_<stem>.py. The glob needs
    # the prefix spelled out, because `test_$*.py` would expand $1 against the
    # filesystem and silently select nothing.
    FILES=""
    for stem in "$@"; do
        match=$(ls "tests/test_${stem}.py" 2>/dev/null || true)
        if [ -z "$match" ]; then
            echo "no such test file: tests/test_${stem}.py" >&2
            exit 1
        fi
        FILES="$FILES $match"
    done
else
    FILES=$(ls tests/test_*.py)
fi

total_pass=0
failed_files=""
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
    # pytest -q writes progress dots to stdout and its summary line ("N passed,
    # M skipped in Xs") to a separate terminal writer, which is NOT stdout when
    # stdout is a file. So the file holds the dots and the "[100%]" marker and
    # nothing else, and grepping it for "passed" finds nothing on a file that in
    # fact passed. The dot line is therefore the verdict: '.' is a pass, 's' is a
    # skip, and anything else is an F or an E.
    flagged=$(tr -d ' \n' < "$out" | tr -cd 'FE')
    if [ -n "$flagged" ]; then
        verdict="FAILED (${flagged})"
        failed_files="$failed_files $name"
    else
        verdict="clean"
    fi
    printf '%-32s exit=%-3s %s\n' "$name" "$status" "$verdict"
    if [ "$status" -ne 0 ] && [ ! -s "$out" ]; then
        died="$died $name"
    fi
done

echo
if [ -n "$failed_files" ]; then
    echo "files with failures or errors:$failed_files"
fi
if [ -n "$died" ]; then
    echo "files that produced NO output at all, so they died:$died"
    exit 1
fi
[ -n "$failed_files" ] && exit 1
echo "no file reported a failure or an error"
