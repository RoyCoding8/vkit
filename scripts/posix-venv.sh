#!/bin/bash
# Create the POSIX virtualenv this repository's POSIX verification runs in.
#
# The Windows host uses a venv with pywin32. The POSIX host uses the same venv
# layout minus pywin32, which is declared `sys_platform == 'win32'` in
# pyproject.toml and is deliberately absent here. Nothing in src/vkit imports it
# on this path; scripts/check_posix_deps.py asserts that.
#
# Run:  bash scripts/posix-venv.sh
# Then: bash scripts/posix-run.sh tests/ -q
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

echo
echo "== installed =="
"$VENV/bin/python" -m pip list 2>/dev/null | grep -Ei 'jsonschema|pytest|mcp|anyio|vkit|pywin32' || true
echo
echo "== vkit console script =="
"$VENV/bin/vkit" --version 2>&1 || true
echo
echo "done. Run the suite with:"
echo "  bash scripts/posix-run.sh tests/ -q"
