"""Atomic, cross-process ownership of named resources.

A claim is one valid owner generation or free, and mutual exclusion lives in
the database. `claim_holders` has `resource_key` as its primary key, so a second
claimant for an exclusive resource loses to a constraint violation rather than
to an `if not already_claimed` test in Python that two processes can both pass.
The test would be a race between processes, which is the specific thing this
layer exists to prevent.

`acquire` takes a whole list of resources as one unit. Checking availability
and then writing would reopen the window the primary key just closed, so the
batch runs inside a single `BEGIN IMMEDIATE` transaction: the write lock
serializes the callers for its duration, and the first refusal rolls the batch
back so no resource named in the call is left held. A task therefore locks only
the resources it names, never the repository.

Ownership is held by a generation, not by a task. A superseded attempt is a
different generation and so cannot release or be seen as the owner of what its
successor holds, which is how an old attempt is stopped from submitting
accepted evidence after reassignment.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

from .storage import ConflictError, Store

# One statement grants a slot or grants nothing. The WHERE clause is the whole
# arbitration: an exclusive row carries a NULL capacity, so `held < capacity` is
# NULL and the update is skipped; a capacity row advances only while a slot is
# free. Pinning the declared capacity the same way means a caller cannot quietly
# widen a resource another caller believes is smaller, and there is one branch
# instead of a read followed by a kind-dependent write.
_TAKE_SLOT = """
INSERT INTO claim_holders (resource_key, kind, capacity, held, task_id, generation, acquired_at)
VALUES (?, ?, ?, 1, ?, ?, ?)
ON CONFLICT(resource_key) DO UPDATE SET held = claim_holders.held + 1
 WHERE claim_holders.capacity IS ?
   AND claim_holders.held < claim_holders.capacity
"""

# A holder's slot is recorded by row, not inferred from the counter. Re-inserting
# a holder the pool already has is a no-op on the members table, so the primary
# key is what stops one task taking three of a three-slot pool.
_TAKE_MEMBER = """
INSERT OR IGNORE INTO claim_members (resource_key, task_id, generation, acquired_at)
VALUES (?, ?, ?, ?)
"""

_CLAIM_COLUMNS = "resource_key, kind, capacity, held, task_id, generation, acquired_at"


@dataclass(frozen=True)
class ResourceSpec:
    """A named resource and the ownership it grants.

    The kind/capacity pairing is checked here rather than left to the schema, so
    a malformed request is a caller error at the call site instead of a CHECK
    violation translated back out of the driver.
    """

    key: str
    kind: Literal["exclusive", "capacity"]
    capacity: int | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("a resource key is required")
        if self.kind == "exclusive":
            if self.capacity is not None:
                raise ValueError(f"resource {self.key!r} is exclusive and cannot declare a capacity")
        elif self.kind == "capacity":
            if self.capacity is None or self.capacity < 1:
                raise ValueError(f"resource {self.key!r} is capacity-kind and needs a positive capacity")
        else:
            raise ValueError(f"resource {self.key!r} has unknown kind {self.kind!r}")


@dataclass(frozen=True)
class Claim:
    """Current ownership of one resource.

    `held` is how many of `capacity` are in use, so one row describes a whole
    pool rather than one row per member of it.
    """

    resource_key: str
    kind: str
    capacity: int | None
    held: int
    task_id: str
    generation: int
    acquired_at: str


def acquire(store: Store, task_id: str, generation: int, specs: Iterable[ResourceSpec]) -> None:
    """Take every named resource, or none of them.

    Raises ConflictError naming the first resource that could not be taken. On
    that error no resource in the call is held, including those the transaction
    had already granted.
    """
    with store.transaction() as conn:
        acquire_in(conn, task_id, generation, specs)


def acquire_in(
    conn: sqlite3.Connection, task_id: str, generation: int, specs: Iterable[ResourceSpec]
) -> None:
    """`acquire`, on a connection whose transaction the caller already opened.

    Admission must write a task row and take that task's claims together, so the
    acquisition has to join a transaction this module did not open. It is split
    out rather than restated in `tasks` because the arbitration is one SQL
    statement, and a second copy of it in the caller would be a second rule that
    could be fixed without the first.

    The caller commits. Nothing here commits or rolls back, so a caller's
    refusal rolls the claims back with everything else it wrote.
    """
    for spec in _distinct(specs):
        # capacity is bound twice: once as the value a new row declares,
        # once as the declaration an existing row must already match.
        params = (spec.key, spec.kind, spec.capacity, task_id, generation, _now(), spec.capacity)
        if conn.execute(_TAKE_SLOT, params).rowcount == 0:
            raise ConflictError(_unavailable(conn, spec))
        # The counter moved, so record who moved it. A task that already holds a
        # slot of this pool is not given a second one, and the counter is
        # corrected rather than left inflated.
        taken = conn.execute(
            _TAKE_MEMBER, (spec.key, task_id, generation, _now())
        ).rowcount
        if taken == 0:
            conn.execute(
                "UPDATE claim_holders SET held = held - 1 WHERE resource_key = ?",
                (spec.key,),
            )


def release(store: Store, task_id: str, generation: int, keys: Iterable[str] | None = None) -> None:
    """Give up this generation's claims, freeing the resources for anyone else.

    Naming a key the task does not hold is not an error and takes nothing from
    whoever does hold it: the delete is scoped to this task and generation, so a
    resource owned elsewhere is simply not matched. A superseded generation is
    refused outright, because letting it run would report success for an attempt
    that no longer owns anything.
    """
    with store.transaction() as conn:
        held_elsewhere = conn.execute(
            "SELECT DISTINCT generation FROM claim_members WHERE task_id = ? AND generation != ?",
            (task_id, generation),
        ).fetchall()
        if held_elsewhere:
            current = ", ".join(str(row[0]) for row in held_elsewhere)
            raise ConflictError(
                f"task {task_id!r} was superseded: generation {generation} cannot release "
                f"resources held at generation {current}"
            )

        # Membership decides what this release may take, and the counter is
        # corrected from the rows that survive. A task holding nothing therefore
        # takes nothing away, and a middle holder's slot is as recoverable as the
        # first holder's.
        if keys is None:
            conn.execute(
                "DELETE FROM claim_members WHERE task_id = ? AND generation = ?",
                (task_id, generation),
            )
        else:
            wanted = tuple(dict.fromkeys(keys))
            if wanted:
                marks = ",".join("?" * len(wanted))
                conn.execute(
                    f"DELETE FROM claim_members WHERE task_id = ? AND generation = ?"
                    f" AND resource_key IN ({marks})",
                    (task_id, generation, *wanted),
                )
        _resync_counts(conn)


def _resync_counts(conn: sqlite3.Connection) -> None:
    """Make `held` and membership agree, then drop any pool nobody holds.

    Both statements read the members table rather than adjusting by one, so a
    counter that has drifted cannot be laundered by a release: it is recomputed
    from the rows that actually exist. The second statement is what frees the
    resource key entirely, which the primary key needs in order for a later
    acquire to insert a fresh row.
    """
    conn.execute(
        "UPDATE claim_holders SET held = ("
        "  SELECT COUNT(*) FROM claim_members m"
        "  WHERE m.resource_key = claim_holders.resource_key)"
    )
    conn.execute(
        "DELETE FROM claim_holders WHERE NOT EXISTS ("
        "  SELECT 1 FROM claim_members m WHERE m.resource_key = claim_holders.resource_key)"
    )


#: Public name for `recover`, which releases a claim on a human's evidence and so
#: must not restate the counter arithmetic. One implementation of the rule, two
#: callers: a second copy is how the two drift apart and a pool starts leaking
#: again.
resync_claim_counts = _resync_counts


def holder(store: Store, key: str) -> Claim | None:
    """Who owns a resource right now, or None if it is free."""
    with store._connect() as conn:
        row = conn.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM claim_holders WHERE resource_key = ?", (key,)
        ).fetchone()
    return Claim(*row) if row is not None else None


def holders(store: Store, task_id: str | None = None) -> tuple[Claim, ...]:
    """Every held resource, optionally only one task's, in a stable order."""
    sql = f"SELECT {_CLAIM_COLUMNS} FROM claim_holders"
    params: list[object] = []
    if task_id is not None:
        sql += " WHERE task_id = ?"
        params.append(task_id)
    sql += " ORDER BY resource_key"
    with store._connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return tuple(Claim(*row) for row in rows)


def _distinct(specs: Iterable[ResourceSpec]) -> tuple[ResourceSpec, ...]:
    """Refuse a repeated key, which would mean two slots for one resource."""
    ordered = tuple(specs)
    seen: set[str] = set()
    for spec in ordered:
        if spec.key in seen:
            raise ValueError(f"resource {spec.key!r} is named twice in one acquire")
        seen.add(spec.key)
    return ordered


def _unavailable(conn: sqlite3.Connection, spec: ResourceSpec) -> str:
    """Say why the resource could not be taken, in terms a user can act on."""
    row = conn.execute(
        f"SELECT {_CLAIM_COLUMNS} FROM claim_holders WHERE resource_key = ?", (spec.key,)
    ).fetchone()
    if row is None:
        return f"resource {spec.key!r} could not be claimed and is not recorded as held"
    current = Claim(*row)
    owner = f"task {current.task_id!r} at generation {current.generation}"
    if spec.capacity is not None and current.capacity != spec.capacity:
        return (f"resource {spec.key!r} is declared with capacity {current.capacity}, "
                f"not {spec.capacity}, and is held by {owner}")
    if spec.capacity is None:
        return f"resource {spec.key!r} is already held by {owner}"
    return (f"resource {spec.key!r} is full, {current.held} of {current.capacity} "
            f"slots held, owned by {owner}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
