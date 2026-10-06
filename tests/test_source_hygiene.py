from __future__ import annotations

import re

from helpers import ROOT

NARRATION = re.compile(r"Plan \d|[Cc]heckpoint|CONTRACT\.md|milestone|[Rr]eceipt|supervisor|task_finalize")


def test_source_describes_what_the_code_does_now_not_how_it_got_here():
    hits = [f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
            for base in ("src", "plugin")
            for path in sorted((ROOT / base).rglob("*"))
            if path.suffix in {".py", ".md", ".json", ".html"}
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if NARRATION.search(line)]
    assert hits == []
