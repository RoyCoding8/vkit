#!/bin/bash
# Set up everything a POSIX verification run of this repository needs.
#
# Three things, and each one exists because a run failed without it:
#
#   1. A venv. The Windows host uses one with pywin32. The POSIX host uses the
#      same layout minus pywin32, which pyproject.toml declares
#      `sys_platform == 'win32'` and which is deliberately absent here.
#   2. The venv's bin on PATH, because the example manifests name the
#      interpreter `python` and a stock Linux ships only `python3`. Without it
#      every check reported BLOCKED with prerequisite_missing.
#   3. node, because examples/node-cli and examples/node-http are Node programs.
#      tests/test_features.py skips honestly when node is missing, but a skipped
#      suite verifies nothing about them, so node is installed rather than
#      tolerated.
#
# Run:  bash scripts/posix-venv.sh
# Then: bash scripts/posix-run.sh -m pytest tests/ -q
set -euo pipefail

# The venv lives on the WSL-native filesystem by default, not beside the repo.
# Measured: `python3 -m venv` under /mnt/d did not finish in ten minutes,
# because every file it writes crosses the DrvFs boundary into NTFS. On
# /home it takes seconds. The repo itself stays on /mnt/d so the worktree is
# this checkout; only the interpreter is relocated. Override with
# VKIT_POSIX_VENV=/some/path.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"

echo "repo: $REPO_ROOT"
echo "venv: $VENV"

if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet "jsonschema>=4.23,<5" "pytest>=8.0" "mcp>=2.2,<3" "anyio>=4.5"

# The editable install puts this checkout's src on sys.path, so an acceptance
# script that spawns the vkit console script exercises this tree and not
# whichever vkit happens to be importable.
"$VENV/bin/python" -m pip install --quiet --no-deps -e "$REPO_ROOT"

if ! command -v node > /dev/null; then
    echo
    echo "node is not installed; installing it for the Node examples"
    if [ "$(id -u)" -eq 0 ]; then
        apt-get update -qq && apt-get install -y -qq nodejs
    else
        sudo apt-get update -qq && sudo apt-get install -y -qq nodejs
    fi
fi

echo
echo "== installed =="
"$VENV/bin/python" -m pip list 2>/dev/null | grep -Ei 'jsonschema|pytest|mcp|anyio|vkit|pywin32' || true
echo "node:  $(node --version 2>&1 || echo MISSING)"
echo "python on PATH from the venv: $("$VENV/bin/python" -c 'import sys; print(sys.executable)')"
echo
echo "== vkit console script =="
"$VENV/bin/vkit" --version 2>&1 || true
echo
echo "done. Run the suite with:"
echo "  bash scripts/posix-run.sh -m pytest tests/ -q"
