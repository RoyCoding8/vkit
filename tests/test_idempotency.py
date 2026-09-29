"""Behavior of the idempotency keys, driven through the public surface.

Every test calls `begin` the way a client calls it and reads the result back out
of the database file with an independent connection, because the value that
matters is what survives a process boundary, not what a function returned. The
race test launches real interpreters against one shared file for the same
reason: a lock held inside one process is not what separates a CLI from a
supervisor on the other side of a socket.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

# conftest.py puts this checkout's src ahead of the editable install, so the
# module under test is this worktree's. Assert it rather than trust it: a gate
# that silently measures another owner's code is a false pass.
_THIS_SRC = (Path(__file__).resolve().parents[1] / "src").resolve()
import vkit.idempotency as _idempotency  # noqa: E402

if Path(_idempotency.__file__).resolve() != (_THIS_SRC / "vkit" / "idempotency.py"):
    raise AssertionError(
        f"vkit.idempotency resolved to {_idempotency.__file__}, not this worktree's "
        f"{_THIS_SRC / 'vkit' / 'idempotency.py'}. Refusing to run the gate against "
        "another owner's code."
    )

Store = _idempotency.Store
begin = _idempotency.begin
forget_subject = _idempotency.forget_subject
subject_of = _idempotency.subject_of

ConflictError = _idempotency.ConflictError
StoreError = _idempotency.StoreError

# The interpreter under test, not a bare "python". A child launched from the
# system interpreter would import a different vkit, and the race would be run
# against code this gate never measured.
PYTHON = sys.executable

RACE_PROCESSES = 6
RACE_TIMEOUT_S = 60.0
# Each child has to reach its insert before any of them is released, or the race
# is really a queue. A second of deadline pressure on a loaded CI box is not
# evidence of a broken synchronisation.
SYNC_DEADLINE_S = 30.0


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "verification-kit" / "state.sqlite3"


@pytest.fixture()
def store(db_path: Path) -> Store:
    return Store(db_path)


def recorded(db_path: Path, request_id: str, operation: str) -> tuple | None:
    """Read the key row back over an independent connection."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT payload_hash, subject_id, created_at FROM request_keys"
            " WHERE request_id = ? AND operation = ?",
            (request_id, operation),
        ).fetchone()


def rows_for(db_path: Path, request_id: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT operation, payload_hash, subject_id FROM request_keys"
            " WHERE request_id = ? ORDER BY operation",
            (request_id,),
        ).fetchall()


def count(db_path: Path, request_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM request_keys WHERE request_id = ?", (request_id,)
        ).fetchone()[0]


def test_first_call_records_the_key_and_returns_a_subject(store: Store, db_path: Path) -> None:
    subject = begin(store, request_id="req-1", operation="check start", payload={"check": "unit"})

    assert subject != ""
    assert subject_of(store, request_id="req-1", operation="check start") == subject
    row = recorded(db_path, "req-1", "check start")
    assert row is not None
    assert row[1] == subject
    assert len(row[0]) == 64
    assert row[2].startswith("20")


def test_the_callers_subject_is_the_one_recorded(store: Store, db_path: Path) -> None:
    subject = begin(
        store, request_id="req-1", operation="run cancel", payload={"run": "run-1"},
        subject_id="run-1",
    )

    assert subject == "run-1"
    assert recorded(db_path, "req-1", "run cancel")[1] == "run-1"


def test_the_same_call_again_returns_the_same_subject(store: Store, db_path: Path) -> None:
    first = begin(store, request_id="req-1", operation="check start", payload={"check": "unit"})
    second = begin(store, request_id="req-1", operation="check start", payload={"check": "unit"})

    assert second == first
    # One request, one subject. A second row is the whole failure this module
    # exists to prevent, so the table is checked, not just the return value.
    assert count(db_path, "req-1") == 1


def test_a_retry_still_finds_its_subject_after_the_store_is_rebuilt(
    db_path: Path,
) -> None:
    first = begin(Store(db_path), request_id="req-1", operation="check start", payload={"n": 1})

    # A different Store object, as a second process would build. The key has to
    # be in the file, not in whatever state the first connection held.
    again = begin(Store(db_path), request_id="req-1", operation="check start", payload={"n": 1})

    assert again == first


def test_the_same_key_with_a_different_payload_is_refused(store: Store, db_path: Path) -> None:
    begin(store, request_id="req-7", operation="check start", payload={"check": "unit", "task": "t-1"})

    with pytest.raises(ConflictError) as caught:
        begin(store, request_id="req-7", operation="check start", payload={"check": "unit", "task": "t-2"})

    message = str(caught.value)
    assert "req-7" in message
    assert "check start" in message
    assert "different payload" in message
    # The refusal changes nothing. The first call still owns the key, so a
    # client that mistypes its payload has not replaced the recorded subject.
    assert recorded(db_path, "req-7", "check start")[1] is not None
    assert count(db_path, "req-7") == 1


def test_one_key_per_operation_names_both_holds(store: Store, db_path: Path) -> None:
    """A single request id driving two operations keeps two distinguishable rows.

    A schema that keyed on the request id alone would have thrown the second
    call away, and a message that named only the id would be ambiguous to a
    client that reused the id on purpose.
    """
    start_subject = begin(
        store, request_id="req-9", operation="check start", payload={"check": "unit"}
    )
    cancel_subject = begin(
        store, request_id="req-9", operation="run cancel", payload={"run": "run-1"}
    )

    assert start_subject != cancel_subject
    assert subject_of(store, request_id="req-9", operation="check start") == start_subject
    assert subject_of(store, request_id="req-9", operation="run cancel") == cancel_subject

    holds = rows_for(db_path, "req-9")
    assert [row[0] for row in holds] == ["check start", "run cancel"]
    # The two payloads differ, so the two recorded digests differ too, which is
    # what keeps the operations from being mistaken for one another.
    assert holds[0][1] != holds[1][1]
    assert len({row[2] for row in holds}) == 2


def test_a_reordered_payload_is_a_retry_not_a_conflict(store: Store) -> None:
    first = begin(
        store, request_id="req-2", operation="check start",
        payload={"check": "unit", "task": "t-1", "options": {"retries": 2, "verbose": True}},
    )

    # Same request, resent with the keys in another order at both levels. Dict
    # ordering is not part of a request's identity; a digest that noticed it
    # would refuse every retry a client reserializes differently.
    second = begin(
        store, request_id="req-2", operation="check start",
        payload={"options": {"verbose": True, "retries": 2}, "task": "t-1", "check": "unit"},
    )

    assert second == first


def test_a_reordered_list_is_a_different_payload(store: Store) -> None:
    begin(store, request_id="req-3", operation="task finalize", payload={"checks": ["a", "b"]})

    # The counterpart to the reordering test: order inside a list is content,
    # not presentation, so this is a genuine conflict and not a retry.
    with pytest.raises(ConflictError, match="different payload"):
        begin(store, request_id="req-3", operation="task finalize", payload={"checks": ["b", "a"]})


def test_subject_of_is_none_for_an_unknown_key(store: Store) -> None:
    assert subject_of(store, request_id="never-used", operation="check start") is None

    begin(store, request_id="req-4", operation="check start", payload={})

    # Present for one operation, still unknown for the other. A request id on
    # its own is not a lookup key.
    assert subject_of(store, request_id="never-used", operation="check start") is None
    assert subject_of(store, request_id="req-4", operation="run cancel") is None


def test_forget_subject_removes_only_that_subject(store: Store, db_path: Path) -> None:
    kept = begin(store, request_id="req-5", operation="check start", payload={"n": 1})
    dropped = begin(
        store, request_id="req-5", operation="run cancel", payload={"n": 2}, subject_id="run-1"
    )

    forget_subject(store, dropped)

    assert subject_of(store, request_id="req-5", operation="run cancel") is None
    assert subject_of(store, request_id="req-5", operation="check start") == kept
    assert [row[0] for row in rows_for(db_path, "req-5")] == ["check start"]


def test_a_conflict_error_is_a_store_error(store: Store) -> None:
    """A caller that catches StoreError catches the conflict too.

    The conflict is a legitimate answer the user is shown, so it has to arrive
    through the same door as every other thing the store refuses. A caller that
    caught only sqlite3.IntegrityError would be reaching past its own module's
    contract into a driver detail.
    """
    begin(store, request_id="req-6", operation="check start", payload={"n": 1})

    with pytest.raises(StoreError):
        begin(store, request_id="req-6", operation="check start", payload={"n": 2})


def test_the_recorded_subject_is_the_one_a_later_process_reads(db_path: Path) -> None:
    """The subject is in the file, not in the connection that minted it.

    The id is generated before the insert, so a process that lost the race never
    held a second one to hand back, and this reads the id the row actually
    carries rather than the value the local variable still names.
    """
    begin(Store(db_path), request_id="req-8", operation="check start", payload={"n": 1})

    row = recorded(db_path, "req-8", "check start")

    assert row is not None and row[1]
    assert subject_of(Store(db_path), request_id="req-8", operation="check start") == row[1]

# --- cross-process ------------------------------------------------------------
#
# A thread cannot demonstrate what the product needs to claim. Two clients are
# two processes with two connections to one file, so the children below are real
# interpreters, and the only thing standing between them is the database.

"""The child every cross-process test launches.

Written from a template so one test can vary the operation and the payload
without editing the script another test is already running. The doubled braces
are the literal dict the child passes to `begin`.
"""
CHILD_SOURCE = '''
import os, sys, time
sys.path.insert(0, {src!r})
from vkit.idempotency import begin
from vkit.storage import Store

db_path, gate, ready, out_path, request_id = sys.argv[1:6]

# The pid file is written by the child itself, so its presence means this
# process is past its import and parked on the gate, not that a spawn returned.
with open(ready, "w") as fh:
    fh.write(str(os.getpid()))

# Bounded so a gate that is never opened delays this child rather than hanging
# the gate, and the gate's own check reports the problem instead of timing out
# on a process that is still waiting on a file nobody wrote.
if gate:
    deadline = time.monotonic() + {wait}
    while not os.path.exists(gate) and time.monotonic() < deadline:
        time.sleep(0.002)

subject = begin(Store(db_path), request_id=request_id, operation={operation!r}, payload={payload})
sys.stdout.write(subject)
with open(out_path, "w") as fh:
    fh.write(subject)
'''


def write_child(directory: Path, *, operation: str, payload: str = '{"check": "unit"}') -> Path:
    script = directory / "claim.py"
    script.write_text(
        CHILD_SOURCE.format(
            src=str(_THIS_SRC), wait=SYNC_DEADLINE_S, operation=operation, payload=payload
        ),
        encoding="utf-8",
    )
    return script


def ready_dir(directory: Path) -> Path:
    path = directory / "ready"
    path.mkdir(exist_ok=True)
    return path


def child_argv(
    script: Path, db_path: Path, directory: Path, request_id: str, index: int, *, gate: bool
) -> list[str]:
    # An empty gate argument means the child never waits: its presence alone is
    # enough to press the insert, which is what a second concurrent client does.
    return [
        PYTHON, str(script), str(db_path),
        str(directory / "gate") if gate else "",
        str(ready_dir(directory) / f"ready-{index}.pid"),
        str(directory / f"out-{index}.txt"),
        request_id,
    ]


def spawn(script: Path, db_path: Path, directory: Path, request_id: str):
    """Start RACE_PROCESSES children, each announcing itself with a pid file."""
    return [
        subprocess.Popen(
            child_argv(script, db_path, directory, request_id, index, gate=True),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for index in range(RACE_PROCESSES)
    ]


def wait_for(path: Path, timeout: float) -> bool:
    """Poll for a file, finishing as soon as it appears rather than on a fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.01)
    return False


def release_and_collect(gate: Path, children) -> list[str]:
    """Open the gate only once every child is parked on it, then take what each got.

    Releasing the children one at a time would measure process scheduling rather
    than the database, so the gate stays shut until all of them have announced
    themselves from inside their own process.
    """
    for index in range(len(children)):
        assert wait_for(gate.parent / "ready" / f"ready-{index}.pid", SYNC_DEADLINE_S), (
            f"child {index} never reached the gate within {SYNC_DEADLINE_S}s, so the "
            "inserts did not contend and the race was never run"
        )

    gate.write_text("go", encoding="utf-8")
    subjects = []
    try:
        for child in children:
            try:
                stdout, stderr = child.communicate(timeout=RACE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise AssertionError("a child never returned from begin()")
            assert child.returncode == 0, f"a child failed: {stdout}\n{stderr}"
            assert stdout.strip(), f"a child printed no subject: {stderr}"
            subjects.append(stdout.strip())
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()
    return subjects


def test_two_processes_racing_on_one_key_produce_one_subject(db_path: Path, tmp_path: Path) -> None:
    """Plan 02 asks for separate processes, and for exactly one subject.

    Six real interpreters are released onto one fresh key at the same instant.
    Each prints the subject it was handed and writes it to its own file. The
    claim under test is that all six got the same id and the database holds
    exactly one row naming it.
    """
    Store(db_path)  # the schema exists before the children start, so the race
    # under test is the claim and not the migration.
    script = write_child(tmp_path, operation="check start")
    children = spawn(script, db_path, tmp_path, "req-race")

    subjects = release_and_collect(tmp_path / "gate", children)

    row = recorded(db_path, "req-race", "check start")
    assert count(db_path, "req-race") == 1
    assert row is not None
    assert subjects[0] == row[1]
    assert len(set(subjects)) == 1, (
        f"the processes disagreed about the subject: {sorted(set(subjects))}"
    )
    # Read what the children themselves wrote, so the agreement is not only a
    # parent's reading of their stdout.
    written = sorted(path.read_text(encoding="utf-8") for path in tmp_path.glob("out-*.txt"))
    assert len(written) == RACE_PROCESSES
    assert set(written) == {row[1]}
    # And a process that starts afterwards reads the same subject.
    assert subject_of(Store(db_path), request_id="req-race", operation="check start") == row[1]


def test_racing_processes_with_a_foreign_payload_are_all_refused(
    db_path: Path, tmp_path: Path
) -> None:
    """The cross-process half of "same key plus different payload is an error".

    The winner recorded a different payload for this key, so a child asking for
    this one must be refused rather than handed the winner's subject, which
    belongs to a request it never made. No gate here: the key is already
    recorded, so the children contend on the insert immediately.
    """
    begin(Store(db_path), request_id="req-clash", operation="check start", payload={"check": "integration"})

    script = write_child(tmp_path, operation="check start", payload='{"check": "unit"}')
    for index in range(RACE_PROCESSES):
        done = subprocess.run(
            child_argv(script, db_path, tmp_path, "req-clash", index, gate=False),
            capture_output=True, text=True, timeout=RACE_TIMEOUT_S, cwd=str(tmp_path),
        )
        assert done.returncode != 0, f"a conflicting payload was accepted: {done.stdout}"
        assert "different payload" in done.stderr
        assert "req-clash" in done.stderr
        assert "check start" in done.stderr
        assert not (tmp_path / f"out-{index}.txt").exists()

    # The refusals changed nothing. The winner still owns the key, and it is
    # still that winner's subject.
    assert count(db_path, "req-clash") == 1
    assert subject_of(Store(db_path), request_id="req-clash", operation="check start") is not None


def test_a_key_recorded_by_one_process_is_honoured_by_the_next(
    db_path: Path, tmp_path: Path
) -> None:
    """No clock retires a key, so a retry in a later process still attaches.

    Plan 02 stores these identities for the task lifetime rather than expiring
    them while a retry can still duplicate execution. Nothing waits and nothing
    cleans up between these two processes, and the second still receives the
    first one's subject.
    """
    first = begin(
        Store(db_path), request_id="req-handoff", operation="check start", payload={"check": "unit"}
    )

    script = write_child(tmp_path, operation="check start")
    done = subprocess.run(
        child_argv(script, db_path, tmp_path, "req-handoff", 0, gate=False),
        capture_output=True, text=True, timeout=RACE_TIMEOUT_S, cwd=str(tmp_path),
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == first
    assert count(db_path, "req-handoff") == 1

