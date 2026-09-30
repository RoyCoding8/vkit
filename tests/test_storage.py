"""Behavior of the durable store, exercised through its public surface.

These tests deliberately avoid asserting on SQL. They call Store the way the
execution path does and check what a reader would observe on disk, because a
test that restates the schema proves nothing about whether a report survives.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
