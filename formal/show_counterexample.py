"""Print one mutant's counterexample verbatim, for the plan's evidence record.

The plan asks for counterexample traces, not for a count. A count says four
mutants were caught; the trace says what the model did and which states it went
through, which is the part a reviewer can check against the model by hand.

This is a reporting tool over the same mutation `formal/run_mutants.py`
performs. It does not decide anything; `run_mutants.py` is the thing that
fails a build, and this is the thing that shows why it should.

Run directly:

    python formal/show_counterexample.py MUTANT_STALE_GENERATION
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TLA_DIR = ROOT / "formal" / "tla"

sys.path.insert(0, str(ROOT / "formal"))
from run_mutants import MUTANTS, _find_java, _find_jar  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {m.name for m in MUTANTS}:
        print("usage: show_counterexample.py <mutant name>", file=sys.stderr)
        print(f"known mutants: {', '.join(m.name for m in MUTANTS)}", file=sys.stderr)
        return 2

    mutant = next(m for m in MUTANTS if m.name == sys.argv[1])
    java, jar = _find_java(), _find_jar()
    if java is None or jar is None:
        print("BLOCKED: TLC is not available on this host.", file=sys.stderr)
        return 2

    source = (TLA_DIR / "OwnershipAcceptance.tla").read_text(encoding="utf-8")
    if mutant.removes not in source:
        print(f"the text {mutant.name} removes is no longer in the model", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix=f"vkit-trace-{mutant.name}-") as work:
        workdir = Path(work)
        (workdir / "OwnershipAcceptance.tla").write_text(
            source.replace(mutant.removes, mutant.replacement, 1), encoding="utf-8")
        shutil.copy(TLA_DIR / "OwnershipAcceptance.cfg", workdir / "OwnershipAcceptance.cfg")
        done = subprocess.run(
            [str(java), "-XX:+UseParallelGC", "-Xmx2g", "-cp", str(jar), "tlc2.TLC",
             "-workers", "1", "-nowarning", "-cleanup",
             "-metadir", str(workdir / "states"), "OwnershipAcceptance"],
            cwd=workdir, capture_output=True, text=True, timeout=3600, check=False,
        )

    print(f"{mutant.name}: removes the guard that {mutant.covers}")
    print(f"expected to be caught by {mutant.expect_property}")
    print("=" * 72)
    print(done.stdout + done.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
