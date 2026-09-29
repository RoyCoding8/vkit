"""Behavior of resource claims, exercised through their public surface.

Every assertion reads observable state twice: once through `holder`/`holders`,
and once through a fresh query that opens its own connection to the database
file. A claim that existed only in the object the writer returned would satisfy
the first and fail the second, and the second is the one that crosses a process
boundary.

The last test launches real operating system processes. Threads would share one
interpreter and could be serialised by the GIL or by a connection the thread
happens to share, which is not the race the design exists to survive. Plan 02 is
explicit that thread-only tests do not establish cross-process coordination.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.claims import (  # noqa: E402
    Claim, ResourceSpec, acquire, holder, holders, release,
)
from vkit.storage import ConflictError, Store, StoreError  # noqa: E402

EXCLUSIVE = "exclusive"
POOL = "capacity"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "verification.sqlite3"


@pytest.fixture()
def store(db_path: Path) -> Store:
    return Store(db_path)


def rows(db_path: Path, table: str = "claim_holders") -> list[dict]:
    """Read the table over a new connection, bypassing everything but the file.

    This is the independent observer. It shares no connection, no transaction
    and no object with the code under test, so it sees only what actually
    survived to disk.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            f"SELECT resource_key, kind, capacity, held, task_id, generation, acquired_at FROM {table}"
        )
        keys = ("resource_key", "kind", "capacity", "held", "task_id", "generation", "acquired_at")
        return [dict(zip(keys, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def test_acquiring_a_free_exclusive_resource_succeeds(store: Store, db_path: Path) -> None:
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])

    held = holder(store, "build")
    assert held is not None
    assert held.task_id == "t1"
    assert held.generation == 1
    assert held.kind == EXCLUSIVE
    assert held.capacity is None
    assert held.held == 1
    assert held.acquired_at is not None

    assert rows(db_path) == [
        {"resource_key": "build", "kind": EXCLUSIVE, "capacity": None, "held": 1,
         "task_id": "t1", "generation": 1, "acquired_at": held.acquired_at}
    ]


def test_a_second_task_is_refused_the_exclusive_resource(store: Store) -> None:
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])

    with pytest.raises(ConflictError) as raised:
        acquire(store, "t2", 1, [ResourceSpec("build", EXCLUSIVE)])

    assert str(raised.value) == "resource 'build' is already held by task 't1' at generation 1"
    # The refusal must not have moved ownership.
    assert holder(store, "build").task_id == "t1"


def test_disjoint_resource_sets_both_succeed(store: Store, db_path: Path) -> None:
    """Two tasks naming different resources must not contend."""
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE), ResourceSpec("publish", EXCLUSIVE)])
    acquire(store, "t2", 1, [ResourceSpec("lint", EXCLUSIVE)])

    assert [claim.resource_key for claim in holders(store)] == ["build", "lint", "publish"]
    assert {claim.task_id for claim in holders(store)} == {"t1", "t2"}
    assert len(rows(db_path)) == 3


def test_a_multi_resource_acquire_that_fails_holds_nothing(store: Store, db_path: Path) -> None:
    """The all-or-nothing row: the second resource is taken, so the first must not stay taken."""
    acquire(store, "other", 1, [ResourceSpec("db", EXCLUSIVE)])

    with pytest.raises(ConflictError) as raised:
        acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE), ResourceSpec("db", EXCLUSIVE)])

    assert str(raised.value) == "resource 'db' is already held by task 'other' at generation 1"
    # 'build' was inserted first in the transaction and must have been rolled back.
    assert holder(store, "build") is None
    assert [claim.resource_key for claim in holders(store)] == ["db"]
    assert [row["resource_key"] for row in rows(db_path)] == ["db"]
    assert holders(store, "t1") == ()


def test_a_refused_batch_leaves_the_earlier_grant_reclaimable(store: Store) -> None:
    """A rolled-back acquire must not poison the resources it briefly touched."""
    acquire(store, "other", 1, [ResourceSpec("db", EXCLUSIVE)])
    with pytest.raises(ConflictError):
        acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE), ResourceSpec("db", EXCLUSIVE)])

    acquire(store, "t2", 1, [ResourceSpec("build", EXCLUSIVE)])
    assert holder(store, "build").task_id == "t2"


def test_a_capacity_resource_is_grantable_to_two_tasks_and_refused_a_third(
    store: Store, db_path: Path
) -> None:
    acquire(store, "t1", 1, [ResourceSpec("db", POOL, capacity=2)])
    acquire(store, "t2", 1, [ResourceSpec("db", POOL, capacity=2)])

    held = holder(store, "db")
    assert held.task_id == "t1"          # first in owns the row
    assert held.kind == POOL
    assert held.capacity == 2
    assert held.held == 2                 # both slots taken, by two different tasks
    assert rows(db_path)[0]["held"] == 2

    with pytest.raises(ConflictError) as raised:
        acquire(store, "t3", 1, [ResourceSpec("db", POOL, capacity=2)])
    assert str(raised.value) == (
        "resource 'db' is full, 2 of 2 slots held, owned by task 't1' at generation 1"
    )
    assert rows(db_path)[0]["held"] == 2


def test_a_capacity_freed_slot_is_reusable(store: Store) -> None:
    acquire(store, "t1", 1, [ResourceSpec("db", POOL, capacity=1)])
    with pytest.raises(ConflictError):
        acquire(store, "t2", 1, [ResourceSpec("db", POOL, capacity=1)])

    release(store, "t1", 1)
    acquire(store, "t2", 1, [ResourceSpec("db", POOL, capacity=1)])
    assert holder(store, "db").task_id == "t2"


def test_release_frees_the_resource_for_another_task(store: Store, db_path: Path) -> None:
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    assert holder(store, "build") is not None

    release(store, "t1", 1)

    assert holder(store, "build") is None
    assert holders(store) == ()
    assert rows(db_path) == []
    assert holders(store, "t1") == ()

    acquire(store, "t2", 1, [ResourceSpec("build", EXCLUSIVE)])
    assert holder(store, "build").task_id == "t2"


def test_release_with_a_stale_generation_is_refused(store: Store, db_path: Path) -> None:
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    acquire(store, "t1", 2, [ResourceSpec("publish", EXCLUSIVE)])

    with pytest.raises(ConflictError) as raised:
        release(store, "t1", 1, keys=["build"])

    assert str(raised.value) == (
        "task 't1' was superseded: generation 1 cannot release resources held at generation 2"
    )
    # The refused release must have changed nothing, in either generation.
    assert sorted(claim.resource_key for claim in holders(store)) == ["build", "publish"]
    assert len(rows(db_path)) == 2


def test_a_stale_generation_cannot_release_everything_either(store: Store, db_path: Path) -> None:
    """The theft a scoped delete alone would allow: gen 1 releasing gen 2's work.

    The delete is scoped to (task_id, generation), so a stale caller cannot reach
    its successor's resources even if the supersession check were missing. Both
    generations must still be intact afterwards.
    """
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    acquire(store, "t1", 2, [ResourceSpec("lint", EXCLUSIVE)])

    with pytest.raises(ConflictError) as raised:
        release(store, "t1", 1)

    assert str(raised.value) == (
        "task 't1' was superseded: generation 1 cannot release resources held at generation 2"
    )
    assert sorted(claim.resource_key for claim in holders(store)) == ["build", "lint"]
    assert sorted(row["resource_key"] for row in rows(db_path)) == ["build", "lint"]


def test_a_new_generation_does_not_inherit_the_old_one_resources(store: Store) -> None:
    """A superseded attempt's claims do not transfer to the attempt replacing it.

    This is why supersession is a conflict and not a handoff: the old generation
    still owns what it took, so whoever reconciles it has to decide explicitly
    rather than have it vanish under the new attempt.
    """
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])

    with pytest.raises(ConflictError) as raised:
        acquire(store, "t1", 2, [ResourceSpec("build", EXCLUSIVE)])

    assert str(raised.value) == "resource 'build' is already held by task 't1' at generation 1"
    assert holder(store, "build").generation == 1


def test_release_by_a_task_that_does_not_hold_the_resource_does_not_steal_it(
    store: Store, db_path: Path
) -> None:
    acquire(store, "owner", 1, [ResourceSpec("build", EXCLUSIVE)])

    release(store, "intruder", 1, keys=["build"])

    held = holder(store, "build")
    assert held.task_id == "owner"
    assert rows(db_path)[0]["task_id"] == "owner"


def test_release_by_a_task_that_holds_nothing_does_not_raise(store: Store) -> None:
    acquire(store, "owner", 1, [ResourceSpec("build", EXCLUSIVE)])

    release(store, "stranger", 1)
    release(store, "stranger", 1, keys=["build", "nonexistent"])

    assert holder(store, "build").task_id == "owner"


def test_releasing_everything_when_keys_is_omitted(store: Store, db_path: Path) -> None:
    acquire(store, "t1", 1, [
        ResourceSpec("build", EXCLUSIVE),
        ResourceSpec("db", POOL, capacity=2),
        ResourceSpec("lint", EXCLUSIVE),
    ])
    acquire(store, "t2", 1, [ResourceSpec("publish", EXCLUSIVE)])
    assert len(rows(db_path)) == 4

    release(store, "t1", 1)

    assert [claim.resource_key for claim in holders(store)] == ["publish"]
    assert [claim.task_id for claim in holders(store)] == ["t2"]
    assert holders(store, "t1") == ()
    assert [row["resource_key"] for row in rows(db_path)] == ["publish"]


def test_releasing_a_named_subset_leaves_the_rest_held(store: Store, db_path: Path) -> None:
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE), ResourceSpec("lint", EXCLUSIVE)])

    release(store, "t1", 1, keys=["build"])

    assert [claim.resource_key for claim in holders(store)] == ["lint"]
    assert [row["resource_key"] for row in rows(db_path)] == ["lint"]


def test_duplicate_keys_in_one_acquire_are_refused(store: Store, db_path: Path) -> None:
    with pytest.raises(ValueError) as raised:
        acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE), ResourceSpec("build", EXCLUSIVE)])
    assert str(raised.value) == "resource 'build' is named twice in one acquire"
    assert rows(db_path) == []


def test_acquiring_nothing_succeeds_and_touches_nothing(store: Store) -> None:
    acquire(store, "t1", 1, [])
    assert holders(store) == ()


def test_a_capacity_declaration_must_match_the_one_in_force(store: Store, db_path: Path) -> None:
    acquire(store, "t1", 1, [ResourceSpec("db", POOL, capacity=2)])

    with pytest.raises(ConflictError) as raised:
        acquire(store, "t2", 1, [ResourceSpec("db", POOL, capacity=4)])

    assert str(raised.value) == (
        "resource 'db' is declared with capacity 2, not 4, and is held by task 't1' at generation 1"
    )
    assert rows(db_path)[0]["capacity"] == 2


@pytest.mark.parametrize(
    "args, message",
    [
        (("x", EXCLUSIVE, 2), "resource 'x' is exclusive and cannot declare a capacity"),
        (("x", POOL, None), "resource 'x' is capacity-kind and needs a positive capacity"),
        (("x", POOL, 0), "resource 'x' is capacity-kind and needs a positive capacity"),
        (("", POOL, 1), "a resource key is required"),
    ],
)
def test_malformed_specs_are_refused_on_construction(args: tuple, message: str) -> None:
    """Rejected when the spec is built, so a bad request never reaches a transaction."""
    with pytest.raises(ValueError) as raised:
        ResourceSpec(*args)
    assert str(raised.value) == message


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError) as raised:
        ResourceSpec("x", "shared")
    assert str(raised.value) == "resource 'x' has unknown kind 'shared'"


def test_claim_is_a_value_not_a_window_onto_the_row(store: Store) -> None:
    acquire(store, "t1", 1, [ResourceSpec("db", POOL, capacity=2)])
    before = holder(store, "db")
    acquire(store, "t2", 1, [ResourceSpec("db", POOL, capacity=2)])

    assert before.held == 1
    assert holder(store, "db").held == 2
    assert before == Claim("db", POOL, 2, 1, "t1", 1, before.acquired_at)


# --------------------------------------------------------------------------
# Cross-process coordination. Separate OS processes, one database file.
# --------------------------------------------------------------------------

# Run as a script by a real interpreter. It reports the outcome as JSON on one
# line so the parent never has to guess from a traceback, and it reports the path
# of the claims module it actually loaded: an editable install can put another
# checkout's src on sys.path, and a cross-process test that silently ran against
# different code than the one under test would pass for the wrong reason.
CHILD = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from vkit import claims
from vkit.storage import ConflictError, Store

db, key, task_id, generation, capacity, deadline = (
    sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]),
    int(sys.argv[6]) if sys.argv[6] != "none" else None, float(sys.argv[7]),
)
while time.time() < deadline:
    time.sleep(0.002)

spec = claims.ResourceSpec(key, "capacity", capacity) if capacity else claims.ResourceSpec(key, "exclusive")
result = {"pid": os.getpid(), "task_id": task_id, "module": claims.__file__}
try:
    claims.acquire(Store(db), task_id, generation, [spec])
    result["status"] = "acquired"
except ConflictError as exc:
    result["status"] = "conflict"
    result["error"] = str(exc)
print(json.dumps(result))
"""


def test_separate_processes_contend_for_one_exclusive_resource(db_path: Path) -> None:
    """Plan 02: exactly one winner, and the losers are told which resource.

    Threads are not used. Each child is an independent interpreter with its own
    SQLite connection to one database file in a real temporary directory, so
    the only thing coordinating them is the filesystem.
    """
    processes = 4
    key = "deploy-lock"
    # The parent migrates once so the children contend for the claim itself
    # rather than for schema creation.
    Store(db_path)
    # A common start instant keeps the children inside one another's window.
    deadline = time.time() + 1.0

    children = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(Path(__file__).resolve().parents[1] / "src"),
             str(db_path), key, f"task-{index}", "1", "none", str(deadline)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for index in range(processes)
    ]
    outputs = [child.communicate(timeout=60) for child in children]
    assert [child.returncode for child in children] == [0] * processes, outputs

    reports = [json.loads(out.strip().splitlines()[-1]) for out, _ in outputs]

    # Distinct OS processes, not threads in one interpreter.
    assert len({report["pid"] for report in reports}) == processes
    assert {report["module"] for report in reports} == {str((Path(__file__).resolve().parents[1] / "src" / "vkit" / "claims.py").resolve())}

    winners = [report for report in reports if report["status"] == "acquired"]
    losers = [report for report in reports if report["status"] == "conflict"]
    assert len(winners) == 1, reports
    assert len(losers) == processes - 1, reports

    # Every loser names the resource it could not get, not a generic failure.
    for loser in losers:
        assert loser["error"] == (
            f"resource {key!r} is already held by task {winners[0]['task_id']!r} at generation 1"
        )

    # And the file agrees with exactly one process.
    assert len(rows(db_path)) == 1
    winner = rows(db_path)[0]
    assert winner["resource_key"] == key
    assert winner["kind"] == EXCLUSIVE
    assert winner["held"] == 1
    assert winner["task_id"] == winners[0]["task_id"]


def test_separate_processes_share_a_bounded_pool_without_overfilling(db_path: Path) -> None:
    """Capacity is the same arbitration against a different limit."""
    processes = 5
    key = "db"
    capacity = 2
    Store(db_path)
    deadline = time.time() + 1.0

    children = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(Path(__file__).resolve().parents[1] / "src"),
             str(db_path), key, f"task-{index}", "1", str(capacity), str(deadline)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for index in range(processes)
    ]
    outputs = [child.communicate(timeout=60) for child in children]
    assert [child.returncode for child in children] == [0] * processes, outputs
    reports = [json.loads(out.strip().splitlines()[-1]) for out, _ in outputs]

    assert len([r for r in reports if r["status"] == "acquired"]) == capacity
    assert len([r for r in reports if r["status"] == "conflict"]) == processes - capacity
    for report in reports:
        if report["status"] == "conflict":
            assert report["error"].startswith(f"resource {key!r} is full, 2 of 2 slots held")
    assert len(rows(db_path)) == 1
    assert rows(db_path)[0]["held"] == capacity


def test_store_error_is_distinct_from_conflict(db_path: Path) -> None:
    """A conflict is an answer the caller shows; a store failure is not."""
    store = Store(db_path)
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])

    with pytest.raises(ConflictError):
        acquire(store, "t2", 1, [ResourceSpec("build", EXCLUSIVE)])

    assert issubclass(ConflictError, StoreError)
