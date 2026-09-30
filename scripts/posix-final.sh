#!/bin/bash
# The definitive single-process POSIX run, on whatever is committed.
#
# Detached with setsid and given a pseudo-terminal, because the wsl.exe wrapper
# on this host is cut mid-invocation and takes a foreground run with it. The
# venv's bin and ~/.local/bin go on PATH from the one definition, and the
# repository is pointed at by its /mnt form, because a worktree's .git names a
# Windows path WSL cannot resolve.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
. scripts/posix-env.sh
posix_path
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
OUT="$HOME/vkit-posix-reports"
mkdir -p "$OUT"

echo "repo:   $(pwd -P)"
echo "GIT_DIR: ${GIT_DIR:-<unset, this is a normal checkout>}"
echo "python:  $(command -v python || echo NONE)"
echo "claude:  $(command -v claude || echo NONE)"
echo
"$VENV/bin/python" -m pytest tests/ -p no:cacheprovider --tb=no --color=no -rf \
    --junitxml="$OUT/final.xml" 2>&1 | tail -25
