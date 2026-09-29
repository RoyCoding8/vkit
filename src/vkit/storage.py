"""The durable run record and its migrations.

Nothing here knows what a check is. This layer stores identity, lifecycle, and
ownership so a crashed run stays discoverable and a superseded attempt cannot
publish accepted evidence.

Migrations are numbered, applied once, and recorded in `schema_version`. A plan
01 report written by an earlier build must still be readable after any later
migration, because the report is the evidence and the evidence does not expire
with the software that produced it.
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

# Migrations are an ordered list of SQL blocks. Each runs exactly once, inside one
# transaction, and the applied version is recorded before the transaction commits.
# Never edit a shipped migration: a database that already applied it would then
# hold a shape the code no longer expects.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, """
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
    """),
    (2, """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id          TEXT PRIMARY KEY,
        contract_json    TEXT NOT NULL,
        policy_digest    TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('active','paused','closed')),
        generation       INTEGER NOT NULL,
        readiness        TEXT CHECK (readiness IS NULL OR readiness IN ('READY','REJECTED','BLOCKED')),
        opened_at        TEXT NOT NULL,
        closed_at        TEXT
    );

    -- A resource has at most one owner, and this table is the only record of
    -- that. The primary key IS the mutual exclusion, so a second claimant loses
    -- in the database rather than in application code that might be racing.
    --
    -- An earlier draft also carried a `claims` history table. It was deleted
    -- rather than filled in: nothing wrote it, so it would have been a second,
    -- divergent record of the same fact. The acquisition time and owner on this
    -- row are the history. A released claim leaves no trace, which is correct
    -- for a current-state table and is what `recover` inspects.
    CREATE TABLE IF NOT EXISTS claim_holders (
        resource_key  TEXT PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN ('exclusive','capacity')),
        capacity      INTEGER,
        held          INTEGER NOT NULL DEFAULT 0,
        task_id       TEXT NOT NULL,
        generation    INTEGER NOT NULL,
        acquired_at   TEXT NOT NULL,
        released_at   TEXT
    );

    -- Idempotency keys outlive the operation they name. A retry arriving after
    -- the operation completed must still find its original record, so these are
    -- not expired while a duplicate execution is still possible.
    CREATE TABLE IF NOT EXISTS request_keys (
        request_id   TEXT NOT NULL,
        operation    TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        subject_id   TEXT,
        created_at   TEXT NOT NULL,
        PRIMARY KEY (request_id, operation)
    );

    CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id);
    """),
)


class StoreError(Exception):
    """The durable state could not record what it was asked to record."""


class ConflictError(StoreError):
    """The request is well formed but contradicts recorded state.

    Separate from StoreError because a conflict is a legitimate answer the caller
    shows the user, while a StoreError means the store itself could not decide.
    """


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


class _Transaction:
    """One BEGIN IMMEDIATE, committed only if the body returns.

    The write lock is taken before the caller's first read. That is the whole
    point: read-then-write without it is the race two processes both win.
    """

    __slots__ = ("_conn",)

    def __init__(self, store: "Store"):
        self._conn = store._connect()

    def __enter__(self) -> sqlite3.Connection:
        conn = self._conn.__enter__()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._conn.__exit__(None, None, None)
            else:
                try:
                    self._conn.execute("ROLLBACK")
                finally:
                    self._conn.__exit__(None, None, None)
        finally:
            self._conn = None
        return False


class Store:
    """Run, task, and claim records, plus the artifact directories beside them."""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        _reject_network_path(self._db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def transaction(self):
        """A connection inside one BEGIN IMMEDIATE, committed only on success.

        Public because ownership and idempotency both need read-then-write
        coordination, and a module reaching into `_connect` is one rename away
        from silently losing its atomicity. Callers commit or roll back.

            with store.transaction() as conn:
                conn.execute(...)
        """
        return _Transaction(self)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None keeps every statement its own committed
        # transaction, so no caller can leave one open across a subprocess.
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=BUSY_TIMEOUT_S)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def _migrate(self) -> None:
        """Apply pending migrations, each atomically.

        `executescript` issues its own COMMIT before running anything, so an
        explicit BEGIN or ROLLBACK around it does not survive the call and
        raises "no transaction is active". The script is therefore the
        transaction: each migration appends its own version insert, so SQLite
        applies the tables and the recorded version together and a crash rolls
        the whole thing back.

        The version is re-read immediately before each script under the write
        lock `executescript` itself takes, so two processes starting together
        cannot both apply the same migration.
        """
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            for version, sql in MIGRATIONS:
                row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                if row and row[0] is not None and version <= row[0]:
                    continue
                # The trailing semicolon matters: a migration's last statement has
                # none of its own, so without this the version INSERT is appended
                # to that statement and the whole script is a syntax error.
                conn.executescript(
                    sql.rstrip().rstrip(";") + f";\nINSERT INTO schema_version (version) VALUES ({int(version)});"
                )

    def version(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return row[0] if row and row[0] is not None else 0

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
            try:
                conn.execute(
                    "INSERT INTO runs (run_id, check_id, task_id, attempt, lifecycle,"
                    " source_json, configuration_digest, fixture_digest, registered_at)"
                    " VALUES (?, ?, ?, ?, 'preparing', ?, ?, ?, ?)",
                    (
                        run_id, check_id, task_id, attempt, _dumps(source),
                        configuration_digest, fixture_digest, _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"run {run_id} is already registered") from exc

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

    def attach_task(self, run_id: str, task_id: str, attempt: int) -> None:
        """Bind a run to the attempt that started it."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET task_id = ?, attempt = ? WHERE run_id = ?",
                (task_id, attempt, run_id),
            )

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

    def list_runs(self, *, task_id: str | None = None, limit: int = 50) -> list[dict]:
        """Recent runs, newest first. Bounded so a console cannot ask for all.

        Ordered by rowid rather than registered_at. Two runs started in the same
        microsecond would tie on the timestamp, and SQLite does not promise an
        order between equal values, so "the most recent run" would be whichever
        row the query happened to return first. rowid is monotonic with
        insertion, so the ordering is total and the answer is reproducible.
        """
        sql = ("SELECT run_id, check_id, task_id, lifecycle, result, reason,"
               " registered_at, ended_at FROM runs")
        params: list[object] = []
        if task_id is not None:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        keys = ("run_id", "check_id", "task_id", "lifecycle", "result", "reason",
                "registered_at", "ended_at")
        return [dict(zip(keys, row)) for row in rows]

    def run_process_identity(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT process_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row or not row[0]:
            return None
        return json.loads(row[0])
