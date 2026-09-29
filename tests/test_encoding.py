"""Paths must round-trip through git exactly. Encoding guards.

git emits UTF-8 paths. `text=True` decodes with the locale's ANSI code page,
which silently produces a *different* string for a non-ASCII path rather than
raising. The failure then surfaces much later as WinError 267 from an unrelated
CreateProcess, so it reads as a mysterious launch error rather than an encoding
bug. It cost two workstreams an afternoon before it was traced here.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_no_module_decodes_subprocess_output_with_the_locale_code_page() -> None:
    """Guard the class of bug, not one instance.

    Reading a path back with the wrong encoding does not look wrong in review, so
    make it a test rather than a convention.
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"\btext\s*=\s*True\b", line):
                offenders.append(f"{path.relative_to(_SRC)}:{number}: {line.strip()}")
    assert not offenders, (
        "decode subprocess output with encoding='utf-8', not text=True:\n  "
        + "\n  ".join(offenders)
    )
