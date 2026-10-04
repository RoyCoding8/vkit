"""Check the correspondence file really skips when Hypothesis is absent.

Plan 08 is optional and Hypothesis is an optional dependency, so
test_formal_correspondence.py has to skip cleanly without it. That is a
requirement, and a requirement nobody checks is a requirement that quietly
stops being true the first time somebody adds an unguarded import at module
level.

So it is checked, by running pytest in a subprocess with hypothesis hidden from
the import system. The assertion is about the SKIP, not about the tests: the
file must report skipped, and it must not report an error, because a module
that fails to import is a broken suite rather than an absent optional
dependency.

The two non-Hypothesis tests live in test_stale_attempt_traces.py precisely so
they still run in this situation. That file is imported here too, and the
count of tests that actually executed is asserted, because "everything skipped"
would be the wrong outcome for the guard itself.
"""
from __future__ import annotations
import subproc

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BLOCKER = """
import sys

class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "hypothesis" or name.startswith("hypothesis."):
            # ModuleNotFoundError is what a genuinely absent package raises, and
            # it is a subclass of ImportError, which is what importorskip
            # catches. Returning None instead would leave the other finders to
            # succeed and hypothesis would still import.
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

sys.meta_path.insert(0, _Blocker())
for name in [n for n in sys.modules if n == "hypothesis" or n.startswith("hypothesis.")]:
    del sys.modules[name]
"""


def test_the_correspondence_file_skips_cleanly_without_hypothesis() -> None:
    driver = BLOCKER + """
import sys, pytest
sys.exit(pytest.main([
    "tests/test_formal_correspondence.py",
    "tests/test_stale_attempt_traces.py",
    "-q", "-rs", "-p", "no:cacheprovider", "-p", "no:hypothesispytest", "--no-header",
]))
"""
    done = subproc.run(
        [sys.executable, "-c", driver],
        cwd=ROOT, capture_output=True, text=True, timeout=900, check=False,
    )
    output = done.stdout + done.stderr

    assert "SKIPPED [1]" in output, (
        f"expected the correspondence file to skip without hypothesis:\\n{output[-2000:]}"
    )
    assert "Plan 08 is optional" in output, (
        f"the skip must say why it skipped, so a reader knows the toolchain was "
        f"deliberately optional rather than broken:\\n{output[-2000:]}"
    )
    assert "error" not in output.lower(), (
        f"a missing optional dependency must skip, not error:\\n{output[-2000:]}"
    )
    assert output.splitlines()[0].count(".") >= 2, (
        f"the stale-attempt trace tests must still run without hypothesis, since "
        f"they cover the guard this plan asks to be mutation-checked:\\n{output[-2000:]}"
    )
