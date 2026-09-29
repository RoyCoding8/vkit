"""Make the tests exercise THIS checkout, not whichever one an install points at.

A worktree worker runs against a shared virtualenv, and an editable install puts
one absolute `src` directory on sys.path. Without this, `import vkit` from a
worktree silently resolves to the main checkout, so a worker's suite can pass
against code it did not write, or miss its own module entirely. That is a false
pass, the one failure mode this product exists to prevent.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

if _SRC.is_dir():
    # Drop other entries that point at a vkit src tree, then put this one first.
    # sys.path[0] is the test directory, so index 0 beats the .pth entry that the
    # editable install appends at interpreter start.
    _mine = str(_SRC)
    sys.path[:] = [e for e in sys.path if e != _mine]
    sys.path.insert(0, _mine)
