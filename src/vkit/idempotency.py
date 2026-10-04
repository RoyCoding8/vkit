"""Idempotency keys, so a retried request does not execute twice.

A client that loses its connection does not know whether its request ran. If the
retry starts a second run, the caller is left holding two executions and one
answer, with no way to tell which the evidence describes. So the identity of a
request is recorded before anything executes, and a retry finds the identity it
already has instead of minting a new one.

## The insert is the coordination point

The obvious implementation is a SELECT to see whether the key exists, and an
INSERT when it does not. That is a race pointed the damaging way: two processes
both see "not present" and both proceed, and the primary key that would have
caught it is consulted after it can no longer stop anything. Here the INSERT is
attempted unconditionally and the constraint violation is the answer. The
database decides, under its own lock, which claimant owns the key, so the loser
learns who won instead of guessing.

Minting the subject id sits inside the protected region for the same reason. A
subject generated after a lost insert would be a second subject for one request,
and the caller holding it would run the operation again.

## Keys do not expire

There is no TTL, no sweeper, and no background cleanup, and `forget_subject`
exists for a future plan that has a specific subject to retire, not because the
rows need sweeping. A key must outlive the operation it names: a retry arriving
after the operation completed still has to find its original record, and a row
removed on a timer is a duplicated execution waiting for the next timeout. The
row is removed by a decision, never by a clock.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .storage import ConflictError, Store, StoreError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload_hash(payload: Any) -> str:
    """The identity of a payload: sha256 over canonical JSON.

    Sorting the keys is the whole point. A client that resends the same request
    with its object's keys in another order has resent the same request, and a
    digest that noticed the ordering would refuse a legitimate retry as a
    conflicting payload. The separators and the non-ASCII passthrough match the
    canonical form in `storage`, so the codebase serialises one way.

    List order stays significant, because it is: two payloads that differ only
    in list order are different requests.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def begin(
    store: Store,
    *,
    request_id: str,
    operation: str,
    payload: Any,
    subject_id: str | None = None,
) -> str:
    """Claim `(request_id, operation)` for `payload` and return the subject to use.

    The first call records the key and returns `subject_id`, generating one when
    the caller has none. A repeat with the same payload returns the subject
    already recorded, so the caller attaches to the existing run instead of
    starting a second. A repeat with a different payload raises, because one key
    naming two different requests is a client bug and guessing which to honour
    is how the duplicate gets created.
    """
    digest = payload_hash(payload)
    candidate = subject_id or uuid.uuid4().hex
    with store._connect() as conn:
        try:
            conn.execute(
                "INSERT INTO request_keys (request_id, operation, payload_hash, subject_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (request_id, operation, digest, candidate, _now()),
            )
        except sqlite3.IntegrityError as exc:
            row = conn.execute(
                "SELECT payload_hash, subject_id FROM request_keys"
                " WHERE request_id = ? AND operation = ?",
                (request_id, operation),
            ).fetchone()
            if row is None:
                raise StoreError(
                    f"idempotency key {request_id!r} for {operation!r} collided but could "
                    "not be read back"
                ) from exc
            recorded_hash, recorded_subject = row
            if recorded_hash != digest:
                raise ConflictError(
                    f"idempotency key {request_id!r} for {operation!r} was already used "
                    "with a different payload"
                ) from exc
            if recorded_subject is None:
                raise StoreError(
                    f"idempotency key {request_id!r} for {operation!r} is recorded without "
                    "a subject, so this retry has nothing to attach to"
                ) from exc
            return recorded_subject
        return candidate


def subject_of(store: Store, *, request_id: str, operation: str) -> str | None:
    """The subject recorded for this key, or None when there is none to give.

    The operation is part of the key, so a request id used for one operation
    says nothing about another.
    """
    with store._connect() as conn:
        row = conn.execute(
            "SELECT subject_id FROM request_keys WHERE request_id = ? AND operation = ?",
            (request_id, operation),
        ).fetchone()
    return None if row is None else row[0]


def forget_subject(store: Store, subject_id: str) -> None:
    """Remove every request key that named this subject.

    Nothing in Plan 02 calls this. A key has to outlive the operation it names,
    so a retry arriving after the operation completed still finds its original
    record instead of starting a second execution; a key removed on a timer
    reopens exactly that window. This is here for a later plan that has a
    specific subject to retire, and it is a decision, not a sweep.
    """
    with store._connect() as conn:
        conn.execute("DELETE FROM request_keys WHERE subject_id = ?", (subject_id,))
