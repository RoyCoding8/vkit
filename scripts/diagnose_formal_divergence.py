"""Replay the formal correspondence divergence deterministically.

tests/test_formal_correspondence.py::test_the_core_and_the_reference_model_agree
fails on this host once hypothesis is installed, on the two-step sequence:

    [('record', 'w1', 'unit', 'rev1', 'FAIL'), ('crash', 'w1')]

It diverges on step 1, the `crash`: the core answers BLOCKED where the reference
model answers REJECTED, and the two agree on the generation (2 and 2). So this is
a disagreement about what a crashed attempt's recorded FAIL still proves, not
about generations or claims.

This uses the same `Core` adapter the test uses, so the replay cannot differ from
the test by reimplementing anything, and it prints what each side decided.

Run:  bash scripts/posix-run.sh scripts/diagnose_formal_divergence.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "formal"))
sys.path.insert(0, str(ROOT / "tests"))

import reference  # noqa: E402
from test_formal_correspondence import Core, _reference_init  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.tasks import compute_readiness  # noqa: E402

SEQUENCE = [
    ("record", "w1", "unit", "rev1", "FAIL"),
    ("crash", "w1"),
]


def main() -> int:
    work = Path(tempfile.mkdtemp())
    store = Store(work / "state.sqlite3")
    real = Core(store)
    ideal = _reference_init()

    print("== replaying the sequence hypothesis found ==")
    for step, op in enumerate(SEQUENCE):
        kind = op[0]
        if kind == "record":
            _, owner, check, revision, outcome = op
            print(f"step {step}: record {owner} {check} @ {revision} -> {outcome}")
            real.record(owner, check, revision, outcome)
            ideal = ideal.record(owner, check, revision, outcome)
        elif kind == "crash":
            _, owner = op
            print(f"step {step}: crash {owner}")
            real.crash(owner)
            ideal = ideal.crash(owner)
            ideal = ideal.reconcile(owner)
        print(f"  generations: core={real.generations('w1')} "
              f"reference={ideal.generations['w1']}")
        for owner in reference.OWNERS:
            core = real.decide(owner, ideal.revision)
            ref = ideal.decide(owner, ideal.generations[owner], ideal.revision)
            mark = "  " if core == ref else "!!"
            print(f"  {mark} {owner}: core={core:8} reference={ref:8}")
            if core != ref:
                runs = [
                    (r["check_id"], r["result"])
                    for r in store.list_runs(limit=50)
                    if r.get("task_id") == owner
                ]
                print(f"     core runs recorded for {owner}: {runs}")
                detail = compute_readiness(
                    store, owner, required_check_ids=list(reference.REQUIRED_CHECKS)
                )
                print(f"     core gaps: {detail.gaps}")
                print(f"     core keeps evidence at generations "
                      f"{sorted(getattr(ideal, 'generations', {}))}: "
                      f"the reference still holds the recorded FAIL at generation "
                      f"{ideal.generations[owner]}")

    print()
    print("== what this is ==")
    print("  The generation advanced identically on both sides, so this is not a")
    print("  disagreement about generations or claims. It is a disagreement about")
    print("  whether a recorded FAIL still counts as evidence after its attempt has")
    print("  been superseded: the core drops it, the reference model keeps it.")
    print()
    print("  It is NOT a POSIX finding. The test drives Store and pure functions and")
    print("  touches no process, no path convention and no OS API, so it would fail")
    print("  identically on Windows. Hypothesis only found it because the file had")
    print("  been skipping on every host where it ran, which is the same")
    print("  never-actually-ran problem GAP-2 is about.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
