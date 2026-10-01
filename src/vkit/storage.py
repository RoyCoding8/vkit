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

import contextlib
import ctypes
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

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
    (3, """
    -- Which task holds which slot of a capacity pool.
    --
    -- `claim_holders` is one row per resource, with `held` counting the slots in
    -- use. That answers "is the pool full" but not "who is using it", and the
    -- difference is a bug rather than a limitation: `release` deleted the row
    -- scoped by task_id, so only the task that happened to acquire the pool first
    -- could ever give a slot back, and a pool filled by three tasks leaked two of
    -- them permanently.
    --
    -- A decrement alone would be worse, because a task holding nothing could then
    -- free a slot it never took. So membership is recorded here and `held` is the
    -- count of these rows. The two are written in the same transaction, so they
    -- cannot disagree.
    --
    -- An exclusive resource is a pool of one. It is represented the same way
    -- rather than as a special case, so there is one code path for both kinds and
    -- a slot cannot be released by anything other than the holder named here.
    CREATE TABLE IF NOT EXISTS claim_members (
        resource_key  TEXT NOT NULL,
        task_id       TEXT NOT NULL,
        generation    INTEGER NOT NULL,
        acquired_at   TEXT NOT NULL,
        PRIMARY KEY (resource_key, task_id, generation)
    );

    CREATE INDEX IF NOT EXISTS claim_members_by_task ON claim_members(task_id, generation);
    """),
    (4, """
    -- The integration decision is its own record, and this table is the only
    -- writable authority for it. Its columns are exactly the CONTRACT.md
    -- acceptance row: the computed decision, the exact source and policy
    -- identities, the required checks, the gaps, and the context the decision
    -- was made in. Nothing writes a verdict anywhere else, and a client cannot
    -- supply one.
    --
    -- The manifest at the candidate revision is recorded here rather than being
    -- left to be re-read later, because the comparison that matters -- does the
    -- candidate's own policy still require what the trusted policy requires --
    -- is only answerable against the bytes the candidate shipped. Re-deriving
    -- them from a checkout that has since moved would answer a different
    -- question.
    CREATE TABLE IF NOT EXISTS acceptances (
        acceptance_id   TEXT PRIMARY KEY,
        integration_id  TEXT NOT NULL,
        context         TEXT NOT NULL CHECK (context IN ('local','protected')),
        decision        TEXT NOT NULL CHECK (decision IN ('ACCEPTED','REJECTED','BLOCKED')),
        candidate       TEXT NOT NULL,
        target          TEXT NOT NULL,
        candidate_parent TEXT,
        source_json     TEXT NOT NULL,
        policy_json     TEXT NOT NULL,
        verifier_json   TEXT NOT NULL,
        environment_json TEXT NOT NULL,
        fixture_json    TEXT NOT NULL,
        manifest_json   TEXT NOT NULL,
        required_checks TEXT NOT NULL,
        checks_json     TEXT NOT NULL,
        gaps_json       TEXT NOT NULL,
        findings_json   TEXT NOT NULL,
        decided_at      TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS acceptances_by_integration ON acceptances(integration_id);
    """),
    (5, """
    -- A run's lifecycle reaches two states beyond the three migration 1 allowed.
    -- `launching` is the window between CreateProcess returning and the identity
    -- being published, and `cancelling` is the window between a cancel being
    -- requested and the terminal outcome being known. Both are states a recovery
    -- reader must be able to tell apart from "finished", so both are named.
    --
    -- SQLite cannot widen a CHECK constraint in place. `ALTER TABLE runs ADD
    -- COLUMN` adds the column and leaves the old constraint standing, so the
    -- three-step rename dance below is the only way to keep the constraint
    -- honest -- which matters because an unconstrained lifecycle column is exactly
    -- the class of typo this product exists to reject. Measured on this schema: a
    -- 'launching' insert is rejected before the rebuild and accepted after it,
    -- and a typo'd 'prepring' is still rejected after it.
    --
    -- The explicit BEGIN/COMMIT is load-bearing and is not decoration.
    -- `executescript` on an autocommit connection commits statement by statement,
    -- so without it a failure after the DROP would leave `runs` absent while the
    -- recorded version still said 4. Measured, with the version INSERT failing
    -- after the rebuild: without the transaction the old table was gone; with it
    -- the rebuild committed as one unit. The surrounding statements stay outside
    -- it so that the version INSERT `_migrate` appends to the script still runs
    -- as its own statement.
    BEGIN IMMEDIATE;

    CREATE TABLE runs_new (
        run_id               TEXT PRIMARY KEY,
        check_id             TEXT NOT NULL,
        task_id              TEXT,
        attempt              INTEGER,
        lifecycle            TEXT NOT NULL
            CHECK (lifecycle IN ('preparing','launching','cancelling','running','terminal')),
        result               TEXT CHECK (result IS NULL OR result IN ('PASS','FAIL','BLOCKED')),
        reason               TEXT,
        source_json          TEXT NOT NULL,
        configuration_digest TEXT NOT NULL,
        fixture_digest       TEXT,
        process_json         TEXT,
        registered_at        TEXT NOT NULL,
        ended_at             TEXT
    );

    INSERT INTO runs_new (
        run_id, check_id, task_id, attempt, lifecycle, result, reason, source_json,
        configuration_digest, fixture_digest, process_json, registered_at, ended_at
    )
    SELECT run_id, check_id, task_id, attempt, lifecycle, result, reason, source_json,
           configuration_digest, fixture_digest, process_json, registered_at, ended_at
    FROM runs;

    DROP TABLE runs;
    ALTER TABLE runs_new RENAME TO runs;

    -- `DROP TABLE` takes the index with it, and nothing fails when it goes: the
    -- query that most needs it, `recover`'s lookup of the runs holding a claim,
    -- would quietly become a full table scan on the one path that decides whether
    -- a resource may be released. Measured before and after: SEARCH runs USING
    -- INDEX runs_by_task, then SCAN runs, then SEARCH again once re-issued.
    CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id);

    COMMIT;

    -- What was launched, recorded before it was launched.
    --
    -- This row is the durable half of a run's ownership. `process_json` cannot
    -- hold it, because that column is written once the child exists and a crash
    -- between the two writes would leave a run with no record that anything was
    -- ever started. The two pieces of ownership are therefore persisted at the
    -- two moments they first exist: the job name here, before CreateProcess, and
    -- the pid in `process_json`, immediately after it. A reader takes the union.
    --
    -- `state_dir` and `project_root` are absolute so the detached supervisor
    -- re-opens this database rather than guessing it from a working directory
    -- that a client may not have set. `argv_json` is the resolved argv, written
    -- before the spawn, so a run that never produced a report can still say what
    -- it was going to run.
    --
    -- `job_name` and the three `abandon*` columns are here because the two
    -- features that need them are named against this table elsewhere: the job
    -- name is what a second process opens to cancel the tree, and `abandoned` is
    -- what `recover`'s ABANDON_LAUNCH writes once a human has supplied the
    -- evidence that an unresolved launch is over. Neither belongs in
    -- `process_json`, which is written later and by a different writer.
    CREATE TABLE IF NOT EXISTS launches (
        run_id            TEXT PRIMARY KEY REFERENCES runs(run_id),
        project_root      TEXT NOT NULL,
        state_dir         TEXT NOT NULL,
        manifest_dir      TEXT NOT NULL,
        check_id          TEXT NOT NULL,
        argv_json         TEXT NOT NULL,
        cwd               TEXT NOT NULL,
        stdout_path       TEXT NOT NULL,
        stderr_path       TEXT NOT NULL,
        env_json          TEXT NOT NULL,
        timeout_seconds   REAL NOT NULL,
        job_name          TEXT,
        supervisor_pid    INTEGER NOT NULL,
        supervisor_start  INTEGER,
        requested_at      TEXT NOT NULL,
        kind              TEXT NOT NULL CHECK (kind IN ('detached','inline')),
        abandoned         INTEGER NOT NULL DEFAULT 0,
        abandoned_at      TEXT,
        abandon_evidence  TEXT
    );

    -- Which attempt asked for which run, and the only reader of "which generation
    -- of a task does this run belong to".
    --
    -- `runs.attempt` records the generation too, and this table is not a second
    -- copy of that column: it is the membership relation recovery needs, and it
    -- is written in the same transaction as the launch it describes. A run is
    -- either a member of exactly one generation of exactly one task, or it is not
    -- a member of any, and the second case is a run registered by a caller that
    -- never had a task -- which is every inline `run_check` caller, and none of
    -- them holds a claim.
    CREATE TABLE IF NOT EXISTS run_intents (
        run_id     TEXT NOT NULL,
        task_id    TEXT NOT NULL,
        generation INTEGER NOT NULL,
        operation  TEXT NOT NULL CHECK (operation IN ('check_start','run_cancel')),
        requested_at TEXT NOT NULL,
        PRIMARY KEY (run_id, operation, task_id, generation)
    );

    CREATE INDEX IF NOT EXISTS run_intents_by_task ON run_intents(task_id, generation);

    -- A cancel asked for before the run had an identity to cancel.
    --
    -- The row is what makes a cancel that arrives early a deferred cancel rather
    -- than a lost one. The supervisor checks for it at the two points where it
    -- would otherwise begin executing, so a cancel that lands during startup is
    -- honoured before any check code runs.
    CREATE TABLE IF NOT EXISTS cancel_intents (
        run_id       TEXT PRIMARY KEY,
        requested_at TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        resolved     INTEGER NOT NULL DEFAULT 0
    );
    """),
)


class StoreError(Exception):
    """The durable state could not record what it was asked to record."""


class ConflictError(StoreError):
    """The request is well formed but contradicts recorded state.

    Separate from StoreError because a conflict is a legitimate answer the caller
    shows the user, while a StoreError means the store itself could not decide.
    """


@dataclass(frozen=True)
class LaunchPlan:
    """Everything needed to start a run, decided before anything is started.

    Named rather than passed as fourteen arguments because the ordering is the
    point: this is one value, it is complete, and a caller cannot record a launch
    with half of it. The paths are absolute because the detached supervisor
    re-opens the store and re-reads the manifest from a working directory no
    client controls.
    """

    project_root: Path
    state_dir: Path
    manifest_dir: Path
    check_id: str
    argv: tuple[str, ...]
    cwd: Path
    stdout_path: Path
    stderr_path: Path
    env: dict[str, str]
    timeout_seconds: float
    job_name: str
    kind: str


def probe_state(state_root: Path, db_path: Path) -> tuple[bool, str]:
    """Whether the state store at these paths can be opened, and why not.

    One probe, so `vkit doctor` and the console's readiness view cannot report
    different answers about the same repository. They each used to run their own
    copy: `cmd_doctor` did this and let `ok` depend on it, while the console
    hardcoded `state_writable: True`. A project whose state store could not be
    opened was therefore reported ready by the console and not ready by the CLI.

    Opening a `Store` migrates it, so this is a write. It is the same write the
    store does on any open, which is why `ok` may depend on it: a project that
    cannot take that write cannot run a check either.
    """
    try:
        state_root.mkdir(parents=True, exist_ok=True)
        Store(db_path)
    except (StoreError, OSError) as exc:
        return False, str(exc)
    return True, str(state_root)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# The columns `list_runs` returns, in the order its query selects them. Named
# once beside the query that fills them, because a positional zip against a
# separate key tuple is a mapping that can silently reorder.
_RUN_COLUMNS = (
    "run_id", "check_id", "task_id", "attempt", "lifecycle", "result", "reason",
    "registered_at", "ended_at", "source_json", "configuration_digest", "fixture_digest",
)

# The `launches` columns `load_launch` returns, named beside the query that fills
# them for the same reason `_RUN_COLUMNS` is.
_LAUNCH_COLUMNS = (
    "run_id", "project_root", "state_dir", "manifest_dir", "check_id", "argv_json",
    "cwd", "stdout_path", "stderr_path", "env_json", "timeout_seconds", "job_name",
    "supervisor_pid", "supervisor_start", "requested_at", "kind", "abandoned",
    "abandoned_at", "abandon_evidence",
)


def _supervisor_start() -> int | None:
    """This process's creation FILETIME, or None where that has no meaning.

    Recorded beside the supervisor's pid so a reader can tell "this supervisor is
    gone" from "this pid belongs to a process that started later". Windows
    recycles pids, so the pid alone cannot answer that, and an answer of "the
    supervisor is gone" is what `recover` needs before it will clear an
    unresolved launch.
    """
    if sys.platform != "win32":
        return None
    with contextlib.suppress(Exception):
        from .procidentity import read_identity

        identity = read_identity(os.getpid())
        if identity is not None:
            return identity.creation_time
    return None


def open_task(conn: sqlite3.Connection, task_id: str, contract: dict, policy_digest: str) -> None:
    """Write one task row, on a connection the caller already owns.

    Takes a connection rather than the store because admission must write this
    row and the task's claims in the same transaction, and `claims.acquire`
    opens its own. The SQL lives here so the insert has exactly one site: two
    writers of the tasks table is two places a rule can be missed.
    """
    conn.execute(
        "INSERT INTO tasks (task_id, contract_json, policy_digest, status, generation, opened_at)"
        " VALUES (?, ?, ?, 'active', 1, ?)",
        (task_id, _dumps(contract), policy_digest, _now()),
    )


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

    The commit is explicit. Closing the connection in autocommit mode rolls an
    open transaction back, so an earlier version that simply let `_connect` close
    discarded every write and left the lock held. It looked correct and silently
    was not, which is worse than not existing.
    """

    __slots__ = ("_cm", "_conn")

    def __init__(self, store: "Store"):
        self._cm = store._connect()
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self._conn = self._cm.__enter__()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # The lock is taken here, so this is the only place that can say a
            # caller failed to get it. Adapters report a StoreError as a store
            # failure rather than showing the user "database is locked": a
            # process that could not take the lock has not been told the
            # resource is unavailable, so it is not a conflict.
            self._cm.__exit__(None, None, None)
            self._conn = None
            raise StoreError(f"could not take the write lock: {exc}") from exc
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self._conn is not None:
                if exc_type is None:
                    self._conn.execute("COMMIT")
                else:
                    with contextlib.suppress(sqlite3.OperationalError):
                        self._conn.execute("ROLLBACK")
        finally:
            self._cm.__exit__(None, None, None)
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

    def run_process_identity(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT process_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row or not row[0]:
            return None
        return json.loads(row[0])

    def mark_running(self, run_id: str, process: dict, *, command: dict | None = None) -> None:
        """Attach a verified process identity, and the command that was launched.

        The legacy name for `publish_identity`, kept because callers outside this
        milestone's scope still use it -- the acceptance harness and two test
        modules -- and because it is not a second authority: it performs the same
        single guarded write, so a run cannot end up `running` through one name and
        `launching` through the other. It is deleted with the last caller.

        The command is recorded here rather than only in the final report because
        a run cancelled before it finished still has to describe what it was
        going to run. The report schema requires a non-empty argv, so a cancel
        report with nothing to put there would be rejected as invalid.
        """
        payload = dict(process)
        if command is not None:
            payload["command"] = command
        self.publish_identity(run_id, payload)

    def attach_task(self, run_id: str, task_id: str, attempt: int) -> None:
        """Bind a run to the attempt that started it."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET task_id = ?, attempt = ? WHERE run_id = ?",
                (task_id, attempt, run_id),
            )

    # ------------------------------------------------------------- launches

    def record_launch(
        self, *, run_id: str, task_id: str | None, generation: int | None, plan: LaunchPlan
    ) -> None:
        """Record what is about to be launched, before it is launched.

        One transaction for the `launches` row, the `run_intents` row and the
        move to `preparing`-with-known-intent, because a launch that recorded one
        of the two without the other would be exactly the unreadable state this
        row exists to prevent: a claim that cannot be told from a run that never
        started.

        A second call for the same run id is refused rather than overwritten. The
        first one is the truth about what was launched; a caller that disagrees
        with it is a caller that would put two launches under one identity.

        `supervisor_pid` is left null here and filled in by whoever owns the job.
        The launcher is not the supervisor in the detached case -- it starts one and
        exits -- so recording the launcher's own pid would name a process that is
        gone by the time anyone asks whether the supervisor is still alive. That
        question is one of the three ABANDON_LAUNCH has to answer, so a wrong pid
        there is a reconciliation that refuses forever. Measured: with the
        launcher's pid recorded, a supervisor that had died was still reported
        alive and the abandon was refused.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT run_id FROM launches WHERE run_id = ?", (run_id,)
                ).fetchone()
                if existing is not None:
                    raise ConflictError(
                        f"run {run_id} already has a recorded launch; a second launch under "
                        "one run id would be two executions with one identity"
                    )
                conn.execute(
                    "INSERT INTO launches (run_id, project_root, state_dir, manifest_dir,"
                    " check_id, argv_json, cwd, stdout_path, stderr_path, env_json,"
                    " timeout_seconds, job_name, supervisor_pid, supervisor_start, requested_at,"
                    " kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?)",
                    (
                        run_id, str(plan.project_root), str(plan.state_dir),
                        str(plan.manifest_dir), plan.check_id, _dumps(list(plan.argv)),
                        str(plan.cwd), str(plan.stdout_path), str(plan.stderr_path),
                        _dumps(plan.env), float(plan.timeout_seconds), plan.job_name,
                        _now(), plan.kind,
                    ),
                )
                if task_id is not None and generation is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO run_intents (run_id, task_id, generation,"
                        " operation, requested_at) VALUES (?,?,?,'check_start',?)",
                        (run_id, task_id, int(generation), _now()),
                    )
            except BaseException:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def claim_supervisor(self, run_id: str) -> dict[str, Any]:
        """Record that *this* process is the supervisor for a recorded launch.

        Written by the supervisor and by nobody else, for the reason
        `record_launch` leaves it null: the process that registers a run is not
        the process that owns its job once the run is detached, and an identity
        recorded from the wrong one is an identity that outlives the truth.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE launches SET supervisor_pid = ?, supervisor_start = ?"
                " WHERE run_id = ?",
                (os.getpid(), _supervisor_start(), run_id),
            )
            if cursor.rowcount == 0:
                raise StoreError(f"no launch is recorded for run {run_id!r} to supervise")
        return {"run_id": run_id, "supervisor_pid": os.getpid()}

    def load_launch(self, run_id: str) -> dict | None:
        """The recorded launch, or None when nothing was ever launched for this run."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT " + ", ".join(_LAUNCH_COLUMNS) + " FROM launches WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(_LAUNCH_COLUMNS, row))

    def mark_launching(self, run_id: str, process: dict) -> None:
        """Record that a process was created and its identity is not yet published.

        `ownership_known` false is the whole content of this write. The window it
        names is the dangerous one: a child exists, the tree is contained by a
        job nobody else can name yet, and the only truthful answer to "is
        anything running" is "something was started and I cannot yet prove what".
        A reader that treated the absent pid as proof of absence would release a
        claim this run's task still holds.
        """
        payload = dict(process)
        payload["ownership_known"] = False
        payload["launch_state"] = "started"
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runs SET lifecycle = 'launching', process_json = ?"
                " WHERE run_id = ? AND lifecycle != 'terminal'",
                (_dumps(payload), run_id),
            )
            if cursor.rowcount == 0:
                raise StoreError(
                    f"cannot mark launching: {run_id} is unknown or already terminal"
                )

    def publish_identity(self, run_id: str, identity: dict) -> None:
        """Publish the verified owner of a running run.

        Distinct from `mark_launching` and guarded the same way, so a cancel that
        has already published a terminal outcome wins over a late identity write.
        That is the correct outcome rather than a lost update: the run was
        cancelled, and a cancelled run does not become running again because its
        supervisor was slow.
        """
        payload = dict(identity)
        payload["ownership_known"] = True
        payload["launch_state"] = "identity_published"
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runs SET lifecycle = 'running', process_json = ?"
                " WHERE run_id = ? AND lifecycle != 'terminal'",
                (_dumps(payload), run_id),
            )
            if cursor.rowcount == 0:
                raise StoreError(
                    f"cannot publish identity: {run_id} is unknown or already terminal"
                )

    def mark_cancelling(self, run_id: str) -> None:
        """Record that termination has been asked for and the outcome is not yet known."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET lifecycle = 'cancelling'"
                " WHERE run_id = ? AND lifecycle NOT IN ('terminal', 'cancelling')",
                (run_id,),
            )

    def record_cancel_intent(self, run_id: str, requested_by: str) -> dict:
        """Note a cancel for a run, and whether it can be acted on yet.

        Returns the row so the caller can tell a deferred cancel from an applied
        one. `applied` is false when the run has no published owner, which is not
        a refusal: it is the window the intent exists to cover, and the
        supervisor picks it up at the next point it would have started work.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT lifecycle, process_json FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise StoreError(f"no run is recorded under {run_id!r}")
                conn.execute(
                    "INSERT INTO cancel_intents (run_id, requested_at, requested_by, resolved)"
                    " VALUES (?,?,?,0) ON CONFLICT(run_id) DO UPDATE SET requested_at = excluded.requested_at",
                    (run_id, _now(), requested_by),
                )
                process = json.loads(row[1]) if row[1] else None
                ownership_known = bool((process or {}).get("ownership_known"))
                if not ownership_known and row[0] != "terminal":
                    conn.execute(
                        "UPDATE runs SET lifecycle = 'cancelling'"
                        " WHERE run_id = ? AND lifecycle != 'terminal'",
                        (run_id,),
                    )
            except BaseException:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return {
            "run_id": run_id,
            "requested_by": requested_by,
            "ownership_known": ownership_known,
            "applied": ownership_known,
            "lifecycle": "terminal" if row[0] == "terminal" else "cancelling",
        }

    def cancel_intent(self, run_id: str) -> dict | None:
        """The pending cancel for a run, or None when nothing asked for one."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id, requested_at, requested_by, resolved FROM cancel_intents"
                " WHERE run_id = ? AND resolved = 0",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(("run_id", "requested_at", "requested_by", "resolved"), row))

    def resolve_cancel_intent(self, run_id: str) -> None:
        """Mark a cancel as answered, so it is not re-applied to a later run."""
        with self._connect() as conn:
            conn.execute("UPDATE cancel_intents SET resolved = 1 WHERE run_id = ?", (run_id,))

    def abandon_launch(self, run_id: str, evidence: str) -> None:
        """Record that an unresolved launch was reconciled by a human.

        Never deletes and never touches a claim. The claim is released by
        `recover`'s own RELEASE_CLAIM, which asks again whether any holder is
        live; this row only records that the unresolved state was decided, so a
        second request to decide it is refused rather than answered afresh.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE launches SET abandoned = 1, abandoned_at = ?, abandon_evidence = ?"
                " WHERE run_id = ?",
                (_now(), evidence, run_id),
            )
            if cursor.rowcount == 0:
                raise StoreError(
                    f"cannot abandon the launch of {run_id!r}: no launch was recorded for it"
                )

    def launch_abandoned(self, run_id: str) -> bool:
        """Whether this run's launch was already reconciled by a human."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT abandoned FROM launches WHERE run_id = ?", (run_id,)
            ).fetchone()
        return bool(row and row[0])

    def runs_for_task(self, task_id: str, generation: int) -> tuple[dict, ...]:
        """The runs belonging to one generation of one task.

        Read from `run_intents` rather than from `runs.task_id`, because the
        question recovery asks is about membership of a generation and the
        intent row is where that membership is recorded. A run with no intent
        belongs to no generation and holds no claim, so it is correctly absent
        rather than being read as a member of whatever is current.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT " + ", ".join(_RUN_COLUMNS) + " FROM runs WHERE run_id IN"
                " (SELECT run_id FROM run_intents WHERE task_id = ? AND generation = ?)"
                " ORDER BY rowid",
                (task_id, int(generation)),
            ).fetchall()
        return tuple(dict(zip(_RUN_COLUMNS, row)) for row in rows)

    def run_status(self, run_id: str) -> dict | None:
        """What is known about a run that may not have published a report.

        Every in-flight run is report-less by design, so a status read of one is a
        valid request with a valid answer. This is that answer: the recorded
        lifecycle, the identity if one is known, and no outcome. None is returned
        only for a run id that does not exist at all, which is the one case where
        a reader has named something this store has never heard of.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id, check_id, task_id, attempt, lifecycle, result, reason,"
                " registered_at, ended_at, process_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        status = {
            "run_id": row[0], "check_id": row[1], "task_id": row[2], "attempt": row[3],
            "lifecycle": row[4], "result": row[5], "reason": row[6],
            "registered_at": row[7], "ended_at": row[8],
        }
        launch = self.load_launch(run_id)
        if launch is not None:
            status["job_name"] = launch["job_name"]
        process = json.loads(row[9]) if row[9] else None
        if process is not None:
            status["process"] = process
        report = self.run_dir(run_id) / REPORT_NAME
        if report.is_file():
            status["report_path"] = str(report)
        return status

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

        The three identity columns come back with every row because acceptance
        cannot decide without them: a pass is only a pass against the source,
        policy and fixtures it actually ran under. A reader that had to ask for
        them separately would be free to forget, and that is how evidence
        produced under one identity came to satisfy a contract written under
        another.
        """
        sql = ("SELECT run_id, check_id, task_id, attempt, lifecycle, result, reason,"
               " registered_at, ended_at, source_json, configuration_digest,"
               " fixture_digest FROM runs")
        params: list[object] = []
        if task_id is not None:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[dict] = []
        for row in rows:
            record = dict(zip(_RUN_COLUMNS, row))
            # The source column is a JSON document and the only field acceptance
            # compares is the inventory digest inside it. Parsing it here, rather
            # than in each caller, is what stops two callers picking two
            # different fields out of the same document.
            source = json.loads(record.pop("source_json") or "{}")
            record["source_inventory_digest"] = source.get("inventory_digest")
            out.append(record)
        return out

    # The acceptance record's own storage, beside the runs it summarises. One
    # module owns the durable shape of both, so a reader never has to decide
    # which file is authoritative for a decision.

    _ACCEPTANCE_COLUMNS = (
        "acceptance_id, integration_id, context, decision, candidate, target, "
        "candidate_parent, source_json, policy_json, verifier_json, "
        "environment_json, fixture_json, manifest_json, required_checks, "
        "checks_json, gaps_json, findings_json, decided_at"
    )

    def record_acceptance(self, acceptance_id: str, record: dict) -> None:
        """Write the integration decision exactly once.

        The guard is the primary key, for the same reason it is on `runs`: a
        retry that recomputed a different answer under one identity would be two
        decisions wearing one name. The acceptance id is derived from the
        candidate, the target, the policy and the context, so a genuine retry
        recomputes the same id and finds the same decision.
        """
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO acceptances (" + self._ACCEPTANCE_COLUMNS + ")"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        acceptance_id, record["integration_id"], record["context"],
                        record["decision"], record["candidate"], record["target"],
                        record["candidate_parent"],
                        _dumps(record["source"]), _dumps(record["policy"]),
                        _dumps(record["verifier"]), _dumps(record["environment"]),
                        _dumps(record["fixture"]), _dumps(record["manifest"]),
                        _dumps(record["required_checks"]),
                        _dumps(record["checks"]), _dumps(record["gaps"]),
                        _dumps(record["findings"]), record["decided_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"acceptance {acceptance_id} is already recorded") from exc

    def load_acceptance(self, acceptance_id: str) -> dict | None:
        """The recorded decision, or None. The single reader of that table."""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._ACCEPTANCE_COLUMNS} FROM acceptances WHERE acceptance_id = ?",
                (acceptance_id,),
            ).fetchone()
        if row is None:
            return None
        return _acceptance_from_row(row)

    def list_acceptances(
        self, *, integration_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Recorded decisions, newest first. Bounded, like every other listing."""
        sql = f"SELECT {self._ACCEPTANCE_COLUMNS} FROM acceptances"
        params: list[object] = []
        if integration_id is not None:
            sql += " WHERE integration_id = ?"
            params.append(integration_id)
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_acceptance_from_row(row) for row in rows]


def _acceptance_from_row(row) -> dict:
    """Rebuild the acceptance record, with the policy fields made explicit.

    `candidate_parent` is a nullable column because "the candidate's parent is
    the target" is the fact a coordinator re-checks on every publish, and
    folding it only into JSON would make it unqueryable. It is restored into
    the policy here so there is one value in the returned record and one place
    on disk that a writer has to set.
    """
    keys = (
        "acceptance_id", "integration_id", "context", "decision", "candidate", "target",
        "candidate_parent", "source", "policy", "verifier", "environment", "fixture",
        "manifest", "required_checks", "checks", "gaps", "findings", "decided_at",
    )
    record = dict(zip(keys, row))
    for key in ("source", "policy", "verifier", "environment", "fixture", "manifest",
                "required_checks", "checks", "gaps", "findings"):
        record[key] = json.loads(record[key])
    record["policy"]["candidate_parent"] = record["candidate_parent"]
    return record
