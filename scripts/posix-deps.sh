#!/bin/bash
# Report which packages the POSIX venv has, and install what the suite needs.
#
# hypothesis is required by tests/test_formal_correspondence.py, which imports
# it at module scope via importorskip. Without it the whole file skips, so the
# two property tests never run while the suite still reads green. The file's own
# comment claimed the two tests that do not need hypothesis still ran; both of
# them are decorated @given, so that claim was false and the coverage it
# described did not exist.
#
# Run:  bash scripts/posix-deps.sh
set -euo pipefail

VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
    echo "no POSIX venv at $VENV; run: bash scripts/posix-venv.sh" >&2
    exit 1
fi

# One definition of the PATH a POSIX run needs. This script only installs into
# the venv and reports versions, so it has nothing to lose a directory from yet,
# and the point of sourcing it here is that the day it grows a line which does,
# the PATH is already right. See scripts/posix-env.sh.
. "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
posix_path

# pywin32 is deliberately absent: pyproject.toml declares it
# `sys_platform == 'win32'` and nothing on the POSIX path imports it.
"$PY" -m pip install --quiet \
    "jsonschema>=4.23,<5" \
    "pytest>=8.0" \
    "mcp>=2.2,<3" \
    "anyio>=4.5" \
    "hypothesis>=6.100"

echo "== versions =="
"$PY" - <<'PY'
import importlib.metadata as md
for name in ("jsonschema", "pytest", "mcp", "anyio", "hypothesis", "vkit"):
    try:
        print(f"  {name}: {md.version(name)}")
    except md.PackageNotFoundError:
        print(f"  {name}: NOT INSTALLED")
PY
