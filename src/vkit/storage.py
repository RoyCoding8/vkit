"""The durable run record: a small authoritative SQLite row, plus the report file.

Nothing here knows what a check is. It stores identity and lifecycle so a crashed
run is still discoverable, and publishes a terminal report so no reader can see
half of one.
"""
from __future__ import annotations

import ctypes
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

RUNS_DIR_NAME = "runs"
REPORT_NAME = "report.json"
BUSY_TIMEOUT_S = 5.0
DRIVE_REMOTE = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id               TEXT PRIMARY KEY,
    check_id             TEXT NOT NULL,
    task_id              TEXT,
    attempt              INTEGER,
    lifecycle            TEXT NOT NULL CHECK (lifecycle IN ('preparing','running','terminal')),
    result               TEXT CHECK (result IS NULL OR result IN ('PASS','FAIL','BLOCKED')),
    reason               TEXT,
    source_json          TEXT NOT NULL,
    configuration_digest TEXT NOT NULL,
    fixture_digest       TEXT,
    process_json         TEXT,
    registered_at        TEXT NOT NULL,
    ended_at             TEXT
)
"""


class StoreError(Exception):
    """The durable state could not record what it was asked to record."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _reject_network_path(path: Path) -> None:
    """SQLite's file locking cannot be trusted on a network filesystem, and it
    fails by corrupting rather than by complaining, so refuse the path up front."""
    absolute = os.path.abspath(path)
    drive, _ = os.path.splitdrive(absolute)
    if absolute.startswith(("//",)) or drive.startswith("\\\\"):
        raise StoreError(f"network-backed state path is unsupported: {path}")
    if os.name == "nt" and drive and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE:
        raise StoreError(f"network-backed state path is unsupported: {path}")


class Store:
    """One run table and the artifact directories beside it."""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        _reject_network_path(self._db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None keeps every statement its own committed transaction,
        # so no caller can leave one open across a subprocess.
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=BUSY_TIMEOUT_S)
        try:
            yield conn
        finally:
            conn.close()

    def run_dir(self, run_id: str) -> Path:
        return self._db_path.parent / RUNS_DIR_NAME / run_id

    def register_run(
        self,
        run_id: str,
        check_id: str,
        *,
        task_id: str | None,
        attempt: int | None,
        source: dict,
        configuration_digest: str,
        fixture_digest: str | None,
    ) -> None:
        """Create the run and its artifact directory before anything executes.

        A run id is random and unique; a repeat registration is a caller bug and
        the primary key says so instead of overwriting the first run's identity.
        """
        self.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, check_id, task_id, attempt, lifecycle,"
                " source_json, configuration_digest, fixture_digest, registered_at)"
                " VALUES (?, ?, ?, ?, 'preparing', ?, ?, ?, ?)",
                (
                    run_id, check_id, task_id, attempt, _dumps(source),
                    configuration_digest, fixture_digest, _now(),
                ),
            )

    def mark_running(self, run_id: str, process: dict) -> None:
        """Attach process identity. A terminal run never returns to running: a retry
        is a new run, and the first result must survive it."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runs SET lifecycle = 'running', process_json = ?"
                " WHERE run_id = ? AND lifecycle != 'terminal'",
                (_dumps(process), run_id),
            )
            if cursor.rowcount == 0:
                raise StoreError(f"cannot mark running: {run_id} is unknown or already terminal")

    def publish(self, run_id: str, report: dict) -> None:
        """Put the report where a reader expects it, atomically, and exactly once.

        Ordering is the whole design here. The file is fully written and fsynced
        to a temp name first, the row is claimed second, and the temp is swapped
        into place last. Claiming the row before the swap is what stops a refused
        republish from overwriting evidence: an earlier bug wrote report.json
        first and only then discovered the run was already terminal, so a retry
        silently replaced a FAIL with a PASS. Staging first means the guard runs
        while the only thing on disk is a temp file nobody reads.

        A crash between the claim and the swap leaves a terminal row with no
        report, and `load` raises rather than inventing an outcome. That is the
        correct failure direction: absent evidence, never a fabricated PASS.
        """
        outcome = report.get("outcome")
        if report.get("run_id") != run_id or report.get("lifecycle") != "terminal":
            raise StoreError(f"refusing to publish a report that is not terminal for {run_id}")
        if not isinstance(outcome, dict) or "result" not in outcome:
            raise StoreError(f"refusing to publish {run_id} with no outcome: a terminal run has exactly one")
        if outcome["result"] == "BLOCKED" and not outcome.get("reason"):
            raise StoreError(f"refusing to publish {run_id} as BLOCKED with no reason")

        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        # Same directory, so the replace stays on one filesystem and stays atomic.
        temp = run_dir / f"{REPORT_NAME}.{os.getpid()}.tmp"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())

            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE runs SET lifecycle = 'terminal', result = ?, reason = ?, ended_at = ?"
                    " WHERE run_id = ? AND lifecycle != 'terminal'",
                    (outcome["result"], outcome.get("reason"), report.get("ended_at"), run_id),
                )
                if cursor.rowcount == 0:
                    raise StoreError(f"cannot publish: {run_id} is unknown or already terminal")
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

        os.replace(temp, run_dir / REPORT_NAME)

    def load(self, run_id: str) -> dict:
        """Read the published report back."""
        path = self.run_dir(run_id) / REPORT_NAME
        if not path.is_file():
            raise StoreError(f"no published report for run {run_id}: {path}")
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def resolve_artifact(self, run_id: str, name: str) -> Path:
        """Resolve a check-written artifact name inside the run directory.

        The artifact is written by the process under test, so its name is the
        least trusted string in the system. Containment is checked after
        resolution, because a prefix test loses to `..` and to absolute paths.
        """
        run_dir = self.run_dir(run_id).resolve()
        candidate = (run_dir / name).resolve()
        if candidate != run_dir and run_dir not in candidate.parents:
            raise StoreError(f"artifact {name!r} escapes run directory {run_dir}")
        return candidate
