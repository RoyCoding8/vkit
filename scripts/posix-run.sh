#!/bin/sh
# Run something in the WSL Ubuntu instance against this checkout.
#
# MSYS_NO_PATHCONV is the whole point of this script. Git Bash rewrites any
# argument that looks like an absolute POSIX path before wsl.exe ever sees it,
# so `/mnt/d/AI` arrives as `C:/Program Files/Git/mnt/d/AI` and every call fails
# with a path that has nothing to do with the repository. A worker lost a whole
# cycle to this and reported it as a quoting problem; the apostrophe and space in
# the directory name are a red herring.
#
# The glob below is deliberate too: it avoids quoting the space entirely, so
# there is exactly one layer of quoting to get wrong instead of three.
set -eu
REPO=$(echo /mnt/d/AI/Poteto*Style)
cd "$REPO"
exec ./.venv-posix/bin/python -m pytest "${@:-tests/ -q}"
