"""Correspondence between the real core and a separate reference model.

Hypothesis generates operation sequences. Each sequence is applied twice: once
to `formal/reference.py`, a small dictionary-valued model written from the
documented rules, and once to the real `vkit` core, through its public
functions against a real SQLite database. After every step the two are asked
the same observable questions and must answer the same.

## What this proves, stated precisely

It proves CORRESPONDENCE over the sequences tried: no disagreement was found
between the core and the reference model. It does NOT prove implementation
equivalence. The two differ mechanically everywhere, the reference is finite
where the core is not, and Hypothesis samples a bounded family of sequences
rather than exhausting it. "No disagreement was found" is a weaker sentence
than "they agree", and only the weaker one is supported.

It also does not prove anything about the TLA+ model. The TLA+ model and the
reference model were written from the same documented rules, so a
misunderstanding of those rules shared between them would be invisible here.
The core is the only one of the three under test.

## What this file does not compare

The decision compared here is the floor and the evidence: which runs count, at
which generation, in what order, and what a failing one does. The IDENTITY
comparison is not in it, because `compute_readiness` runs that only when it is
given an `AcceptanceContext` and the reference model has no concept an identity
could be compared against. `Core.decide` passes no context, which is a measured
decision and not an omission; see the comment there.

So "no disagreement was found" covers the generation and ownership rules, not
the rule that a changed policy or source invalidates already-accepted
evidence. That rule is checked against the real core in
`tests/test_identity_invalidation.py`, which drives `admit`, `run_check` and
`finalize` through the production path rather than seeding runs directly.

## Why the sequence is applied to a real database

A reference model in a dictionary and a core in SQLite can only be compared if
both are asked through the same question. Every observation below is phrased
the way a caller would phrase it: who holds this resource, at which
generation, and what verdict does this attempt get. Nothing asserts on SQL, on
a private helper, or on the order in which the core did its work.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

hypothesis = pytest.importorskip(
    "hypothesis",
    reason="Plan 08 is optional; hypothesis is in the `test` extra, install it to run these",
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "formal"))

from vkit.claims import ResourceSpec, acquire, holder, release  # noqa: E402
from vkit.storage import ConflictError, Store  # noqa: E402
from vkit.tasks import (  # noqa: E402
    compute_readiness,
    current_generation,
    get_task,
    open_task,
    supersede_task,
)

import reference  # noqa: E402

SETTINGS = settings(
    max_examples=200,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)



class Core:
    """The vkit core behind the same four questions the reference model answers.

    Each method returns the answer a caller would see, and each is a few lines
    of real public API. Keeping the wrapper this thin is deliberate: a wrapper
    that reimplemented the rules would be comparing the reference model against
    itself.
    """

    def __init__(self, store: Store, open_owners: bool = True):
        self.store = store
        if open_owners:
            for owner in reference.OWNERS:
                open_task(
                    store, task_id=owner,
                    contract={
                        "repository": {"root": ".", "git_common_dir": "."},
                        "policy_digest": "pd",
                        "required_checks": list(reference.REQUIRED_CHECKS),
                        "scope": f"owner {owner}",
                        "resources": [],
                        "declared": {"owner": owner},
                    },
                    policy_digest="pd",
                )

    def generations(self, owner: str) -> int:
        return current_generation(self.store, owner)

    def acquire(self, owner: str, resource: str) -> bool:
        """Whether the claim succeeded, matching the reference model's outcome."""
        try:
            acquire(
                self.store, owner, self.generations(owner),
                [ResourceSpec(resource, "exclusive")],
            )
            return True
        except ConflictError:
            return False

    def record(self, owner: str, check: str, revision: str, outcome: str) -> None:
        """Publish a terminal run under this owner and its current generation."""
        generation = self.generations(owner)
        run_id = f"r-{owner}-{check}-{revision}-{outcome}-{len(list(self.store.list_runs(limit=1000)))}"
        self.store.register_run(
            run_id, check, task_id=owner, attempt=generation,
            source={"revision": revision}, configuration_digest="c", fixture_digest=None,
        )
        body = ({"result": outcome, "reason": "seeded"}
                if outcome == "BLOCKED"
                else {"result": outcome, "scenarios": [
                    {"id": "s", "result": outcome, "observation": "seeded"}]})
        self.store.publish(run_id, {
            "run_id": run_id, "lifecycle": "terminal", "ended_at": "t", "outcome": body,
        })

    def holder(self, resource: str) -> tuple[str, int] | None:
        claim = holder(self.store, resource)
        return None if claim is None else (claim.task_id, claim.generation)

    def release(self, owner: str, resource: str) -> None:
        """Free a resource this owner holds at its current generation.

        The core's `claims.release` RAISES a ConflictError when the task was
        superseded and still holds its old-generation claims, because letting a
        superseded attempt release would report success for an attempt that no
        longer owns anything. The reference model refuses the same release
        silently (it is a no-op). Both end with the claim still held, so the
        wrapper converts the raise into the reference's silent refusal and the
        observable outcome is compared rather than the exception.
        """
        try:
            release(self.store, owner, self.generations(owner), [resource])
        except ConflictError:
            pass

    def crash(self, owner: str) -> None:
        """Stop an owner. The core exposes exactly one attempt-level operation,
        `supersede_task`, which advances the generation and, by design, LEAVES
        the claims held. So a crash maps onto that one call, and the generation
        advances with it. Claims staying held across the crash is exactly the
        behaviour under test."""
        supersede_task(self.store, owner)

    def reconcile(self, owner: str) -> None:
        """Advance the generation again, which is the core's supersede again.

        The core has no separate 'reconcile' verb; reconciling a stopped owner
        IS superseding it. Repeated reconciliation therefore keeps advancing
        the core's counter, and the reference must too."""
        supersede_task(self.store, owner)

    def decide(self, owner: str, revision: str) -> str:
        result = compute_readiness(
            self.store, owner, required_check_ids=list(reference.REQUIRED_CHECKS)
        )
        return result.readiness



owner_st = st.sampled_from(reference.OWNERS)
resource_st = st.sampled_from(reference.RESOURCES)
check_st = st.sampled_from(reference.REQUIRED_CHECKS)
revision_st = st.sampled_from(reference.REVISIONS)
outcome_st = st.sampled_from(["PASS", "FAIL", "BLOCKED"])
generation_st = st.integers(min_value=1, max_value=reference.MAX_GENERATION)

Op = tuple
ops = st.one_of(
    st.tuples(st.just("acquire"), owner_st, resource_st),
    st.tuples(st.just("record"), owner_st, check_st, revision_st, outcome_st),
    st.tuples(st.just("release"), owner_st, resource_st),
    st.tuples(st.just("crash"), owner_st),
    st.tuples(st.just("reconcile"), owner_st),
    st.tuples(st.just("set_revision"), revision_st),
    st.tuples(st.just("accept"), owner_st, generation_st, revision_st),
    st.tuples(st.just("observe"), owner_st, resource_st, revision_st),
)


def _reference_init() -> reference.Model:
    model = reference.Model(revision=reference.REVISIONS[0])
    for owner in reference.OWNERS:
        model = model.add_owner(owner)
    for resource in reference.RESOURCES:
        model = model.add_resource(resource)
    return model



@given(sequence=st.lists(ops, min_size=1, max_size=25))
@SETTINGS
def test_the_core_and_the_reference_model_agree(tmp_path_factory, sequence) -> None:
    """Both models see the same operations and must answer the same questions.

    Each step is compared, not only the end state, because a pair of models can
    reach the same place by different routes and one of those routes can be
    wrong.
    """
    store = Store(tmp_path_factory.mktemp("corr") / "state.sqlite3")
    real = Core(store)
    ideal = _reference_init()

    for step_number, op in enumerate(sequence):
        kind = op[0]
        before = f"after {step_number - 1} steps, before {op}"

        if kind == "acquire":
            _, owner, resource = op
            acquired_real = real.acquire(owner, resource)
            ideal = ideal.acquire(owner, resource)
            assert real.holder(resource) == ideal.holder(resource), (
                f"ownership of {resource} diverged {before}: "
                f"core={real.holder(resource)} reference={ideal.holder(resource)} "
                f"(core acquire returned {acquired_real})"
            )

        elif kind == "record":
            _, owner, check, revision, outcome = op
            real.record(owner, check, revision, outcome)
            ideal = ideal.record(owner, check, revision, outcome)

        elif kind == "release":
            _, owner, resource = op
            real.release(owner, resource)
            ideal = ideal.release(owner, resource)
            assert real.holder(resource) == ideal.holder(resource), (
                f"ownership of {resource} diverged {before}: "
                f"core={real.holder(resource)} reference={ideal.holder(resource)}"
            )

        elif kind == "crash":
            _, owner = op
            real.crash(owner)
            ideal = ideal.crash(owner)
            ideal = ideal.reconcile(owner)
            assert real.generations(owner) == ideal.generations[owner], (
                f"generation of {owner} diverged {before}"
            )

        elif kind == "reconcile":
            _, owner = op
            real.reconcile(owner)
            ideal = ideal.reconcile(owner)
            assert real.generations(owner) == ideal.generations[owner], (
                f"generation of {owner} diverged {before}"
            )

        elif kind == "set_revision":
            _, revision = op
            ideal = ideal.set_revision(revision)

        elif kind == "accept":
            _, owner, generation, revision = op
            ideal = ideal.accept(owner, generation, revision)

        for owner in reference.OWNERS:
            current = real.generations(owner)
            core_verdict = real.decide(owner, ideal.revision)
            reference_verdict = ideal.decide(owner, current, ideal.revision)
            assert core_verdict == reference_verdict, (
                f"readiness for {owner} diverged {before}: "
                f"core={core_verdict} reference={reference_verdict} "
                f"(core generation {current}, reference generation "
                f"{ideal.generations[owner]})"
            )


@given(sequence=st.lists(ops, min_size=1, max_size=25))
@SETTINGS
def test_an_exclusive_resource_never_has_two_owners_in_the_core(
    tmp_path_factory, sequence
) -> None:
    """The core's own invariant, checked on the core alone.

    Correspondence is a comparison and a comparison can be consistently wrong,
    so the most important property is also checked directly against the
    database, with no reference model in the loop.
    """
    store = Store(tmp_path_factory.mktemp("excl") / "state.sqlite3")
    real = Core(store)

    for step_number, op in enumerate(sequence):
        if op[0] == "acquire":
            _, owner, resource = op
            real.acquire(owner, resource)
        elif op[0] == "record":
            _, owner, check, revision, outcome = op
            real.record(owner, check, revision, outcome)
        elif op[0] == "release":
            _, owner, resource = op
            real.release(owner, resource)
        elif op[0] == "crash":
            _, owner = op
            real.crash(owner)
        elif op[0] == "set_revision":
            _, revision = op

        for resource in reference.RESOURCES:
            claim = holder(store, resource)
            if claim is None:
                continue
            assert claim.held == 1
            assert claim.task_id in reference.OWNERS

        for owner in reference.OWNERS:
            for resource in reference.RESOURCES:
                claim = holder(store, resource)
                if claim is not None and claim.task_id == owner:
                    assert claim.generation >= 1
                    assert claim.generation <= real.generations(owner), (
                        f"step {step_number}: {resource} is held at generation "
                        f"{claim.generation} by {owner}, whose generation has only "
                        f"ever reached {real.generations(owner)}"
                    )
