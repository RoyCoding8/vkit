"""The refusals in `storage.py` that no other test reaches.

Each test here drives one branch that the rest of the suite asserts nothing
about. They are kept in a separate file rather than appended to
`test_storage.py` because that file is the surface a reader opens first, and a
guard buried among happy-path assertions is a guard nobody re-runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.storage import Store, StoreError  # noqa: E402


def make_report(run_id: str, **extra) -> dict:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "check_id": "unit",
        "lifecycle": "terminal",
        "started_at": "2026-10-01T10:00:00+00:00",
        "ended_at": "2026-10-01T10:00:01+00:00",
        "outcome": {"result": "PASS", "scenarios": []},
        "source": {"head": "a" * 40, "inventory_digest": "b" * 64, "dirty": False},
        "command": {"argv": ["python", "-c", "pass"], "cwd": "/repo"},
        "configuration_digest": "c" * 64,
        **extra,
    }


def acceptance_record(decision: str = "ACCEPTED", **extra) -> dict:
    return {
        "integration_id": "int-1", "context": "local", "decision": decision,
        "candidate": "a" * 40, "target": "b" * 40, "candidate_parent": None,
        "source": {}, "policy": {}, "verifier": {}, "environment": {},
        "fixture": None, "manifest": None, "required_checks": [],
        "checks": [], "gaps": [], "findings": [],
        "decided_at": "2026-10-01T10:00:00+00:00",
        **extra,
    }


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "verification-kit" / "state.sqlite3")
    s.register_run(
        "run-1", "unit", task_id=None, attempt=None,
        source={"head": "a" * 40, "inventory_digest": "b" * 64, "dirty": False},
        configuration_digest="c" * 64, fixture_digest=None,
    )
    return s


def test_a_report_with_no_outcome_is_refused(store: Store) -> None:
    """A terminal run has exactly one outcome; a report without one is not evidence.

    The refusal has to happen before anything reaches the filesystem. A caller
    that dropped the key would otherwise get a `report.json` on disk that reads
    back as a terminal run with no result, and every later reader of that file
    would have to decide what it meant.
    """
    report = make_report("run-1")
    del report["outcome"]
    with pytest.raises(StoreError) as raised:
        store.publish("run-1", report)
    assert str(raised.value) == (
        "refusing to publish run-1 with no outcome: a terminal run has exactly one"
    )
    assert not (store.run_dir("run-1") / "report.json").exists()


def test_an_outcome_that_is_not_a_mapping_is_refused(store: Store) -> None:
    """`outcome` must be a mapping, and `None` is what a dropped field becomes.

    The guard checks the type before it looks for the key, because the lookup
    underneath it is not safe on every value. A caller here gets a `StoreError`
    rather than a `TypeError` escaping `publish`, and the run is still
    publishable a moment later.
    """
    with pytest.raises(StoreError) as raised:
        store.publish("run-1", make_report("run-1", outcome=None))
    assert str(raised.value) == (
        "refusing to publish run-1 with no outcome: a terminal run has exactly one"
    )
    store.publish("run-1", make_report("run-1"))
    assert store.load("run-1")["outcome"]["result"] == "PASS"


def test_supervision_cannot_be_claimed_for_a_run_that_never_launched(store: Store) -> None:
    """`claim_supervisor` writes the pid of whoever supervises a *recorded* launch.

    A run with no launch row has no job to own. Writing a supervisor pid against
    it would record an owner for a process that does not exist, and the row
    that says so is the only evidence that survives the supervisor exiting.
    """
    assert store.load_launch("run-1") is None
    with pytest.raises(StoreError) as raised:
        store.claim_supervisor("run-1")
    assert str(raised.value) == (
        "no launch is recorded for run 'run-1' to supervise"
    )
    assert store.load_launch("run-1") is None


def test_a_second_acceptance_under_one_id_is_refused(tmp_path: Path) -> None:
    """The acceptance id names one decision, and the second one is a contradiction.

    `integration/verify.py` catches this refusal on purpose: it reads the
    recorded decision back and compares it, so the refusal is its signal rather
    than a failure. That is also why it has never been asserted here -- the
    caller handles it, and a handler that swallows it looks like coverage. The
    first decision has to survive the second attempt untouched.
    """
    s = Store(tmp_path / "verification-kit" / "state.sqlite3")
    s.record_acceptance("acc-1", acceptance_record())
    with pytest.raises(StoreError) as raised:
        s.record_acceptance("acc-1", acceptance_record("REJECTED"))
    assert str(raised.value) == "acceptance acc-1 is already recorded"
    assert s.load_acceptance("acc-1")["decision"] == "ACCEPTED"

def test_a_record_the_schema_refuses_is_not_reported_as_a_duplicate(tmp_path: Path) -> None:
    """A CHECK failure is a different refusal from a collision on the id.

    `context` is constrained to `('local', 'protected')`, so a record carrying
    anything else arrives as the same `sqlite3.IntegrityError` the primary key
    does. Reporting it as "already recorded" sent `verify.py:689` looking for a
    row that was never written: it read the id back, got `None`, and re-raised
    to the caller as a contradiction between two observations when only one was
    ever attempted.
    """
    s = Store(tmp_path / "verification-kit" / "state.sqlite3")
    with pytest.raises(StoreError) as raised:
        s.record_acceptance("acc-1", acceptance_record(context="staging"))
    assert "is already recorded" not in str(raised.value)
    assert "context IN" in str(raised.value)
    assert s.load_acceptance("acc-1") is None
