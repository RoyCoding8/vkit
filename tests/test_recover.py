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

from conftest import procfs_available  # noqa: E402

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

#: Marks a test whose subject is a value only Windows produces. POSIX identity
#: IS established, so a test that merely calls `procidentity.read_identity` and
#: compares a pair runs on both platforms and is NOT marked. These six assert one
#: of two things a POSIX host cannot produce:
#:
#:   * `STILL_ACTIVE` is 259, the constant `GetExitCodeProcess` returns for a
#:     process that has not exited. On POSIX `kill(pid, 0)` carries no exit code,
#:     so a live process reports `exit_code is None` and there is no 259 to
#:     compare. The equivalent POSIX fact, the state character in
#:     /proc/<pid>/stat, is covered by tests/test_procidentity_posix.py.
#:   * "no process carries that pid" is the phrase `_liveness_windows` composes.
#:     `_liveness_posix` reports the kernel's words instead, so an assertion on
#:     the Windows string is an assertion on Windows. The property behind it,
#:     which is what matters, is asserted by the `dead_pid` fixture above.
#:
#: Per test rather than per module, because most of this file is
#: platform-neutral and skipping all of it on POSIX would leave the module
#: unverified there rather than only its Windows-specific corners.
requires_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="asserts a value only Windows produces: STILL_ACTIVE (259) from "
           "GetExitCodeProcess, or the Windows wording for a reclaimed pid",
)

#: The counterpart marker, for the same reason and at the same granularity.
#:
#: Six tests in this file call `procidentity.read_identity` on the `alive_pid`
#: fixture and then assert the result is not None. The reason is that the
#: stranger verdicts they check are only meaningful when the fixture process is
#: actually readable -- one of them says so outright, "the fixture process must
#: be readable for this to mean anything". The read goes through whichever
#: backend `read_identity` dispatches to, so the predicate has to ask about that
#: backend rather than about procfs alone.
#:
#: **The platform check is load-bearing and is the easy one to get wrong.**
#: `read_identity` reads a FILETIME through `OpenProcess` on Windows, so those
#: four of the six run on Windows today. Gating them on procfs alone skips them
#: on Windows too, which silently deletes passing coverage of the exact policy
#: they exist to pin -- a test that stops running is invisible in a green report,
#: and this suite is about a false pass. So: Windows needs no procfs and is
#: always allowed; a POSIX host needs procfs to read field 22 of a stat line.
#:
#: This is a fixture that cannot run, not a test that cannot. The subjects are
#: recovery POLICY -- whether a mismatched creation time reads UNCERTAIN rather
#: than DEAD, and whether the claim survives -- and policy does not vary by
#: platform. So these six are marked rather than the module: the rest of the file
#: never reads an identity and runs everywhere, and marking the whole module
#: would leave recovery policy unverified on every POSIX host that is not Linux.
requires_readable_identity = pytest.mark.skipif(
    sys.platform != "win32" and not procfs_available(),
    reason="reads a live process's start time to decide whether a stranger's pid "
           "is UNCERTAIN rather than DEAD. On POSIX that read is "
           "/proc/<pid>/stat, and this host has no /proc, so the fixture process "
           "cannot be described and the verdict under test cannot be established. "
           "Windows reads the FILETIME through OpenProcess and needs no procfs.",
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "verification.sqlite3"


@pytest.fixture()
def store(db_path: Path) -> Store:
    return Store(db_path)


def a_task(store: Store, task_id: str = "t1", policy_digest: str = "pd") -> None:
    """Write a task row these tests can own claims under.

    `open_task` validates the contract before storing it, so a contract
    without a mandatory floor is refused rather than written. Nothing here
    reaches acceptance -- the subject is which claim recovery reports on --
    so the floor is named and nothing else is. What the check is must be
    a real one, though: a floor of `[]` is the shape `from_json` exists to
    reject, and a floor of invented ids would test a contract no
    admission could have produced.
    """
    where = store_db(store).parent
    open_task(
        store, task_id=task_id,
        contract={
            "repository": {"root": str(where), "git_common_dir": str(where)},
            "policy_digest": policy_digest,
            "required_checks": ["build"],
            "scope": "build",
            "resources": [],
            "declared": {},
        },
        policy_digest=policy_digest,
    )


@pytest.fixture()
def alive_pid():
    """A real running process, killed on teardown however the test ends.

    The sleep is long enough that a slow, loaded machine cannot run the clock out
    during a test, and the teardown kills it whatever happens, so a failure never
    leaves a 60-second orphan behind. The mirror of `dead_pid`'s problem -- a
    live process that quietly exits before the assertion -- is avoided the same
    way, by making the window generous rather than by hoping.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture()
def dead_pid():
    """A pid that has stopped answering entirely, re-proved at the moment of use.

    `liveness` reaches DEAD two ways and both are correct. A process that exited
    while something still holds it open reports its real exit code; one whose
    object has been reclaimed reports that no process carries the pid, and so has
    no exit code to report. The second is the one this fixture needs, because it
    is the one that cannot decay: a number can be recycled, but an unopenable pid
    has nothing to be recycled into.

    **The property asserted is `exit_code is None`, not a message.** A pid that
    nothing holds has no exit code, and that is the fact these tests need. An
    earlier version of this fixture matched the English string "no process
    carries that pid", which is the WINDOWS wording: `_liveness_windows` composes
    it, `_liveness_posix` reports the kernel's own words instead. So the fixture
    was asserting a message as though it were the contract, and on a POSIX host
    it never matched, timing out after 30s and erroring every test that used it
    (measured: 11 errors, "no pid was fully reclaimed across 6 attempts in 30s").

    The state alone is not enough either. Waiting for `state is DEAD` returned
    while the process object was still openable, and the tests that assert on
    `exit_code is None` then failed roughly one run in four: the state was DEAD
    on both sides, and the test was checking the stronger of two truths with a
    fixture that guaranteed only the weaker.

    A pid that flips back to openable is retired and another minted, because a
    number that names a live process is not what this promised to hand out.
    """
    deadline = time.time() + 30.0
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        pid = proc.pid
        del proc
        settled = time.time() + 5.0
        while time.time() < settled:
            verdict = liveness(pid)
            # DEAD plus no exit code is the property: the kernel is saying no
            # process carries this number, so there is nothing left to have
            # exited with a code. Checking the state alone accepts a process that
            # has exited but is still openable, which is the decayed case.
            if verdict.state is LivenessState.DEAD and verdict.exit_code is None:
                return pid
            time.sleep(0.05)
    raise AssertionError(
        f"no pid was fully reclaimed across {attempts} attempts in 30s; this host is "
        "holding process objects open longer than the fixture allows. Looked for a "
        "DEAD whose exit_code is None, which is a pid nothing holds."
    )


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
              attempt: int | None = None, reconcile: bool = False,
              creation_time: int | None = None) -> None:
    """Register a run and, when a pid is named, attach it exactly as execution does.

    `creation_time` is part of the identity `execution.run_check` records, so it
    is a parameter rather than an omission. A fixture that never wrote it would
    leave every test reading the weaker bare-pid liveness path and the identity
    check untested, which is exactly how a recycled pid shipped.
    """
    store.register_run(
        run_id, f"check-{run_id}", task_id=task_id, attempt=attempt,
        source={"head": "abc"}, configuration_digest="cfg", fixture_digest=None,
    )
    if pid is not None:
        process = {
            "pid": pid, "ownership": "windows_job_object",
            "exit_code": None, "timed_out": False,
        }
        if creation_time is not None:
            process["creation_time"] = creation_time
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


@requires_windows
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


@requires_windows
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
    a_task(store)
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    start_run(store, "r-live", pid=alive_pid, task_id="t1", attempt=1)

    report = inspect(store)

    assert report.of(FindingKind.CLAIM_STALE_GENERATION) == ()
    assert holder(store, "build").generation == 1


# ------------------------------------------------------------------- inspection


def test_inspect_changes_nothing(store: Store, db_path: Path, alive_pid: int, dead_pid: int) -> None:
    """A read that writes is not a read. Compared on the file, not the object."""
    a_task(store)
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
            "ownership_known": True, "launch_state": "identity_published",
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
    a_task(store)
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
    a_task(store)
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
    a_task(store)
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


@requires_windows
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


@requires_windows
def test_releasing_a_claim_held_by_a_live_process_is_refused(store: Store, alive_pid: int) -> None:
    a_task(store)
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
    a_task(store)
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

    # The refusal now names the action that does apply. MARK_RUN_DEAD decides a
    # question about a pid, and a run that never published one has no such pid;
    # ABANDON_LAUNCH is the action for a run whose ownership was never
    # established, because it carries the job-name evidence a dead-pid check does
    # not have. Sending the operator to a dead end is how a state like this stays
    # unreconciled forever.
    assert str(raised.value) == (
        "refusing to mark run 'r-preparing' dead: it is preparing and its ownership was never "
        "established, so there is no pid whose death could be confirmed. ABANDON_LAUNCH is the "
        "action for this state, because it carries the job-name evidence that a dead-pid check "
        "does not have"
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
    a_task(store)
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
    a_task(store)
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    start_run(store, "r-old", pid=dead_pid, task_id="t1", attempt=1)
    supersede_task(store, "t1")

    apply_action(store, Action.RELEASE_CLAIM, target="build",
                 evidence=f"run r-old's pid {dead_pid} has exited")

    assert holder(store, "build") is None


@requires_readable_identity
def test_a_recycled_pid_does_not_release_its_claim(store: Store, alive_pid: int) -> None:
    """The end-to-end consequence, and the reason the identity check exists.

    A stale-generation claim looks releasable whenever no run of the old
    generation is alive. That test consults a pid. If the OS has handed that
    number to an unrelated process, the check reports "live" and the claim
    stays held -- safe, but only by accident, because a stranger happened to be
    running.

    The dangerous direction is the reverse. A run whose process really did exit
    leaves a pid behind, and if the OS has *not* yet reused it, every reader says
    DEAD and the claim is released. That is correct today. The day the number is
    reused by something whose creation time the record does not match, the same
    code must not read DEAD, or a second task is handed a resource whose real
    owner may still be working on it.

    Here the recorded identity belongs to a process that has exited, and the pid
    is now held by a live stranger. The claim must survive.
    """
    from vkit.procidentity import read_identity

    stranger = read_identity(alive_pid)
    assert stranger is not None

    a_task(store)
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    # The recorded identity is one this pid has never had, standing in for a
    # process that exited and left its number behind.
    start_run(store, "r-old", pid=alive_pid, task_id="t1", attempt=1,
              creation_time=stranger.creation_time + 1)
    supersede_task(store, "t1")

    findings = inspect(store)
    holder_states = {
        f.kind for f in findings.findings
        if f.kind in (FindingKind.RUN_LIVE_PROCESS, FindingKind.RUN_UNCERTAIN_PROCESS,
                      FindingKind.RUN_DEAD_PROCESS)
    }

    assert holder_states == {FindingKind.RUN_UNCERTAIN_PROCESS}, (
        "a pid whose creation time does not match the record is a stranger's "
        "process, and reporting it live or dead both misdescribe our run"
    )
    assert holder(store, "build") is not None, (
        "the claim was released on the strength of a process that never ran the check"
    )


@requires_readable_identity
def test_marking_a_run_dead_refuses_when_its_pid_was_recycled(
    store: Store, alive_pid: int
) -> None:
    """`mark dead` is the action that actually releases, so it refuses hardest.

    A run record naming a pid that a stranger now holds is not a crashed run. It
    is a run whose process ended and whose number was reused, and confirming
    that death from the pid alone would be a stranger's exit code filed as ours.
    """
    from vkit.procidentity import read_identity

    stranger = read_identity(alive_pid)
    assert stranger is not None

    start_run(store, "r-gone", pid=alive_pid,
              creation_time=stranger.creation_time + 1)

    with pytest.raises(RecoveryRefused) as caught:
        apply_action(store, Action.MARK_RUN_DEAD, target="r-gone",
                     evidence="the operator believes this crashed")

    assert "recycled" in str(caught.value) or "creation time" in str(caught.value)


def test_a_paused_task_keeps_its_claims_and_reports_no_finding(store: Store) -> None:
    """Paused records that the owner is idle, not gone. Nothing here infers otherwise."""
    a_task(store)
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    set_status(store, "t1", "paused")

    assert inspect(store).findings == ()
    assert holder(store, "build") is not None


def test_a_claim_whose_task_vanished_is_reported(store: Store) -> None:
    a_task(store)
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


@requires_windows
@pytest.mark.parametrize("winerror", [6, 1168, 5, 1, 9999])
def test_a_failure_that_does_not_establish_death_is_uncertain(
    monkeypatch: pytest.MonkeyPatch, alive_pid: int, winerror: int
) -> None:
    """No code but 87 releases a claim, whatever its name suggests.

    ERROR_INVALID_HANDLE and ERROR_NOT_FOUND used to sit on the DEAD side of this
    branch, alongside 87. A DEAD verdict here is not a report, it is a permission:
    _live_holder treats DEAD as the only state that stops blocking, and
    _release_claim then deletes the claim row. So each of these codes, replayed
    against a pid that is demonstrably alive, must read UNCERTAIN and leave the
    claim held.

    9999 is in the list because the guarantee is about the shape of the answer and
    not about an enumeration. A code nobody has seen has to be able to fall on the
    safe side, and a test that only named the four known codes would keep passing
    against a table that classified 9999 as death.

    The pid is a real running process this file launched, so a DEAD verdict here
    would be a false statement about the world and not a defensible reading of a
    hard case.
    """
    import pywintypes
    import win32api

    assert liveness(alive_pid).state is LivenessState.ALIVE, (
        "the pid under test must be alive, or this asserts nothing"
    )

    def refused(access: int, inherit: bool, target: int):
        raise pywintypes.error(winerror, "OpenProcess", "replayed")

    monkeypatch.setattr(win32api, "OpenProcess", refused)

    state = liveness(alive_pid)

    assert state.state is LivenessState.UNCERTAIN, (
        f"Windows error {winerror} does not establish that no process carries the "
        "pid, and reading it as death releases a live run's claim to a stranger"
    )
    assert state.dead is False
    assert str(winerror) in state.detail


@requires_windows
def test_the_claim_survives_a_live_pid_reported_unopenable(
    monkeypatch: pytest.MonkeyPatch, store: Store, alive_pid: int
) -> None:
    """The consequence, not the classification: the resource is still held.

    The test above pins the verdict. This one pins what the verdict is for. A
    stale-generation claim looks releasable whenever no run of the old generation
    is alive, so a DEAD verdict on a live pid hands the resource to a second task
    while the first is still working on it.

    ERROR_NOT_FOUND is the code used because it is the one most likely to be read
    as "the object does not exist, therefore the process is gone", and because
    that reading was in this file until the two classifiers were made one.
    """
    import pywintypes
    import win32api

    from vkit.procidentity import read_identity

    real = read_identity(alive_pid)
    assert real is not None, "the fixture process must be readable for this to mean anything"

    a_task(store)
    acquire(store, "t1", 1, [ResourceSpec("build", EXCLUSIVE)])
    start_run(store, "r-old", pid=alive_pid, task_id="t1", attempt=1,
              creation_time=real.creation_time)
    supersede_task(store, "t1")

    def refused(access: int, inherit: bool, target: int):
        raise pywintypes.error(1168, "OpenProcess", "The object was not found.")

    monkeypatch.setattr(win32api, "OpenProcess", refused)

    findings = inspect(store)
    holder_states = {
        f.kind for f in findings.findings
        if f.kind in (FindingKind.RUN_LIVE_PROCESS, FindingKind.RUN_UNCERTAIN_PROCESS,
                      FindingKind.RUN_DEAD_PROCESS)
    }
    assert holder_states == {FindingKind.RUN_UNCERTAIN_PROCESS}

    with pytest.raises(RecoveryRefused) as caught:
        apply_action(store, Action.RELEASE_CLAIM, target="build",
                     evidence="the operator was told the pid could not be opened")

    assert "build" in str(caught.value)
    # Read back from the database, not from the object under test.
    assert holder(store, "build") is not None
    assert holder(store, "build").task_id == "t1"


@requires_windows
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


@requires_readable_identity
def test_a_pid_now_held_by_a_stranger_is_uncertain_and_never_dead(alive_pid: int) -> None:
    """A recycled pid must not be read as our dead process.

    Windows recycles process identifiers. A run that exited leaves its pid
    behind, and the OS is free to hand that number to an unrelated process.
    Reading such a pid as DEAD releases the run's claims on the strength of a
    stranger's process, which is the exact failure `vkit.procidentity` exists to
    prevent -- and this module was doing it, because it asked about a bare pid
    while the record carried the creation time that settles the question.

    UNCERTAIN is the only safe reading. It retains the claim and reports the run
    as needing reconciliation.
    """
    from vkit.procidentity import read_identity

    real = read_identity(alive_pid)
    assert real is not None, "the fixture process must be readable for this to mean anything"

    # The same pid, with a creation time that is not the live process's. This is
    # exactly the record a run written before its process exited now carries.
    # The boot is passed too, because a POSIX start time is ticks since boot and
    # is only comparable within one boot. Passing a real identity's tick count
    # WITHOUT its boot is a record this module cannot fully corroborate, and the
    # stranger verdict below would then be right for the wrong reason.
    stranger_creation_time = real.creation_time + 1

    state = liveness(
        alive_pid,
        creation_time=stranger_creation_time,
        boot_id=real.boot_id,
    )

    assert state.state is LivenessState.UNCERTAIN, (
        "a pid whose creation time does not match is a stranger's process, and "
        "reporting it DEAD releases a claim on the wrong evidence"
    )
    # The detail has to name WHICH half failed, or a reader cannot tell a
    # stranger from an unreadable identity.
    assert "creation time" in state.detail
    assert "boot" not in state.detail, (
        "the boot matched, so the detail must not blame the boot; if it does, the "
        "verdict came from the wrong check"
    )
    assert not state.dead


@requires_readable_identity
def test_a_matching_creation_time_still_reads_alive(alive_pid: int) -> None:
    """The pair must not make a genuinely running process look uncertain.

    The exit code is asserted only where one exists. STILL_ACTIVE (259) comes
    from `GetExitCodeProcess`; `kill(pid, 0)` carries no exit code, so a live
    POSIX process reports None. Asserting 259 everywhere would be asserting a
    Windows value on both platforms, which is what the `requires_windows` tests
    beside it do.
    """
    from vkit.procidentity import read_identity

    real = read_identity(alive_pid)
    assert real is not None

    state = liveness(
        alive_pid, creation_time=real.creation_time, boot_id=real.boot_id
    )

    assert state.state is LivenessState.ALIVE
    if sys.platform == "win32":
        assert state.exit_code == 259
    else:
        assert state.exit_code is None


@requires_readable_identity
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="boot_id is POSIX-only; a Windows FILETIME is absolute and needs no boot",
)
def test_a_record_from_another_boot_is_a_stranger(alive_pid: int) -> None:
    """The tick count is only meaningful within one boot.

    WSL restarts the init namespace, so a record written before a restart can
    carry the same tick count as a process running after it. The pid and the
    ticks both match and it is still a different process, which is the case a
    tick-count-only comparison would call a match.

    Skipped on Windows, and the reason is the design rather than an
    inconvenience: `boot_id` is empty there because a FILETIME is an absolute
    instant, so there is no boot for a record to disagree with. The Windows
    equivalent of this property is the creation-time comparison, which
    `test_a_pid_now_held_by_a_stranger_is_uncertain_and_never_dead` covers on
    both platforms.
    """
    from vkit.procidentity import boot_id, read_identity

    real = read_identity(alive_pid)
    assert real is not None

    state = liveness(
        alive_pid,
        creation_time=real.creation_time,
        boot_id="a-different-boot",
    )

    assert state.state is LivenessState.UNCERTAIN, (
        "a matching pid and tick count under a different boot is a stranger, and "
        "reading it as our own process is how a claim is released on a stranger"
    )
    assert boot_id() in state.detail, "the detail must name the boot that did not match"
    assert not state.dead


@requires_readable_identity
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a Windows FILETIME is absolute and needs no boot to be meaningful, so "
           "there is no boot half to withhold",
)
def test_a_record_carrying_no_boot_is_treated_as_unproven(alive_pid: int) -> None:
    """A record written before the boot half existed is weaker evidence.

    Whether a tick count with no boot should corroborate a live process is a
    product decision about what a pre-boot_id record is worth. The direction
    chosen here is the one that keeps the claim: an uncorroborated record reads
    UNCERTAIN, so nothing is released on it. Stated here because it is a choice,
    not a consequence.
    """
    from vkit.procidentity import read_identity

    real = read_identity(alive_pid)
    assert real is not None

    state = liveness(alive_pid, creation_time=real.creation_time)

    assert state.state is LivenessState.UNCERTAIN
    assert not state.dead


@requires_windows
def test_an_absent_pid_is_still_dead_with_a_recorded_identity(dead_pid: int) -> None:
    """A pid no process carries is DEAD even when a creation time was recorded.

    The creation time does not conjure a process. When the pid is simply gone
    there is no stranger to confuse it with, and calling that UNCERTAIN would
    strand every crashed run's claim forever.
    """
    state = liveness(dead_pid, creation_time=133000000000000000)

    assert state.state is LivenessState.DEAD
    assert "no process carries that pid" in state.detail


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