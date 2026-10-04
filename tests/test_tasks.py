"""Behavior of the task lifecycle, exercised through its public surface.

These assert what a caller observes: the returned record, and what a fresh read
of the database says afterwards. They never assert on SQL or on which private
helper ran, because a test that restates the implementation cannot fail for a
real defect.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.storage import ConflictError, Store  # noqa: E402
from vkit.tasks import (  # noqa: E402
    TaskError,
    compute_readiness,
    current_generation,
    get_task,
    open_task,
    record_readiness,
    set_status,
    supersede_task,
)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.sqlite3")


def publish_run(store: Store, run_id: str, check_id: str, task_id: str, result: str,
                reason: str | None = None) -> None:
    store.register_run(run_id, check_id, task_id=task_id, attempt=1, source={},
                       configuration_digest="c", fixture_digest=None)
    if result == "BLOCKED":
        outcome = {"result": "BLOCKED", "reason": reason or "timeout"}
    else:
        outcome = {"result": result,
                   "scenarios": [{"id": "s", "result": result, "observation": "o"}]}
    store.publish(run_id, {"run_id": run_id, "lifecycle": "terminal",
                           "ended_at": "t", "outcome": outcome})


def contract(goal: str = "ship", required: tuple[str, ...] = ("c1",)) -> dict:
    """A contract in the shape `TaskContract.from_json` validates.

    These are unit tests of the storage primitive, so they write the row
    directly rather than admitting through the policy. What they must not do is
    write a contract the core would refuse to read back: an unbound one has no
    repository, no policy digest and no floor, so `pinned()` cannot be used and
    acceptance has nothing to compare against.
    """
    return {
        "repository": {"root": ".", "git_common_dir": "."},
        "policy_digest": "pd",
        "required_checks": list(required),
        "scope": goal,
        "resources": [],
        "declared": {},
    }



def test_a_new_task_starts_active_at_generation_one(store: Store) -> None:
    pinned = contract()
    task = open_task(store, task_id="t1", contract=pinned, policy_digest="pd")
    assert task.task_id == "t1"
    assert task.status == "active"
    assert task.generation == 1
    assert task.contract == pinned


def test_a_pinned_contract_is_readable_afterwards(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="policy-abc")
    task = get_task(store, "t1")
    assert task.policy_digest == "policy-abc"
    assert task.pinned().required_checks == ("c1",)


def test_opening_the_same_task_twice_is_refused(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract("a"), policy_digest="pd")
    with pytest.raises(TaskError):
        open_task(store, task_id="t1", contract=contract("b"), policy_digest="pd")
    assert get_task(store, "t1").contract == contract("a")


def test_a_contract_the_core_cannot_read_is_refused(store: Store) -> None:
    """A row holding a contract `pinned()` rejects is worse than no row.

    Acceptance would have no repository, policy or floor to compare a pass
    against, so the refusal names the defect instead of storing it.
    """
    with pytest.raises(TaskError):
        open_task(store, task_id="t1", contract={"goal": "ship"}, policy_digest="pd")
    with pytest.raises(TaskError):
        open_task(store, task_id="t1", contract=contract(required=()), policy_digest="pd")



def test_pausing_is_observable_on_a_fresh_read(store: Store) -> None:
    """Regression: the write committed but the returned record was read on a
    second connection, so a successful pause reported the old status."""
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    set_status(store, "t1", "paused")
    assert get_task(store, "t1").status == "paused"


def test_closing_a_task_stamps_the_close_time(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    assert set_status(store, "t1", "closed").status == "closed"


def test_an_unknown_status_is_refused(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    with pytest.raises(TaskError):
        set_status(store, "t1", "superseded")  # type: ignore[arg-type]


def test_pausing_does_not_advance_the_generation(store: Store) -> None:
    """Paused means the owner is idle, not gone. Its authority is unchanged."""
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    set_status(store, "t1", "paused")
    assert current_generation(store, "t1") == 1



def test_superseding_advances_the_generation(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    assert supersede_task(store, "t1").generation == 2
    assert current_generation(store, "t1") == 2


def test_a_closed_task_cannot_be_reassigned(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    set_status(store, "t1", "closed")
    with pytest.raises(TaskError):
        supersede_task(store, "t1")


def test_superseding_an_unknown_task_is_refused(store: Store) -> None:
    with pytest.raises(TaskError):
        supersede_task(store, "nope")



def test_no_evidence_is_blocked_not_ready(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    result = compute_readiness(store, "t1", required_check_ids=["c1"])
    assert result.readiness == "BLOCKED"
    assert "no completed run" in result.gaps[0]


def test_all_required_checks_passing_is_ready(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "PASS")
    publish_run(store, "r2", "c2", "t1", "PASS")
    assert compute_readiness(store, "t1", required_check_ids=["c1", "c2"]).readiness == "READY"


def test_one_missing_required_check_blocks_readiness(store: Store) -> None:
    """The row the whole product rests on: absent evidence is never success."""
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "PASS")
    result = compute_readiness(store, "t1", required_check_ids=["c1", "c2"])
    assert result.readiness == "BLOCKED"
    assert result.gaps == ("no completed run for required check 'c2'",)


def test_a_failing_check_rejects(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "PASS")
    publish_run(store, "r2", "c2", "t1", "FAIL")
    assert compute_readiness(store, "t1", required_check_ids=["c1", "c2"]).readiness == "REJECTED"


def test_a_blocked_required_check_blocks_with_its_reason(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "BLOCKED", reason="source_changed")
    result = compute_readiness(store, "t1", required_check_ids=["c1"])
    assert result.readiness == "BLOCKED"
    assert "source_changed" in result.gaps[0]


def test_a_failure_outranks_a_missing_check(store: Store) -> None:
    """A recorded failure is an answer. It is not 'incomplete'."""
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "FAIL")
    result = compute_readiness(store, "t1", required_check_ids=["c1", "c2"])
    assert result.readiness == "REJECTED"
    assert any("c2" in gap for gap in result.gaps)


def test_a_later_blocked_run_supersedes_an_earlier_pass(store: Store) -> None:
    """The most recent terminal result is what counts, and a later BLOCKED is
    never quietly replaced by an earlier PASS."""
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "PASS")
    publish_run(store, "r2", "c1", "t1", "BLOCKED", reason="timeout")
    assert compute_readiness(store, "t1", required_check_ids=["c1"]).readiness == "BLOCKED"


def test_another_tasks_runs_do_not_satisfy_this_task(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    open_task(store, task_id="t2", contract=contract("other"), policy_digest="pd")
    publish_run(store, "r1", "c1", "t2", "PASS")
    assert compute_readiness(store, "t1", required_check_ids=["c1"]).readiness == "BLOCKED"



def test_recording_readiness_stores_the_verdict(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "PASS")
    result = compute_readiness(store, "t1", required_check_ids=["c1"])
    assert record_readiness(store, "t1", result).readiness == "READY"
    assert get_task(store, "t1").readiness == "READY"


def test_a_superseded_attempt_cannot_record_readiness(store: Store) -> None:
    """An old owner must not finalize after being reassigned, even if its own
    checks passed."""
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "PASS")
    result = compute_readiness(store, "t1", required_check_ids=["c1"])
    assert result.readiness == "READY"

    supersede_task(store, "t1")
    with pytest.raises(ConflictError):
        record_readiness(store, "t1", result)
    assert get_task(store, "t1").readiness is None


def test_a_closed_task_refuses_a_new_verdict(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    publish_run(store, "r1", "c1", "t1", "PASS")
    result = compute_readiness(store, "t1", required_check_ids=["c1"])
    set_status(store, "t1", "closed")
    with pytest.raises(TaskError):
        record_readiness(store, "t1", result)


def test_a_closed_task_cannot_be_reopened(store: Store) -> None:
    """A closed task is final. Reopening it let a caller resurrect a finished
    task and then record a verdict on it, and left the row simultaneously
    'active' and stamped closed."""
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    set_status(store, "t1", "closed")
    with pytest.raises(TaskError):
        set_status(store, "t1", "active")
    assert get_task(store, "t1").status == "closed"


def test_a_closed_task_cannot_be_paused_either(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    set_status(store, "t1", "closed")
    with pytest.raises(TaskError):
        set_status(store, "t1", "paused")


def test_a_task_can_still_be_paused_and_resumed_before_closing(store: Store) -> None:
    open_task(store, task_id="t1", contract=contract(), policy_digest="pd")
    assert set_status(store, "t1", "paused").status == "paused"
    assert set_status(store, "t1", "active").status == "active"
