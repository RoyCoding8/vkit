"""Behavior of the durable store, exercised through its public surface.

These tests deliberately avoid asserting on SQL. They call Store the way the
execution path does and check what a reader would observe on disk, because a
test that restates the schema proves nothing about whether a report survives.
"""
from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import vkit.storage as storage  # noqa: E402
from vkit import recover  # noqa: E402
from vkit.execution import _environment_facts  # noqa: E402
from vkit.storage import REPORT_NAME, Store, StoreError  # noqa: E402


def make_report(run_id: str, result: str = "PASS", **extra) -> dict:
    outcome = {"result": result} if result == "BLOCKED" else {
        "result": result,
        "scenarios": [{"id": "s1", "result": "PASS", "observation": "printed 0"}],
    }
    if result == "BLOCKED":
        outcome["reason"] = "timeout"
    return {
        "schema_version": 1,
        "run_id": run_id,
        "check_id": "unit",
        "lifecycle": "terminal",
        "started_at": "2026-09-29T10:00:00+00:00",
        "ended_at": "2026-09-29T10:00:01+00:00",
        "outcome": outcome,
        "source": {"head": "a" * 40, "inventory_digest": "b" * 64, "dirty": False},
        "command": {"argv": ["python", "-c", "pass"], "cwd": "/repo"},
        "configuration_digest": "c" * 64,
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


def test_registration_creates_the_artifact_directory(store: Store) -> None:
    assert store.run_dir("run-1").is_dir()


def test_published_report_reads_back_identically(store: Store) -> None:
    report = make_report("run-1")
    store.publish("run-1", report)
    assert store.load("run-1") == report


def test_report_file_lands_at_the_documented_path(store: Store) -> None:
    store.publish("run-1", make_report("run-1"))
    written = store.run_dir("run-1") / REPORT_NAME
    assert json.loads(written.read_text(encoding="utf-8"))["run_id"] == "run-1"


def test_non_terminal_report_is_refused(store: Store) -> None:
    report = make_report("run-1")
    report["lifecycle"] = "running"
    with pytest.raises(StoreError):
        store.publish("run-1", report)
    assert not (store.run_dir("run-1") / REPORT_NAME).exists()


def test_report_for_a_different_run_is_refused(store: Store) -> None:
    with pytest.raises(StoreError):
        store.publish("run-1", make_report("run-2"))
    assert not (store.run_dir("run-1") / REPORT_NAME).exists()


def test_blocked_without_a_reason_is_refused(store: Store) -> None:
    report = make_report("run-1")
    report["outcome"] = {"result": "BLOCKED"}
    with pytest.raises(StoreError):
        store.publish("run-1", report)


def test_a_terminal_run_cannot_be_republished(store: Store) -> None:
    store.publish("run-1", make_report("run-1", result="FAIL"))
    with pytest.raises(StoreError):
        store.publish("run-1", make_report("run-1", result="PASS"))
    # The original failure must survive the later attempt.
    assert store.load("run-1")["outcome"]["result"] == "FAIL"


def test_duplicate_run_id_is_refused(tmp_path: Path) -> None:
    """A repeated run id is a caller bug, reported as a store error.

    Asserting a raw sqlite3.IntegrityError here would pin a storage detail as
    public contract; the caller should catch StoreError, not a driver exception.
    """
    s = Store(tmp_path / "state.sqlite3")
    s.register_run(
        "same", "unit", task_id=None, attempt=None, source={},
        configuration_digest="c", fixture_digest=None,
    )
    with pytest.raises(StoreError):
        s.register_run(
            "same", "unit", task_id=None, attempt=None, source={},
            configuration_digest="c", fixture_digest=None,
        )


def test_loading_an_unpublished_run_raises(store: Store) -> None:
    with pytest.raises(StoreError):
        store.load("run-1")


def test_failing_to_take_the_write_lock_is_a_store_failure(tmp_path: Path) -> None:
    """A lock the store could not take is not a resource conflict.

    `BEGIN IMMEDIATE` is what makes read-then-write safe, and every writer that
    matters goes through `store.transaction`. When the lock is already held by
    another connection, sqlite raises `OperationalError`, which is a driver
    type. Letting that out would escape the `except StoreError` clauses the CLI,
    the MCP tools and the console all rely on, and a caller holding a busy
    database would see "database is locked" instead of a store failure.

    It must also not read as a `ConflictError`, which subclasses `StoreError`:
    a process that could not take the lock has not been told the resource is
    unavailable, so showing the user a claim conflict would be a second, wrong
    answer to a question the store never got to.
    """
    from vkit.storage import ConflictError

    holder = Store(tmp_path / "state.sqlite3")
    with holder.transaction() as conn:
        conn.execute(
            "INSERT INTO claim_holders (resource_key, kind, capacity, held,"
            " task_id, generation, acquired_at) VALUES"
            " ('key-1', 'exclusive', 1, 1, 't1', 1, '2026-09-29T10:00:00+00:00')"
        )
        busy = Store(tmp_path / "state.sqlite3")
        with pytest.raises(StoreError) as caught:
            with busy.transaction() as conn:
                conn.execute("SELECT 1")
        assert not isinstance(caught.value, ConflictError), (
            "a lock failure was reported as a resource conflict"
        )
        assert "write lock" in str(caught.value)


def test_a_terminated_store_keeps_the_report_readable(tmp_path: Path) -> None:
    """The evidence must outlive the process that wrote it."""
    s = Store(tmp_path / "state.sqlite3")
    s.register_run(
        "run-x", "unit", task_id=None, attempt=None, source={},
        configuration_digest="c", fixture_digest=None,
    )
    report = make_report("run-x")
    s.publish("run-x", report)
    del s

    reopened = Store(tmp_path / "state.sqlite3")
    assert reopened.load("run-x") == report


def test_artifact_escape_is_refused(store: Store) -> None:
    with pytest.raises(StoreError):
        store.resolve_artifact("run-1", "../../escape.json")


def test_artifact_inside_the_run_directory_resolves(store: Store) -> None:
    resolved = store.resolve_artifact("run-1", "result.json")
    assert resolved == store.run_dir("run-1").resolve() / "result.json"


def test_absolute_artifact_path_is_refused(store: Store) -> None:
    """An absolute path escapes the run directory on every host.

    The path is built from this host's own separator. `C:/Windows/...` is an
    ordinary relative filename to a POSIX resolver, so it was joined onto the run
    directory, resolved to something inside it, and correctly not refused. The
    property is about absoluteness, not about which drive letter names one.
    """
    absolute = str(Path(sys.executable).anchor or os.sep) + "escape.json"

    with pytest.raises(StoreError):
        store.resolve_artifact("run-1", absolute)


def test_mark_running_after_terminal_is_refused(store: Store) -> None:
    store.publish("run-1", make_report("run-1"))
    with pytest.raises(StoreError):
        store.mark_running("run-1", {"pid": 1})


def test_transaction_commits_its_work(tmp_path: Path) -> None:
    """Regression: transaction() opened BEGIN IMMEDIATE and then let the
    connection close in autocommit mode, which rolled the write back and left the
    lock held. Every call looked correct and discarded its change."""
    s = Store(tmp_path / "state.sqlite3")
    s.register_run("r1", "c", task_id=None, attempt=None, source={},
                   configuration_digest="c", fixture_digest=None)
    assert s.list_runs()[0]["lifecycle"] == "preparing"
    with s.transaction() as conn:
        conn.execute("UPDATE runs SET lifecycle = 'running' WHERE run_id = 'r1'")
    assert s.list_runs()[0]["lifecycle"] == "running"


def test_transaction_rolls_back_on_an_exception(tmp_path: Path) -> None:
    s = Store(tmp_path / "state.sqlite3")
    s.register_run("r1", "c", task_id=None, attempt=None, source={},
                   configuration_digest="c", fixture_digest=None)
    with pytest.raises(RuntimeError):
        with s.transaction() as conn:
            conn.execute("UPDATE runs SET lifecycle = 'running' WHERE run_id = 'r1'")
            raise RuntimeError("boom")
    assert s.list_runs()[0]["lifecycle"] == "preparing"


def test_an_interrupted_report_write_leaves_no_acceptance_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The matrix row "Disk full or interrupted report write -> No complete
    acceptance record; partial artifacts remain diagnosable".

    Both halves were implemented and neither had a test. `publish` stages the
    report to a temp name, fsyncs it, claims the row, and swaps the temp into
    place last; `docs/ACCEPTANCE-MATRIX.md` advertises the row and
    `scripts/acceptance02.py` row 13 concedes in its own note that a full disk is
    "NOT induced". What was measured was the locking half of the row, not this.

    A full disk is not inducible here, so the failure is placed at the one call
    that fails when the volume fills: `fsync`, which is the last write the staged
    file does. That is the real error the kernel returns, and the real point in
    the sequence at which it arrives, so what is exercised is the store's own
    behaviour and not a synthetic exception raised at an arbitrary line.

    The three assertions are the row's three claims, and they are checked against
    what a reader can observe rather than against the exception's class, because
    the class is a driver detail that R1 already changed once.
    """
    s = Store(tmp_path / "state.sqlite3")
    s.register_run("r1", "unit", task_id=None, attempt=None, source={},
                   configuration_digest="c", fixture_digest=None)

    # ENOSPC, as the kernel reports a full volume.
    full = OSError(28, "No space left on device")
    full.errno = 28
    monkeypatch.setattr(storage.os, "fsync", lambda _fd: (_ for _ in ()).throw(full))

    with pytest.raises(OSError):
        s.publish("r1", make_report("r1"))

    monkeypatch.undo()

    run_dir = s.run_dir("r1")
    # No complete acceptance record: nothing a reader could mistake for evidence.
    assert not (run_dir / REPORT_NAME).exists()
    assert list(run_dir.iterdir()) == [], (
        f"the staged report was left behind: {sorted(p.name for p in run_dir.iterdir())}"
    )
    with pytest.raises(StoreError):
        s.load("r1")

    # The run did not become terminal, so nothing claims a verdict exists.
    assert s.run_status("r1")["lifecycle"] == "preparing"
    assert s.run_status("r1")["result"] is None

    # Partial artifacts remain diagnosable: recovery reports on the store rather
    # than raising, and the schema is intact.
    assert recover.inspect(s).to_json()["findings"] is not None
    assert s.version() >= 1


def test_a_failed_report_swap_leaves_no_acceptance_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The step `test_an_interrupted_report_write_leaves_no_acceptance_record`
    cannot reach.

    That test induces ENOSPC at `fsync`, which is the last write the staged file
    makes and the last call inside the `try`. `publish` does two more things
    after it: it claims the row, and then it swaps the temp into place. Both are
    outside the `try`, so neither the cleanup that removes the staged file nor
    the refusal that the test above asserts are in force here.

    A swap is a `rename`, and a rename is the one step in the sequence that a
    full volume can genuinely take away after everything has been written. So
    this places ENOSPC at the real line -- `storage.os.replace` at
    `storage.py:949` -- and then checks what a reader observes.

    The row being claimed is that an interrupted report write leaves no complete
    acceptance record. Three assertions say whether that held, and they are
    checked against observable state rather than the exception:

    * `load` refuses, so nothing can be read as a verdict. The direction matters.
      A fabricated PASS is the failure this store is built to make impossible.
    * The row is terminal with a result but no report, and recovery names exactly
      that window instead of guessing at an outcome. `publish`'s docstring claims
      this is "the correct failure direction: absent evidence, never a fabricated
      PASS"; that claim is only worth anything if the window is reported.
    * The staged file does not survive as debris. This is the assertion that
      fails today, and it is the one that makes the record honest: the orphaned
      `report.json.<pid>.tmp` is a complete, valid report file sitting in the run
      directory under a name a reader or a later tool could mistake for evidence.
      It is not merely untidy. `publish`'s cleanup is keyed on the `try`, and the
      swap is outside it, so nothing removes it.
    """
    s = Store(tmp_path / "state.sqlite3")
    s.register_run("r1", "unit", task_id=None, attempt=None, source={},
                   configuration_digest="c", fixture_digest=None)

    # ENOSPC, as the kernel reports a full volume, raised by the rename itself.
    def no_space(_src, _dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage.os, "replace", no_space)

    with pytest.raises(OSError):
        s.publish("r1", make_report("r1"))

    monkeypatch.undo()

    run_dir = s.run_dir("r1")

    # No complete acceptance record: the reader is refused rather than served a
    # verdict that was never durably placed.
    assert not (run_dir / REPORT_NAME).exists()
    with pytest.raises(StoreError):
        s.load("r1")

    # The window is visible rather than silent. A terminal row with a result and
    # no report is the documented state, and recovery names it as such.
    status = s.run_status("r1")
    assert status["lifecycle"] == "terminal"
    assert status["result"] == "PASS"
    assert "report_path" not in status

    findings = recover.inspect(s).to_json()["findings"]
    assert [f["kind"] for f in findings] == ["terminal_run_without_report"], (
        f"the claim-without-report window was not reported: {findings}"
    )

    # No debris. The staged file was fully written and fsynced before the rename,
    # so on this path it survives as a complete report under a temp name.
    assert list(run_dir.iterdir()) == [], (
        "the staged report outlived the failed swap: "
        f"{sorted(p.name for p in run_dir.iterdir())}"
    )


def test_a_reported_environment_carries_no_credential(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix row "Secret appears in command output -> do not dump full
    environment variables", for the half of it the report can enforce.

    The report's `environment` block is the one place the product chooses what to
    record about the machine a run happened on, and it is written by
    `_environment_facts` rather than by a check. The row asks that a reader never
    be handed the process environment, so the test sets a secret in the ambient
    environment and asserts on what the report would carry.

    The negative control is in the same body: the value is in `os.environ` and is
    not in the facts, so the assertion is about the report's shape rather than
    about a secret that was never set.

    What this does NOT cover is the other half of the row, a secret the check
    itself prints. `logs` names `stdout.log` and `stderr.log`, and those files
    hold the raw bytes the check wrote with no filtering between the check and the
    disk. There is no redaction in the product today (measured: no call to any
    redaction routine anywhere under `src/`), so that half of the row is
    unimplemented rather than untested. See `review/posix-triage.md`.
    """
    secret = "sk-do-not-persist-this-value"
    monkeypatch.setenv("VKIT_TEST_SECRET", secret)

    facts = _environment_facts(None)

    assert secret not in repr(facts), "a credential from the environment reached the report"
    # The facts a reader needs to reproduce the run are still there. A report that
    # achieved the row by recording nothing would satisfy the assertion above.
    assert facts["python_version"] == sys.version.split()[0]
    # Named against literals, not against self-consistency. `_environment_facts`
    # `continue`s past any tool it cannot resolve (execution.py:187 and :195), so a
    # `tool_versions` of `{}` satisfied `all(...)` over its values -- the same
    # vacuous pass this file already guards against above. Every entry is now
    # pinned to the string the tool prints, so a missing one raises instead.
    assert facts["platform"].startswith(platform.system())
    assert set(facts) == {"python_version", "platform", "tool_versions"}
    assert set(facts["tool_versions"]) == {"git", "python"}
    assert facts["tool_versions"]["git"].startswith("git version")
    assert facts["tool_versions"]["python"] == f"Python {sys.version.split()[0]}"


def test_transaction_releases_the_write_lock(tmp_path: Path) -> None:
    """A held lock would make every later write block for the busy timeout."""
    s = Store(tmp_path / "state.sqlite3")
    s.register_run("r1", "c", task_id=None, attempt=None, source={},
                   configuration_digest="c", fixture_digest=None)
    with s.transaction() as conn:
        conn.execute("UPDATE runs SET lifecycle = 'running' WHERE run_id = 'r1'")
    with s.transaction() as conn:
        conn.execute("UPDATE runs SET result = 'BLOCKED' WHERE run_id = 'r1'")
    assert s.list_runs()[0]["result"] == "BLOCKED"
