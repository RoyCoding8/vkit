"""Task attempts, their generations, and the readiness computed from evidence.

An attempt is `active`, `paused`, `superseded`, or `closed`. Modelling those as
four variants rather than a status string plus a null owner means "superseded
but still holding resources" cannot be half-written by accident: the resources
each state implies are the state.

`readiness` is the only place that decides whether a task is READY, and it is a
pure function of recorded evidence. It is never a client-supplied verdict, and
it is never merge permission: READY means local requirements were satisfied
under the recorded contract, nothing more.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from .storage import ConflictError, Store, StoreError

TaskStatus = Literal["active", "paused", "closed"]
Readiness = Literal["READY", "REJECTED", "BLOCKED"]


class TaskError(Exception):
    """The requested task operation is not valid for the recorded state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    status: TaskStatus
    generation: int
    contract: dict[str, Any]
    policy_digest: str
    readiness: Readiness | None


def _in_transaction(fn):
    """Run `fn(conn, store, ...)` inside one BEGIN IMMEDIATE, committing only on success.

    Read-then-write without holding the write lock is the race this whole module
    exists to prevent, so the lock is taken before the first read every time.
    The decorated function takes the store first at the call site; it is
    unpacked here so the body sees both the connection and the store.
    """
    def wrapped(store: Store, *args, **kwargs):
        with store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn, store, *args, **kwargs)
                conn.execute("COMMIT")
                return result
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    return wrapped


def _row_to_task(conn, task_id: str) -> TaskRecord:
    """Read a task on the caller's connection.

    Every read inside a transaction must use that connection. Opening a second
    one returns the pre-commit state, which made a successful `set_status`
    report the old status; the write was right and the answer was a lie.
    """
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise TaskError(f"no such task: {task_id}")
    return _row_to_task_row(row)


_TASK_COLUMNS = "task_id, status, generation, contract_json, policy_digest, readiness"


def _row_to_task_row(row) -> TaskRecord:
    return TaskRecord(
        task_id=row[0],
        status=row[1],
        generation=row[2],
        contract=json.loads(row[3]),
        policy_digest=row[4],
        readiness=row[5],
    )


@_in_transaction
def open_task(conn, store: Store, *, task_id: str, contract: dict, policy_digest: str) -> TaskRecord:
    """Open a task at generation 1, pinning its contract and policy.

    The contract and policy digest are pinned here, not read at finalize time, so
    a later policy change cannot retroactively satisfy this task, and a task
    cannot quietly run against a weaker contract than the one reviewed.
    """
    if not isinstance(contract, dict) or not contract:
        raise TaskError("a task contract must be a non-empty object")
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, status, generation, contract_json, policy_digest, opened_at)"
            " VALUES (?, 'active', 1, ?, ?, ?)",
            (task_id, json.dumps(contract, sort_keys=True), policy_digest, _now()),
        )
    except Exception as exc:
        raise TaskError(f"task {task_id} already exists") from exc
    return _row_to_task(conn, task_id)


def get_task(store: Store, task_id: str) -> TaskRecord:
    with store._connect() as conn:
        return _row_to_task(conn, task_id)


@_in_transaction
def supersede_task(conn, store: Store, task_id: str) -> TaskRecord:
    """Advance the generation, invalidating the previous attempt's authority.

    The generation advances only here, and only while the task is not closed.
    Claims held at the old generation are deliberately NOT released: the old
    process may still be alive and writing, and a superseded attempt whose
    resources vanished under it is how a stale worker clobbers a live one.
    Reconciliation is an explicit recovery action with evidence behind it.
    """
    row = conn.execute("SELECT status, generation FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskError(f"no such task: {task_id}")
    status, generation = row
    if status == "closed":
        raise TaskError(f"task {task_id} is closed and cannot be reassigned")
    conn.execute(
        "UPDATE tasks SET generation = ?, status = 'active' WHERE task_id = ?",
        (generation + 1, task_id),
    )
    return _row_to_task(conn, task_id)


@_in_transaction
def set_status(conn, store: Store, task_id: str, status: TaskStatus) -> TaskRecord:
    """Pause or close a task.

    Paused does not mean the resources are safe to release. A paused attempt's
    process may still be running, so the claims stay held and the state records
    that the owner is idle rather than gone.
    """
    if status not in ("active", "paused", "closed"):
        raise TaskError(f"unknown task status {status!r}")
    row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskError(f"no such task: {task_id}")
    if row[0] == "closed":
        # A closed task retains its reports and its result is final. Reopening it
        # let a caller resurrect a finished task and then record a verdict on it,
        # and left the row simultaneously 'active' and stamped closed.
        raise TaskError(f"task {task_id} is closed and cannot be reopened")
    if status == "closed":
        conn.execute(
            "UPDATE tasks SET status = 'closed', closed_at = ? WHERE task_id = ?",
            (_now(), task_id),
        )
    else:
        conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))
    return _row_to_task(conn, task_id)


def current_generation(store: Store, task_id: str) -> int:
    return get_task(store, task_id).generation


@dataclass(frozen=True)
class ReadinessResult:
    readiness: Readiness
    gaps: tuple[str, ...]
    context: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"readiness": self.readiness, "gaps": list(self.gaps), "context": self.context}


def compute_readiness(
    store: Store,
    task_id: str,
    *,
    required_check_ids: Iterable[str],
) -> ReadinessResult:
    """Decide READY, REJECTED, or BLOCKED from recorded evidence only.

    Order matters and encodes the domain:
      1. A recorded FAIL or BLOCKED decides immediately. A failing check is not
         "incomplete", it is an answer, and REJECTED outranks BLOCKED.
      2. A missing required check is BLOCKED, not READY. Absent evidence is
         never success, which is the rule the whole product rests on.
      3. Only a complete set of passing runs under the current contract and
         policy reaches READY.

    The generation is re-read inside the same transaction that records the
    result, so an attempt that was superseded while this was computing cannot
    publish READY for a contract it no longer holds.
    """
    required = tuple(dict.fromkeys(required_check_ids))
    gaps: list[str] = []
    task = get_task(store, task_id)

    runs = store.list_runs(task_id=task_id, limit=1000)
    by_check: dict[str, list[dict]] = {}
    for run in runs:
        if run["lifecycle"] == "terminal" and run["result"]:
            by_check.setdefault(run["check_id"], []).append(run)

    # A run counts as PASSING evidence for this attempt only if it belongs to
    # this attempt. `supersede_task` advances the generation to invalidate the
    # previous attempt's authority, and a retry is a new run linked to its
    # predecessor rather than a continuation of it. Without this filter a
    # reassigned task was handed its own READY: generation 2 had produced no
    # runs at all and inherited every pass from generation 1.
    #
    # A stale run still counts as a FAILURE. CONTRACT says to preserve the
    # original failure, flag the instability, and never overwrite it with the
    # later pass. A first draft filtered stale runs out entirely, which is the
    # blunt reading, and the property test caught it on its first ever
    # execution: `record FAIL` then `crash` reported BLOCKED where the answer is
    # REJECTED. A superseded attempt's failure is still a failure, and a retry
    # that has not yet re-run the check is not a repair of it.
    #
    # A run recorded before attempts existed carries no attempt number. CONTRACT
    # says such a report stays readable as standalone evidence and that migration
    # must not invent task acceptance for it, so it cannot pass acceptance.
    stale: list[str] = []
    stale_failures: list[str] = []
    by_check = {}
    for run in runs:
        if run["lifecycle"] != "terminal" or not run["result"]:
            continue
        if run["attempt"] != task.generation:
            stale.append(
                f"check {run['check_id']!r} was recorded at attempt "
                f"{run['attempt'] if run['attempt'] is not None else 'none'}, not the current "
                f"attempt {task.generation}"
            )
            if run["result"] == "FAIL":
                stale_failures.append(
                    f"check {run['check_id']!r} FAILED at attempt {run['attempt']} and has not "
                    f"been re-run at attempt {task.generation}; the original failure is "
                    f"preserved rather than replaced by a later pass"
                )
            continue
        by_check.setdefault(run["check_id"], []).append(run)

    rejected = False
    for check_id in required:
        outcomes = by_check.get(check_id, [])
        if not outcomes:
            gaps.append(f"no completed run for required check {check_id!r}")
            continue
        latest = outcomes[0]
        if latest["result"] == "FAIL":
            rejected = True
        elif latest["result"] == "BLOCKED":
            gaps.append(
                f"required check {check_id!r} is BLOCKED: {latest['reason'] or 'no reason recorded'}"
            )
        elif latest["result"] != "PASS":
            gaps.append(f"required check {check_id!r} has no usable result")

    # A stale run is not silently dropped. It is reported, so a reader who can
    # see a passing run for a required check is told why it does not count,
    # rather than left to conclude the check never ran.
    gaps = tuple(dict.fromkeys(gaps)) + tuple(dict.fromkeys(stale))

    if stale_failures:
        # REJECTED, and the reason says the failure is a preserved earlier one
        # rather than a fresh verdict, so nobody reads REJECTED as "this attempt
        # just failed" when the truth is "the earlier one did and nothing has
        # re-run it since".
        return ReadinessResult(
            "REJECTED",
            tuple(dict.fromkeys(gaps)) + tuple(dict.fromkeys(stale_failures)),
            {"generation": task.generation, "policy_digest": task.policy_digest},
        )
    if rejected:
        return ReadinessResult(
            "REJECTED", gaps,
            {"generation": task.generation, "policy_digest": task.policy_digest},
        )
    if gaps:
        return ReadinessResult("BLOCKED", gaps,
                               {"generation": task.generation, "policy_digest": task.policy_digest})
    return ReadinessResult(
        "READY", (),
        {"generation": task.generation, "policy_digest": task.policy_digest},
    )


@_in_transaction
def record_readiness(conn, store: Store, task_id: str, result: ReadinessResult) -> TaskRecord:
    """Store a readiness verdict, refusing if the attempt was superseded.

    The generation is compared inside this transaction. A client that began
    finalizing at generation 1 and was reassigned mid-computation must not be
    able to record READY at generation 2.
    """
    row = conn.execute("SELECT generation, status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskError(f"no such task: {task_id}")
    generation, status = row
    if status == "closed":
        raise TaskError(f"task {task_id} is closed; its result is final")
    if result.context.get("generation") != generation:
        raise ConflictError(
            f"task {task_id} was reassigned from generation "
            f"{result.context.get('generation')} to {generation}; refusing to record "
            "readiness for a superseded attempt"
        )
    conn.execute("UPDATE tasks SET readiness = ? WHERE task_id = ?", (result.readiness, task_id))
    return _row_to_task(conn, task_id)
