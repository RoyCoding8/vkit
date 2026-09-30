"""Crash recovery, exercised the way a user drives it: inspect, then act.

Every assertion reads the outcome through the public surface and again through a
fresh connection to the database file, so a finding that existed only in the
object the code returned would fail the second read. The second read is the one
that crosses a process boundary.

Liveness is exercised against real operating system processes. A sleeping
interpreter is alive and a completed one is dead, and nothing in this file mocks
that distinction, because the whole module is one error-direction decision and a
mock would only assert the decision was written down twice.

The refusal cases matter more than the successes. Each one asserts both halves:
the action is refused, and the record it would have changed is untouched.
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

from vkit.claims import ResourceSpec, acquire, holder  # noqa: E402
from vkit.recover import (  # noqa: E402
    Action,
    FindingKind,
    LivenessState,
    RecoveryRefused,
    apply_action,
    inspect,
    liveness,
)
from vkit.storage import Store  # noqa: E402
from vkit.tasks import open_task, set_status, supersede_task  # noqa: E402

EXCLUSIVE = "exclusive"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "verification.sqlite3"


@pytest.fixture()
def store(db_path: Path) -> Store:
    return Store(db_path)


@pytest.fixture()
def alive_pid():
    """A real running process, killed on teardown however the test ends."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture()
def dead_pid():
    """A real process that has exited and been reaped.

    Reaping is not by itself enough, and the distinction matters. A pid the
    parent never opened stays openable after the parent drops it, so `kill(pid, 0)`
    and `GetExitCodeProcess` both keep reporting on a process that no longer runs,
    and it can hold file locks. This waits for the handle to become unopenable,
    which is the same proof `liveness` demands before it will call a pid dead.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pid = proc.pid
    del proc
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if liveness(pid).state is LivenessState.DEAD:
            return pid
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} never stopped answering a liveness check")


# ------------------------------------------------------------- independent reads


def db_snapshot(db_path: Path) -> dict:
    """Every table in the database, read over a new connection.

    The observer shares no connection, no transaction and no object with the code
    under test, so it sees only what actually reached the file.
    """
    conn = sqlite3.connect(db_path)
    try:
        snapshot = {}
        for table in ("runs", "tasks", "claim_holders", "request_keys", "schema_version"):
            cursor = conn.execute(f"SELECT * FROM {table}")
            columns = [column[0] for column in cursor.description]
            snapshot[table] = sorted(
                json.dumps(dict(zip(columns, row)), sort_keys=True)
                for row in cursor.fetchall()
            )
        return snapshot
    finally:
        conn.close()


def run_row(db_path: Path, run_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT lifecycle, result, process_json FROM runs WHERE run_id = ?", (run_id,)
        )
        row = cursor.fetchone()
        assert row is not None, f"no run row for {run_id}"
        return {"lifecycle": row[0], "result": row[1], "process_json": row[2]}
    finally:
        conn.close()


def process_record(db_path: Path, run_id: str) -> dict:
    return json.loads(run_row(db_path, run_id)["process_json"])


def start_run(store: Store, run_id: str, *, pid: int | None, task_id: str | None = None,
              attempt: int | None = None, reconcile: bool = False) -> None:
    """Register a run and, when a pid is named, attach it exactly as execution does."""
    store.register_run(
        run_id, f"check-{run_id}", task_id=task_id, attempt=attempt,
        source={"head": "abc"}, configuration_digest="cfg", fixture_digest=None,
    )
    if pid is not None:
        process = {
            "pid": pid, "ownership": "windows_job_object",
            "exit_code": None, "timed_out": False,
        }
        if reconcile:
            process["reconciliation"] = {
                "state": "process_dead_confirmed",
                "evidence": "an operator confirmed the pid had exited",
                "decided_at": "2026-09-28T00:00:00+00:00",
                "exit_code": None,
            }
        store.mark_running(run_id, process)


def go_terminal_without_a_report(store: Store, run_id: str, result: str = "PASS") -> None:
    """Reproduce the publish crash window, where the row flipped but the file did not land.

    `publish` orders its own writes so this state is not reachable through it. That
    is the point of that ordering and why the only way here is direct SQL: the
    window is what the code is designed to make unrecoverable by accident, and a
    fixture that reached it through the public API would be asserting a bug.
    """
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE runs SET lifecycle = 'terminal', result = ? WHERE run_id = ?",
            (result, run_id),
        )
        conn.execute("COMMIT")


def finish_run(store: Store, run_id: str, result: str = "PASS") -> None:
    """Publish a terminal run the way execution does, report file included."""
    report = {
        "run_id": run_id, "lifecycle": "terminal", "ended_at": "2026-09-29T00:00:00+00:00",
        "outcome": {"result": result, "scenarios": []},
    }
    store.publish(run_id, report)


# ----------------------------------------------------------------- empty store


def test_inspecting_an_empty_store_finds_nothing(store: Store, db_path: Path) -> None:
    report = inspect(store)

    assert report.findings == ()
    assert report.to_json() == {"findings": []}
    assert db_snapshot(db_path)["claim_holders"] == []


def test_inspect_reports_nothing_for_a_completed_run_with_its_report(store: Store, db_path: Path) -> None:
    start_run(store, "r-clean", pid=None)
    finish_run(store, "r-clean")

    assert inspect(store).findings == ()


# -------------------------------------------------------------- crashed in flight


def test_a_running_run_whose_process_is_dead_is_reported(store: Store, dead_pid: int) -> None:
    start_run(store, "r-dead", pid=dead_pid, task_id="t1", attempt=1)

    report = inspect(store)

    found = report.of(FindingKind.RUN_DEAD_PROCESS)
    assert len(found) == 1
    finding = found[0]
    assert finding.target == "r-dead"
    assert finding.liveness.state is LivenessState.DEAD
    assert f"pid {dead_pid}" in finding.detail
    assert "recorded running" in finding.detail
    assert "stopped mid-flight" in finding.detail
    assert finding.action is Action.MARK_RUN_DEAD
    assert finding.actionable is True


def test_a_live_pid_records_the_still_active_code_as_its_evidence(alive_pid: int) -> None:
    """The check that must not be skipped: anything but STILL_ACTIVE reads a live
    worker as dead, because a process that has not exited has no exit code."""
    state = liveness(alive_pid)

    assert state.state is LivenessState.ALIVE
    assert state.exit_code == 259
    assert "reported exit code 259 (STILL_ACTIVE)" in state.detail


def test_a_preparing_run_with_no_process_is_reported_and_never_acted_on(store: Store) -> None:
    start_run(store, "r-preparing", pid=None, task_id="t1", attempt=1)

    finding = inspect(store).of(FindingKind.RUN_WITHOUT_PROCESS)[0]

    assert finding.target == "r-preparing"
    assert finding.liveness is None
    assert "records no process identity" in finding.detail
    # A run with no pid has nothing whose death could be confirmed, so no action
    # against it is ever offered.
    assert finding.action is None
    assert finding.actionable is False


# ------------------------------------------------------------- the live process


def test_a_running_run_whose_process_is_alive_is_not_abandoned(store: Store, alive_pid: int) -> None:
    start_run(store, "r-live", pid=alive_pid, task_id="t1", attempt=1)

    report = inspect(store)

    assert report.of(FindingKind.RUN_DEAD_PROCESS) == ()
    assert report.of(FindingKind.RUN_UNCERTAIN_PROCESS) == ()
    found = report.of(FindingKind.RUN_LIVE_PROCESS)
    assert len(found) == 1
    finding = found[0]
    assert finding.target == "r-live"
    assert finding.liveness.state is LivenessState.ALIVE
    assert f"STILL_ACTIVE" in finding.detail
    assert "This run is still working" in finding.detail
    assert finding.action is None
    assert finding.actionable is False


def test_a_live_run_holds_its_resources_and_reports_no_stale_claim(store: Store, alive_pid: int) -> None:
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    start_run(store, "r-live", pid=alive_pid, task_id="t1", attempt=1)

    report = inspect(store)

    assert report.of(FindingKind.CLAIM_STALE_GENERATION) == ()
    assert holder(store, "build").generation == 1


# ------------------------------------------------------------------- inspection


def test_inspect_changes_nothing(store: Store, db_path: Path, alive_pid: int, dead_pid: int) -> None:
    """A read that writes is not a read. Compared on the file, not the object."""
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    start_run(store, "r-live", pid=alive_pid, task_id="t1", attempt=1)
    start_run(store, "r-dead", pid=dead_pid, task_id="t1", attempt=1)
    start_run(store, "r-preparing", pid=None)
    start_run(store, "r-terminal", pid=None)
    go_terminal_without_a_report(store, "r-terminal", "FAIL")
    set_status(store, "t1", "paused")

    before = db_snapshot(db_path)
    assert before["runs"] and before["claim_holders"]
    report = inspect(store)
    after = db_snapshot(db_path)

    assert [finding.kind for finding in report.findings] == [
        FindingKind.RUN_LIVE_PROCESS,
        FindingKind.RUN_DEAD_PROCESS,
        FindingKind.RUN_WITHOUT_PROCESS,
        FindingKind.RUN_TERMINAL_NO_REPORT,
    ]
    assert after == before
    # The run that was mid-flight is exactly as it was, on the independent read.
    assert run_row(db_path, "r-dead") == {
        "lifecycle": "running", "result": None,
        "process_json": json.dumps({
            "pid": dead_pid, "ownership": "windows_job_object",
            "exit_code": None, "timed_out": False,
        }, sort_keys=True, separators=(",", ":")),
    }


def test_a_terminal_run_missing_its_report_is_the_publish_crash_window(store: Store) -> None:
    """A crash between claiming the row and swapping the report into place."""
    start_run(store, "r-gapped", pid=None, task_id="t1", attempt=1)
    go_terminal_without_a_report(store, "r-gapped")
    assert not (store.run_dir("r-gapped") / "report.json").exists()

    report = inspect(store)

    found = report.of(FindingKind.RUN_TERMINAL_NO_REPORT)
    assert len(found) == 1
    assert found[0].target == "r-gapped"
    assert "no report exists at" in found[0].detail
    assert "recovery does not invent one" in found[0].detail
    # No recovery action is offered: there is no process to confirm and no
    # evidence to publish. The finding is for a human.
    assert found[0].actionable is False


# --------------------------------------------------------------- stale claims


def test_a_claim_at_a_stale_generation_is_reported_and_named_explicitly(
    store: Store, db_path: Path
) -> None:
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    supersede_task(store, "t1")

    report = inspect(store)

    found = report.of(FindingKind.CLAIM_STALE_GENERATION)
    assert len(found) == 1
    finding = found[0]
    assert finding.target == "build"
    assert finding.action is Action.RELEASE_CLAIM
    assert finding.actionable is True
    assert "held by task 't1' at generation 1" in finding.detail
    assert "advanced to generation 2" in finding.detail
    # Inspection changed nothing.
    assert holder(store, "build").generation == 1


def test_a_current_generation_claim_is_not_reported(store: Store) -> None:
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])

    assert inspect(store).of(FindingKind.CLAIM_STALE_GENERATION) == ()


# ---------------------------------------------------------------- refusals


def test_an_action_with_empty_evidence_is_refused(store: Store, dead_pid: int) -> None:
    start_run(store, "r-dead", pid=dead_pid)
    before = run_row(store_db(store), "r-dead")

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.MARK_RUN_DEAD, target="r-dead", evidence="")

    assert str(raised.value) == (
        "refusing to mark_run_dead 'r-dead': the evidence permitting the change was empty"
    )
    assert run_row(store_db(store), "r-dead") == before


def test_whitespace_is_not_evidence(store: Store, dead_pid: int) -> None:
    start_run(store, "r-dead", pid=dead_pid)

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.MARK_RUN_DEAD, target="r-dead", evidence="   \n")

    assert "the evidence permitting the change was empty" in str(raised.value)
    assert "reconciliation" not in (process_record(store_db(store), "r-dead") or {})


def test_an_action_naming_an_unknown_run_is_refused(store: Store) -> None:
    start_run(store, "r-real", pid=None)
    before = db_snapshot(store_db(store))

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.MARK_RUN_DEAD, target="r-missing", evidence="operator observed it")

    assert str(raised.value) == (
        "refusing to mark run 'r-missing' dead: no run is recorded under that id"
    )
    assert db_snapshot(store_db(store)) == before


def test_an_action_naming_an_unknown_resource_is_refused(store: Store) -> None:
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.RELEASE_CLAIM, target="publish", evidence="operator observed it")

    assert str(raised.value) == (
        "refusing to release resource 'publish': no claim is recorded under that key, "
        "so there is nothing to release"
    )
    assert holder(store, "build").generation == 1


def test_an_action_with_an_empty_target_is_refused(store: Store) -> None:
    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.RELEASE_CLAIM, target="  ", evidence="operator observed it")

    assert str(raised.value) == "a recovery action must name the run or resource it affects"


def test_a_string_instead_of_a_named_action_is_refused(store: Store) -> None:
    """Recovery never resolves a loose name to a verb."""
    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, "release_claim", target="build", evidence="operator observed it")  # type: ignore[arg-type]

    assert str(raised.value) == (
        "unknown recovery action 'release_claim'; recovery never guesses"
    )


def test_marking_a_live_run_dead_is_refused_however_old_it_looks(store: Store, alive_pid: int) -> None:
    """The safety property. Elapsed time is not evidence, so nothing here ages it out."""
    start_run(store, "r-live", pid=alive_pid, task_id="t1", attempt=1)
    before = run_row(store_db(store), "r-live")

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.MARK_RUN_DEAD, target="r-live",
                     evidence="it has been running since last week")

    assert str(raised.value) == (
        f"refusing to mark run 'r-live' dead: pid {alive_pid} opened and reported exit code "
        "259 (STILL_ACTIVE), so it is still running. A running process is never abandoned "
        "on the strength of an old timestamp"
    )
    assert run_row(store_db(store), "r-live") == before


def test_releasing_a_claim_held_by_a_live_process_is_refused(store: Store, alive_pid: int) -> None:
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    start_run(store, "r-live", pid=alive_pid, task_id="t1", attempt=1)
    supersede_task(store, "t1")
    before = db_snapshot(store_db(store))

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.RELEASE_CLAIM, target="build",
                     evidence="generation 1 looks abandoned")

    assert str(raised.value) == (
        f"refusing to release resource 'build': task 't1' has advanced to generation 2 while "
        f"the resource is held at generation 1, but run 'r-live' is running and its process is "
        f"alive (pid {alive_pid} opened and reported exit code 259 (STILL_ACTIVE), so it is "
        "still running). A resource stays held while the attempt that owns it might still "
        "be writing."
    )
    assert db_snapshot(store_db(store)) == before
    assert holder(store, "build").generation == 1


def test_releasing_a_current_generation_claim_is_refused(store: Store) -> None:
    """A claim nobody superseded is not stale, and is not recovery's to take."""
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.RELEASE_CLAIM, target="build", evidence="nothing else wants it")

    assert str(raised.value) == (
        "refusing to release resource 'build': task 't1' is still at generation 1, which is "
        "the generation holding it. The claim is current, not stale, and releasing it would "
        "take a live attempt's resource."
    )
    assert holder(store, "build").generation == 1


def test_marking_a_run_dead_is_refused_when_it_records_no_process(store: Store) -> None:
    start_run(store, "r-preparing", pid=None)

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.MARK_RUN_DEAD, target="r-preparing", evidence="operator observed it")

    assert str(raised.value) == (
        "refusing to mark run 'r-preparing' dead: it records no process identity, so there is "
        "no pid whose death could be confirmed"
    )


def test_marking_a_terminal_run_dead_is_refused(store: Store, dead_pid: int) -> None:
    start_run(store, "r-done", pid=dead_pid)
    finish_run(store, "r-done", "PASS")

    with pytest.raises(RecoveryRefused) as raised:
        apply_action(store, Action.MARK_RUN_DEAD, target="r-done", evidence="operator observed it")

    assert str(raised.value) == (
        "refusing to mark run 'r-done' dead: it is already terminal with result 'PASS', and a "
        "terminal run is not reconciled by liveness"
    )


# ------------------------------------------------------------------- successes


def test_marking_a_genuinely_dead_run_succeeds_and_records_the_evidence(
    store: Store, dead_pid: int
) -> None:
    start_run(store, "r-dead", pid=dead_pid, task_id="t1", attempt=1)

    report = apply_action(
        store, Action.MARK_RUN_DEAD, target="r-dead",
        evidence=f"OpenProcess reported no process at pid {dead_pid} at 2026-09-29T00:00:00Z",
    )

    record = process_record(store_db(store), "r-dead")
    evidence = (
        f"OpenProcess reported no process at pid {dead_pid} at 2026-09-29T00:00:00Z"
    )
    assert record["reconciliation"]["state"] == "process_dead_confirmed"
    assert record["reconciliation"]["evidence"] == evidence
    assert record["reconciliation"]["decided_at"].endswith("+00:00")
    assert record["reconciliation"]["exit_code"] is None
    # The run is still not terminal: death of a process is not an outcome.
    assert run_row(store_db(store), "r-dead")["lifecycle"] == "running"
    with pytest.raises(Exception) as raised:
        store.load("r-dead")
    assert "no published report for run r-dead" in str(raised.value)

    # The returned report is the state recovery produced, not the state it assumed.
    still_dead = report.of(FindingKind.RUN_DEAD_PROCESS)
    assert len(still_dead) == 1
    assert still_dead[0].reconciled is True
    assert still_dead[0].actionable is False


def test_marking_a_dead_run_dead_twice_is_refused_as_nothing_to_decide(
    store: Store, dead_pid: int
) -> None:
    start_run(store, "r-dead", pid=dead_pid)
    apply_action(store, Action.MARK_RUN_DEAD, target="r-dead", evidence="first pass")

    report = inspect(store)
    assert report.of(FindingKind.RUN_DEAD_PROCESS)[0].actionable is False

    with pytest.raises(RecoveryRefused):
        # Even though a pid cannot come back to life, the run stays reconcilable
        # and this refuses, because reconciliation is a human decision recorded once.
        apply_action(store, Action.MARK_RUN_DEAD, target="r-dead", evidence="second pass")


def test_releasing_a_stale_claim_with_no_live_holder_succeeds(store: Store) -> None:
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    acquire(store, "t1", 2, [ResourceSpec("publish", EXCLUSIVE)])
    supersede_task(store, "t1")

    report = apply_action(
        store, Action.RELEASE_CLAIM, target="build",
        evidence="task t1 advanced to generation 2 and no run of t1 records a live process",
    )

    assert holder(store, "build") is None
    # Only the named resource moved.
    assert holder(store, "publish").generation == 2
    assert report.of(FindingKind.CLAIM_STALE_GENERATION) == ()

    # And the freed resource is actually grantable again.
    acquire(store, "t2", 1, [ResourceSpec("build", EXCLUSIVE)])
    assert holder(store, "build").task_id == "t2"


def test_releasing_a_claim_whose_holder_run_is_dead_succeeds(
    store: Store, dead_pid: int
) -> None:
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    start_run(store, "r-old", pid=dead_pid, task_id="t1", attempt=1)
    supersede_task(store, "t1")

    apply_action(store, Action.RELEASE_CLAIM, target="build",
                 evidence=f"run r-old's pid {dead_pid} has exited")

    assert holder(store, "build") is None


def test_a_paused_task_keeps_its_claims_and_reports_no_finding(store: Store) -> None:
    """Paused records that the owner is idle, not gone. Nothing here infers otherwise."""
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    set_status(store, "t1", "paused")

    assert inspect(store).findings == ()
    assert holder(store, "build") is not None


def test_a_claim_whose_task_vanished_is_reported(store: Store) -> None:
    open_task(store, task_id="t1", contract={"goal": "build"}, policy_digest="pd")
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM tasks WHERE task_id = 't1'")
        conn.execute("COMMIT")

    found = inspect(store).of(FindingKind.CLAIM_OWNER_MISSING)

    assert len(found) == 1
    assert found[0].target == "build"
    assert "no task 't1' exists to own it" in found[0].detail


# ------------------------------------------------------------------ liveness


def test_liveness_reports_a_pid_that_is_not_a_pid_as_uncertain() -> None:
    for pid in (None, 0, -1):
        state = liveness(pid)
        assert state.state is LivenessState.UNCERTAIN
        assert "does not name a single process" in state.detail


def test_liveness_distinguishes_a_real_process_from_an_absent_one(alive_pid: int, dead_pid: int) -> None:
    alive = liveness(alive_pid)
    dead = liveness(dead_pid)

    assert alive.state is LivenessState.ALIVE
    assert alive.exit_code == 259
    assert "STILL_ACTIVE" in alive.detail
    assert dead.state is LivenessState.DEAD
    # A pid that no longer opens carries no exit code, because there is no
    # process left to have exited with one.
    assert dead.exit_code is None
    assert "no process carries that pid" in dead.detail


def test_a_dead_run_stays_reconciled_on_the_next_inspection(store: Store, dead_pid: int) -> None:
    start_run(store, "r-done", pid=dead_pid, reconcile=True)

    finding = inspect(store).of(FindingKind.RUN_DEAD_PROCESS)[0]

    assert finding.liveness.state is LivenessState.DEAD
    assert finding.reconciled is True
    assert finding.actionable is False
    assert liveness(dead_pid).state is LivenessState.DEAD


def store_db(store: Store) -> Path:
    """The path of the database a store is using.

    The independent readers in this file are handed a path rather than a store so
    that nothing they observe can come through the code under test. `Store` keeps
    its path private, and reaching through `_db_path` here is deliberate: these
    readers exist precisely to bypass the object under test.
    """
    return store._db_path