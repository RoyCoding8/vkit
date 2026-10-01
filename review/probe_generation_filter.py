"""Does the core still accept generation-1 evidence at generation 2?

`formal/RESULTS.md` has asserted since Plan 08 that "`compute_readiness` filters
by neither the attempt generation nor the source and policy revision. A
generation 2 attempt therefore reaches READY on generation 1 evidence alone."
`formal/reference.py` has since been corrected to the opposite reading about
generation: its `acceptable` docstring records that the method once accepted the
generation argument and ignored it, that this "was true until the readiness fix,
and then it was not", and that the property test found the disagreement on its
first execution.

Two records in the same repository therefore make opposite claims about the same
code, which is the defect class R5 exists to remove. This probe measures it over
the real `Store`, through the public API, and prints which of the two the product
actually does. The revision half of the old sentence is measured by
`probe_revision_movement.py`; this one covers generation only, because the two
halves were repaired independently and the old sentence bundled them.

    python review/probe_generation_filter.py

Writes `review/generation-filter.json`. Exits 0 when the measured verdicts are
the ones this docstring claims and 1 when they are not.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit import tasks  # noqa: E402
from vkit.storage import Store  # noqa: E402

TASK = "t"
CHECKS = ("unit", "lint")


def _contract() -> dict:
    return {
        "repository": {"root": ".", "git_common_dir": "."},
        "policy_digest": "pd",
        "required_checks": list(CHECKS),
        "scope": "generation probe",
        "resources": [],
        "declared": {"owner": TASK},
    }


def _seed(store: Store, generation: int) -> None:
    """One passing run per required check, at the generation named."""
    for index, check in enumerate(CHECKS):
        run_id = f"r-{check}-{generation}-{index}"
        store.register_run(
            run_id, check, task_id=TASK, attempt=generation,
            source={"inventory_digest": "src-1"}, configuration_digest="pd",
            fixture_digest=None,
        )
        store.publish(run_id, {
            "run_id": run_id, "lifecycle": "terminal", "ended_at": "t",
            "outcome": {"result": "PASS", "scenarios": [
                {"id": "s", "result": "PASS", "observation": "seeded"}]},
        })


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vkit-generation-") as raw:
        store = Store(Path(raw) / "state.sqlite3")
        tasks.open_task(
            store, task_id=TASK, contract=_contract(), policy_digest="pd",
        )
        _seed(store, generation=1)
        at_one = tasks.compute_readiness(
            store, TASK, required_check_ids=list(CHECKS)
        ).readiness

        tasks.supersede_task(store, TASK)
        at_two = tasks.compute_readiness(
            store, TASK, required_check_ids=list(CHECKS)
        ).readiness

        # And a fresh attempt at the new generation is decided on its own runs
        # only, so the answer above is a refusal and not a permanent BLOCKED.
        _seed(store, generation=2)
        repaired = tasks.compute_readiness(
            store, TASK, required_check_ids=list(CHECKS)
        ).readiness

        recorded = tasks.get_task(store, TASK)
        report = {
            "generation_1_with_its_own_evidence": at_one,
            "generation_2_with_generation_1_evidence_only": at_two,
            "generation_2_after_its_own_runs": repaired,
            "current_generation": recorded.generation,
            "gaps_at_generation_2": list(
                tasks.compute_readiness(
                    store, TASK, required_check_ids=list(CHECKS)
                ).gaps
            ),
        }

    (ROOT / "review" / "generation-filter.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    print("does the core filter evidence by the attempt generation")
    print("=" * 88)
    print(f"  generation 1, its own evidence      {at_one}")
    print(f"  generation 2, generation-1 evidence {at_two}")
    print(f"  generation 2, its own evidence      {repaired}")
    print(f"  current generation on the task      {recorded.generation}")
    for gap in report["gaps_at_generation_2"]:
        print(f"  gap now                            {gap}")
    print("=" * 88)
    print("written: review/generation-filter.json")

    if (at_one, at_two, repaired) != ("READY", "BLOCKED", "READY"):
        print(
            "\nFAILED: the core does not behave as both records describe. "
            f"got {(at_one, at_two, repaired)}"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
