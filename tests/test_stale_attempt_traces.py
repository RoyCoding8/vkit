"""The two traces the mutation harness breaks, in a file that needs nothing.

Plan 08 is optional and Hypothesis is an optional test dependency, so these two
tests are deliberately NOT in test_formal_correspondence.py. That file skips
itself when Hypothesis is missing, and if these lived there they would skip with
it. These are the tests that cover the stale-generation guard, which is the
guard the plan asks to be mutation-checked, and that must not go unchecked
because a package is absent from a host.

Both are single fixed traces rather than generated sequences, so they need only
the standard library, pytest, and the core.

What they assert is what a caller observes. Neither reaches into SQL, a private
helper, or the order in which the core did its work.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "formal"))

from vkit.storage import ConflictError, Store  # noqa: E402
from vkit.tasks import (  # noqa: E402
    compute_readiness,
    current_generation,
    get_task,
    open_task,
    record_readiness,
    supersede_task,
)

import reference  # noqa: E402

REQUIRED_CHECKS = reference.REQUIRED_CHECKS


def _record_pass(store: Store, owner: str, check: str, revision: str = "rev1") -> None:
    """Publish one terminal passing run, the way a completed check does."""
    run_id = f"r-{owner}-{check}"
    store.register_run(
        run_id, check, task_id=owner, attempt=current_generation(store, owner),
        source={"revision": revision}, configuration_digest="c", fixture_digest=None,
    )
    store.publish(run_id, {
        "run_id": run_id, "lifecycle": "terminal", "ended_at": "t",
        "outcome": {"result": "PASS", "scenarios": [
            {"id": "s", "result": "PASS", "observation": "seeded"}]},
    })


def test_a_superseded_attempt_cannot_publish_ready(tmp_path: Path) -> None:
    """The trace a removed stale-generation guard lets through.

    An attempt computes READY at generation 1 from evidence it actually
    produced. It is then reassigned, which advances it to generation 2 and
    deliberately leaves its claims held at generation 1. The old attempt, still
    holding the verdict it already computed, tries to record it.

    `record_readiness` compares the generation that computed the verdict
    against the current one and refuses. Removing that comparison
    (MUTANT_STALE_GENERATION) lets the superseded attempt stamp READY on the
    reassigned task, and the assertion below is the counterexample: a mutated
    core leaves the task row reading READY.
    """
    store = Store(tmp_path / "state.sqlite3")
    open_task(store, task_id="w1", contract={"g": "x"}, policy_digest="pd")
    for check in REQUIRED_CHECKS:
        _record_pass(store, "w1", check)

    verdict = compute_readiness(store, "w1", required_check_ids=list(REQUIRED_CHECKS))
    assert verdict.readiness == "READY"
    assert verdict.context["generation"] == 1

    # Reassigned. The claims stay held at generation 1, which is the point.
    supersede_task(store, "w1")
    assert current_generation(store, "w1") == 2

    with pytest.raises(ConflictError):
        record_readiness(store, "w1", verdict)

    assert get_task(store, "w1").readiness is None, (
        "a superseded attempt published a verdict; the stale-generation guard "
        "did not hold"
    )


def test_one_missing_required_check_is_never_ready(tmp_path: Path) -> None:
    """The row the whole product rests on: absent evidence is never success.

    One required check has a passing run, the other has nothing at all. The
    verdict must be BLOCKED and must name the missing check.

    MUTANT_MISSING_CHECK drops the last entry from the readiness loop, so the
    absent check is never examined. The test uses two required checks, and the
    mutated core returns READY where this asserts BLOCKED. That is the
    counterexample.
    """
    store = Store(tmp_path / "state.sqlite3")
    open_task(store, task_id="w1", contract={"g": "x"}, policy_digest="pd")
    _record_pass(store, "w1", REQUIRED_CHECKS[0])

    verdict = compute_readiness(store, "w1", required_check_ids=list(REQUIRED_CHECKS))
    assert verdict.readiness == "BLOCKED", (
        f"acceptance succeeded with {REQUIRED_CHECKS[1]} absent; absent "
        f"evidence is never success"
    )
    assert any(REQUIRED_CHECKS[1] in gap for gap in verdict.gaps), (
        f"the gap does not name the missing check: {verdict.gaps}"
    )


def test_evidence_from_a_superseded_attempt_cannot_satisfy_readiness(tmp_path: Path) -> None:
    """Generation 2 must not be made READY by generation 1's passes.

    The guard in `record_readiness` stops an old attempt publishing a verdict it
    already computed. It does not stop a NEW attempt computing one from the old
    attempt's evidence, and `compute_readiness` filtered only on `task_id`. So
    reassigning a task handed its successor a free READY: the successor had
    produced nothing, checked nothing, and inherited every pass.

    CONTRACT.md settles which way this goes. `supersede_task` advances the
    generation to invalidate the previous attempt's authority, and a retry is a
    new run linked to its predecessor rather than a continuation of it. A
    generation that cannot own a claim must not be able to reach acceptance on
    another generation's evidence either -- the same authority, read in a
    different direction.

    The TLA+ model forbids this in Properties 3 and 4, so the specification and
    the implementation disagreed. This is the test that says which one is right.
    """
    store = Store(tmp_path / "state.sqlite3")
    open_task(store, task_id="w1", contract={"g": "x"}, policy_digest="pd")
    for check in REQUIRED_CHECKS:
        _record_pass(store, "w1", check)

    first = compute_readiness(store, "w1", required_check_ids=list(REQUIRED_CHECKS))
    assert first.readiness == "READY", "the first attempt has genuinely earned this"

    supersede_task(store, "w1")
    assert current_generation(store, "w1") == 2

    second = compute_readiness(store, "w1", required_check_ids=list(REQUIRED_CHECKS))

    assert second.readiness != "READY", (
        "generation 2 reached READY on generation 1's evidence; it has produced "
        "no runs of its own, so its acceptance rests on another attempt's work"
    )
    assert second.gaps, "a readiness short of READY must say what is missing"
    assert any("attempt" in gap or "generation" in gap for gap in second.gaps), (
        f"the gaps must name the staleness, not merely the absence: {second.gaps}"
    )
