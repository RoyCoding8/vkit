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

export PATH="$VENV/bin:$PATH"
cd "$REPO_ROOT"
exec "$VENV/bin/python" "$@"
