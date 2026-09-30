"""Bounded concurrency, measured with real processes.

The plan asks for a low capacity bound and one hundred simulated clients, and
it is explicit that these are real separate processes using the public app
interface. Threads would share one interpreter and could be serialised by a
connection the thread happens to hold, which is the specific failure the claim
table is about, so nothing here is threaded.

**What is measured.** Every client is a real subprocess that opens its own
connection to the same SQLite file and calls the same `claims.acquire` a
coordinator would. The number of *granted* slots can never exceed the bound,
because the bound is the primary key's WHERE clause, and the test asserts it
from an observer that opens its own connection to the file rather than from
anything the writers returned.

**Progress resumes.** The bound is low enough that one client holding a slot
blocks the rest, and the test then releases that slot and shows a client that
was refused moments earlier now succeeds. A capacity pool that never drains
would pass the "never exceeded" half of this and fail the other half.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

sys.path.insert(0, str(SRC))

from vkit.claims import ResourceSpec, acquire, holder, release  # noqa: E402
from vkit.storage import Store  # noqa: E402

WRITER_POOL = "capacity:writers"
EXCLUSIVE = "exclusive"

# The client program, run once per simulated client in its own process. It is a
# separate file rather than an inline `-c` so that what each process executes is
# readable, and so a failure names the code that failed.
CLIENT_SOURCE = '''\
"""One simulated client. Real process, real connection, real claim."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, {src!r})

from vkit.claims import ResourceSpec, acquire, release
from vkit.storage import ConflictError, Store

db_path, pool_key, kind, capacity, task_id, index = sys.argv[1:7]
if kind == "exclusive":
    spec = ResourceSpec(pool_key, "exclusive")
else:
    spec = ResourceSpec(pool_key, "capacity", capacity=int(capacity))

store = Store(Path(db_path))
result = {{"index": int(index), "status": "conflict"}}
# A small stagger so the clients are genuinely concurrent rather than a queue
# that happens to be spread out. A bound that only holds for a serialized run is
# not a bound.
time.sleep((int(index) % 7) * 0.002)
try:
    acquire(store, task_id, 1, [spec])
    result = {{"index": int(index), "status": "granted"}}
except ConflictError as exc:
    result = {{"index": int(index), "status": "conflict", "detail": str(exc)[:200]}}
print(json.dumps(result))
'''


def client_program(tmp_path: Path) -> Path:
    path = tmp_path / "client.py"
    path.write_text(CLIENT_SOURCE.format(src=str(SRC)), encoding="utf-8")
    return path


def run_clients(
    program: Path, db_path: Path, pool_key: str, kind: str, capacity: int | None,
    count: int, hold_seconds: float = 0.0,
) -> list[dict]:
    """Launch `count` real processes and collect what each one was told."""
    running = [
        subprocess.Popen(
            [sys.executable, str(program), str(db_path), pool_key, kind,
             str(capacity or 0), f"task-{i:03d}", str(i)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        for i in range(count)
    ]
    if hold_seconds:
        time.sleep(hold_seconds)
    results = []
    for proc in running:
        out, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, f"client failed: {err}"
        results.append(json.loads(out.strip().splitlines()[-1]))
    return results


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "state.sqlite3"


def test_a_hundred_clients_never_exceed_a_low_bound_and_progress_resumes(
    db_path: Path, tmp_path: Path
) -> None:
    """The plan's eighth row, measured rather than asserted.

    A hundred real processes, a bound of three. The peak concurrent holders is
    read from an observer that opens its own connection, so it is what survived
    to disk rather than what any writer reported. Then a slot is released and a
    client that was refused gets in.
    """
    program = client_program(tmp_path)
    store = Store(db_path)
    # Prime the pool so its capacity is on disk before the clients race for it.
    acquire(store, "prime", 1, [ResourceSpec(WRITER_POOL, "capacity", capacity=3)])
    release(store, "prime", 1, [WRITER_POOL])

    results = run_clients(program, db_path, WRITER_POOL, "capacity", 3, 100)
    granted = [r for r in results if r["status"] == "granted"]
    refused = [r for r in results if r["status"] == "conflict"]

    assert len(results) == 100
    assert len(granted) == 3, f"{len(granted)} clients were granted a bound of 3"
    assert len(refused) == 97
    # Every refusal names the pool, so a client that lost knows what it lost to.
    assert all("capacity:writers" in r["detail"] for r in refused)

    # The observer: a fresh connection to the file, sharing nothing with the
    # writers. Whatever it says is what is on disk.
    observed = holder(store, WRITER_POOL)
    assert observed is not None
    assert observed.capacity == 3
    assert observed.held == 3

    # Progress resumes when slots release. Every granted client releases its OWN
    # slot and the count falls by exactly one each time, which is the assertion
    # that matters: a pool where only the recorded holder's release works would
    # keep `held` at 3 through two of these and delete the whole row on the
    # third, hiding two lost slots behind a freed row. Checking the count after
    # EACH release is what makes that visible.
    for client in granted:
        before = holder(store, WRITER_POOL).held
        release(store, f"task-{client['index']:03d}", 1, [WRITER_POOL])
        remaining = holder(store, WRITER_POOL)
        if before > 1:
            assert remaining is not None and remaining.held == before - 1, (
                f"releasing {client['index']} did not return its slot: "
                f"held was {before}, now {None if remaining is None else remaining.held}"
            )
        else:
            # The last release empties the pool and the row goes with it. A
            # capacity row with no members is not a bound of zero, it is no
            # bound, and leaving it would be a second way to read "full".
            assert remaining is None, f"an empty pool still has a row: {remaining}"
    # Every slot is back, and a fresh client is admitted.
    assert holder(store, WRITER_POOL) is None
    second = run_clients(program, db_path, WRITER_POOL, "capacity", 3, 1)
    assert second[0]["status"] == "granted", second
    assert holder(store, WRITER_POOL).held == 1


def test_a_held_slot_keeps_the_peak_at_the_bound_while_clients_still_ask(
    db_path: Path, tmp_path: Path
) -> None:
    """The bound holds under sustained contention, not just a burst.

    One client takes a slot and holds it. While it is held, more clients arrive
    and are all refused. The peak is read from the observer throughout. Without
    the hold, a fast burst can look correct by accident; the hold is what makes
    the measurement mean something.
    """
    program = client_program(tmp_path)
    store = Store(db_path)
    acquire(store, "prime", 1, [ResourceSpec(WRITER_POOL, "capacity", capacity=2)])
    release(store, "prime", 1, [WRITER_POOL])

    holder_proc = subprocess.Popen(
        [sys.executable, str(program), str(db_path), WRITER_POOL, "capacity", "2",
         "task-holder", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Wait for the holder rather than sleeping and hoping. The client acquires,
    # prints and exits, so there is a window in which it holds nothing and the
    # row would read None; `time.sleep(0.4)` bet that a Python interpreter
    # starts in under 0.4s, which holds on Windows and did not on WSL, where the
    # interpreter and the repository are both on /mnt/d. Measured there: still
    # None at 0.2s, 0.4s and 0.8s, present at 1.5s, by which point the client had
    # already exited. Raising the constant only moves the bet, so the condition
    # is polled instead, and the failure names what was never observed.
    deadline = time.monotonic() + 60.0
    held = None
    while time.monotonic() < deadline:
        held = holder(store, WRITER_POOL)
        if held is not None and held.held == 1:
            break
        if holder_proc.poll() is not None:
            # The client is gone and never held the slot. Its output says why.
            out, err = holder_proc.communicate(timeout=30)
            raise AssertionError(
                f"the holder client exited (status {holder_proc.returncode}) without "
                f"ever holding {WRITER_POOL}; it said: {out!r} {err.strip()[-300:]!r}"
            )
        time.sleep(0.02)
    assert held is not None and held.held == 1, (
        f"the holder client never acquired {WRITER_POOL} within 60s; last read {held}"
    )

    # The bound is two and the holder has one, so exactly one slot is free. A
    # burst of twenty therefore produces one winner and nineteen refusals --
    # never two winners, which is the property being measured. A bound of one
    # would make every contender lose and would not show that a free slot is
    # actually handed out.
    contended = run_clients(program, db_path, WRITER_POOL, "capacity", 2, 20)
    winners = [r for r in contended if r["status"] == "granted"]
    assert len(winners) == 1, contended
    assert len([r for r in contended if r["status"] == "conflict"]) == 19
    assert holder(store, WRITER_POOL).held == 2

    # Both slots are now held by processes that have exited. Time alone does not
    # justify reassigning a resource whose owner may still be writing, so the
    # release is explicit and by owner. Releasing the last holder removes the
    # row, which is what a current-state table does; a capacity row with no
    # holders is not a bound of zero, it is no bound.
    out, err = holder_proc.communicate(timeout=60)
    assert holder_proc.returncode == 0, err
    release(store, "task-holder", 1, [WRITER_POOL])
    release(store, f"task-{winners[0]['index']:03d}", 1, [WRITER_POOL])
    assert holder(store, WRITER_POOL) is None

    after = run_clients(program, db_path, WRITER_POOL, "capacity", 2, 1)
    assert after[0]["status"] == "granted", after
    assert holder(store, WRITER_POOL).held == 1