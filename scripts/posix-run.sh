#!/bin/bash
# Run something from this checkout against the POSIX virtualenv.
#
# Wraps the interpreter path so a POSIX run never silently falls back to the
# system python3, which has no pytest and no mcp. Everything after the script
# name goes to pytest, or to any other command, unchanged.
#
# The venv's bin directory goes on PATH as well, because the example manifests
# and several acceptance rows name the interpreter `python` and this host ships
# only `python3`. Measured without it: every check run reported BLOCKED with
# `prerequisite_missing: python: 'python' is not on PATH`, which is a missing
# name rather than a product defect. The venv provides `python`, so putting it
# on PATH is the environment being set up, not a test being relaxed.
#
# Run:  bash scripts/posix-run.sh tests/ -q
#       bash scripts/posix-run.sh scripts/acceptance02.py
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"

if [ ! -x "$VENV/bin/python" ]; then
    echo "no POSIX venv at $VENV; run: bash scripts/posix-venv.sh" >&2
    exit 1
fi

cd "$REPO_ROOT"

# The PATH and the git directory a POSIX run needs, from one definition, so this
# entry point cannot drift from the others. See scripts/posix-env.sh.
#
. "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
posix_path

# A leading `-m pytest` unless the caller named a command. Without this the
# script ran `python tests/test_recover.py -q`, which is not a pytest
# invocation: Python tried to import the test file as a module and pytest never
# ran at all. It exited 0 having written nothing, which read as a passing suite
# and was the reason the POSIX run looked like it produced no results. Measured
# before the fix: `bash -x` showed the exec line and the run returned RC=0 with
# an empty junit file, while the same tests through `python -m pytest` printed
# 38 dots and a summary.
if [ $# -eq 0 ]; then
    set -- tests/ -q
elif [ "${1#-}" = "$1" ] && [ ! -e "$1" ] && [[ "$1" != */* ]]; then
    # A bare word with no path and no slash: treat it as a script under
    # scripts/. The path is added here because `python -m acceptance02` does not
    # resolve -- scripts/ is not on sys.path -- and the alternative is an error
    # message that looks like a missing dependency.
    set -- -m pytest "scripts/$1"
else
    set -- -m pytest "$@"
fi

exec "$VENV/bin/python" "$@"
